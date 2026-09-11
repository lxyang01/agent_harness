# tests/test_isolation.py
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from billguard.bills import BillService
from billguard.tools import Tool, ToolError, ToolRegistry

DEMO = ("tx_id,paid_at,merchant,category,amount,method,note\n"
        "TX-1,2026-08-05 21:00:00,腾讯视频,订阅,25.0,微信,月费\n")


class ScopedDataTests(unittest.TestCase):
    def test_user_b_cannot_see_user_a_data(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            alice = service.for_user("alice")
            bob = service.for_user("bob")
            result = alice.import_bills("demo.csv", DEMO)
            self.assertEqual(1, result["imported_rows"])
            self.assertEqual(0, bob.overview()["count"])
            self.assertEqual([], bob.query()["items"])
            self.assertEqual([], bob.anomalies(31, "price_hike", 10)["items"])

    def test_admin_sees_legacy_null_rows_others_do_not(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            # 根句柄写入(NULL owner,模拟存量)
            service.import_bills("legacy.csv", DEMO)
            admin = service.for_user("admin")
            bob = service.for_user("bob")
            self.assertEqual(1, admin.overview()["count"])
            self.assertEqual(0, bob.overview()["count"])

    def test_cross_owner_workflow_update_is_noop(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            alice = service.for_user("alice")
            alice.import_bills("demo.csv", DEMO)
            result = service.for_user("bob").update_workflow(
                ["TX-1"], "bob", status="待核查")
            self.assertEqual(0, result["count"])

    def test_first_for_user_seeds_default_categories(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            categories = service.for_user("bob").categories()
            names = {item["name"] for item in categories}
            self.assertIn("餐饮", names)
            self.assertIn("订阅", names)

    def test_same_csv_imports_independently_per_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            alice = service.for_user("alice")
            bob = service.for_user("bob")
            first = alice.import_bills("demo.csv", DEMO)
            second = bob.import_bills("demo.csv", DEMO)
            # 相同编号的账单文件,各 owner 各自完整入库
            self.assertEqual(first["imported_rows"], second["imported_rows"])
            self.assertEqual(0, second["duplicate_rows"])
            self.assertEqual(1, alice.overview()["count"])
            self.assertEqual(1, bob.overview()["count"])
            # 同一 owner 重复导入仍按自己的数据去重
            again = alice.import_bills("demo.csv", DEMO)
            self.assertEqual(0, again["imported_rows"])
            self.assertEqual(1, again["duplicate_rows"])
            # 同编号交易的工单/审计按 owner 作用域
            result = bob.update_workflow(["TX-1"], "bob", status="待核查")
            self.assertEqual(1, result["count"])
            self.assertEqual("正常", alice.query()["items"][0]["status"])
            self.assertEqual("待核查", bob.query()["items"][0]["status"])
            self.assertEqual(1, len(bob.transaction_audits("TX-1")))

    def test_update_transaction_category_within_owner_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            bob = service.for_user("bob")
            alice = service.for_user("alice")
            bob.import_bills("demo.csv", DEMO)
            alice.import_bills("demo.csv", DEMO)
            result = bob.update_transaction_category("TX-1", "娱乐")
            self.assertEqual("订阅", result["old_category"])
            self.assertEqual("娱乐", result["new_category"])
            row = next(item for item in bob.query()["items"] if item["tx_id"] == "TX-1")
            self.assertEqual("娱乐", row["category"])
            audits = bob.transaction_audits("TX-1")
            self.assertEqual("bob", audits[-1]["owner"])
            self.assertEqual("web-user", audits[-1]["operator"])
            # 同号交易的另一 owner 行不受影响
            alice_row = next(item for item in alice.query()["items"] if item["tx_id"] == "TX-1")
            self.assertEqual("订阅", alice_row["category"])

    def test_for_user_rejects_empty_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            with self.assertRaises(ToolError):
                service.for_user("")
            with self.assertRaises(ToolError):
                service.for_user("   ")


DEMO_BOB = ("tx_id,paid_at,merchant,category,amount,method,note\n"
            "TX-2,2026-08-06 12:30:00,美团外卖,餐饮,60.0,支付宝,午餐\n")

DEMO_PII = ("tx_id,paid_at,merchant,category,amount,method,note\n"
            "TX-3,2026-08-07 09:00:00,腾讯云,订阅,99.0,微信,订单号 SO-ABCD12345\n")


class WebScopedTests(unittest.TestCase):
    def test_two_users_isolated_through_app(self):
        from billguard.agents import BillMockLLM
        from billguard.auth import User
        from billguard.web import BillGuardApp
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = BillGuardApp(root / "sessions", root / "docs", BillMockLLM())
            alice, bob = User("alice", "approver"), User("bob", "viewer")
            app.import_bills(alice, {"filename": "d.csv", "csv_text": DEMO})
            self.assertEqual(0, app.snapshot(bob, "s1")["overview"]["count"])
            self.assertEqual(1, app.snapshot(alice, "s1")["overview"]["count"])

    def test_bob_chat_answers_with_own_data_only(self):
        from billguard.agents import BillMockLLM
        from billguard.auth import User
        from billguard.web import BillGuardApp
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = BillGuardApp(root / "sessions", root / "docs", BillMockLLM())
            alice, bob = User("alice", "approver"), User("bob", "viewer")
            app.import_bills(alice, {"filename": "a.csv", "csv_text": DEMO})
            app.import_bills(bob, {"filename": "b.csv", "csv_text": DEMO_BOB})
            # 本地模式 registry 按 bob 装配:bill_overview 只见 bob 自己的行
            result = app.chat(bob, "s-bob", "总结一下当前的支出情况")
            self.assertIn("1 笔支出", result["answer"])
            self.assertIn("美团外卖", result["answer"])
            self.assertNotIn("腾讯视频", result["answer"])
            self.assertEqual(1, result["overview"]["count"])

    def test_scoped_registry_search_works_and_masks(self):
        # 受限视图须透传纯文本脱敏助手,bill_search 命中行时才不会炸
        from billguard.agents import build_bill_registry
        from billguard.bills import BillService
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            service.for_user("bob").import_bills("b.csv", DEMO_PII)
            registry = build_bill_registry(service.for_user("bob"))
            result = registry.execute("bill_search", {"query": "腾讯", "limit": 5})
            self.assertEqual(1, result["total"])
            self.assertIn("[订单号]", result["items"][0]["note"])


DEMO_ALICE = ("tx_id,paid_at,merchant,category,amount,method,note\n"
              "AL-1,2026-08-05 12:00:00,盒马鲜生,购物,120.0,支付宝,生鲜采购\n")


class _OwnerProbeManager:
    """MCP 假管理器:bill.* 工具直达真实 BillService,并记录每次调用到达服务的 owner。
    schema 与真实 FastMCP 广播形态同构:owner 在 properties、不在 required、
    无 additionalProperties 限制(伪造的 owner 参数可以到达 handler,由包装层覆盖)。"""

    def __init__(self, service: BillService) -> None:
        self.service = service
        self.seen_owners: list[str | None] = []
        self.seen_calls: list[dict] = []

    def snapshots(self):
        return [SimpleNamespace(name="bill", transport="test", server_name="t",
                                server_version="t", protocol_version="t",
                                tools=(), resources=(), prompts=())]

    def register_tools(self, registry: ToolRegistry, server_name: str) -> tuple:
        registry.register(Tool(
            "bill.aggregate", "Aggregate bills",
            {"type": "object", "properties": {
                "merchant": {"type": "string"},
                "owner": {"type": "string"},
            }},
            self._aggregate))
        registry.register(Tool(
            "bill.update_status", "Update workflow status",
            {"type": "object", "properties": {
                "tx_ids": {"type": "array", "items": {"type": "string"}},
                "status": {"type": "string"},
                "operator": {"type": "string"},
                "owner": {"type": "string"},
            }, "required": ["tx_ids", "status", "operator"]},
            self._update_status))
        # 非 bill.* 前缀的工具必须原样透传,不受注入影响
        registry.register(Tool(
            "work-items.list_issues", "List work items",
            {"type": "object", "properties": {"owner": {"type": "string"}}},
            self._list_issues))
        return registry.names()

    def _aggregate(self, **kwargs):
        owner = kwargs.get("owner")
        self.seen_owners.append(owner)
        self.seen_calls.append(dict(kwargs))
        return self.service.overview(owner=owner)

    def _update_status(self, **kwargs):
        owner = kwargs.pop("owner", None)
        self.seen_owners.append(owner)
        self.seen_calls.append(dict(kwargs))
        return self.service.update_workflow(
            kwargs["tx_ids"], kwargs["operator"],
            status=kwargs["status"], owner=owner)

    def _list_issues(self, **kwargs):
        self.seen_owners.append(kwargs.get("owner"))
        self.seen_calls.append(dict(kwargs))
        return {"count": 0}


class OwnerInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = BillService(Path(self.temp.name))
        self.service.import_bills("legacy.csv", DEMO)  # owner=None → 存量 NULL 行
        self.service.for_user("alice").import_bills("alice.csv", DEMO_ALICE)
        self.manager = _OwnerProbeManager(self.service)

    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        self.manager.register_tools(registry, "bill")
        return registry

    def test_model_forged_owner_is_overwritten(self):
        from billguard.web import inject_owner_identity
        registry = inject_owner_identity(self._registry(), "alice")
        result = registry.execute("bill.aggregate", {"owner": "mallory"})
        # 到达服务的 owner 是注入的服务端身份,模型伪造值被覆盖
        self.assertEqual(["alice"], self.manager.seen_owners)
        self.assertEqual(1, result["count"])
        self.assertEqual({"盒马鲜生"},
                         {item["name"] for item in result["top_merchants"]})
        # 写路径同样受边界保护:存量 NULL 行不在 alice 视野,伪造 mallory 也改不到
        update = registry.execute("bill.update_status", {
            "tx_ids": ["TX-1"], "status": "待核查", "operator": "model",
            "owner": "mallory",
        })
        self.assertEqual(0, update["count"])
        self.assertEqual(["alice", "alice"], self.manager.seen_owners)

    def test_schema_hides_owner_from_model(self):
        from billguard.web import inject_owner_identity
        registry = inject_owner_identity(self._registry(), "alice")
        parameters = registry.get("bill.aggregate").parameters
        self.assertNotIn("owner", parameters.get("properties", {}))
        self.assertNotIn("owner", parameters.get("required", []))
        # 模型侧实际拿到的 schema 同样不含 owner
        schema = next(item for item in registry.schemas(registry.names())
                      if item["name"] == "bill.aggregate")
        self.assertNotIn("owner", schema["parameters"].get("properties", {}))
        # work-items.* 原样透传:handler 未包装,owner 参数保留可用
        self.assertIn("owner", registry.get("work-items.list_issues").parameters["properties"])
        registry.execute("work-items.list_issues", {"owner": "someone-else"})
        self.assertEqual("someone-else", self.manager.seen_owners[-1])

    def test_agent_mcp_tools_carry_server_identity(self):
        # web MCP 分支:_agent 产出的注册表已按登录用户注入,伪造 owner 无效
        from billguard.agents import BillMockLLM
        from billguard.auth import User
        from billguard.web import BillGuardApp
        app = BillGuardApp(Path(self.temp.name) / "web", Path(self.temp.name) / "docs",
                           BillMockLLM(), self.manager)
        agent = app._agent(User("alice", "viewer"), "s-inject")
        result = agent.tools.execute("bill.aggregate", {"owner": "mallory"})
        self.assertEqual(["alice"], self.manager.seen_owners)
        self.assertEqual(1, result["count"])

    def test_named_parameter_collision_cannot_override_identity(self):
        # 包装函数不得有具名参数:模型显式传 _owner 同名关键字会覆盖默认绑定
        # (_owner="admin" 即取得 admin 视野:跨 owner 读 + 存量行写)
        from billguard.web import inject_owner_identity
        registry = inject_owner_identity(self._registry(), "alice")
        result = registry.execute("bill.aggregate", {
            "_owner": "admin", "merchant": "盒马鲜生",
        })
        self.assertEqual(["alice"], self.manager.seen_owners)
        self.assertEqual({"盒马鲜生"},
                         {item["name"] for item in result["top_merchants"]})
        # 写路径同理:伪造 _owner 也改不到 alice 视野之外的存量行
        update = registry.execute("bill.update_status", {
            "tx_ids": ["TX-1"], "status": "待核查", "operator": "model",
            "_owner": "admin",
        })
        self.assertEqual(0, update["count"])
        self.assertEqual(["alice", "alice"], self.manager.seen_owners)

    def test_unknown_keys_never_reach_handler(self):
        # Schema 之外的键(含 mcp_runtime 动态 handler 的 _server/_tool 内部键)
        # 必须在包装层被丢弃,不得改变身份或路由
        from billguard.web import inject_owner_identity
        registry = inject_owner_identity(self._registry(), "alice")
        registry.execute("bill.aggregate", {
            "_server": "work-items", "_tool": "commit_issue", "_original": "x",
            "anything": 1, "merchant": "盒马鲜生", "owner": "mallory",
        })
        self.assertEqual({"merchant": "盒马鲜生", "owner": "alice"},
                         self.manager.seen_calls[-1])


if __name__ == "__main__":
    unittest.main()
