# tests/test_isolation.py
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from billguard.bills import BillService
from billguard.tools import ToolError

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


if __name__ == "__main__":
    unittest.main()
