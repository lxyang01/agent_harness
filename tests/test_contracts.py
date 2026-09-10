from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from minimal_agent.agents import create_feedback_agent
from minimal_agent.feedback import FeedbackService
from minimal_agent.harness.contracts import compile_request_contract


PROJECT_SKILLS = Path(__file__).resolve().parents[1] / "skills"


class ContractCompilerTests(unittest.TestCase):
    def test_sample_limit_is_compiled_for_query_and_sample_tools(self):
        contract = compile_request_contract(
            "登录问题的根因可能是什么？检索相关反馈并读取最多8条样本。",
            ["root-cause-analysis"],
        )
        sample_schema = {"properties": {"tag": {"type": "string"}, "limit": {"type": "integer"}}}
        query_schema = {"properties": {"query_text": {"type": "string"}, "limit": {"type": "integer"}}}
        self.assertTrue(contract.tool_violations(
            "feedback.get_samples", {"tag": "登录问题"}, sample_schema,
        ))
        self.assertEqual([], contract.tool_violations(
            "feedback.get_samples", {"tag": "登录问题", "limit": 8}, sample_schema,
        ))
        self.assertEqual([], contract.tool_violations(
            "feedback.query", {"query_text": "登录问题", "limit": 8}, query_schema,
        ))

    def test_report_contract_accepts_json_and_rejects_missing_sections(self):
        contract = compile_request_contract(
            "生成最近7天客户反馈周报，包含异常问题和代表性样本。",
            ["executive-report", "anomaly-investigation"],
        )
        self.assertEqual(
            ["行动建议", "数据局限"],
            contract.missing_sections(json.dumps({
                "summary": "摘要", "data_facts": {}, "anomalies": [], "samples": [],
            }, ensure_ascii=False)),
        )
        self.assertEqual([], contract.missing_sections(json.dumps({
            "summary": "摘要", "data_facts": {}, "anomalies": [], "samples": [],
            "recommendations": [], "limitations": [],
        }, ensure_ascii=False)))

    def test_harness_blocks_missing_limit_before_tool_execution(self):
        class LimitLLM:
            def __init__(self):
                self.outputs = iter([
                    json.dumps({"thought": "missing bound", "tool_call": {
                        "name": "feedback_samples", "arguments": {"tag": "登录问题"},
                    }}),
                    json.dumps({"thought": "fixed", "tool_call": {
                        "name": "feedback_samples", "arguments": {
                            "tag": "登录问题", "limit": 8,
                        },
                    }}),
                    json.dumps({"thought": "done", "final": "已按最多8条读取样本。"}),
                ])

            def complete(self, messages, tools):
                return next(self.outputs)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = create_feedback_agent(
                LimitLLM(), "limit-session", root / "sessions",
                FeedbackService(root / "feedback"), skill_dir=PROJECT_SKILLS,
            )
            events = []
            agent.hooks.append(events.append)
            response = agent.run("limit-session", "读取最多8条登录问题样本")
            self.assertEqual("completed", response.status)
            blocked = [event for event in events if event.event_type == "argument_blocked"]
            self.assertEqual(1, len(blocked))
            starts = [event for event in events if event.event_type == "tool_start"]
            self.assertEqual(1, len(starts))
            self.assertEqual(8, starts[0].data["arguments"]["limit"])

    def test_harness_blocks_incomplete_report_final(self):
        class ReportLLM:
            def __init__(self):
                self.outputs = iter([
                    json.dumps({"thought": "draft", "final": "### 执行摘要\n只有摘要"}),
                    json.dumps({"thought": "complete", "final": (
                        "### 执行摘要\n摘要\n### 数据事实\n暂无数据\n"
                        "### 行动建议\n补充调研\n### 数据局限\n当前证据不足"
                    )}),
                ])

            def complete(self, messages, tools):
                return next(self.outputs)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = create_feedback_agent(
                ReportLLM(), "report-session", root / "sessions",
                FeedbackService(root / "feedback"), skill_dir=PROJECT_SKILLS,
            )
            events = []
            agent.hooks.append(events.append)
            response = agent.run(
                "report-session", "生成管理层报告：概述主要问题、数据限制和可执行建议。",
            )
            self.assertEqual("completed", response.status)
            blocked = [event for event in events if event.event_type == "output_contract_blocked"]
            self.assertEqual(1, len(blocked))
            self.assertEqual(
                ["数据事实", "行动建议", "数据局限"],
                blocked[0].data["missing_sections"],
            )


if __name__ == "__main__":
    unittest.main()
