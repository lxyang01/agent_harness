from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from billguard.agents import create_bill_agent
from billguard.bills import BillService
from billguard.skills import SkillError, SkillRuntime


PROJECT_SKILLS = Path(__file__).resolve().parents[1] / "skills"


class CaptureLLM:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.tools: list[dict] = []

    def complete(self, messages, tools):
        self.messages = messages
        self.tools = tools
        return json.dumps({"thought": "skill context captured", "final": "ok"})


class SkillRuntimeTests(unittest.TestCase):
    def test_catalog_discovers_four_standard_skills(self):
        runtime = SkillRuntime(PROJECT_SKILLS)
        names = {skill.name for skill in runtime.catalog()}
        self.assertEqual({
            "bill-triage",
            "anomaly-investigation",
            "root-cause-analysis",
            "monthly-guard-report",
        }, names)

    def test_implicit_routing_can_activate_multiple_relevant_skills(self):
        runtime = SkillRuntime(PROJECT_SKILLS)
        activations = runtime.activate("分析订阅涨价异常的原因")
        names = {activation.name for activation in activations}
        self.assertEqual({"anomaly-investigation", "root-cause-analysis"}, names)
        self.assertTrue(all(activation.version for activation in activations))
        self.assertTrue(all(activation.reason.startswith("trigger:") for activation in activations))

    def test_compound_anomaly_and_work_item_intent_activates_completion_contract(self):
        runtime = SkillRuntime(PROJECT_SKILLS)
        activations = runtime.activate(
            "分析最近的重复扣费异常并生成守卫报告，创建取消订阅工单",
        )
        self.assertEqual(
            {"anomaly-investigation", "monthly-guard-report"},
            {activation.name for activation in activations},
        )
        report = next(item for item in activations if item.name == "monthly-guard-report")
        self.assertEqual(
            (
                ("bill_overview", "bill.aggregate"),
                ("work-items.prepare_issue",),
            ),
            report.required_tool_groups,
        )
        self.assertEqual(
            (("work-items.prepare_issue",),),
            report.required_tool_plan[-1:],
        )

    def test_completion_contract_declares_alias_groups_and_report_priority(self):
        runtime = SkillRuntime(PROJECT_SKILLS)
        anomaly = next(item for item in runtime.activate(
            "排查重复扣费，读取样本",
        ) if item.name == "anomaly-investigation")
        self.assertIn(
            ("bill_samples", "bill.get_samples"),
            anomaly.required_tool_groups,
        )
        self.assertEqual(
            (("bill_samples", "bill.get_samples"),),
            anomaly.required_tool_plan,
        )
        report = runtime.activate("生成本月守卫报告")
        self.assertEqual("monthly-guard-report", report[0].name)
        self.assertEqual(
            (("bill_overview", "bill.aggregate"),),
            report[0].required_tool_groups,
        )

    def test_explicit_routing_has_priority_and_unknown_skill_is_rejected(self):
        runtime = SkillRuntime(PROJECT_SKILLS, max_active=1)
        activation = runtime.activate("请使用 $monthly-guard-report 分析异常")[0]
        self.assertEqual("monthly-guard-report", activation.name)
        self.assertEqual("explicit:$monthly-guard-report", activation.reason)
        with self.assertRaises(SkillError):
            runtime.activate("请使用 $not-installed-skill")

    def test_default_route_and_tool_policy(self):
        runtime = SkillRuntime(PROJECT_SKILLS)
        activations = runtime.activate("你好，请帮我看看")
        self.assertEqual(["bill-triage"], [item.name for item in activations])
        tools = runtime.allowed_tools(activations, (
            "bill_overview", "bill_compare", "bill_anomalies",
            "bill_search", "bill_samples",
        ))
        self.assertEqual(("bill_overview", "bill_search", "bill_samples"), tools)

    def test_skill_body_is_loaded_on_activation_not_cached_at_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill_dir = root / "demo-skill"
            skill_dir.mkdir()
            skill_path = skill_dir / "SKILL.md"
            skill_path.write_text(
                "---\nname: demo-skill\ndescription: Demonstrates progressive loading.\n---\n\nVersion one.",
                encoding="utf-8",
            )
            (root / "routes.json").write_text(json.dumps({
                "default_skill": "demo-skill",
                "routes": [{"skill": "demo-skill", "triggers": ["demo"], "allowed_tools": ["read"]}],
            }), encoding="utf-8")
            runtime = SkillRuntime(root)
            self.assertEqual("Demonstrates progressive loading.", runtime.catalog()[0].description)
            skill_path.write_text(
                "---\nname: demo-skill\ndescription: Demonstrates progressive loading.\n---\n\nVersion two.",
                encoding="utf-8",
            )
            self.assertIn("Version two.", runtime.activate("demo")[0].instructions)

    def test_harness_injects_skill_and_only_exposes_skill_tools(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            llm = CaptureLLM()
            agent = create_bill_agent(
                llm, "skill-session", root / "sessions",
                BillService(root / "billguard"), skill_dir=PROJECT_SKILLS,
            )
            events = []
            agent.hooks.append(events.append)
            response = agent.run("skill-session", "调查最近7天订阅涨价异常")

            self.assertEqual(("anomaly-investigation",), response.active_skills)
            exposed = {item["name"] for item in llm.tools}
            self.assertEqual({
                "bill_compare", "bill_anomalies", "bill_search", "bill_samples",
            }, exposed)
            system_text = "\n".join(item["content"] for item in llm.messages if item["role"] == "system")
            self.assertIn("账单异常调查", system_text)
            activation_events = [event for event in events if event.event_type == "skill_activated"]
            self.assertEqual("anomaly-investigation", activation_events[0].data["skill"])
            self.assertTrue(activation_events[0].data["version"])

    def test_harness_resolves_completion_alias_and_blocks_early_final(self):
        class ContractLLM:
            def __init__(self):
                self.outputs = iter([
                    json.dumps({"thought": "too early", "final": "稍后读取样本"}),
                    json.dumps({"thought": "read samples", "tool_call": {
                        "name": "bill_samples", "arguments": {"query": "视频会员", "limit": 5},
                    }}),
                    json.dumps({"thought": "done", "final": "已读取脱敏样本，未发现重复扣费。"}),
                ])

            def complete(self, messages, tools):
                return next(self.outputs)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = create_bill_agent(
                ContractLLM(), "contract-session", root / "sessions",
                BillService(root / "billguard"), skill_dir=PROJECT_SKILLS,
            )
            events = []
            agent.hooks.append(events.append)
            response = agent.run("contract-session", "订阅为什么涨价？读取5条样本")
            self.assertEqual("completed", response.status)
            blocked = [event for event in events if event.event_type == "completion_blocked"]
            self.assertEqual(
                ["bill_samples"],
                blocked[0].data["missing_tools"],
            )
            self.assertEqual(["bill_samples"], [
                event.data.get("tool") for event in events if event.event_type == "tool_end"
            ])


if __name__ == "__main__":
    unittest.main()
