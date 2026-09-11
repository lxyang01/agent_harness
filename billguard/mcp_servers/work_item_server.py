from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..work_items import WorkItemStore

1
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)
PREPARE_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                idempotentHint=False, openWorldHint=False)
COMMIT_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                               idempotentHint=True, openWorldHint=False)


def build_server(data_dir: str | Path) -> FastMCP:
    store = WorkItemStore(data_dir)
    server = FastMCP(
        "Work Item MCP",
        instructions=(
            "管理客户反馈行动项。创建工单采用 prepare/approve/commit 三阶段协议；"
            "approve 只能由 MCP 通道外的人类操作完成。"
        ),
        json_response=True,
        stateless_http=True,
    )

    @server.tool(name="list_issues", annotations=READ_ONLY, structured_output=True)
    def list_issues(status: Literal["open", "in_progress", "done"] | None = None,
                    limit: int = 50) -> dict[str, Any]:
        """列出已创建的行动项。"""
        return store.list_issues(status, limit)

    @server.tool(name="get_issue", annotations=READ_ONLY, structured_output=True)
    def get_issue(issue_id: str) -> dict[str, Any]:
        """按 ID 获取一个行动项。"""
        return store.get_issue(issue_id)

    @server.tool(
        name="prepare_issue", annotations=PREPARE_WRITE, structured_output=True,
        meta={"risk_level": "low_write", "requires_approval": False,
              "policy_reason": "Creates only an expiring approval request"},
    )
    def prepare_issue(title: str, description: str,
                      priority: Literal["low", "medium", "high", "urgent"] = "medium",
                      evidence_refs: list[str] | None = None) -> dict[str, Any]:
        """准备创建行动项并返回审批 ID；此步骤不会创建正式工单。"""
        return store.prepare_issue(title, description, priority, evidence_refs)

    @server.tool(
        name="commit_issue", annotations=COMMIT_WRITE, structured_output=True,
        meta={"risk_level": "high_write", "requires_approval": True,
              "policy_reason": "Creates a durable external work item"},
    )
    def commit_issue(approval_id: str) -> dict[str, Any]:
        """提交已经由通道外人类批准的行动项；未批准请求必定失败。"""
        return store.commit_issue(approval_id)

    @server.resource(
        "work-items://schema", name="work-item-schema", mime_type="application/json",
        description="工单字段及审批状态机。",
    )
    def work_item_schema() -> str:
        return json.dumps({
            "issue": {
                "id": "ISS-NNNN",
                "priority": ["low", "medium", "high", "urgent"],
                "status": ["open", "in_progress", "done"],
            },
            "approval_flow": ["pending", "approved_or_rejected", "consumed"],
            "rule": "approve is deliberately not exposed as an MCP tool",
        }, ensure_ascii=False)

    @server.prompt(name="create-bill-action-item")
    def create_bill_action_item(problem: str, evidence_refs: str = "") -> str:
        """生成从账单守卫发现创建行动项的受控任务模板。"""
        return (
            f"为以下账单问题准备行动项：{problem}。"
            f"证据引用：{evidence_refs or '未提供'}。"
            "先调用 prepare_issue 取得完整参数和 approval_id，再用该 ID 提出 commit_issue 调用。"
            "Host 必须在调用到达本服务前暂停并请求人类审批；拿到成功结果前不得声称工单已创建。"
        )

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Work Item MCP Server")
    parser.add_argument("--data-dir", default=".sessions/work-items")
    subparsers = parser.add_subparsers(dest="command")
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--transport", choices=("stdio", "streamable-http"),
                              default="streamable-http")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8020)
    approve_parser = subparsers.add_parser("approve")
    approve_parser.add_argument("approval_id")
    approve_parser.add_argument("--by", required=True)
    reject_parser = subparsers.add_parser("reject")
    reject_parser.add_argument("approval_id")
    reject_parser.add_argument("--by", required=True)
    subparsers.add_parser("pending")
    args = parser.parse_args()

    store = WorkItemStore(args.data_dir)
    if args.command == "approve":
        print(json.dumps(store.decide(args.approval_id, True, args.by), ensure_ascii=False, indent=2))
        return
    if args.command == "reject":
        print(json.dumps(store.decide(args.approval_id, False, args.by), ensure_ascii=False, indent=2))
        return
    if args.command == "pending":
        print(json.dumps(store.pending_approvals(), ensure_ascii=False, indent=2))
        return

    server = build_server(args.data_dir)
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8020)
    transport = getattr(args, "transport", "streamable-http")
    server.settings.host = host
    server.settings.port = port
    server.run(transport=transport)


if __name__ == "__main__":
    main()
