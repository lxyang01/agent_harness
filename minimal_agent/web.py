from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .agents import FeedbackMockLLM, create_feedback_agent, create_mcp_feedback_agent
from .auth import (AuthError, PermissionDenied, can, clear_session_cookie,
                   session_cookie)
from .auth import AuthSessionStore, Authenticator, UserStore
from .feedback import FeedbackFilters, FeedbackService
from .guardrails import MAX_HTTP_REQUEST_BYTES, validate_user_input
from .evaluation import EvaluationReportStore
from .llm import OpenAICompatibleLLM
from .mcp_runtime import MCPClientManager
from .observability import TraceStore
from .policy import ApprovalStore, PolicyError, PolicyGateway
from .session import SessionStore
from .tools import ToolError
from .work_items import WorkItemError, WorkItemStore


STATIC_ROOT = Path(__file__).with_name("web_static")


class BusyError(RuntimeError):
    """429: all model-concurrency slots are taken; retry shortly."""


class FeedbackWebApp:
    """Customer feedback insight service shared by the HTTP handler and tests."""

    def __init__(self, data_dir: str | Path, docs_dir: str | Path, llm: Any,
                 mcp_manager: MCPClientManager | None = None,
                 policy_gateway: PolicyGateway | None = None,
                 work_item_store: WorkItemStore | None = None,
                 authenticator: Any = None,
                 max_concurrent_llm: int = 4) -> None:
        if max_concurrent_llm < 1:
            raise ValueError("max_concurrent_llm must be >= 1")
        self.data_dir = Path(data_dir)
        self.session_dir = self.data_dir / "feedback_sessions"
        self.evidence_dir = self.data_dir / "feedback_evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.feedback = FeedbackService(self.data_dir / "feedback")
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
            "overview": self.feedback.overview(),
            "anomalies": self.feedback.anomalies(),
            "feedback": self.feedback.query(page=1, page_size=30),
            "tags": self.feedback.tags(),
            "tag_audits": self.feedback.tag_rule_audits(),
            "imports": self.feedback.imports(),
            "reports": self.feedback.reports(),
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
            agent = self._agent(session_id)
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
                "overview": self.feedback.overview(),
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

    def _agent(self, session_id: str):
        if self.mcp_manager is None:
            return create_feedback_agent(self.llm, session_id, self.session_dir, self.feedback)
        return create_mcp_feedback_agent(
            self.llm, session_id, self.mcp_manager, self.session_dir,
            policy_gateway=self.policy_gateway,
        )

    def decide_approval(self, user: Any, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not can(user.role, "approval_decide"):
            raise PermissionDenied("当前角色无审批权限")
        self._require_session_access(user, session_id)
        if self.policy_gateway is None:
            raise PolicyError("approval workflow is not enabled")
        approval_id = str(body.get("approval_id", "")).strip()
        decision = str(body.get("decision", "")).strip().lower()
        if not approval_id or decision not in {"approve", "reject"}:
            raise PolicyError("approval_id and decision (approve or reject) are required")
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
                raise PolicyError("approval does not belong to this session")
            approved = decision == "approve"
            decided_by = user.username  # 服务端身份,忽略请求体中的 decided_by

            # The Work Item service owns a second, domain-level approval state. Keeping
            # both gates means a forged Harness checkpoint still cannot create an issue.
            if current.tool_name.endswith("commit_issue") and self.work_item_store is not None:
                remote_approval_id = str(current.arguments.get("approval_id", "")).strip()
                if not remote_approval_id:
                    raise PolicyError("commit_issue approval has no remote approval_id")
                self.work_item_store.decide(remote_approval_id, approved, decided_by)

            decided = self.policy_gateway.store.decide(
                approval_id, approved, decided_by, note,
            )
            agent = self._agent(session_id)
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
                "overview": self.feedback.overview(),
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
            tool = event.data.get("tool")
            result = event.data.get("result", {})
            if tool in {"feedback_overview", "feedback_anomalies"}:
                source = result.get("top_tags", []) if tool == "feedback_overview" else result.get("items", [])
                for item in source[:8]:
                    count = item.get("count", item.get("current_count", 0))
                    evidence.append({"label": item.get("name", "问题标签"), "description": f"{count} 条相关反馈",
                                     "filters": {"tag": item.get("name", "")}})
            elif tool == "feedback_compare":
                period = result.get("current_period", {})
                evidence.append({"label": "查看本周期反馈", "description": f"{period.get('total', 0)} 条",
                                 "filters": {"date_from": period.get("from", ""), "date_to": period.get("to", "")}})
            elif tool in {"feedback_samples", "feedback_search"}:
                source = result.get("samples", result.get("items", []))
                for item in source[:10]:
                    evidence.append({"label": item.get("ticket_id", "查看反馈"), "description": item.get("product_module", "原始反馈"),
                                     "filters": {"query": item.get("ticket_id", "")}})
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in evidence:
            key = json.dumps(item.get("filters", {}), ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique[:12]

    @staticmethod
    def _filters(body: dict[str, Any]) -> FeedbackFilters:
        source = body.get("filters", body)
        allowed = FeedbackFilters.__dataclass_fields__
        return FeedbackFilters(**{key: value for key, value in source.items() if key in allowed and value not in (None, "")})

    def feedback_overview(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.feedback.overview(self._filters(body))

    def feedback_query(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.feedback.query(
            self._filters(body),
            int(body.get("page", 1)),
            int(body.get("page_size", 30)),
        )

    def feedback_anomalies(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.feedback.anomalies(
            int(body.get("days", 7)), str(body.get("dimension", "tag")), int(body.get("limit", 10))
        )

    def import_feedback(self, body: dict[str, Any]) -> dict[str, Any]:
        filename = str(body.get("filename", "feedback.csv")).strip() or "feedback.csv"
        csv_text = body.get("csv_text")
        if not isinstance(csv_text, str):
            raise ValueError("csv_text is required")
        result = self.feedback.import_csv(filename, csv_text)
        return {"result": result, "overview": self.feedback.overview(), "imports": self.feedback.imports()}

    def update_feedback_tags(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        ticket_id = str(body.get("ticket_id", "")).strip()
        tags = body.get("tags", [])
        if not ticket_id or not isinstance(tags, list):
            raise ValueError("ticket_id and tags are required")
        result = self.feedback.update_tags(ticket_id, [str(tag) for tag in tags], user.username)
        return {"result": result, "tags": self.feedback.tags()}

    def save_tag_rule(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        keywords = body.get("keywords", [])
        if not isinstance(keywords, list):
            raise ValueError("keywords must be an array")
        result = self.feedback.save_tag_rule(
            str(body.get("name", "")), [str(item) for item in keywords], bool(body.get("enabled", True)),
            int(body["tag_id"]) if body.get("tag_id") is not None else None, user.username,
        )
        return {"result": result, "tags": self.feedback.tags(), "audits": self.feedback.tag_rule_audits()}

    def delete_tag_rule(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        result = self.feedback.delete_tag_rule(int(body.get("tag_id", 0)), user.username)
        return {"result": result, "tags": self.feedback.tags(), "audits": self.feedback.tag_rule_audits()}

    def rematch_tags(self) -> dict[str, Any]:
        result = self.feedback.rematch_tags()
        return {"result": result, "tags": self.feedback.tags(), "overview": self.feedback.overview()}

    def update_workflow(self, user: Any, body: dict[str, Any]) -> dict[str, Any]:
        ticket_ids = body.get("ticket_ids", [])
        updates = body.get("updates", {})
        if not isinstance(ticket_ids, list) or not isinstance(updates, dict):
            raise ValueError("ticket_ids and updates are required")
        return self.feedback.update_workflow(ticket_ids, user.username, **updates)

    def feedback_audits(self, body: dict[str, Any]) -> dict[str, Any]:
        ticket_id = str(body.get("ticket_id", "")).strip()
        return {"audits": self.feedback.feedback_audits(ticket_id)}

    def export_feedback(self, body: dict[str, Any]) -> dict[str, Any]:
        return {"filename": "feedback-export.csv", "csv_text": self.feedback.export_csv(self._filters(body))}

    def save_report(self, user: Any, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_session_access(user, session_id)
        report = self.feedback.save_report(session_id, str(body.get("title", "")), str(body.get("content", "")))
        return {"report": report, "reports": self.feedback.reports()}

    def delete_report(self, body: dict[str, Any]) -> dict[str, Any]:
        result = self.feedback.delete_report(int(body.get("report_id", 0)))
        return {"result": result, "reports": self.feedback.reports()}

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


# Backward-compatible import for callers of the earlier PlanningAgent UI.
PlanningWebApp = FeedbackWebApp

_AUTH_EXEMPT_POST = {"/api/auth/login"}
_CAPABILITY_BY_PATH = {
    "/api/reports/save": "report_write",
    "/api/reports/delete": "report_write",
    "/api/feedback/import": "feedback_write",
    "/api/feedback/tags": "feedback_write",
    "/api/tag-rules/save": "feedback_write",
    "/api/tag-rules/delete": "feedback_write",
    "/api/tag-rules/rematch": "feedback_write",
    "/api/feedback/workflow": "feedback_write",
    "/api/approvals/decide": "approval_decide",
    "/api/admin/users": "users_manage",
    "/api/admin/users/role": "users_manage",
    "/api/admin/users/password": "users_manage",
    "/api/admin/users/disable": "users_manage",
}


def make_handler(app: FeedbackWebApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "PlanningAgent/1.0"

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
                raise ValueError("invalid Content-Length")
            if length > MAX_HTTP_REQUEST_BYTES:
                raise ValueError("request body is too large")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("JSON body must be an object")
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
                    raise ValueError("session_id is required")
                if self.path == "/api/snapshot":
                    result = app.snapshot(user, session_id)
                elif self.path == "/api/chat":
                    message = str(body.get("message", "")).strip()
                    if not message:
                        raise ValueError("message is required")
                    result = app.chat(user, session_id, message)
                elif self.path == "/api/feedback/overview":
                    result = app.feedback_overview(body)
                elif self.path == "/api/feedback/query":
                    result = app.feedback_query(body)
                elif self.path == "/api/feedback/anomalies":
                    result = app.feedback_anomalies(body)
                elif self.path == "/api/feedback/import":
                    result = app.import_feedback(body)
                elif self.path == "/api/feedback/tags":
                    result = app.update_feedback_tags(user, body)
                elif self.path == "/api/tag-rules/save":
                    result = app.save_tag_rule(user, body)
                elif self.path == "/api/tag-rules/delete":
                    result = app.delete_tag_rule(user, body)
                elif self.path == "/api/tag-rules/rematch":
                    result = app.rematch_tags()
                elif self.path == "/api/feedback/workflow":
                    result = app.update_workflow(user, body)
                elif self.path == "/api/feedback/audits":
                    result = app.feedback_audits(body)
                elif self.path == "/api/feedback/export":
                    result = app.export_feedback(body)
                elif self.path == "/api/reports/save":
                    result = app.save_report(user, session_id, body)
                elif self.path == "/api/reports/delete":
                    result = app.delete_report(body)
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
                    self._json(404, {"error": "not found"})
                    return
                self._json(200, result)
            except (ValueError, ToolError, PolicyError, WorkItemError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            except PermissionDenied as exc:
                self._json(403, {"error": str(exc)})
            except BusyError as exc:
                self._json(429, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": f"request failed: {exc}"})

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8000, data_dir: str = ".sessions",
          docs_dir: str = "docs", llm_name: str = "mock", model: str = "gpt-4.1-mini",
          base_url: str = "https://api.openai.com/v1", tool_source: str = "local",
          mcp_timeout: float = 20.0, work_item_mcp_url: str = "",
          work_item_data_dir: str = ".sessions/work-items",
          llm_proxy: str | None = None, max_concurrent_llm: int = 4) -> None:
    llm = (FeedbackMockLLM() if llm_name == "mock" else
           OpenAICompatibleLLM(model, base_url=base_url, proxy=llm_proxy))
    mcp_manager: MCPClientManager | None = None
    if tool_source == "mcp":
        mcp_manager = MCPClientManager(request_timeout=mcp_timeout)
        project_root = Path(__file__).resolve().parent.parent
        feedback_dir = (Path(data_dir) / "feedback").resolve()
        mcp_manager.connect_stdio(
            "feedback",
            sys.executable,
            ["-u", "-m", "minimal_agent.mcp_servers.feedback_server",
             "--data-dir", str(feedback_dir)],
            cwd=project_root,
        )
        if work_item_mcp_url:
            mcp_manager.connect_streamable_http("work-items", work_item_mcp_url)
    policy_gateway = PolicyGateway(ApprovalStore(Path(data_dir) / "policy")) if mcp_manager else None
    work_item_store = WorkItemStore(work_item_data_dir) if work_item_mcp_url else None
    auth_root = Path(data_dir) / "auth"
    users_store = UserStore(auth_root)
    if users_store.count() == 0:
        print("用户库为空,请先创建管理员:")
        print('  python -m minimal_agent.users --data-dir <data-dir> add admin --role admin')
        raise SystemExit(1)
    authenticator = Authenticator(users_store, AuthSessionStore(auth_root))
    server = ThreadingHTTPServer(
        (host, port), make_handler(FeedbackWebApp(
            data_dir, docs_dir, llm, mcp_manager, policy_gateway, work_item_store,
            authenticator, max_concurrent_llm,
        )),
    )
    print(f"Feedback Lens Web UI: http://{host}:{server.server_port}")
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
    parser = argparse.ArgumentParser(description="Feedback Lens customer insight Agent")
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
