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

    def test_for_user_rejects_empty_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            with self.assertRaises(ToolError):
                service.for_user("")
            with self.assertRaises(ToolError):
                service.for_user("   ")


if __name__ == "__main__":
    unittest.main()
