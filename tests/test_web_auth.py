from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from billguard.auth import AuthError, PermissionDenied, User, UserStore
from billguard.bills import BillService
from billguard.web import BillGuardApp

DEMO = ("tx_id,paid_at,merchant,category,amount,method,note" + chr(10)
        + "TX-1,2026-08-05 21:00:00,腾讯视频,订阅,25.0,微信,月费" + chr(10))


def build_app(root: Path) -> BillGuardApp:
    from tests.llm_doubles import FinalLLM
    return BillGuardApp(root / "web", root / "docs", FinalLLM())


def make_users(root: Path) -> UserStore:
    users = UserStore(root / "auth")
    users.create("alice", "alice-pass-123", "user")
    users.create("admin", "admin-pass-1234", "admin")
    users.create("mallory", "mallory-pass-12", "user")
    return users


class SessionOwnershipTests(unittest.TestCase):
    def test_sessions_are_private_and_ownerless_admin_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = build_app(root)
            alice = User("alice", "user")
            mallory = User("mallory", "user")
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
                             SessionStore(root / "web" / "billguard" / "sessions").load("s-alice").owner)

    def test_ownerless_legacy_session_admin_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = build_app(root)
            from billguard.session import SessionStore
            store = SessionStore(root / "web" / "billguard" / "sessions")
            store.save(store.load("legacy"))  # 无主旧文件
            boss = User("admin", "admin")
            mallory = User("mallory", "user")
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
            store = SessionStore(root / "web" / "billguard" / "sessions")
            app.chat(User("mallory", "user"), "fresh", "总结问题")
            self.assertEqual("mallory", store.load("fresh").owner)

    def test_delete_session_only_by_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = build_app(root)
            alice = User("alice", "user")
            app.chat(alice, "s1", "总结")
            with self.assertRaises(PermissionDenied):
                app.delete_session(User("mallory", "user"), "s1")
            self.assertTrue(app.delete_session(alice, "s1")["deleted"])


class ServerSideIdentityTests(unittest.TestCase):
    def test_operator_fields_use_server_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = build_app(root)
            service = BillService(root / "web" / "billguard" / "bills")
            alice = User("alice", "user")
            # 数据隔离后业务写入走按用户装配的受限视图:经应用层以 alice 身份入库
            app.import_bills(alice, {
                "filename": "demo.csv",
                "csv_text": "tx_id,paid_at,merchant,amount\nTX-1,2026-08-04 10:30:00,美团外卖,32.5\n"})
            # 请求体里的 operator 一律忽略,取服务端身份
            result = app.update_workflow(alice, {
                "tx_ids": ["TX-1"],
                "updates": {"status": "待核查", "note": "疑似重复", "operator": "ghost"},
                "operator": "ghost"})
            self.assertEqual("alice", result["operator"])
            audits = service.transaction_audits("TX-1")
            self.assertEqual("alice", audits[-1]["operator"])
            # 类别改判同样取服务端身份
            recategorized = app.update_transaction_category(
                alice, {"tx_id": "TX-1", "category": "订阅", "operator": "ghost"})
            self.assertEqual("alice", recategorized["result"]["operator"])
            self.assertEqual("alice", service.transaction_audits("TX-1")[-1]["operator"])

    def test_decide_approval_uses_server_identity_and_blocks_viewer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            users = make_users(root)
            from tests.llm_doubles import FinalLLM
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
            app = BillGuardApp(root / "web", root / "docs", ScriptedLLM(), FakeManager(),
                               gateway, work_items)
            alice = User("alice", "user")
            mallory = User("mallory", "user")

            paused = app.chat(alice, "s-approve", "$monthly-guard-report create issue")
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
            service = BillService(root / "web" / "billguard" / "bills")
            alice = User("alice", "user")
            # 数据隔离后业务写入走按用户装配的受限视图:经应用层以 alice 身份入库
            app.import_bills(alice, {
                "filename": "demo.csv",
                "csv_text": "tx_id,paid_at,merchant,amount\nTX-1,2026-08-04 10:30:00,美团外卖,32.5\n"})
            result = app.update_workflow(alice, {
                "tx_ids": ["TX-1"],
                "updates": {"status": "核查中", "note": "正在核对", "operator": "ghost"},
                "operator": "ghost"})
            self.assertEqual(1, result["count"])
            audits = service.transaction_audits("TX-1")
            self.assertEqual("alice", audits[-1]["operator"])
            # 非法 status 与超长备注按现有校验风格拒绝
            with self.assertRaises(ValueError):
                app.update_workflow(alice,
                                    {"tx_ids": ["TX-1"], "updates": {"status": "处理中"}})
            with self.assertRaises(ValueError):
                app.update_workflow(alice,
                                    {"tx_ids": ["TX-1"], "updates": {"status": "正常", "note": "长" * 201}})


