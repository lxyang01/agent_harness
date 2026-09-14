from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..bills import BillFilters, BillService
from ..harness import AgentSpec, HarnessEngine
from ..policy import PolicyGateway
from ..llm import LLM
from ..mcp_runtime import MCPClientManager
from ..session import SessionStore
from ..skills import SkillRuntime
from ..tools import Tool, ToolRegistry


BILL_AGENT_SPEC = AgentSpec(
    name="BillGuard 账单守卫助手",
    instructions="""你是账单守卫助手。你的职责是基于工具返回的真实数据，帮助用户看清支出结构、发现异常扣费并给出可执行的处理建议。

必须遵守：
1. 所有金额、笔数、比例、趋势和排名必须来自工具结果，不得猜测。
2. 分析异常先查总览了解支出结构，再调用异常检测定位；需要解释原因时，读取最多 20 条已脱敏样本。
3. 回答要区分“数据事实”和“分析推测”，推测不能写成确定结论。
4. 查询具体交易优先使用已知类别、商户和支付方式；全文检索使用能出现在原文中的短关键词，不要把“订阅涨价”等结论名称直接当成内容原句。
5. 查询或样本结果为空时，根据工具的 retry_hint 改用类别、商户或更短关键词重试一次；仍为空才报告证据不足。
6. 判断订阅是否涨价以 subscriptions.expected_amount 为基准；没有商户公告、账单明细或复现证据时禁止写“已确认根因”。
7. 原始账单可能包含敏感信息，只能使用工具返回的脱敏样本。
8. 给出简洁、可执行的处理建议并说明证据范围；取消订阅、退款等高风险动作只能通过工单在审批三阶段中完成，助手不得直接执行。
9. 不执行任意 SQL、Shell、文件修改或外部网络请求。""",
    tool_names=("bill_overview", "bill_compare", "bill_anomalies", "bill_search", "bill_samples"),
    max_steps=8,
)


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def _filter_properties() -> dict[str, Any]:
    return {
        "date_from": {"type": "string", "description": "开始日期，ISO 日期或时间"},
        "date_to": {"type": "string", "description": "结束日期，ISO 日期或时间"},
        "category": {"type": "string"},
        "merchant": {"type": "string"},
        "method": {"type": "string"},
        "status": {"type": "string"},
        "min_amount": {"type": "number"},
        "max_amount": {"type": "number"},
        "query": {"type": "string", "description": "交易编号、商户、备注或类别关键词"},
    }


def _filters(kwargs: dict[str, Any]) -> BillFilters:
    return BillFilters(**{key: value for key, value in kwargs.items() if key in BillFilters.__dataclass_fields__})


def _masked_search(service: BillService, limit: int = 20, **kwargs: Any) -> dict[str, Any]:
    result = service.query(_filters(kwargs), 1, min(limit, 50))
    result["items"] = [{**item, "note": service.mask_pii(item["note"])[0]} for item in result["items"]]
    result["pii_masked"] = True
    return result


