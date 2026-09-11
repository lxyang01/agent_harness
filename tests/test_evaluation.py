from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from billguard.evaluation import (
    EvaluationReportStore,
    RoutingEvaluator,
    load_eval_cases,
    save_evaluation_report,
)
from billguard.skills import SkillRuntime


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RoutingEvaluationTests(unittest.TestCase):
    def test_fixed_fifty_case_ablation_is_reproducible_and_scoped(self):
        cases = load_eval_cases(PROJECT_ROOT / "evals" / "billguard_cases.jsonl")
        self.assertEqual(50, len(cases))
        evaluator = RoutingEvaluator(SkillRuntime(PROJECT_ROOT / "skills"))
        report = evaluator.compare(cases)
        variants = {item["variant"]: item["metrics"] for item in report["variants"]}

        # 账单域 50 条用例按当前 routes 精确命中:baseline 只会预测 bill-triage
        # (10/50),skills/full 的技能路由应为 100%;同时验证消融口径与逐用例
        # 结果自洽,保证报告可复现、指标可由 results 重新推导。
        for name in ("baseline", "skills", "full"):
            results = next(
                item for item in report["reports"] if item["variant"] == name
            )["results"]
            routing = round(sum(item["skill_pass"] for item in results) / len(results), 4)
            self.assertEqual(variants[name]["skill_routing_accuracy"], routing)
            self.assertGreaterEqual(variants[name]["overall_accuracy"], 0.0)
            self.assertLessEqual(variants[name]["overall_accuracy"], 1.0)
        self.assertEqual(0.2, variants["baseline"]["overall_accuracy"])
        self.assertEqual(1.0, variants["skills"]["skill_routing_accuracy"])
        self.assertEqual(1.0, variants["full"]["overall_accuracy"])
        self.assertIn("does not measure LLM groundedness", report["scope_note"])

    def test_report_is_saved_as_json_and_markdown_and_can_be_listed(self):
        cases = load_eval_cases(PROJECT_ROOT / "evals" / "billguard_cases.jsonl")
        report = RoutingEvaluator(SkillRuntime(PROJECT_ROOT / "skills")).compare(cases)
        with tempfile.TemporaryDirectory() as temp:
            paths = save_evaluation_report(report, temp)
            self.assertTrue(Path(paths["json"]).is_file())
            self.assertIn("Completion contract", Path(paths["markdown"]).read_text(encoding="utf-8"))
            listed = EvaluationReportStore(temp).list()
            self.assertEqual(1, len(listed))
            self.assertEqual(50, listed[0]["dataset_size"])
            self.assertEqual(3, len(listed[0]["variants"]))


if __name__ == "__main__":
    unittest.main()
