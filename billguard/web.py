from __future__ import annotations

import argparse
import copy
import hashlib
import json
import mimetypes
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .agents import BillMockLLM, create_bill_agent, create_mcp_bill_agent
from .auth import (AuthError, PermissionDenied, can, clear_session_cookie,
                   session_cookie)
from .auth import AuthSessionStore, Authenticator, UserStore
from .bills import WORKFLOW_STATUSES, BillFilters, BillService
from .guardrails import MAX_HTTP_REQUEST_BYTES, validate_user_input
from .evaluation import EvaluationReportStore
from .llm import OpenAICompatibleLLM
from .mcp_runtime import MCPClientManager
from .observability import TraceStore
from .policy import ApprovalStore, PolicyError, PolicyGateway
from .session import SessionStore
from .tools import Tool, ToolRegistry, ToolError
from .work_items import WorkItemError, WorkItemStore


STATIC_ROOT = Path(__file__).with_name("web_static")


class BusyError(RuntimeError):
    """429: all model-concurrency slots are taken; retry shortly."""


def _make_owner_wrapper(original: Any, owner: str, allowed: frozenset[str]) -> Any:
    """闭包工厂:wrapper 只接受 **arguments,没有任何具名参数——模型显式传
    同名关键字(如 {"_owner": "admin"})无处绑定,无法覆盖注入身份;
    同时按 Schema 过滤参数,未知键(含 _server/_tool 等内部键)一律丢弃。"""
    def wrapped(**arguments: Any) -> Any:
        arguments = {key: value for key, value in arguments.items() if key in allowed}
        arguments["owner"] = owner  # 服务端身份强制覆盖,防伪造跨 owner 访问
        return original(**arguments)
    return wrapped


def inject_owner_identity(registry: ToolRegistry, username: str) -> ToolRegistry:
    """MCP 模式的 owner 身份注入:bill.* 工具只能以当前登录用户执行。

    - handler 包装(_make_owner_wrapper):arguments["owner"] 一律覆盖为服务端
      身份,模型伪造的 owner(如他人用户名)在到达 MCP 服务器前就被覆盖;
      wrapper 无具名参数且按 Schema 过滤参数,具名参数覆盖类攻击无效;
    - Schema 隐藏:properties/required 移除 owner,模型侧根本看不到该参数;
    - 非 bill.* 工具(如 work-items.*)原样透传,不受影响。

    返回替换后的新注册表;入参注册表保持不变,需要恢复时直接弃用返回值即可。
    """
    injected = ToolRegistry()
    for name in registry.names():
        tool = registry.get(name)
        if name.startswith("bill."):
            parameters = copy.deepcopy(tool.parameters)
            properties = parameters.get("properties")
            if isinstance(properties, dict):
                properties.pop("owner", None)
            if isinstance(parameters.get("required"), list):
                parameters["required"] = [key for key in parameters["required"]
                                          if key != "owner"]
            allowed = frozenset(properties) if isinstance(properties, dict) else frozenset()
            tool = replace(
                tool,
                handler=_make_owner_wrapper(tool.handler, username, allowed),
                parameters=parameters,
            )
        injected.register(tool)
    return injected