def build_bill_registry(service: BillService) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(
        "bill_overview",
        "查询账单总金额、笔数、待核查数量、日均支出、类别分布和高频商户。回答总体支出情况前必须调用。",
        _object(_filter_properties()),
        lambda **kwargs: service.overview(_filters(kwargs)),
    ))
    registry.register(Tool(
        "bill_compare",
        "比较最近一段时间与上一相同长度周期的支出金额和类别结构。",
        _object({"days": {"type": "integer", "minimum": 1}}, []),
        lambda days=7: service.compare(days),
    ))
    registry.register(Tool(
        "bill_anomalies",
        "检测最近周期的支出异常：spike 类别激增、duplicate 疑似重复扣费、price_hike 订阅涨价、outlier 大额离群。"
        "用于回答异常、涨价、重复扣费和盗刷问题。",
        _object({
            "days": {"type": "integer", "minimum": 1},
            "dimension": {"type": "string", "enum": ["spike", "duplicate", "price_hike", "outlier"]},
            "limit": {"type": "integer", "minimum": 1},
        }),
        lambda days=7, dimension="spike", limit=10: service.anomalies(days, dimension, limit),
    ))
    registry.register(Tool(
        "bill_search",
        "按筛选条件查询交易明细，返回有限数量的脱敏记录。query 会同时匹配交易编号、商户、备注和类别；"
        "用于定位具体交易，不用于大批量总结。",
        _object({**_filter_properties(), "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
        lambda limit=20, **kwargs: _masked_search(service, limit, **kwargs),
    ))
    registry.register(Tool(
        "bill_samples",
        "读取最多 20 条已脱敏的代表性交易，用于分析某个商户、类别或关键词背后的可能原因。",
        _object({
            "merchant": {"type": "string"}, "category": {"type": "string"}, "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "date_from": {"type": "string"}, "date_to": {"type": "string"},
        }),
        lambda **kwargs: service.samples(**kwargs),
    ))
    return registry


def create_bill_agent(llm: LLM, session_id: str, data_dir: str | Path = ".sessions",
                      service: BillService | None = None,
                      skill_dir: str | Path | None = None,
                      run_timeout: float | None = None) -> HarnessEngine:
    service = service or BillService(Path(data_dir) / "billguard")
    skill_dir = Path(skill_dir) if skill_dir is not None else Path(__file__).resolve().parents[2] / "skills"
    return HarnessEngine(
        replace(BILL_AGENT_SPEC, run_timeout=run_timeout),
        llm,
        build_bill_registry(service),
        SessionStore(data_dir),
        skills=SkillRuntime(skill_dir),
    )


def create_mcp_bill_agent(llm: LLM, session_id: str, manager: MCPClientManager,
                          data_dir: str | Path = ".sessions",
                          skill_dir: str | Path | None = None,
                          policy_gateway: PolicyGateway | None = None,
                          registry: ToolRegistry | None = None,
                          run_timeout: float | None = None) -> HarnessEngine:
    """Create the bill Agent from tools dynamically advertised by MCP servers.

    registry 允许调用方(如 web 层)在 manager.register_tools 完成后注入
    owner 身份边界再交给 Harness;缺省时仍由 manager 即时发现并注册。"""
    if registry is None:
        registry = ToolRegistry()
        for snapshot in manager.snapshots():
            manager.register_tools(registry, snapshot.name)
    if not registry.names():
        raise ValueError("no MCP tools were discovered")
    spec = replace(BILL_AGENT_SPEC, tool_names=registry.names(), run_timeout=run_timeout)
    skill_dir = Path(skill_dir) if skill_dir is not None else Path(__file__).resolve().parents[2] / "skills"
    return HarnessEngine(
        spec,
        llm,
        registry,
        SessionStore(data_dir),
        skills=SkillRuntime(skill_dir),
        policy_gateway=policy_gateway,
    )


_REPORT_WORDS = ("守卫报告", "月报", "周报", "汇报")  # 强名词,与契约门禁词对齐;裸"报告/总结"走普通概览
_ACTION_WORDS = ("取消订阅", "取消", "退款")


def _current_turn(messages) -> list:
    """最近一条用户消息之后的消息:多步流程只看本轮,不串扰历史轮次。"""
    last_user = -1
    for index, item in enumerate(messages):
        if item.get("role") == "user":
            last_user = index
    return messages[last_user:] if last_user >= 0 else messages


def _user_intent(messages) -> str:
    current = _current_turn(messages)
    text = str(current[0].get("content", "")) if current and current[0].get("role") == "user" else ""
    if any(word in text for word in _ACTION_WORDS):
        return "action"
    if any(word in text for word in _REPORT_WORDS):
        return "report"
    return ""


def _wants_report(messages) -> bool:
    return _user_intent(messages) == "report"


def _wants_action(messages) -> bool:
    return _user_intent(messages) == "action"


def _current_user_text(messages) -> str:
    current = _current_turn(messages)
    return str(current[0].get("content", "")) if current and current[0].get("role") == "user" else ""


def _extract_subscription_target(text: str) -> str:
    """从用户话里提取要取消的订阅名:"取消X订阅/取消X的订阅/取消订阅X" 均支持。"""
    match = re.search(r"取消(.{0,16}?)(?:的)?(?:的订阅|订阅|会员|自动续费)", text)
    if match and match.group(1).strip() not in ("", "订阅", "我的"):
        return match.group(1).strip()
    match = re.search(r"取消订阅(.{1,16}?)$", text.strip())  # 取消订阅X
    if match:
        return match.group(1).strip()
    match = re.search(r"(?:把|将)?(.{1,16}?)取消(?:的)?订阅", text)  # 把X取消订阅
    if match and match.group(1).strip():
        return match.group(1).strip()
    return ""


def _last_tool(messages) -> tuple[str, dict]:
    for message in reversed(_current_turn(messages)):
        if message.get("role") == "tool":
            name = str(message.get("name", "")).split(".")[-1]
            try:
                return name, json.loads(message.get("content", "{}"))
            except (json.JSONDecodeError, TypeError):
                return name, {}
    return "", {}


class BillMockLLM:
    """Offline model double that demonstrates the bill tool loop."""
    # ---- 报告/工单多步演示流程 ----

    def _report_step(self, messages, tools) -> str:
        name, result = _last_tool(messages)
        if name in {"bill_overview", "aggregate"}:
            anomaly = self._available(tools, "bill_anomalies", "bill.detect_anomalies")
            return self._call(anomaly, {"days": 31, "dimension": "price_hike", "limit": 10})
        if name in {"bill_anomalies", "detect_anomalies"}:
            if result.get("dimension") != "duplicate":
                anomaly = self._available(tools, "bill_anomalies", "bill.detect_anomalies")
                return self._call(anomaly, {"days": 31, "dimension": "duplicate", "limit": 10})
            return json.dumps({"thought": "汇总成四章节守卫报告", "final": self._report_final(messages)},
                              ensure_ascii=False)
        overview = self._available(tools, "bill_overview", "bill.aggregate")
        return self._call(overview, {})

    def _report_final(self, messages) -> str:
        overview_text, anomaly_lines = "", []
        for message in messages:
            if message.get("role") != "tool":
                continue
            name = str(message.get("name", "")).split(".")[-1]
            try:
                result = json.loads(message.get("content", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            if name in {"bill_overview", "aggregate"}:
                overview_text = (f"共 {result.get('count', 0)} 笔支出，合计 ¥{result.get('total_amount', 0):g}，"
                                 f"待核查 {result.get('pending', 0)} 笔。")
            elif name in {"bill_anomalies", "detect_anomalies"} and result.get("items"):
                anomaly_lines.extend(f"- {item['name']}：{item['detail']}" for item in result["items"])
        nl = chr(10)
        sections = [
            "支出事实：" + (overview_text or "本期无支出记录。"),
            "异常清单：" + nl + (nl.join(anomaly_lines[:8]) if anomaly_lines else "- 本期未检出异常。"),
            "根因推测：以上异常来自工具结果中的确定性阈值判定；涨价类需对照订阅预期金额，"
            "重复类需确认是否为同一服务的两次扣费，目前仅有统计证据，尚未确认根因。",
            "行动计划：" + nl
            + "1. 逐条核对异常清单中的订阅商户与账单原文；" + nl
            + "2. 对确认涨价或重复的服务发起取消订阅/退款工单（将走三阶段人工审批）；" + nl
            + "3. 下月复查同类异常是否复现。",
        ]
        return (nl + nl).join(sections)

    def _action_step(self, messages, tools) -> str:
        available = {str(tool.get("name", "")) for tool in tools}
        if "work-items.prepare_issue" not in available:
            return json.dumps({"thought": "缺少工单服务", "final":
                "取消订阅/退款需要工单服务配合：请以 --tool-source mcp 启动并接入 "
                "Work Item MCP 后重试，届时会走 prepare → 人工审批 → commit 三阶段协议。"},
                ensure_ascii=False)
        name, result = _last_tool(messages)
        if name == "prepare_issue":
            return self._call("work-items.commit_issue",
                              {"approval_id": result.get("approval_id", "")})
        if name == "commit_issue":
            issue = result.get("created", {})
            return json.dumps({"thought": "工单已落库", "final":
                f"已创建取消订阅工单 {issue.get('id', '')}（{issue.get('title', '')}），"
                "由当前登录用户发起，可到 Work Item 服务追踪后续处理。"},
                ensure_ascii=False)
        target = _extract_subscription_target(_current_user_text(messages))
        return self._call("work-items.prepare_issue", {
            "title": f"取消{target}订阅" if target else "取消订阅",
            "description": f"经用户确认，申请取消 {target or '该'} 订阅并停止后续扣费",
            "priority": "high"})

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> str:
        last = messages[-1]
        if _wants_report(messages):
            return self._report_step(messages, tools)
        if _wants_action(messages):
            return self._action_step(messages, tools)
        if last["role"] == "tool":
            result = json.loads(last["content"])
            name = str(last.get("name", "")).split(".")[-1]
            if name in {"bill_overview", "aggregate"}:
                categories = "、".join(f"{item['name']}（¥{item['amount']:g}）"
                                       for item in result.get("by_category", [])[:5]) or "暂无类别数据"
                merchants = "、".join(f"{item['name']}（¥{item['amount']:g}）"
                                     for item in result.get("top_merchants", [])[:5]) or "暂无商户数据"
                answer = (f"当前筛选范围内共 {result.get('count', 0)} 笔支出，合计 ¥{result.get('total_amount', 0):g}，"
                          f"其中待核查 {result.get('pending', 0)} 笔。\n\n"
                          f"类别分布：{categories}\n高频商户：{merchants}\n\n"
                          "建议优先关注金额最大的类别和商户，再决定是否需要深入分析。")
            elif name in {"bill_compare", "compare_periods"}:
                change = result.get("change_percent")
                change_text = "缺少上一周期数据，暂时无法计算变化率" if change is None else f"较上一周期变化 {change:+g}%"
                answer = f"最近 {result['days']} 天合计支出 ¥{result['current_period']['total']:g}，{change_text}。"
            elif name in {"bill_anomalies", "detect_anomalies"}:
                items = result.get("items", [])
                lines = [f"- {item['name']}：{item['detail']}" for item in items[:8]]
                answer = (f"最近 {result.get('days', 7)} 天检测到以下 {result.get('dimension', 'price_hike')} 类型异常：\n" +
                          ("\n".join(lines) if lines else "当前没有可识别的异常。") +
                          "\n\n这些是统计异常，具体原因仍需结合脱敏样本进一步判断。")
            elif name in {"bill_samples", "get_samples"}:
                samples = result.get("samples", [])
                lines = [f"- {item['tx_id']}：{item['merchant']} ¥{item['amount']:g} {item['note']}"
                         for item in samples[:10]]
                answer = f"共匹配 {result.get('matched', 0)} 笔交易，以下是已脱敏样本：\n" + ("\n".join(lines) or "暂无样本。")
            else:
                items = result.get("items", [])
                lines = [f"- {item['tx_id']} [{item['category']}] ¥{item['amount']:g}"
                         for item in items[:10]]
                answer = f"共找到 {result.get('total', 0)} 笔交易：\n" + ("\n".join(lines) or "暂无匹配交易。")
            return json.dumps({"thought": "根据真实工具结果回答", "final": answer}, ensure_ascii=False)

        text = str(last.get("content", ""))
        if any(word in text for word in _REPORT_WORDS):
            return self._report_step(messages, tools)
        if any(word in text for word in ("取消订阅", "取消", "退款")):
            return self._action_step(messages, tools)
        if any(word in text for word in ("异常", "涨价", "重复", "盗刷")):
            # 演示故事线在 8 月上旬,窗口需覆盖到数据最大日 2026-08-31 往前 31 天
            dimension = "duplicate" if "重复" in text else "price_hike"
            return self._call(self._available(tools, "bill_anomalies", "bill.detect_anomalies"),
                              {"days": 31, "dimension": dimension, "limit": 10})
        if any(word in text for word in ("对比", "环比", "变化")):
            days = 30 if "30" in text or "月" in text else 7
            return self._call(self._available(tools, "bill_compare", "bill.compare_periods"),
                              {"days": days})
        if any(word in text for word in ("样本", "明细")):
            return self._call(self._available(tools, "bill_samples", "bill.get_samples"),
                              {"limit": 10})
        if any(word in text for word in ("搜索", "查找")):
            name = self._available(tools, "bill_search", "bill.query")
            query_key = "query" if name == "bill_search" else "query_text"
            return self._call(name, {query_key: text, "limit": 20})
        date_from = None
        today = datetime.now().date()
        if "最近7天" in text or "近7天" in text:
            date_from = (today - timedelta(days=6)).isoformat()
        elif "最近30天" in text or "近30天" in text:
            date_from = (today - timedelta(days=29)).isoformat()
        arguments = {"date_from": date_from} if date_from else {}
        return self._call(self._available(tools, "bill_overview", "bill.aggregate"), arguments)

    @staticmethod
    def _available(tools: list[dict[str, Any]], *candidates: str) -> str:
        available = {str(tool.get("name", "")) for tool in tools}
        for candidate in candidates:
            if candidate in available:
                return candidate
        return candidates[0]

    @staticmethod
    def _call(name: str, arguments: dict[str, Any]) -> str:
        return json.dumps({"thought": "需要查询账单数据", "tool_call": {"name": name, "arguments": arguments}},
                          ensure_ascii=False)
