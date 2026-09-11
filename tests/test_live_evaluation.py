from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from billguard.agents import BillMockLLM
from billguard.harness import AgentResponse, RunEvent
from billguard.live_evaluation import (
    ArgumentRule,
    LiveEvalCase,
    LiveEvaluationRunner,
    load_live_eval_cases,
    save_live_evaluation_report,
    score_live_run,
    summarize_live_results,
)
from billguard.live_eval import parse_case_selector


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LiveEvaluationTests(unittest.TestCase):
    def test_compact_case_selector_expands_ranges(self):
        self.assertEqual(
            ["live-007", "live-008", "live-009", "live-010", "live-012", "live-013", "live-014", "live-015"],
            parse_case_selector("7-10,12-15"),
        )

    def test_compact_case_selector_rejects_descending_range(self):
        with self.assertRaisesRegex(ValueError, "ascending"):
            parse_case_selector("10-7")

    def test_fixed_live_dataset_has_real_workflow_coverage(self):
        cases = load_live_eval_cases(PROJECT_ROOT / "evals" / "live_agent_cases.jsonl")
        self.assertEqual(15, len(cases))
        self.assertEqual({"triage", "anomaly", "root_cause", "report", "approval"},
                         {case.category for case in cases})
        # 审批用例到 prepare/commit 检查点为止;evidence/因果/结构检查覆盖
        # 下钻、根因区分和月度守卫报告四章节。
        self.assertEqual(2, sum(case.expected_status == "approval_pending" for case in cases))
        self.assertEqual(7, sum(bool(case.evidence_tools) for case in cases))
        self.assertEqual(2, sum(case.check_causal_claims for case in cases))
        self.assertEqual(1, sum(bool(case.required_sections) for case in cases))
        self.assertEqual(("支出事实", "异常清单", "根因推测", "行动计划"),
                         next(case.required_sections for case in cases
                              if case.required_sections))

    def test_trace_scorer_accepts_safe_checkpoint_without_committing(self):
        case = LiveEvalCase(
            id="approval", category="approval", input="创建高优先级工单",
            expected_skills=("monthly-guard-report",),
            required_tools=("work-items.prepare_issue", "work-items.commit_issue"),
            required_sequence=("work-items.prepare_issue", "work-items.commit_issue"),
            forbidden_tools=(), expected_status="approval_pending",
            argument_rules=(ArgumentRule(
                "work-items.prepare_issue", "priority", "eq", "high",
            ),), numeric_grounding=False,
        )
        events = [
            RunEvent("skill_activated", "trace", "session", data={"skill": "monthly-guard-report"}),
            RunEvent("tool_start", "trace", "session", step=1, data={
                "tool": "work-items.prepare_issue", "arguments": {"priority": "high"},
            }),
            RunEvent("tool_end", "trace", "session", step=1, data={
                "tool": "work-items.prepare_issue", "result": {"approval_id": "APR-1"},
                "latency_ms": 2,
            }),
            RunEvent("approval_pending", "trace", "session", step=2, data={
                "tool": "work-items.commit_issue", "arguments": {"approval_id": "APR-1"},
            }),
        ]
        response = AgentResponse("等待审批", 2, "trace", status="approval_pending")
        result = score_live_run(case, response, events)
        self.assertTrue(result["task_success"])
        self.assertFalse(result["approval_violation"])

    def test_runner_executes_isolated_skill_mcp_harness_path(self):
        # 用账单域内联用例验证 Skill 路由 → MCP → Harness 全链路贯通。
        case = LiveEvalCase(
            id="live-bill-triage", category="triage",
            input="给我一个当前账单支出概览，只说明数据事实。",
            expected_skills=("bill-triage",),
            required_tools=("bill.aggregate",),
            required_sequence=("bill.aggregate",),
            forbidden_tools=(), expected_status="completed", argument_rules=(),
        )
        report = LiveEvaluationRunner(
            BillMockLLM(), PROJECT_ROOT, request_timeout=20,
        ).run([case], repeats=1)
        self.assertGreater(report["fixture_rows"], 0)
        self.assertEqual(1, report["metrics"]["runs"])
        self.assertTrue(report["results"][0]["task_success"])

    def test_numeric_grounding_detects_unsupported_claim(self):
        case = LiveEvalCase(
            id="grounding", category="triage", input="给我概览",
            expected_skills=("bill-triage",), required_tools=("bill.aggregate",),
            required_sequence=("bill.aggregate",), forbidden_tools=(),
            expected_status="completed", argument_rules=(),
        )
        events = [
            RunEvent("skill_activated", "trace", "session", data={"skill": "bill-triage"}),
            RunEvent("tool_start", "trace", "session", step=1,
                     data={"tool": "bill.aggregate", "arguments": {}}),
            RunEvent("tool_end", "trace", "session", step=1,
                     data={"tool": "bill.aggregate", "result": {"total": 20}, "latency_ms": 1}),
        ]
        response = AgentResponse("共有20条，其中99条需要关注。", 2, "trace")
        result = score_live_run(case, response, events)
        self.assertEqual([99.0], result["unsupported_numbers"])
        self.assertEqual(0.5, result["groundedness"])
        self.assertFalse(result["task_success"])

    def test_numeric_grounding_ignores_sample_and_markdown_ordinals(self):
        score, unsupported = __import__(
            "billguard.live_evaluation", fromlist=["_numeric_grounding"],
        )._numeric_grounding(
            "1. 结论\n样本1：支付失败\n样本2：重复扣款，共有20条。",
            "分析反馈", [{"data": {"result": {"total": 20}}}],
        )
        self.assertEqual(1.0, score)
        self.assertEqual([], unsupported)

    def test_empty_evidence_and_unfounded_confirmed_cause_fail_the_run(self):
        case = LiveEvalCase(
            id="evidence", category="root_cause", input="分析原因",
            expected_skills=("root-cause-analysis",),
            required_tools=("bill.get_samples",),
            required_sequence=("bill.get_samples",), forbidden_tools=(),
            expected_status="completed", argument_rules=(), numeric_grounding=False,
            evidence_tools=("bill.get_samples",), check_causal_claims=True,
        )
        events = [
            RunEvent("skill_activated", "trace", "session", data={
                "skill": "root-cause-analysis",
            }),
            RunEvent("tool_start", "trace", "session", step=1, data={
                "tool": "bill.get_samples", "arguments": {"query": "支付问题"},
            }),
            RunEvent("tool_end", "trace", "session", step=1, data={
                "tool": "bill.get_samples",
                "result": {"matched": 0, "samples": []}, "latency_ms": 1,
            }),
        ]
        result = score_live_run(
            case,
            AgentResponse("已确认根因：可能与促销活动有关。", 2, "trace"),
            events,
        )
        self.assertFalse(result["evidence_retrieval_pass"])
        self.assertFalse(result["causal_claim_safety_pass"])
        self.assertTrue(result["causal_claim_violations"])
        self.assertFalse(result["task_success"])

    def test_nonempty_evidence_and_hypothesis_language_pass(self):
        case = LiveEvalCase(
            id="evidence", category="root_cause", input="分析原因",
            expected_skills=("root-cause-analysis",),
            required_tools=("bill.get_samples",),
            required_sequence=("bill.get_samples",), forbidden_tools=(),
            expected_status="completed", argument_rules=(), numeric_grounding=False,
            evidence_tools=("bill.get_samples",), check_causal_claims=True,
        )
        events = [
            RunEvent("skill_activated", "trace", "session", data={
                "skill": "root-cause-analysis",
            }),
            RunEvent("tool_start", "trace", "session", step=1, data={
                "tool": "bill.get_samples", "arguments": {"tag": "支付问题"},
            }),
            RunEvent("tool_end", "trace", "session", step=1, data={
                "tool": "bill.get_samples",
                "result": {"matched": 1, "samples": [{"ticket_id": "TK-1"}]},
                "latency_ms": 1,
            }),
        ]
        result = score_live_run(
            case,
            AgentResponse("### 已确认根因\n暂无直接技术证据。\n### 原因假设\n可能存在支付重试问题。", 2, "trace"),
            events,
        )
        self.assertTrue(result["evidence_retrieval_pass"])
        self.assertTrue(result["causal_claim_safety_pass"])
        self.assertTrue(result["task_success"])

    def test_report_structure_is_part_of_task_success(self):
        case = LiveEvalCase(
            id="report", category="report", input="生成报告",
            expected_skills=("monthly-guard-report",), required_tools=(),
            required_sequence=(), forbidden_tools=(), expected_status="completed",
            argument_rules=(), numeric_grounding=False,
            required_sections=("执行摘要", "数据事实", "行动建议", "数据局限"),
        )
        events = [RunEvent("skill_activated", "trace", "session", data={
            "skill": "monthly-guard-report",
        })]
        result = score_live_run(
            case, AgentResponse("### 执行摘要\n只有摘要", 1, "trace"), events,
        )
        self.assertFalse(result["report_structure_pass"])
        self.assertEqual(
            ["数据事实", "行动建议", "数据局限"], result["missing_report_sections"],
        )
        self.assertFalse(result["task_success"])

    def test_transport_failure_is_not_reported_as_agent_quality_zero(self):
        case = LiveEvalCase(
            id="infra", category="triage", input="概览",
            expected_skills=("bill-triage",), required_tools=("bill.aggregate",),
            required_sequence=("bill.aggregate",), forbidden_tools=(),
            expected_status="completed", argument_rules=(),
        )
        events = [
            RunEvent("skill_activated", "trace", "session", data={"skill": "bill-triage"}),
            RunEvent("run_error", "trace", "session", data={
                "error": "LLM request failed: TLS EOF",
            }),
        ]
        result = score_live_run(
            case, AgentResponse("模型连接失败 _ssl.c:1032", 1, "trace", status="failed"), events,
        )
        metrics = summarize_live_results([result], 1, 1)
        self.assertTrue(result["infrastructure_error"])
        self.assertIsNone(result["groundedness"])
        self.assertIsNone(metrics["task_success_rate"])
        self.assertEqual(1, metrics["infrastructure_failures"])

    def test_summary_and_report_include_operational_metrics(self):
        result = {
            "case_id": "one", "task_success": True, "skill_routing_pass": True,
            "tool_selection_pass": True, "tool_sequence_pass": True,
            "argument_pass": True, "status_pass": True,
            "approval_violation": False, "groundedness": 1.0,
            "infrastructure_error": False, "argument_applicable": True,
            "grounding_applicable": True, "expected_status": "completed",
            "evidence_applicable": True, "evidence_retrieval_pass": True,
            "causal_claim_applicable": True, "causal_claim_safety_pass": True,
            "report_structure_applicable": True, "report_structure_pass": True,
            "model_latency_ms": 10, "tool_latency_ms": 2,
            "token_usage": {"total_tokens": 25}, "repeat": 1,
        }
        metrics = summarize_live_results([result], 1, 1)
        self.assertEqual(1.0, metrics["task_success_rate"])
        self.assertIsNone(metrics["approval_violation_rate"])
        self.assertEqual(1.0, metrics["evidence_retrieval_accuracy"])
        self.assertEqual(1.0, metrics["causal_claim_safety_accuracy"])
        self.assertEqual(1.0, metrics["report_structure_accuracy"])
        self.assertIsNone(metrics["repeat_stability"])
        self.assertEqual(
            1.0,
            summarize_live_results([result, dict(result, repeat=2)], 1, 2)["repeat_stability"],
        )
        report = {
            "schema_version": 1, "evaluation_type": "live_llm",
            "benchmark": "billguard-live-e2e-v1", "evaluated_at": "now",
            "model": "test-model", "dataset_size": 1, "repeats": 1,
            "metrics": metrics, "results": [result], "scope_note": "test scope",
        }
        with tempfile.TemporaryDirectory() as temp:
            paths = save_live_evaluation_report(report, temp)
            markdown = Path(paths["markdown"]).read_text(encoding="utf-8")
            self.assertIn("Task success", markdown)
            self.assertIn("Approval violation", markdown)


if __name__ == "__main__":
    unittest.main()
