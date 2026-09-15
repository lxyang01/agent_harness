from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from billguard.harness import AgentSpec, HarnessEngine
from billguard.harness.contracts import compile_request_contract
from billguard.session import SessionStore
from billguard.skills import SkillRuntime
from billguard.tools import Tool, ToolRegistry


PROJECT_SKILLS = Path(__file__).resolve().parents[1] / "skills"


class ContractCompilerTests(unittest.TestCase):
    def test_sample_limit_is_compiled_for_query_and_sample_tools(self):
        # 工具名有意用 legacy feedback.* 前缀:覆盖 contracts._tool_role 的
        # legacy 别名组(bill_* 与 bill.* 由其余用例覆盖)。
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
        # 契约从路由 output_contract 读取;构造带契约的激活对象驱动
        from types import SimpleNamespace
        activation = SimpleNamespace(output_contract={
            "gate_terms": ["周报", "报告"],
            "sections": ["执行摘要", "数据事实", "异常问题", "代表性样本",
                          "行动建议", "数据局限"],
        })
        contract = compile_request_contract(
            "生成最近7天客户反馈周报，包含异常问题和代表性样本。",
            [activation],
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
                        "name": "bill.get_samples", "arguments": {"merchant": "爱奇艺"},
                    }}),
                    json.dumps({"thought": "fixed", "tool_call": {
                        "name": "bill.get_samples", "arguments": {
                            "merchant": "爱奇艺", "limit": 8,
                        },
                    }}),
                    json.dumps({"thought": "done", "final": "已按最多8条读取脱敏样本。"}),
                ])

            def complete(self, messages, tools):
                return next(self.outputs)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = ToolRegistry()
            registry.register(Tool(
                "bill.get_samples", "Read masked samples",
                {"type": "object",
                 "properties": {"merchant": {"type": "string"}, "limit": {"type": "integer"}},
                 "required": [], "additionalProperties": False},
                lambda **kwargs: {"matched": 0, "samples": [], "pii_masked": True},
            ))
            agent = HarnessEngine(
                AgentSpec("limit-test", "Use the sample tool.", ("bill.get_samples",), 4),
                LimitLLM(), registry, SessionStore(root / "sessions"),
                skills=SkillRuntime(PROJECT_SKILLS),
            )
            events = []
            agent.hooks.append(events.append)
            response = agent.run("limit-session", "读取最多8条爱奇艺账单样本")
            self.assertEqual("completed", response.status)
            blocked = [event for event in events if event.event_type == "argument_blocked"]
            self.assertEqual(1, len(blocked))
            starts = [event for event in events if event.event_type == "tool_start"]
            self.assertEqual(1, len(starts))
            self.assertEqual(8, starts[0].data["arguments"]["limit"])

    def test_harness_blocks_incomplete_report_final(self):
        # contracts.compile_request_contract 的报告章节契约按 legacy 技能名
        # "executive-report" 编译;用夹具技能验证 Harness 的拦截机制仍然生效。
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
            skill_root = root / "skills"
            skill_dir = skill_root / "executive-report"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: executive-report\ndescription: Report contract fixture.\n---\n"
                "Write the full report.", encoding="utf-8",
            )
            (skill_root / "routes.json").write_text(json.dumps({
                "default_skill": "executive-report",
                "routes": [{"skill": "executive-report", "triggers": ["报告"],
                            "allowed_tools": ["safe.read"],
                            "output_contract": {
                                "sections": ["执行摘要", "数据事实", "行动建议", "数据局限"],
                                "gate_terms": ["报告"]}}],
            }, ensure_ascii=False), encoding="utf-8")
            registry = ToolRegistry()
            registry.register(Tool(
                "safe.read", "Read", {"type": "object", "properties": {},
                                      "required": [], "additionalProperties": False},
                lambda: {},
            ))
            agent = HarnessEngine(
                AgentSpec("report-test", "Use the tool.", ("safe.read",), 3),
                ReportLLM(), registry, SessionStore(root / "sessions"),
                skills=SkillRuntime(skill_root),
            )
            events = []
            agent.hooks.append(events.append)
            response = agent.run("report-session", "生成管理层报告：概述主要问题、数据限制和可执行建议。")
            self.assertEqual("completed", response.status)
            blocked = [event for event in events if event.event_type == "output_contract_blocked"]
            self.assertEqual(1, len(blocked))
            self.assertEqual(
                ["数据事实", "行动建议", "数据局限"],
                blocked[0].data["missing_sections"],
            )

    def test_monthly_guard_report_contract_blocks_on_production_skills(self):
        # 生产路径:真实 skills 目录路由 monthly-guard-report,报告章节契约
        # 按账单域四章(支出事实/异常清单/根因推测/行动计划)拦截缺失章节,
        # 补齐后 final 才被放行。
        class GuardReportLLM:
            def __init__(self):
                self.outputs = iter([
                    json.dumps({"thought": "先取数", "tool_call": {
                        "name": "bill.aggregate", "arguments": {},
                    }}),
                    json.dumps({"thought": "draft", "final": (
                        "### 支出事实\n总支出与结构已核对。\n### 异常清单\n未发现待核查异常。"
                    )}),
                    json.dumps({"thought": "complete", "final": (
                        "### 支出事实\n总支出与结构已核对。\n### 异常清单\n未发现待核查异常。\n"
                        "### 根因推测\n暂无已确认根因，仅保留待验证假设。\n"
                        "### 行动计划\n下月复查订阅扣费并核对预期金额。"
                    )}),
                ])

            def complete(self, messages, tools):
                return next(self.outputs)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = ToolRegistry()
            registry.register(Tool(
                "bill.aggregate", "Aggregate bills",
                {"type": "object",
                 "properties": {"category": {"type": "string"}},
                 "required": [], "additionalProperties": False},
                lambda **kwargs: {"total_amount": 0, "count": 0, "by_category": [],
                                  "top_merchants": [], "pending": 0},
            ))
            agent = HarnessEngine(
                AgentSpec("guard-report-test", "Use the bill tools.",
                          ("bill.aggregate",), 5),
                GuardReportLLM(), registry, SessionStore(root / "sessions"),
                skills=SkillRuntime(PROJECT_SKILLS),
            )
            events = []
            agent.hooks.append(events.append)
            response = agent.run("guard-report-session", "生成月度守卫报告")
            activated = [event.data.get("skill") for event in events
                         if event.event_type == "skill_activated"]
            self.assertEqual(["monthly-guard-report"], activated)
            blocked = [event for event in events if event.event_type == "output_contract_blocked"]
            self.assertEqual(1, len(blocked))
            self.assertEqual(["根因推测", "行动计划"],
                             blocked[0].data["missing_sections"])
            self.assertEqual("completed", response.status)
            self.assertIn("行动计划", response.answer)


if __name__ == "__main__":
    unittest.main()