class UserDeleteTests(unittest.TestCase):
    def _app(self, root: Path):
        from tests.llm_doubles import FinalLLM
        from billguard.auth import AuthSessionStore, Authenticator, UserStore
        from billguard.web import BillGuardApp
        users = UserStore(root / "auth")
        users.create("boss", "boss-pass-1234", "admin")
        users.create("alice", "alice-pass-123", "user")
        app = BillGuardApp(root / "web", root / "docs", FinalLLM(),
                           authenticator=Authenticator(users, AuthSessionStore(root / "auth")))
        return app, users

    def test_delete_user_removes_account_and_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, users = self._app(root)
            boss = User("boss", "admin")
            alice = User("alice", "user")
            app.import_bills(alice, {"filename": "d.csv", "csv_text": DEMO})
            self.assertEqual(1, app.snapshot(alice, "s")["overview"]["count"])

            result = app.admin_delete_user(boss, {"username": "alice"})

            self.assertTrue(result["deleted"])
            with self.assertRaises(Exception):
                users.get("alice")  # 账号已删
            scoped = app.bills.for_user("alice")
            self.assertEqual(0, scoped.overview()["count"])  # 数据同步清除
            with self.assertRaises(AuthError):
                users.verify("alice", "alice-pass-123")  # 登录随账号失效

    def test_delete_guards_self_and_last_admin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, users = self._app(root)
            boss = User("boss", "admin")
            with self.assertRaises(ValueError):
                app.admin_delete_user(boss, {"username": "boss"})  # 不能删自己
            users.create("root2", "root2-pass-1234", "admin")
            app.admin_delete_user(boss, {"username": "root2"})  # 有其他 admin 时可删
            with self.assertRaises(ValueError):
                app.admin_delete_user(boss, {"username": "boss"})  # boss 已是最后一个启用 admin


class PurgeMyDataTests(unittest.TestCase):
    def test_purge_clears_own_data_but_not_others_nor_sessions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = build_app(root)
            alice, mallory = User("alice", "user"), User("mallory", "user")
            app.import_bills(alice, {"filename": "d.csv", "csv_text": DEMO})
            app.import_bills(mallory, {"filename": "d.csv", "csv_text": DEMO})
            app.chat(alice, "s-alice", "hi")  # 对话与 Session 保留

            result = app.purge_my_data(alice, {})

            self.assertGreaterEqual(result["transactions"], 1)
            self.assertEqual(0, app.bills.for_user("alice").overview()["count"])
            self.assertEqual(1, app.bills.for_user("mallory").overview()["count"])  # 他人不受影响
            sessions = app.list_sessions(alice)
            self.assertIn("s-alice", [item["id"] for item in sessions])  # Session 保留


class PurgeLegacyDataTests(unittest.TestCase):
    def test_admin_purge_clears_null_legacy_rows_visible_to_admin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = build_app(root)
            boss, mallory = User("admin", "admin"), User("mallory", "user")
            # NULL 存量行(数据隔离前导入的旧数据):仅 admin 可见
            app.bills.import_bills("legacy.csv", DEMO)
            self.assertEqual(1, app.snapshot(boss, "s")["overview"]["count"])

            result = app.purge_my_data(boss, {})

            self.assertEqual(1, result["transactions"])
            self.assertEqual(0, app.snapshot(boss, "s")["overview"]["count"])

    def test_non_admin_purge_leaves_null_legacy_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = build_app(root)
            app.bills.import_bills("legacy.csv", DEMO)  # NULL 存量
            mallory = User("mallory", "user")
            app.purge_my_data(mallory, {})  # mallory 看不到 NULL 行,也不应清掉它们
            self.assertEqual(1, app.bills.for_user("admin").overview()["count"])
