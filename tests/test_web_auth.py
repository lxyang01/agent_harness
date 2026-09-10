from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from billguard.auth import AuthError, PermissionDenied, User, UserStore
from billguard.feedback import FeedbackService
from billguard.web import FeedbackWebApp


def build_app(root: Path) -> FeedbackWebApp:
    from billguard.agents import FeedbackMockLLM
    return FeedbackWebApp(root / "web", root / "docs", FeedbackMockLLM())


def make_users(root: Path) -> UserStore:
    users = UserStore(root / "auth")
    users.create("alice", "alice-pass-123", "approver")
    users.create("admin", "admin-pass-1234", "admin")
    users.create("mallory", "mallory-pass-12", "viewer")
    return users


class SessionOwnershipTests(unittest.TestCase):
    def test_sessions_are_private_and_ownerless_admin_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = build_app(root)
            alice = User("alice", "approver")
            mallory = User("mallory", "viewer")
            boss = User("admin", "admin")
            app.chat(alice, "s-alice", "最近 7 天的问题")
            # 私有性:他人 session 不可见、不可访问
            self.assertEqual(["s-alice"], [item["id"] for item in app.list_sessions(alice)])
            self.assertEqual([], [item["id"] for item in app.list_sessions(mallory)])
            for method_args in (
                lambda: app.snapshot(mallory, "s-alice"),
                lambda: app.chat(mallory, "s-alice", "hi"),
                lambda: app.list_runs(mallory, "s-alice"),
                lambda: app.run_detail(mallory, "s-alice", {"trace_id": "x"}),
                lambda: app.save_report(mallory, "s-alice", {"title": "t", "content": "c"}),
                lambda: app.delete_session(mallory, "s-alice"),
            ):
                with self.assertRaises(PermissionDenied):
                    method_args()
            # 归属留痕
            from billguard.session import SessionStore
            self.assertEqual("alice",
                             SessionStore(root / "web" / "feedback_sessions").load("s-alice").owner)

    def test_ownerless_legacy_session_admin_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = build_app(root)
            from billguard.session import SessionStore
            store = SessionStore(root / "web" / "feedback_sessions")
            store.save(store.load("legacy"))  # 无主旧文件
            boss = User("admin", "admin")
            mallory = User("mallory", "viewer")
            app.snapshot(boss, "legacy")  # admin 可读
            with self.assertRaises(PermissionDenied):
                app.snapshot(mallory, "legacy")
            self.assertIn("legacy", [item["id"] for item in app.list_sessions(boss)])
            self.assertNotIn("legacy", [item["id"] for item in app.list_sessions(mallory)])
            app.chat(boss, "legacy", "你好")  # admin 写路径接手归属
            self.assertEqual("admin", store.load("legacy").owner)

    def test_new_session_claimed_by_first_user(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = build_app(root)
            from billguard.session import SessionStore
            store = SessionStore(root / "web" / "feedback_sessions")
            app.chat(User("mallory", "viewer"), "fresh", "总结问题")
            self.assertEqual("mallory", store.load("fresh").owner)

    def test_delete_session_only_by_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = build_app(root)
            alice = User("alice", "approver")
            app.chat(alice, "s1", "总结")
            with self.assertRaises(PermissionDenied):
                app.delete_session(User("mallory", "viewer"), "s1")
            self.assertTrue(app.delete_session(alice, "s1")["deleted"])


class ServerSideIdentityTests(unittest.TestCase):
    def test_operator_fields_use_server_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = build_app(root)
            service = FeedbackService(root / "web" / "feedback")
            service.import_csv("demo.csv",
                               "ticket_id,created_at,content\nTK-1,2026-08-04 10:30:00,微信支付失败\n")
            alice = User("alice", "approver")
            # 请求体里的 operator 一律忽略,取服务端身份
            result = app.update_feedback_tags(
                alice, {"ticket_id": "TK-1", "tags": ["支付"], "operator": "ghost"})
            self.assertEqual("alice", result["result"]["operator"])
            # brief 笔误:update_tags 落库到 tag_audit_logs(无公开读取方法),直接查库验证留痕
            import sqlite3
            conn = sqlite3.connect(service.db_path)
            row = conn.execute(
                "SELECT operator FROM tag_audit_logs WHERE ticket_id='TK-1' ORDER BY id DESC").fetchone()
            conn.close()  # Windows:显式关闭,否则临时目录清理时 db 仍被占用
            self.assertEqual("alice", row[0])

    def test_decide_approval_uses_server_identity_and_blocks_viewer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            users = make_users(root)
            from billguard.agents import FeedbackMockLLM
            from billguard.work_items import WorkItemStore
            from billguard.policy import ApprovalStore, PolicyGateway
            from types import SimpleNamespace
            from billguard.tools import Tool, ToolRegistry
            from billguard.policy import ToolPolicy

            work_items = WorkItemStore(root / "work-items")
            remote = work_items.prepare_issue("Fix checkout", "Investigate failures", "high")

            class FakeManager:
                def snapshots(self):
                    return [SimpleNamespace(name="work-items", transport="test", server_name="t",
                                            server_version="t", protocol_version="t",
                                            tools=(), resources=(), prompts=())]

                def register_tools(self, registry: ToolRegistry, server_name: str):
                    registry.register(Tool(
                        "work-items.commit_issue", "Commit approved issue",
                        {"type": "object", "properties": {"approval_id": {"type": "string"}},
                         "required": ["approval_id"], "additionalProperties": False},
                        work_items.commit_issue,
                        policy=ToolPolicy("high_write", True, "Creates durable work item")))
                    return registry.names()

            import json

            class ScriptedLLM:
                def __init__(self) -> None:
                    self.step = 0

                def complete(self, messages, tools) -> str:
                    self.step += 1
                    if self.step == 1:
                        return json.dumps({"thought": "commit", "tool_call": {
                            "name": "work-items.commit_issue",
                            "arguments": {"approval_id": remote["approval_id"]}}}, ensure_ascii=False)
                    return json.dumps({"thought": "done", "final": "Issue created"}, ensure_ascii=False)

            gateway = PolicyGateway(ApprovalStore(root / "web" / "policy"))
            app = FeedbackWebApp(root / "web", root / "docs", ScriptedLLM(), FakeManager(),
                                 gateway, work_items)
            alice = User("alice", "approver")
            mallory = User("mallory", "viewer")

            paused = app.chat(alice, "s-approve", "$executive-report create issue")
            self.assertEqual("approval_pending", paused["status"])
            with self.assertRaises(PermissionDenied):  # viewer 无审批能力
                app.decide_approval(mallory, "s-approve",
                                    {"approval_id": paused["approval"]["id"], "decision": "approve"})
            result = app.decide_approval(alice, "s-approve", {
                "approval_id": paused["approval"]["id"], "decision": "approve",
                "decided_by": "product-owner"})  # 伪造身份被忽略
            self.assertEqual("completed", result["status"])
            self.assertEqual("alice", result["approval"]["decided_by"])
            issue = work_items.list_issues()["items"][0]
            self.assertEqual("alice", issue["created_by"])


if __name__ == "__main__":
    unittest.main()


class WorkflowOperatorKeyTests(unittest.TestCase):
    def test_update_workflow_ignores_operator_key_in_updates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = build_app(root)
            service = FeedbackService(root / "web" / "feedback")
            service.import_csv("demo.csv",
                               "ticket_id,created_at,content\nTK-1,2026-08-04 10:30:00,微信支付失败\n")
            result = app.update_workflow(User("alice", "approver"), {
                "ticket_ids": ["TK-1"],
                "updates": {"status": "处理中", "operator": "ghost"},
                "operator": "ghost"})
            self.assertEqual(1, result["count"])
            audits = service.feedback_audits("TK-1")
            self.assertEqual("alice", audits[-1]["operator"])
