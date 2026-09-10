from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..feedback import FeedbackFilters, FeedbackService
from ..harness import AgentSpec, HarnessEngine
from ..policy import PolicyGateway
from ..llm import LLM
from ..mcp_runtime import MCPClientManager
from ..session import SessionStore
from ..skills import SkillRuntime
from ..tools import Tool, ToolRegistry


FEEDBACK_AGENT_SPEC = AgentSpec(
    name="FeedbackInsightAgent（客户反馈洞察助手）",
    instructions="""你是客户反馈洞察助手。你的职责是基于工具返回的真实数据，帮助产品和运营团队发现高频问题、趋势变化和典型案例。

必须遵守：
1. 所有数量、比例、趋势和排名必须来自工具结果，不得猜测。
2. 分析问题时先查询统计；需要解释原因时，再读取最多 20 条脱敏样本。
3. 回答要区分“数据事实”和“分析推测”，推测不能写成确定结论。
4. 查询具体问题时优先使用已知标签；全文检索使用能出现在原文中的短关键词，不要把“登录问题”等分类名称直接当成内容原句。
5. 查询或样本结果为空时，根据工具的 retry_hint 改用标签或更短关键词重试一次；仍为空才报告证据不足。
6. 当前反馈工具只能支持原因假设，不能单独确认技术根因；没有日志、实验或复现证据时禁止写“已确认根因”。
7. 原始反馈可能包含敏感信息，只能使用工具返回的脱敏样本。
8. 给出简洁、可执行的产品建议，并说明证据范围。
9. 不执行任意 SQL、Shell、文件修改或外部网络请求。""",
    tool_names=("feedback_overview", "feedback_compare", "feedback_anomalies", "feedback_search", "feedback_samples"),
    max_steps=8,
)


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def _filter_properties() -> dict[str, Any]:
    return {
        "date_from": {"type": "string", "description": "开始时间，ISO 日期或时间"},
        "date_to": {"type": "string", "description": "结束时间，ISO 日期或时间"},
        "product_module": {"type": "string"},
        "customer_tier": {"type": "string"},
        "status": {"type": "string"},
        "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
        "assignee": {"type": "string"},
        "tag": {"type": "string"},
        "query": {"type": "string", "description": "反馈内容关键词"},
    }


def _filters(kwargs: dict[str, Any]) -> FeedbackFilters:
    return FeedbackFilters(**{key: value for key, value in kwargs.items() if key in FeedbackFilters.__dataclass_fields__})


def _masked_search(service: FeedbackService, limit: int = 20, **kwargs: Any) -> dict[str, Any]:
    result = service.query(_filters(kwargs), 1, min(limit, 50))
    result["items"] = [{**item, "content": service.mask_pii(item["content"])} for item in result["items"]]
    result["pii_masked"] = True
    return result


def build_feedback_registry(service: FeedbackService) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(
        "feedback_overview",
        "查询客户反馈总量、待处理数量、趋势、模块分布和高频标签。回答总体情况或某类反馈前必须调用。",
        _object(_filter_properties()),
        lambda **kwargs: service.overview(_filters(kwargs)),
    ))
    registry.register(Tool(
        "feedback_compare",
        "比较最近一段时间与上一相同长度周期的反馈量和高频标签。",
        _object({"days": {"type": "integer", "minimum": 1}}, []),
        lambda days=7: service.compare(days),
    ))
    registry.register(Tool(
        "feedback_anomalies",
        "检测最近周期相对上一周期增长最快的问题标签或产品模块。用于回答异常、激增、突发和优先关注问题。",
        _object({
            "days": {"type": "integer", "minimum": 1},
            "dimension": {"type": "string", "enum": ["tag", "module"]},
            "limit": {"type": "integer", "minimum": 1},
        }),
        lambda days=7, dimension="tag", limit=10: service.anomalies(days, dimension, limit),
    ))
    registry.register(Tool(
        "feedback_search",
        "按筛选条件查询反馈明细，返回有限数量的记录。query 会同时匹配内容、工单号、产品模块和标签；用于定位具体工单，不用于大批量总结。",
        _object({**_filter_properties(), "limit": {"type": "integer", "minimum": 1}}),
        lambda limit=20, **kwargs: _masked_search(service, limit, **kwargs),
    ))
    registry.register(Tool(
        "feedback_samples",
        "读取最多 20 条已脱敏的代表性反馈，用于分析某个标签或关键词背后的可能原因。",
        _object({
            "tag": {"type": "string"}, "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
            "date_from": {"type": "string"}, "date_to": {"type": "string"},
        }),
        lambda **kwargs: service.samples(**kwargs),
    ))
    return registry


def create_feedback_agent(llm: LLM, session_id: str, data_dir: str | Path = ".sessions",
                          service: FeedbackService | None = None,
                          skill_dir: str | Path | None = None) -> HarnessEngine:
    service = service or FeedbackService(Path(data_dir) / "feedback")
    skill_dir = Path(skill_dir) if skill_dir is not None else Path(__file__).resolve().parents[2] / "skills"
    return HarnessEngine(
        FEEDBACK_AGENT_SPEC,
        llm,
        build_feedback_registry(service),
        SessionStore(data_dir),
        skills=SkillRuntime(skill_dir),
    )


