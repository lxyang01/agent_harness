from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from minimal_agent.evaluation import (
    EvaluationReportStore,
    RoutingEvaluator,
    load_eval_cases,
    save_evaluation_report,
)
from minimal_agent.skills import SkillRuntime


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RoutingEvaluationTests(unittest.TestCase):
    def test_fixed_fifty_case_ablation_is_reproducible_and_scoped(self):
        cases = load_eval_cases(PROJECT_ROOT / "evals" / "feedback_agent_cases.jsonl")
        self.assertEqual(50, len(cases))
        evaluator = RoutingEvaluator(SkillRuntime(PROJECT_ROOT / "skills"))
        report = evaluator.compare(cases)
        variants = {item["variant"]: item["metrics"] for item in report["variants"]}

        self.assertLess(variants["baseline"]["skill_routing_accuracy"],
                        variants["full"]["skill_routing_accuracy"])
        self.assertEqual(1.0, variants["full"]["skill_routing_accuracy"])
        self.assertEqual(1.0, variants["full"]["completion_contract_accuracy"])
        self.assertIn("does not measure LLM groundedness", report["scope_note"])

    def test_report_is_saved_as_json_and_markdown_and_can_be_listed(self):
        cases = load_eval_cases(PROJECT_ROOT / "evals" / "feedback_agent_cases.jsonl")
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
