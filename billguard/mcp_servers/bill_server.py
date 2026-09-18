from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import apply_streamable_bind

from ..bills import (
    DEFAULT_CATEGORIES,
    DUPLICATE_WINDOW_DAYS,
    HIKE_MIN_ABS,
    HIKE_RATIO,
    OUTLIER_MIN,
    OUTLIER_RATIO,
    SPIKE_MIN,
    SPIKE_RATIO,
    WORKFLOW_STATUSES,
    BillFilters,
    BillService,
)

# F6:进程级 PG 连接池单例;仅当设置了 BILLGUARD_PG_DSN 时才会创建。
_PG_POOL = None


READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                        idempotentHint=False, openWorldHint=False)


def _filters(date_from: str | None = None, date_to: str | None = None,
             category: str | None = None, merchant: str | None = None,
             method: str | None = None, status: str | None = None,
             min_amount: float | None = None, max_amount: float | None = None,
             query: str | None = None) -> BillFilters:
    return BillFilters(date_from or "", date_to or "", category or "",
                       merchant or "", method or "", status or "",
                       min_amount, max_amount, query or "")


def _build_service(data_dir: str | Path) -> BillService:
    """存储工厂(F6):设置 BILLGUARD_PG_DSN 时用 PG(进程级单例连接池),
    未设置时保持 SQLite 行为不变。"""
    global _PG_POOL
    dsn = os.environ.get("BILLGUARD_PG_DSN")
    if not dsn:
        return BillService(data_dir)
    from ..storage_pg import PGBillService, new_pg_pool  # 惰性导入:stdio 模式不依赖 psycopg
    if _PG_POOL is None:
        _PG_POOL = new_pg_pool(dsn)
        # 启动门禁:schema 落后于 migrations/ 时拒绝服务;AUTO_MIGRATE=1 自补齐
        from ..migrate import require_current
        require_current(
            _PG_POOL, auto=os.environ.get("BILLGUARD_AUTO_MIGRATE") == "1")
    return PGBillService(_PG_POOL)


