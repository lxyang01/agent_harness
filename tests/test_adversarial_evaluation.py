from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from billguard.adversarial_evaluation import (
    AdversarialEvaluator,
    save_adversarial_report,
)


class AdversarialEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = AdversarialEvaluator().run()

    def test_fixed_attack_surface_and_honest_known_gaps(self):
        self.assertEqual("billguard-adversarial-v1", self.report["benchmark"])
        self.assertEqual(23, self.report["dataset_size"])
        self.assertEqual(0, self.report["metrics"]["probe_errors"])
        results = {item["id"]: item for item in self.report["results"]}
        self.assertTrue(results["adv-003"]["passed"])
        self.assertTrue(results["adv-010"]["passed"])
        self.assertTrue(results["adv-014"]["passed"])
        self.assertTrue(results["adv-020"]["passed"])
        self.assertTrue(results["adv-015"]["passed"])
        self.assertTrue(results["adv-016"]["passed"])
        self.assertTrue(results["adv-017"]["passed"])
        self.assertTrue(results["adv-018"]["passed"])
        self.assertTrue(results["adv-019"]["passed"])
        self.assertTrue(results["adv-021"]["passed"])
        self.assertTrue(results["adv-022"]["passed"])
        self.assertTrue(results["adv-023"]["passed"])
        self.assertEqual(23, self.report["metrics"]["passed"])
        self.assertEqual(0, self.report["metrics"]["failed"])

    def test_metrics_match_results(self):
        passed = sum(item["passed"] for item in self.report["results"])
        self.assertEqual(passed, self.report["metrics"]["passed"])
        self.assertEqual(23 - passed, self.report["metrics"]["failed"])
        self.assertAlmostEqual(passed / 23, self.report["metrics"]["defense_rate"])

    def test_cross_tenant_leak_probe_evidence(self):
        result = next(item for item in self.report["results"]
                      if item["id"] == "adv-022")
        self.assertEqual("isolation", result["category"])
        self.assertEqual("critical", result["severity"])
        # 到达数据层的 owner 是注入的登录身份,伪造 owner/_owner 均未生效
        self.assertEqual(["mallory"], result["evidence"]["owner_reaching_service"])
        self.assertEqual([{"owner": "mallory"}], result["evidence"]["handler_arguments"])
        # mallory 聚合为空视图,alice 自己的数据不受影响
        self.assertEqual(0, result["evidence"]["mallory_aggregate_count"])
        self.assertEqual(1, result["evidence"]["alice_own_count"])

    def test_report_contains_failures_and_reproduction_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = save_adversarial_report(
                self.report, root / "evaluations", root / "canonical.md",
            )
            markdown = Path(paths["canonical_markdown"]).read_text(encoding="utf-8")
            self.assertIn("对抗评测与失败案例报告", markdown)
            self.assertIn("adv-015", markdown)
            self.assertIn("python -m billguard.adversarial_eval", markdown)
            self.assertTrue(Path(paths["json"]).is_file())


if __name__ == "__main__":
    unittest.main()