class BillGuardApp:
    """BillGuard 账单守卫应用层,供 HTTP handler 与测试共用。"""

    def __init__(self, data_dir: str | Path, docs_dir: str | Path, llm: Any,
                 mcp_manager: MCPClientManager | None = None,
                 policy_gateway: PolicyGateway | None = None,
                 work_item_store: WorkItemStore | None = None,
                 authenticator: Any = None,
                 max_concurrent_llm: int = 4) -> None:
        if max_concurrent_llm < 1:
            raise ValueError("max_concurrent_llm 必须不小于 1")
        self.data_dir = Path(data_dir)
        guard_root = self.data_dir / "billguard"
        self.session_dir = guard_root / "sessions"
        self.evidence_dir = guard_root / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.bills = BillService(guard_root / "bills")
        self.traces = TraceStore(self.session_dir)
        self.evaluations = EvaluationReportStore(self.data_dir / "evaluations")
        self.llm = llm
        self.mcp_manager = mcp_manager
        self.policy_gateway = policy_gateway
        self.work_item_store = work_item_store
        self.authenticator = authenticator
        self._llm_slots = threading.BoundedSemaphore(max_concurrent_llm)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def _scoped(self, user: Any):
        """按用户装配的受限账单视图:查询自动过滤,写入自动盖 owner 戳。"""
        return self.bills.for_user(user.username)

    def _require_session_access(self, user: Any, session_id: str) -> None:
        store = SessionStore(self.session_dir)
        if not store._path(session_id).exists():
            return
        owner = store.load(session_id).owner
        if owner is None:
            if user.role != "admin":
                raise PermissionDenied("该分析会话未分配归属,仅管理员可访问")
            return
        if owner != user.username:
            raise PermissionDenied("该分析会话属于其他用户")

    def _claim_session(self, user: Any, session_id: str) -> None:
        store = SessionStore(self.session_dir)
        session = store.load(session_id)
        if session.owner is None:
            session.owner = user.username
            store.save(session)

    def snapshot(self, user: Any, session_id: str) -> dict[str, Any]:
        self._require_session_access(user, session_id)
        session = SessionStore(self.session_dir).load(session_id)
        evidence = self._load_evidence(session_id)
        scoped = self._scoped(user)
        messages = []
        for message in session.messages:
            if message.role not in {"user", "assistant"} or message.tool_call_id:
                continue
            item = message.as_dict()
            if message.role == "assistant":
                item["evidence"] = evidence.get(self._answer_key(message.content), [])
            messages.append(item)
        return {
            "session_id": session_id,
            "messages": messages,
            "summary": session.summary,
            "sessions": self.list_sessions(user, session_id),
            "overview": scoped.overview(),
            "anomalies": scoped.anomalies(31, "spike", 8),
            "bills": scoped.query(page=1, page_size=30),
            "categories": scoped.categories(),
            "audits": scoped.recent_audits(),
            "imports": scoped.imports(),
            "subscriptions": scoped.subscriptions(),
            "reports": scoped.reports(),
            "mcp_servers": self.mcp_servers(),
            "approvals": self.approvals(user, session_id),
            "runs": self.traces.list_runs(session_id),
            "evaluations": self.evaluations.list(),
        }

    def approvals(self, user: Any, session_id: str) -> list[dict[str, Any]]:
        self._require_session_access(user, session_id)
        if self.policy_gateway is None:
            return []
        return [item.as_dict() for item in self.policy_gateway.store.list(session_id=session_id)]

    def mcp_servers(self) -> list[dict[str, Any]]:
        if self.mcp_manager is None:
            return []
        return [{
            "name": snapshot.name,
            "transport": snapshot.transport,
            "server_name": snapshot.server_name,
            "server_version": snapshot.server_version,
            "protocol_version": snapshot.protocol_version,
            "tools": [tool.name for tool in snapshot.tools],
            "resources": list(snapshot.resources),
            "prompts": list(snapshot.prompts),
        } for snapshot in self.mcp_manager.snapshots()]

    def list_sessions(self, user: Any, active_session_id: str = "") -> list[dict[str, Any]]:
        """Return recoverable session names, newest first, for the sidebar."""
        sessions: list[dict[str, Any]] = []
        self.session_dir.mkdir(parents=True, exist_ok=True)
        for path in self.session_dir.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                session_id = value.get("session_id")
                messages = value.get("messages", [])
                owner = value.get("owner")
                if owner != user.username and not (owner is None and user.role == "admin"):
                    continue
                if isinstance(session_id, str) and session_id:
                    sessions.append({
                        "id": session_id,
                        "message_count": sum(1 for item in messages if item.get("role") in {"user", "assistant"}),
                        "updated_at": path.stat().st_mtime,
                    })
            except (OSError, json.JSONDecodeError, AttributeError, TypeError):
                continue
        if active_session_id and not any(item["id"] == active_session_id for item in sessions):
            sessions.append({"id": active_session_id, "message_count": 0, "updated_at": 0})
        return sorted(sessions, key=lambda item: (item["id"] != active_session_id, -item["updated_at"]))[:20]

    def chat(self, user: Any, session_id: str, message: str) -> dict[str, Any]:
        validate_user_input(message)
        if not self._llm_slots.acquire(blocking=False):
            raise BusyError("服务繁忙,请稍后重试")
        try:
            return self._chat_locked(user, session_id, message)
        finally:
            self._llm_slots.release()

    def _chat_locked(self, user: Any, session_id: str, message: str) -> dict[str, Any]:
        with self._lock(session_id):
            self._require_session_access(user, session_id)
            self._claim_session(user, session_id)
            agent = self._agent(user, session_id)
            events: list[Any] = []
            agent.hooks.append(events.append)
            response = agent.run(session_id, message)
            evidence = self._build_evidence(events)
            self._save_evidence(session_id, response.answer, evidence)
            return {
                "answer": response.answer,
                "steps": response.steps,
                "trace_id": response.trace_id,
                "active_skills": list(response.active_skills),
                "sessions": self.list_sessions(user, session_id),
                "overview": self._scoped(user).overview(),
                "evidence": evidence,
                "mcp_servers": self.mcp_servers(),
                "status": response.status,
                "approval": response.approval,
                "approvals": self.approvals(user, session_id),
                "runs": self.traces.list_runs(session_id),
            }

    def list_runs(self, user: Any, session_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_session_access(user, session_id)
        body = body or {}
        return {"runs": self.traces.list_runs(session_id, int(body.get("limit", 50)))}

    def run_detail(self, user: Any, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_session_access(user, session_id)
        return self.traces.get_run(session_id, str(body.get("trace_id", "")))

    def list_evaluations(self) -> dict[str, Any]:
        return {"evaluations": self.evaluations.list()}

    def _agent(self, user: Any, session_id: str):
        if self.mcp_manager is None:
            # 本地模式:工具注册表按当前用户装配受限视图,Agent 只能查/写本人数据
            return create_bill_agent(self.llm, session_id, self.session_dir, self._scoped(user))
        # MCP 模式:工具注册完成后按当前用户注入 owner 身份(模型不可见、不可伪造)
        registry = ToolRegistry()
        for snapshot in self.mcp_manager.snapshots():
            self.mcp_manager.register_tools(registry, snapshot.name)
        return create_mcp_bill_agent(
            self.llm, session_id, self.mcp_manager, self.session_dir,
            policy_gateway=self.policy_gateway,
            registry=inject_owner_identity(registry, user.username),
        )

    def decide_approval(self, user: Any, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not can(user.role, "approval_decide"):
            raise PermissionDenied("当前角色无审批权限")
        self._require_session_access(user, session_id)
        if self.policy_gateway is None:
            raise PolicyError("审批工作流未启用")
        approval_id = str(body.get("approval_id", "")).strip()
        decision = str(body.get("decision", "")).strip().lower()
        if not approval_id or decision not in {"approve", "reject"}:
            raise PolicyError("approval_id 与 decision(approve 或 reject)不能为空")
        if not self._llm_slots.acquire(blocking=False):
            raise BusyError("服务繁忙,请稍后重试")
        try:
            return self._decide_approval_locked(
                user, session_id, approval_id, decision, str(body.get("note", "")))
        finally:
            self._llm_slots.release()

    def _decide_approval_locked(self, user: Any, session_id: str,
                                approval_id: str, decision: str, note: str) -> dict[str, Any]:
        # 与 chat 共用同一把会话锁:审批恢复与进行中的对话都会加载并回写
        # Session 文件,无锁并发会互相覆盖(丢失更新)。
        with self._lock(session_id):
            current = self.policy_gateway.store.get(approval_id)
            if current.session_id != session_id:
                raise PolicyError("该审批不属于当前会话")
            approved = decision == "approve"
            decided_by = user.username  # 服务端身份,忽略请求体中的 decided_by

            # The Work Item service owns a second, domain-level approval state. Keeping
            # both gates means a forged Harness checkpoint still cannot create an issue.
            if current.tool_name.endswith("commit_issue") and self.work_item_store is not None:
                remote_approval_id = str(current.arguments.get("approval_id", "")).strip()
                if not remote_approval_id:
                    raise PolicyError("commit_issue 审批缺少远程 approval_id")
                self.work_item_store.decide(remote_approval_id, approved, decided_by)

            decided = self.policy_gateway.store.decide(
                approval_id, approved, decided_by, note,
            )
            agent = self._agent(user, session_id)
            events: list[Any] = []
            agent.hooks.append(events.append)
            response = agent.resume(approval_id) if approved else agent.finalize_rejection(approval_id)
            evidence = self._build_evidence(events)
            self._save_evidence(session_id, response.answer, evidence)
            return {
                "answer": response.answer,
                "steps": response.steps,
                "trace_id": response.trace_id,
                "status": response.status,
                "approval": self.policy_gateway.store.get(decided.id).as_dict(),
                "approvals": self.approvals(user, session_id),
                "sessions": self.list_sessions(user, session_id),
                "overview": self._scoped(user).overview(),
                "evidence": evidence,
                "mcp_servers": self.mcp_servers(),
                "runs": self.traces.list_runs(session_id),
            }

    @staticmethod
    def _answer_key(answer: str) -> str:
        return hashlib.sha256(answer.encode("utf-8")).hexdigest()

    def _evidence_path(self, session_id: str) -> Path:
        return self.evidence_dir / f"{SessionStore._key(session_id)}.json"

    def _load_evidence(self, session_id: str) -> dict[str, list[dict[str, Any]]]:
        path = self._evidence_path(session_id)
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_evidence(self, session_id: str, answer: str, evidence: list[dict[str, Any]]) -> None:
        if not evidence:
            return
        value = self._load_evidence(session_id)
        value[self._answer_key(answer)] = evidence
        self._evidence_path(session_id).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _build_evidence(events: list[Any]) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for event in events:
            if getattr(event, "event_type", "") != "tool_end":
                continue
            tool = str(event.data.get("tool") or "")
            leaf = tool.rsplit(".", 1)[-1]  # MCP 工具带 "bill." 前缀
            result = event.data.get("result", {})
            if tool == "bill_overview" or leaf == "aggregate":
                for item in result.get("by_category", [])[:8]:
                    evidence.append({"label": item.get("name", "类别"), "description": f"¥{item.get('amount', 0):g} · {item.get('count', 0)} 笔",
                                     "filters": {"category": item.get("name", "")}})
            elif tool == "bill_compare" or leaf == "compare_periods":
                period = result.get("current_period", {})
                evidence.append({"label": "查看本周期支出", "description": f"¥{period.get('total', 0):g}",
                                 "filters": {"date_from": period.get("from", ""), "date_to": period.get("to", "")}})
            elif tool in {"bill_search", "bill_samples"} or leaf in {"query", "get_samples"}:
                source = result.get("samples", result.get("items", []))
                for item in source[:10]:
                    evidence.append({"label": item.get("merchant", "查看交易"), "description": f"¥{item.get('amount', 0):g}",
                                     "filters": {"merchant": item.get("merchant", "")}})
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in evidence:
            key = json.dumps(item.get("filters", {}), ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique[:12]

    @staticmethod
    def _filters(body: dict[str, Any]) -> BillFilters:
        source = body.get("filters", body)
        allowed = BillFilters.__dataclass_fields__
        return BillFilters(**{key: value for key, value in source.items() if key in allowed and value not in (None, "")})

    def bill_overview(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        return self._scoped(user).overview(self._filters(body))

    def bill_query(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        return self._scoped(user).query(
            self._filters(body),
            int(body.get("page", 1)),
            int(body.get("page_size", 30)),
        )

    def bill_anomalies(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        return self._scoped(user).anomalies(
            int(body.get("days", 31)), str(body.get("dimension", "spike")), int(body.get("limit", 8))
        )

    def import_bills(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        filename = str(body.get("filename", "bills.csv")).strip() or "bills.csv"
        csv_text = body.get("csv_text")
        if not isinstance(csv_text, str):
            raise ValueError("csv_text 不能为空")
        scoped = self._scoped(user)
        result = scoped.import_bills(filename, csv_text)
        payload: dict[str, Any] = {
            "result": result,
            "overview": scoped.overview(),
            "imports": scoped.imports(),
            "subscriptions": scoped.subscriptions(),
        }
        subscriptions_csv_text = body.get("subscriptions_csv_text")
        if isinstance(subscriptions_csv_text, str) and subscriptions_csv_text.strip():
            payload["subscriptions_result"] = scoped.import_subscriptions(
                str(body.get("subscriptions_filename", "subscriptions.csv")).strip() or "subscriptions.csv",
                subscriptions_csv_text,
            )
        return payload

    def update_transaction_category(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        tx_id = str(body.get("tx_id", "")).strip()
        category = str(body.get("category", "")).strip()
        if not tx_id or not category:
            raise ValueError("tx_id 与 category 不能为空")
        scoped = self._scoped(user)
        result = scoped.update_transaction_category(tx_id, category, user.username)
        return {"result": result, "categories": scoped.categories(), "audits": scoped.recent_audits()}

    def save_category(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        keywords = body.get("keywords", [])
        if not isinstance(keywords, list):
            raise ValueError("keywords 必须是数组")
        scoped = self._scoped(user)
        result = scoped.save_category(
            str(body.get("name", "")), [str(item) for item in keywords], bool(body.get("enabled", True)),
            int(body["category_id"]) if body.get("category_id") is not None else None, user.username,
        )
        return {"result": result, "categories": scoped.categories(), "audits": scoped.recent_audits()}

    def delete_category(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        scoped = self._scoped(user)
        result = scoped.delete_category(int(body.get("category_id", 0)), user.username)
        return {"result": result, "categories": scoped.categories(), "audits": scoped.recent_audits()}

    def rematch_categories(self, user: Any) -> dict[str, Any]:
        scoped = self._scoped(user)
        result = scoped.rematch_categories(operator=user.username)
        return {"result": result, "categories": scoped.categories(), "overview": scoped.overview()}

    def update_workflow(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        tx_ids = body.get("tx_ids", [])
        updates = body.get("updates", {})
        if not isinstance(tx_ids, list) or not isinstance(updates, dict):
            raise ValueError("tx_ids 必须是数组,updates 必须是对象")
        updates.pop("operator", None)  # 身份一律取服务端,防止与 kwarg 冲突
        status = str(updates.get("status") or "").strip()
        note = str(updates.get("note") or "").strip()
        if status not in WORKFLOW_STATUSES:
            raise ValueError(f"status 必须是{'、'.join(WORKFLOW_STATUSES)}")
        if len(note) > 200:
            raise ValueError("备注最长 200 字")
        return self._scoped(user).update_workflow(
            [str(item) for item in tx_ids], operator=user.username, status=status, note=note)

    def transaction_audits(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        tx_id = str(body.get("tx_id", "")).strip()
        return {"audits": self._scoped(user).transaction_audits(tx_id)}

    def export_bills(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        return {"filename": "bills-export.csv", "csv_text": self._scoped(user).export_csv(self._filters(body))}

    def save_report(self, user: Any, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_session_access(user, session_id)
        scoped = self._scoped(user)
        report = scoped.save_report(session_id, str(body.get("title", "")), str(body.get("content", "")))
        return {"report": report, "reports": scoped.reports()}

    def delete_report(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        scoped = self._scoped(user)
        result = scoped.delete_report(int(body.get("report_id", 0)))
        return {"result": result, "reports": scoped.reports()}

    def delete_session(self, user: Any, session_id: str) -> dict[str, Any]:
        """Delete all persisted state owned by one exact session id."""
        self._require_session_access(user, session_id)
        store = SessionStore(self.session_dir)
        session_key = store._key(session_id)
        targets = (
            store._path(session_id),
            self.session_dir / "traces" / f"{session_key}.jsonl",
            self._evidence_path(session_id),
        )
        with self._lock(session_id):
            deleted = False
            for target in targets:
                if target.is_file():
                    target.unlink()
                    deleted = True
        with self._locks_guard:
            self._locks.pop(session_id, None)
        if self.policy_gateway is not None:
            self.policy_gateway.store.delete_session(session_id)
        return {"deleted": deleted, "session_id": session_id, "sessions": self.list_sessions(user)}

    def admin_list_users(self) -> list[dict[str, Any]]:
        return [{"username": user.username, "role": user.role, "disabled": user.disabled,
                 "created_at": user.created_at}
                for user in self.authenticator.users.list()]

    def admin_create_user(self, body: dict[str, Any]) -> dict[str, Any]:
        user = self.authenticator.users.create(
            str(body.get("username", "")).strip(),
            str(body.get("password", "")), str(body.get("role", "")))
        return {"user": {"username": user.username, "role": user.role, "disabled": user.disabled}}

    def admin_set_role(self, body: dict[str, Any]) -> dict[str, Any]:
        user = self.authenticator.users.set_role(
            str(body.get("username", "")).strip(), str(body.get("role", "")))
        return {"user": {"username": user.username, "role": user.role, "disabled": user.disabled}}

    def admin_reset_password(self, body: dict[str, Any]) -> dict[str, Any]:
        self.authenticator.users.reset_password(
            str(body.get("username", "")).strip(), str(body.get("password", "")))
        return {"ok": True}

    def admin_set_disabled(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        username = str(body.get("username", "")).strip()
        if not username:
            raise ValueError("用户名不能为空")
        if username == user.username:
            raise ValueError("不能禁用当前登录的账号")
        updated = self.authenticator.users.set_disabled(username, bool(body.get("disabled")))
        return {"user": {"username": updated.username, "role": updated.role,
                         "disabled": updated.disabled}}


_AUTH_EXEMPT_POST = {"/api/auth/login"}
_CAPABILITY_BY_PATH = {
    "/api/reports/save": "report_write",
    "/api/reports/delete": "report_write",
    "/api/bills/import": "bills_write",
    "/api/bills/categories": "bills_write",
    "/api/category-rules/save": "bills_write",
    "/api/category-rules/delete": "bills_write",
    "/api/category-rules/rematch": "bills_write",
    "/api/bills/workflow": "bills_write",
    # 导出含未脱敏 note,按写级保护:viewer 不可导出,前端同步隐藏按钮
    "/api/bills/export": "bills_write",
    "/api/approvals/decide": "approval_decide",
    "/api/admin/users": "users_manage",
    "/api/admin/users/role": "users_manage",
    "/api/admin/users/password": "users_manage",
    "/api/admin/users/disable": "users_manage",
}


def make_handler(app: BillGuardApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "BillGuard/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[web] {self.address_string()} - {fmt % args}")

        def _json(self, status: int, value: Any, set_cookie: str | None = None) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            if set_cookie:
                self.send_header("Set-Cookie", set_cookie)
            self.end_headers()
            self.wfile.write(payload)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("Content-Length 无效")
            if length > MAX_HTTP_REQUEST_BYTES:
                raise ValueError("请求体过大")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("JSON 请求体必须是对象")
            return value

        def _static(self, request_path: str) -> None:
            relative = "index.html" if request_path == "/" else unquote(request_path.lstrip("/"))
            target = (STATIC_ROOT / relative).resolve()
            if not target.is_relative_to(STATIC_ROOT.resolve()) or not target.is_file():
                self.send_error(404)
                return
            payload = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                self._json(200, {"ok": True})
                return
            try:
                if parsed.path == "/api/auth/me":
                    user = app.authenticator.resolve_user(self.headers)
                    self._json(200, {"username": user.username, "role": user.role})
                    return
                if parsed.path == "/api/admin/users":
                    user = app.authenticator.resolve_user(self.headers)
                    if not can(user.role, "users_manage"):
                        raise PermissionDenied("仅管理员可管理用户")
                    self._json(200, {"users": app.admin_list_users()})
                    return
            except AuthError as exc:
                self._json(401, {"error": str(exc)})
                return
            except PermissionDenied as exc:
                self._json(403, {"error": str(exc)})
                return
            self._static(parsed.path)

        def do_POST(self) -> None:
            try:
                if self.path in _AUTH_EXEMPT_POST:
                    body = self._body()
                    username = str(body.get("username", "")).strip()
                    password = str(body.get("password", ""))
                    if not username or not password:
                        raise ValueError("用户名和密码不能为空")
                    try:
                        user, token = app.authenticator.login(username, password)
                    except AuthError as exc:
                        self._json(401, {"error": str(exc)})
                        return
                    self._json(200, {"user": {"username": user.username, "role": user.role}},
                               set_cookie=session_cookie(token))
                    return

                try:
                    user = app.authenticator.resolve_user(self.headers)
                except AuthError as exc:
                    self._json(401, {"error": str(exc)})
                    return
                capability = _CAPABILITY_BY_PATH.get(self.path)
                if capability and not can(user.role, capability):
                    self._json(403, {"error": "当前角色无权执行此操作"})
                    return

                body = self._body()
                if self.path == "/api/auth/logout":  # 登出无需 session_id
                    app.authenticator.logout(self.headers)
                    self._json(200, {"ok": True}, set_cookie=clear_session_cookie())
                    return
                session_id = str(body.get("session_id", "default")).strip()
                if not session_id:
                    raise ValueError("session_id 不能为空")
                if self.path == "/api/snapshot":
                    result = app.snapshot(user, session_id)
                elif self.path == "/api/chat":
                    message = str(body.get("message", "")).strip()
                    if not message:
                        raise ValueError("message 不能为空")
                    result = app.chat(user, session_id, message)
                elif self.path == "/api/bills/overview":
                    result = app.bill_overview(user, body)
                elif self.path == "/api/bills/query":
                    result = app.bill_query(user, body)
                elif self.path == "/api/bills/anomalies":
                    result = app.bill_anomalies(user, body)
                elif self.path == "/api/bills/import":
                    result = app.import_bills(user, body)
                elif self.path == "/api/bills/categories":
                    result = app.update_transaction_category(user, body)
                elif self.path == "/api/category-rules/save":
                    result = app.save_category(user, body)
                elif self.path == "/api/category-rules/delete":
                    result = app.delete_category(user, body)
                elif self.path == "/api/category-rules/rematch":
                    result = app.rematch_categories(user)
                elif self.path == "/api/bills/workflow":
                    result = app.update_workflow(user, body)
                elif self.path == "/api/bills/audits":
                    result = app.transaction_audits(user, body)
                elif self.path == "/api/bills/export":
                    result = app.export_bills(user, body)
                elif self.path == "/api/reports/save":
                    result = app.save_report(user, session_id, body)
                elif self.path == "/api/reports/delete":
                    result = app.delete_report(user, body)
                elif self.path == "/api/session/delete":
                    result = app.delete_session(user, session_id)
                elif self.path == "/api/approvals/list":
                    result = {"approvals": app.approvals(user, session_id)}
                elif self.path == "/api/approvals/decide":
                    result = app.decide_approval(user, session_id, body)
                elif self.path == "/api/runs/list":
                    result = app.list_runs(user, session_id, body)
                elif self.path == "/api/runs/detail":
                    result = app.run_detail(user, session_id, body)
                elif self.path == "/api/evaluations/list":
                    result = app.list_evaluations()
                elif self.path == "/api/admin/users":
                    result = app.admin_create_user(body)
                elif self.path == "/api/admin/users/role":
                    result = app.admin_set_role(body)
                elif self.path == "/api/admin/users/password":
                    result = app.admin_reset_password(body)
                elif self.path == "/api/admin/users/disable":
                    result = app.admin_set_disabled(user, body)
                else:
                    self._json(404, {"error": "接口不存在"})
                    return
                self._json(200, result)
            except (ValueError, ToolError, PolicyError, WorkItemError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            except PermissionDenied as exc:
                self._json(403, {"error": str(exc)})
            except BusyError as exc:
                self._json(429, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": f"请求失败:{exc}"})

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8000, data_dir: str = ".sessions",
          docs_dir: str = "docs", llm_name: str = "mock", model: str = "gpt-4.1-mini",
          base_url: str = "https://api.openai.com/v1", tool_source: str = "local",
          mcp_timeout: float = 20.0, work_item_mcp_url: str = "",
          work_item_data_dir: str = ".sessions/work-items",
          llm_proxy: str | None = None, max_concurrent_llm: int = 4) -> None:
    llm = (BillMockLLM() if llm_name == "mock" else
           OpenAICompatibleLLM(model, base_url=base_url, proxy=llm_proxy))
    mcp_manager: MCPClientManager | None = None
    if tool_source == "mcp":
        mcp_manager = MCPClientManager(request_timeout=mcp_timeout)
        project_root = Path(__file__).resolve().parent.parent
        bills_dir = (Path(data_dir) / "billguard" / "bills").resolve()
        mcp_manager.connect_stdio(
            "bill",
            sys.executable,
            ["-u", "-m", "billguard.mcp_servers.bill_server",
             "--data-dir", str(bills_dir)],
            cwd=project_root,
        )
        if work_item_mcp_url:
            mcp_manager.connect_streamable_http("work-items", work_item_mcp_url)
    policy_gateway = PolicyGateway(ApprovalStore(Path(data_dir) / "billguard" / "policy")) if mcp_manager else None
    work_item_store = WorkItemStore(work_item_data_dir) if work_item_mcp_url else None
    auth_root = Path(data_dir) / "billguard" / "auth"
    users_store = UserStore(auth_root)
    if users_store.count() == 0:
        print("用户库为空,请先创建管理员:")
        print('  python -m billguard.users --data-dir <data-dir> add admin --role admin')
        raise SystemExit(1)
    authenticator = Authenticator(users_store, AuthSessionStore(auth_root))
    server = ThreadingHTTPServer(
        (host, port), make_handler(BillGuardApp(
            data_dir, docs_dir, llm, mcp_manager, policy_gateway, work_item_store,
            authenticator, max_concurrent_llm,
        )),
    )
    print(f"BillGuard Web UI: http://{host}:{server.server_port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if mcp_manager is not None:
            mcp_manager.close()
        if isinstance(llm, OpenAICompatibleLLM):
            llm.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="BillGuard 账单守卫 Agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", default=".sessions")
    parser.add_argument("--docs-dir", default="docs", help=argparse.SUPPRESS)
    parser.add_argument("--llm", choices=("mock", "openai"), default="mock")
    parser.add_argument("--model", default="gpt-4.1-mini")
    parser.add_argument("--base-url", default="https://api.openai.com/v1")
    parser.add_argument("--llm-proxy", default=None,
                        help="Optional HTTP proxy for model requests")
    parser.add_argument("--tool-source", choices=("local", "mcp"), default="local",
                        help="Use in-process tools or dynamically discovered MCP tools")
    parser.add_argument("--mcp-timeout", type=float, default=20.0)
    parser.add_argument("--work-item-mcp-url", default="",
                        help="Optional remote Work Item MCP Streamable HTTP endpoint")
    parser.add_argument("--work-item-data-dir", default=".sessions/work-items",
                        help="Shared Work Item store used for out-of-band web approval")
    parser.add_argument("--max-concurrent-llm", type=int, default=4,
                        help="同时进行的模型调用上限,超出返回 429")
    args = parser.parse_args()
    serve(args.host, args.port, args.data_dir, args.docs_dir, args.llm, args.model,
          args.base_url, args.tool_source, args.mcp_timeout, args.work_item_mcp_url,
          args.work_item_data_dir, args.llm_proxy, args.max_concurrent_llm)


if __name__ == "__main__":
    main()
