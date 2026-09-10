from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..feedback import FeedbackFilters, FeedbackService


READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                        idempotentHint=False, openWorldHint=False)


def _filters(date_from: str | None = None, date_to: str | None = None,
             product_module: str | None = None, customer_tier: str | None = None,
             status: str | None = None, priority: str | None = None,
             assignee: str | None = None, tag: str | None = None,
             query: str | None = None) -> FeedbackFilters:
    return FeedbackFilters(date_from, date_to, product_module, customer_tier,
                           status, priority, assignee, tag, query)


def build_server(data_dir: str | Path) -> FastMCP:
    service = FeedbackService(data_dir)
    server = FastMCP(
        "Feedback Data MCP",
        instructions=(
            "提供客户反馈的确定性查询、聚合、周期对比、异常检测和脱敏样本。"
            "统计结果来自 SQLite；样本内容始终经过 PII 脱敏。"
        ),
        json_response=True,
        stateless_http=True,
    )

    @server.tool(name="aggregate", annotations=READ_ONLY, structured_output=True)
    def aggregate(date_from: str | None = None, date_to: str | None = None,
                  product_module: str | None = None, customer_tier: str | None = None,
                  status: str | None = None, priority: str | None = None,
                  assignee: str | None = None, tag: str | None = None,
                  query: str | None = None) -> dict[str, Any]:
        """聚合反馈总量、待处理数量、趋势、模块、等级和高频标签。"""
        return service.overview(_filters(
            date_from, date_to, product_module, customer_tier, status,
            priority, assignee, tag, query,
        ))

    @server.tool(name="query", annotations=READ_ONLY, structured_output=True)
    def query(date_from: str | None = None, date_to: str | None = None,
              product_module: str | None = None, customer_tier: str | None = None,
              status: str | None = None, priority: str | None = None,
              assignee: str | None = None, tag: str | None = None,
              query_text: str | None = None, limit: int = 20) -> dict[str, Any]:
        """按时间、模块、等级、状态、标签或关键词查询最多 50 条脱敏反馈；关键词同时匹配内容、工单号、模块和标签。"""
        result = service.query(_filters(
            date_from, date_to, product_module, customer_tier, status,
            priority, assignee, tag, query_text,
        ), page=1, page_size=min(max(limit, 1), 50))
        result["items"] = [
            {**item, "content": service.mask_pii(item["content"])} for item in result["items"]
        ]
        result["pii_masked"] = True
        return result

    @server.tool(name="compare_periods", annotations=READ_ONLY, structured_output=True)
    def compare_periods(days: int = 7) -> dict[str, Any]:
        """将最近 N 天与此前等长周期进行确定性比较。"""
        if days < 1 or days > 365:
            raise ValueError("days must be between 1 and 365")
        return service.compare(days)

    @server.tool(name="detect_anomalies", annotations=READ_ONLY, structured_output=True)
    def detect_anomalies(days: int = 7, dimension: Literal["tag", "module"] = "tag",
                         limit: int = 10) -> dict[str, Any]:
        """识别标签或产品模块相对上一等长周期的异常变化。"""
        if days < 1 or days > 365:
            raise ValueError("days must be between 1 and 365")
        if limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        return service.anomalies(days, dimension, limit)

    @server.tool(name="get_samples", annotations=READ_ONLY, structured_output=True)
    def get_samples(tag: str | None = None, query: str | None = None,
                    limit: int = 10, date_from: str | None = None,
                    date_to: str | None = None) -> dict[str, Any]:
        """读取最多 20 条已脱敏代表性样本；具体问题优先传 tag，零结果时按 retry_hint 放宽一次查询。"""
        if limit < 1 or limit > 20:
            raise ValueError("limit must be between 1 and 20")
        return service.samples(tag=tag, query=query, limit=limit,
                               date_from=date_from, date_to=date_to)

    @server.tool(
        name="update_status", annotations=WRITE, structured_output=True,
        meta={"risk_level": "high_write", "requires_approval": True,
              "policy_reason": "Updates customer feedback workflow state"},
    )
    def update_status(ticket_ids: list[str], status: Literal["待处理", "处理中", "已完成"],
                      operator: str) -> dict[str, Any]:
        """更新反馈处理状态。这是写操作，MCP Host 必须在调用前获得用户批准。"""
        if not ticket_ids or len(ticket_ids) > 100:
            raise ValueError("ticket_ids must contain between 1 and 100 items")
        if not operator.strip():
            raise ValueError("operator is required")
        return service.update_workflow(ticket_ids, operator.strip(), status=status)

    @server.resource(
        "feedback://schema", name="feedback-schema", mime_type="application/json",
        description="客户反馈字段、类型和隐私约束。",
    )
    def feedback_schema() -> str:
        return json.dumps({
            "fields": {
                "ticket_id": "string, unique business identifier",
                "created_at": "ISO date-time",
                "product_module": "string",
                "content": "string, PII masked when exposed through MCP",
                "customer_tier": "string",
                "status": "待处理 | 处理中 | 已完成",
                "priority": "low | medium | high | urgent",
                "assignee": "string",
                "tags": "string[]",
            },
            "privacy": "MCP query and sample tools return masked content only",
        }, ensure_ascii=False)

    @server.resource(
        "feedback://taxonomy", name="feedback-taxonomy", mime_type="application/json",
        description="当前启用的反馈标签及匹配关键词。",
    )
    def feedback_taxonomy() -> str:
        return json.dumps({"tags": service.tags()}, ensure_ascii=False)

    @server.resource(
        "feedback://metric-definitions", name="metric-definitions", mime_type="application/json",
        description="异常调查使用的指标定义和低基数规则。",
    )
    def metric_definitions() -> str:
        return json.dumps({
            "absolute_change": "current_count - previous_count",
            "change_percent": "absolute_change / previous_count * 100",
            "new_item": "previous_count is zero and current_count is positive",
            "low_base_warning": "previous_count below 3 requires low-confidence wording",
        }, ensure_ascii=False)

    @server.prompt(name="investigate-feedback-spike")
    def investigate_feedback_spike(days: int = 7,
                                   dimension: Literal["tag", "module"] = "tag") -> str:
        """生成异常反馈调查任务模板。"""
        return (
            f"调查最近 {days} 天客户反馈在 {dimension} 维度的异常变化。"
            "先比较等长周期，再检查绝对增量和低基数，最后读取脱敏样本。"
            "输出数据事实、原因假设、置信度和下一步验证动作。"
        )

    @server.prompt(name="weekly-customer-voice-report")
    def weekly_customer_voice_report() -> str:
        """生成客户声音周报任务模板。"""
        return (
            "生成最近 7 天客户反馈周报，对比此前 7 天。"
            "包括执行摘要、主要问题、异常变化、代表性样本、原因假设、行动项和数据限制。"
        )

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Feedback Data MCP Server")
    parser.add_argument("--data-dir", default=".sessions/feedback")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    server = build_server(args.data_dir)
    server.settings.host = args.host
    server.settings.port = args.port
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
