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
8. 给出简洁、可执行的处理建议并说明证据范围。
9. 用户要求取消订阅或退款时,这是你的本职能力:调用 work-items.prepare_issue 创建工单(标题写清目标订阅),再调用 work-items.commit_issue 提交;系统会自动暂停等待人工审批,批准后才会真正执行。不要拒绝用户,也不要让用户自行联系客服——发起工单就是你处理这类请求的正确方式。
10. 用户要求标记或更新交易核查状态时,先用搜索定位相关交易,再调用 bill.update_status(工具可用时)。
11. 不执行任意 SQL、Shell、文件修改或外部网络请求。
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