def build_server(data_dir: str | Path) -> FastMCP:
    service = _build_service(data_dir)
    backend = "PostgreSQL" if os.environ.get("BILLGUARD_PG_DSN") else "SQLite"
    server = FastMCP(
        "BillGuard Data MCP",
        instructions=(
            "提供个人账单的确定性查询、聚合、周期对比、异常检测和脱敏样本。"
            f"统计结果来自 {backend}；样本备注始终经过 PII 脱敏。"
        ),
        json_response=True,
        stateless_http=True,
    )

    @server.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> Response:
        """就绪探针(compose healthcheck):只确认进程与 HTTP 栈存活,不触存储。"""
        return JSONResponse({"ok": True})

    @server.tool(name="aggregate", annotations=READ_ONLY, structured_output=True)
    def aggregate(date_from: str | None = None, date_to: str | None = None,
                  category: str | None = None, merchant: str | None = None,
                  method: str | None = None, status: str | None = None,
                  min_amount: float | None = None, max_amount: float | None = None,
                  query: str | None = None, owner: str = "") -> dict[str, Any]:
        """聚合账单总金额、笔数、待核查数量、日均支出、类别分布和高频商户。"""
        scoped = service.scoped_or_legacy(owner)
        return scoped.overview(_filters(
            date_from, date_to, category, merchant, method, status,
            min_amount, max_amount, query,
        ))

    @server.tool(name="query", annotations=READ_ONLY, structured_output=True)
    def query(date_from: str | None = None, date_to: str | None = None,
              category: str | None = None, merchant: str | None = None,
              method: str | None = None, status: str | None = None,
              min_amount: float | None = None, max_amount: float | None = None,
              query: str | None = None, limit: int = 20,
              owner: str = "") -> dict[str, Any]:
        """按时间、类别、商户、金额区间或关键词查询最多 50 条脱敏交易；关键词同时匹配交易编号、商户、备注和类别。"""
        scoped = service.scoped_or_legacy(owner)
        result = scoped.query(_filters(
            date_from, date_to, category, merchant, method, status,
            min_amount, max_amount, query,
        ), page=1, page_size=min(max(limit, 1), 50))
        result["items"] = [
            {**item, "note": scoped.mask_pii(item["note"])[0]} for item in result["items"]
        ]
        result["pii_masked"] = True
        return result

    @server.tool(name="compare_periods", annotations=READ_ONLY, structured_output=True)
    def compare_periods(days: int = 7, owner: str = "") -> dict[str, Any]:
        """将最近 N 天支出与此前等长周期进行确定性比较。"""
        if days < 1 or days > 365:
            raise ValueError("days 必须在 1 到 365 之间")
        return service.scoped_or_legacy(owner).compare(days)

    @server.tool(name="detect_anomalies", annotations=READ_ONLY, structured_output=True)
    def detect_anomalies(days: int = 7,
                         dimension: Literal["spike", "duplicate", "price_hike", "outlier"] = "spike",
                         limit: int = 10, owner: str = "") -> dict[str, Any]:
        """识别四类账单异常：类别激增、疑似重复扣费、订阅涨价和大额离群。"""
        if days < 1 or days > 365:
            raise ValueError("days 必须在 1 到 365 之间")
        if limit < 1 or limit > 50:
            raise ValueError("limit 必须在 1 到 50 之间")
        return service.scoped_or_legacy(owner).anomalies(days, dimension, limit)

    @server.tool(name="get_samples", annotations=READ_ONLY, structured_output=True)
    def get_samples(merchant: str | None = None, category: str | None = None,
                    query: str | None = None, limit: int = 10,
                    date_from: str | None = None,
                    date_to: str | None = None, owner: str = "") -> dict[str, Any]:
        """读取最多 20 条已脱敏代表性交易；具体问题优先传 merchant，其次 category，零结果时按 retry_hint 放宽一次查询。"""
        if limit < 1 or limit > 20:
            raise ValueError("limit 必须在 1 到 20 之间")
        return service.scoped_or_legacy(owner).samples(
            merchant=merchant, category=category, query=query,
            limit=limit, date_from=date_from, date_to=date_to)

    @server.tool(
        name="update_status", annotations=WRITE, structured_output=True,
        meta={"risk_level": "high_write", "requires_approval": True,
              "policy_reason": "Updates bill transaction workflow state"},
    )
    def update_status(tx_ids: list[str],
                      status: Literal[WORKFLOW_STATUSES],
                      operator: str, note: str = "", owner: str = "") -> dict[str, Any]:
        """更新交易核查状态。这是写操作，MCP Host 必须在调用前获得用户批准。"""
        if not tx_ids or len(tx_ids) > 100:
            raise ValueError("tx_ids 必须包含 1 到 100 个交易编号")
        if not operator.strip():
            raise ValueError("operator 不能为空")
        return service.scoped_or_legacy(owner).update_workflow(
            tx_ids, operator.strip(), status=status, note=note)

    @server.resource(
        "bill://schema", name="bill-schema", mime_type="application/json",
        description="账单交易字段、类型和隐私约束。",
    )
    def bill_schema() -> str:
        return json.dumps({
            "fields": {
                "tx_id": "string, unique business identifier",
                "paid_at": "ISO date-time",
                "merchant": "string",
                "category": "string",
                "amount": "number, CNY yuan",
                "method": "string",
                "note": "string, PII masked when exposed through MCP",
                "status": " | ".join(WORKFLOW_STATUSES),
            },
            "privacy": "MCP query and sample tools return masked notes only",
        }, ensure_ascii=False)

    @server.resource(
        "bill://categories", name="bill-categories", mime_type="application/json",
        description="账单类目目录:名称、关键词与启用位(静态、与 owner 无关)。",
    )
    def bill_categories() -> str:
        # §4:类别资源是 owner 无关的静态目录,只含 name/keywords/enabled,
        # 不查库——任何 owner 的个性化名称/关键词与计数都不经此资源泄露
        return json.dumps({
            "categories": [
                {"name": name, "keywords": [word.strip() for word in keywords.split(",")],
                 "enabled": True}
                for name, keywords in DEFAULT_CATEGORIES
            ],
        }, ensure_ascii=False)

    @server.resource(
        "bill://metric-definitions", name="metric-definitions", mime_type="application/json",
        description="四类异常调查使用的指标定义和阈值。",
    )
    def metric_definitions() -> str:
        return json.dumps({
            "compare": {
                "change_percent": "(current_total - previous_total) / previous_total * 100",
                "empty_previous": "previous_total is zero: change is undefined, report amounts only",
            },
            "dimensions": {
                "spike": {
                    "definition": "category current-period amount >= spike_ratio x previous and >= spike_min",
                    "spike_ratio": SPIKE_RATIO,
                    "spike_min": SPIKE_MIN,
                },
                "duplicate": {
                    "definition": "same merchant and amount charged again within duplicate_window_days",
                    "duplicate_window_days": DUPLICATE_WINDOW_DAYS,
                },
                "price_hike": {
                    "definition": "subscription latest charge differs from expected_amount by max(hike_min_abs, hike_ratio x expected)",
                    "hike_min_abs": HIKE_MIN_ABS,
                    "hike_ratio": HIKE_RATIO,
                },
                "outlier": {
                    "definition": "single charge >= outlier_min and >= outlier_ratio x its category mean",
                    "outlier_min": OUTLIER_MIN,
                    "outlier_ratio": OUTLIER_RATIO,
                },
            },
            "privacy": "anomaly items never expose raw notes; use get_samples for masked evidence",
        }, ensure_ascii=False)

    @server.prompt(name="investigate-bill-anomaly")
    def investigate_bill_anomaly(days: int = 7,
                                 dimension: Literal["spike", "duplicate", "price_hike", "outlier"] = "spike") -> str:
        """生成账单异常调查任务模板。"""
        return (
            f"调查最近 {days} 天账单在 {dimension} 维度的异常。"
            "先对比等长周期确认口径，再核对四类阈值定义，最后读取脱敏样本取证。"
            "输出数据事实、原因假设、置信度和下一步验证动作；未经证实不得写已确认根因。"
        )

    @server.prompt(name="monthly-guard-report")
    def monthly_guard_report() -> str:
        """生成月度守卫报告任务模板。"""
        return (
            "生成最近 30 天的月度守卫报告，对比此前 30 天。"
            "包括支出事实、异常清单（四类维度）、根因推测、行动计划和数据限制；"
            "取消订阅、退款等行动必须走工单审批，不得宣称已执行。"
        )

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="BillGuard Data MCP Server")
    parser.add_argument("--data-dir", default=".sessions/billguard/bills")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    server = build_server(args.data_dir)
    # 按最终绑定地址重算 DNS 重绑定防护(否则 0.0.0.0 下服务名 Host 被 421)
    apply_streamable_bind(server, args.host, args.port)
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