def create_mcp_feedback_agent(llm: LLM, session_id: str, manager: MCPClientManager,
                              data_dir: str | Path = ".sessions",
                              skill_dir: str | Path | None = None,
                              policy_gateway: PolicyGateway | None = None) -> HarnessEngine:
    """Create the feedback Agent from tools dynamically advertised by MCP servers."""
    registry = ToolRegistry()
    for snapshot in manager.snapshots():
        manager.register_tools(registry, snapshot.name)
    if not registry.names():
        raise ValueError("no MCP tools were discovered")
    spec = replace(FEEDBACK_AGENT_SPEC, tool_names=registry.names())
    skill_dir = Path(skill_dir) if skill_dir is not None else Path(__file__).resolve().parents[2] / "skills"
    return HarnessEngine(
        spec,
        llm,
        registry,
        SessionStore(data_dir),
        skills=SkillRuntime(skill_dir),
        policy_gateway=policy_gateway,
    )


class FeedbackMockLLM:
    """Offline model double that demonstrates the feedback tool loop."""

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> str:
        last = messages[-1]
        if last["role"] == "tool":
            result = json.loads(last["content"])
            name = str(last.get("name", "")).split(".")[-1]
            if name in {"feedback_overview", "aggregate"}:
                tags = "、".join(f"{item['name']}（{item['count']}）" for item in result.get("top_tags", [])[:5]) or "暂无标签数据"
                modules = "、".join(f"{item['name']}（{item['count']}）" for item in result.get("modules", [])[:5]) or "暂无模块数据"
                answer = (f"当前筛选范围内共有 {result.get('total', 0)} 条客户反馈，其中待处理 {result.get('pending', 0)} 条。\n\n"
                          f"高频问题：{tags}\n产品模块：{modules}\n\n"
                          "建议优先查看高频标签对应的原始反馈，再判断具体原因。")
            elif name in {"feedback_compare", "compare_periods"}:
                change = result.get("change_percent")
                change_text = "缺少上一周期数据，暂时无法计算变化率" if change is None else f"较上一周期变化 {change:+g}%"
                answer = f"最近 {result['days']} 天共收到 {result['current_period']['total']} 条反馈，{change_text}。"
            elif name in {"feedback_anomalies", "detect_anomalies"}:
                items = result.get("items", [])
                lines = []
                for item in items[:8]:
                    change = "上一周期未出现" if item["is_new"] else f"变化 {item['change_percent']:+g}%"
                    lines.append(f"- {item['name']}：本周期 {item['current_count']} 条，上周期 {item['previous_count']} 条，{change}")
                answer = (f"已比较 {result.get('days', 7)} 天周期，增长较明显的项目如下：\n" +
                          ("\n".join(lines) if lines else "当前没有可识别的异常增长。") +
                          "\n\n这些是统计异常，具体原因仍需结合脱敏样本进一步判断。")
            elif name in {"feedback_samples", "get_samples"}:
                samples = result.get("samples", [])
                lines = [f"- {item['ticket_id']}：{item['content']}" for item in samples[:10]]
                answer = f"共匹配 {result.get('matched', 0)} 条反馈，以下是已脱敏样本：\n" + ("\n".join(lines) or "暂无样本。")
            else:
                items = result.get("items", [])
                lines = [f"- {item['ticket_id']} [{item['product_module']}] {item['content']}" for item in items[:10]]
                answer = f"共找到 {result.get('total', 0)} 条反馈：\n" + ("\n".join(lines) or "暂无匹配反馈。")
            return json.dumps({"thought": "根据真实工具结果回答", "final": answer}, ensure_ascii=False)

        text = str(last.get("content", ""))
        if any(word in text for word in ("异常", "激增", "突发", "优先关注")):
            return self._call(self._available(tools, "feedback_anomalies", "feedback.detect_anomalies"),
                              {"days": 7, "dimension": "tag", "limit": 10})
        if any(word in text for word in ("对比", "相比", "增长", "下降", "变化")):
            days = 30 if "30" in text or "月" in text else 7
            return self._call(self._available(tools, "feedback_compare", "feedback.compare_periods"),
                              {"days": days})
        if any(word in text for word in ("原始", "样本", "典型", "例子")):
            return self._call(self._available(tools, "feedback_samples", "feedback.get_samples"),
                              {"limit": 10})
        if any(word in text for word in ("搜索", "查找", "包含")):
            name = self._available(tools, "feedback_search", "feedback.query")
            query_key = "query" if name == "feedback_search" else "query_text"
            return self._call(name, {query_key: text, "limit": 20})
        date_from = None
        today = datetime.now().date()
        if "最近7天" in text or "近7天" in text:
            date_from = (today - timedelta(days=6)).isoformat()
        elif "最近30天" in text or "近30天" in text:
            date_from = (today - timedelta(days=29)).isoformat()
        arguments = {"date_from": date_from} if date_from else {}
        return self._call(self._available(tools, "feedback_overview", "feedback.aggregate"), arguments)

    @staticmethod
    def _available(tools: list[dict[str, Any]], *candidates: str) -> str:
        available = {str(tool.get("name", "")) for tool in tools}
        for candidate in candidates:
            if candidate in available:
                return candidate
        return candidates[0]

    @staticmethod
    def _call(name: str, arguments: dict[str, Any]) -> str:
        return json.dumps({"thought": "需要查询客户反馈数据", "tool_call": {"name": name, "arguments": arguments}}, ensure_ascii=False)
