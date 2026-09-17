# tests/test_bills.py
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from billguard.bills import BillFilters, BillService
from billguard.tools import ToolError


def demo_csv() -> str:
    return (
        "tx_id,paid_at,merchant,category,amount,method,note\n"
        "TX-001,2026-07-05 10:00:00,饿了么,餐饮,35.5,支付宝,午餐\n"
        "TX-002,2026-07-05 21:00:00,腾讯视频,订阅,15.0,微信,月费\n"
        "TX-003,2026-08-05 21:00:00,腾讯视频,订阅,25.0,微信,月费\n"
        "TX-004,2026-08-06 10:00:00,百度网盘,订阅,18.0,支付宝,月费\n"
        "TX-005,2026-08-06 10:05:00,百度网盘,订阅,18.0,支付宝,月费,重复\n"
    )


class ImportTests(unittest.TestCase):
    def test_import_dedup_and_auto_category(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            result = service.import_bills("demo.csv", demo_csv())
            self.assertEqual(5, result["imported_rows"])
            result2 = service.import_bills("demo.csv", demo_csv())
            self.assertEqual(0, result2["imported_rows"])
            self.assertEqual(5, result2["duplicate_rows"])
            overview = service.overview()
            self.assertEqual(111.5, round(overview["total_amount"], 2))
            self.assertEqual(5, overview["count"])

    def test_import_rejects_bad_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            result = service.import_bills("bad.csv",
                "tx_id,paid_at,merchant,category,amount,method,note\n"
                "TX-1,不是时间,饿了么,餐饮,35.5,支付宝,x\n"
                "TX-2,2026-07-05 10:00:00,饿了么,餐饮,abc,支付宝,x\n")
            self.assertEqual(2, result["failed_rows"])
            self.assertEqual(0, result["imported_rows"])


class OverviewCoverageTests(unittest.TestCase):
    """overview 必须带出过滤后数据集的实际覆盖区间(空结果缺日期锚点会诱发模型编造年份)。"""

    def test_overview_reports_data_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            service.import_bills("demo.csv", demo_csv())
            # 全量:demo 数据跨 2026-07-05..2026-08-06
            overview = service.overview()
            self.assertEqual("2026-07-05", overview["data_from"])
            self.assertEqual("2026-08-06", overview["data_to"])
            # 非空过滤集:区间必须来自过滤后的行,而非全库
            july = service.overview(BillFilters(date_from="2026-07-01", date_to="2026-07-31"))
            self.assertEqual(2, july["count"])
            self.assertEqual("2026-07-05", july["data_from"])
            self.assertEqual("2026-07-05", july["data_to"])

    def test_overview_coverage_none_when_no_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            service.import_bills("demo.csv", demo_csv())
            # 过滤后为空(如 9 月无数据):无覆盖区间可报
            empty = service.overview(BillFilters(date_from="2026-09-01", date_to="2026-09-30"))
            self.assertEqual(0, empty["count"])
            self.assertIsNone(empty["data_from"])
            self.assertIsNone(empty["data_to"])
            # 空库同理
            fresh = BillService(Path(temp) / "fresh")
            blank = fresh.overview()
            self.assertIsNone(blank["data_from"])
            self.assertIsNone(blank["data_to"])

    def test_overview_data_note_empty_db_vs_filtered_empty(self):
        """空结果集必须带确定性说明:整库为空提示导入;仅筛选落空提示放宽条件。

        None 日期锚点 + 明确 note 双保险,杜绝模型在空库时编造“2024年6月”类区间。"""
        with tempfile.TemporaryDirectory() as temp:
            # 空库:note 指向“库为空,先导入”
            blank = BillService(Path(temp) / "fresh").overview()
            self.assertEqual(0, blank["count"])
            self.assertIn("账单库为空", blank["data_note"])
            self.assertIn("导入", blank["data_note"])
            # 有数据但筛选落空:note 指向筛选条件
            service = BillService(Path(temp))
            service.import_bills("demo.csv", demo_csv())
            empty = service.overview(BillFilters(date_from="2026-09-01", date_to="2026-09-30"))
            self.assertEqual(0, empty["count"])
            self.assertIsNone(empty["data_from"])
            self.assertIn("筛选条件", empty["data_note"])
            # 非空结果集:不带提示
            self.assertIsNone(service.overview()["data_note"])


class SubscriptionTests(unittest.TestCase):
    def test_subscriptions_import_and_hike_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            service.import_bills("demo.csv", demo_csv())
            service.import_subscriptions("subs.csv",
                "name,merchant,cycle,expected_amount\n腾讯视频,腾讯视频,月,15.0\n")
            subs = service.subscriptions()
            self.assertEqual(1, len(subs))
            anomalies = service.anomalies(62, "price_hike", 10)
            # 腾讯视频预期 15,实扣 25 → 涨价异常
            self.assertTrue(any(item["name"] == "腾讯视频" for item in anomalies["items"]))


class AnomalyTests(unittest.TestCase):
    def test_duplicate_and_outlier_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            service.import_bills("demo.csv", demo_csv())
            duplicates = service.anomalies(31, "duplicate", 10)
            # 百度网盘 5 分钟内同金额两笔
            self.assertTrue(any("百度网盘" in item["name"] for item in duplicates["items"]))
            outliers = service.anomalies(62, "outlier", 10)
            # 无大额离群时为空
            self.assertEqual([], outliers["items"])

    def test_spike_detection_by_category(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            rows = ["tx_id,paid_at,merchant,category,amount,method,note"]
            for day in range(1, 29):
                month = 7 if day <= 20 else 8
                rows.append(f"TX-{day:03d},2026-{month:02d}-{day if day<=20 else day-20:02d} 12:00:00,美团,餐饮,20,支付宝,饭")
            service.import_bills("m.csv", "\n".join(rows) + "\n")
            spikes = service.anomalies(31, "spike", 5)
            # 结构断言:返回结构完整可被上层消费
            self.assertIn("items", spikes)
            self.assertIn("thresholds", spikes)
            self.assertEqual("spike", spikes["dimension"])


class WorkflowAndMaskTests(unittest.TestCase):
    def test_workflow_update_records_operator(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            service.import_bills("demo.csv", demo_csv())
            result = service.update_workflow(["TX-001"], "alice", status="待核查", note="查一下")
            self.assertEqual(1, result["count"])
            audits = service.transaction_audits("TX-001")
            self.assertEqual("alice", audits[-1]["operator"])

    def test_mask_pii(self):
        masked, counts = BillService(Path(".")).mask_pii("订单号 SO-20260801123 手机 13812345678")
        self.assertNotIn("13812345678", masked)
        self.assertIn("订单号", counts)


class CategoryNameLengthTests(unittest.TestCase):
    """类别名超长(>40 字符)一律拒绝,不再静默截断。"""

    def test_update_transaction_category_rejects_long_name(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            service.import_bills("demo.csv", demo_csv())
            with self.assertRaises(ToolError) as ctx:
                service.update_transaction_category("TX-001", "超" * 41, "alice")
            self.assertIn("40", str(ctx.exception))
            names = [item["name"] for item in service.categories()]
            self.assertNotIn("超" * 40, names)  # 没有截断入库

    def test_save_category_rejects_long_name(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp))
            with self.assertRaises(ToolError) as ctx:
                service.save_category("名" * 41, ["关键词"], True, operator="alice")
            self.assertIn("40", str(ctx.exception))
            self.assertNotIn("名" * 40,
                             [item["name"] for item in service.categories()])
            # 40 字符边界仍可保存
            result = service.save_category("名" * 40, ["关键词"], True, operator="alice")
            self.assertEqual("名" * 40, result["name"])


if __name__ == "__main__":
    unittest.main()
