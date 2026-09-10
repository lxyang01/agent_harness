from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from billguard.agents import create_feedback_agent
from billguard.feedback import FeedbackService
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
            "feedback-triage",
            "anomaly-investigation",
            "root-cause-analysis",
            "executive-report",
        }, names)

    def test_implicit_routing_can_activate_multiple_relevant_skills(self):
        runtime = SkillRuntime(PROJECT_SKILLS)
        activations = runtime.activate("分析支付问题异常增长的原因")
        names = {activation.name for activation in activations}
        self.assertEqual({"anomaly-investigation", "root-cause-analysis"}, names)
        self.assertTrue(all(activation.version for activation in activations))
        self.assertTrue(all(activation.reason.startswith("trigger:") for activation in activations))

    def test_compound_anomaly_and_work_item_intent_activates_completion_contract(self):
        runtime = SkillRuntime(PROJECT_SKILLS)
        activations = runtime.activate("分析最近异常增长的问题，并创建一个高优先级跟进工单")
        self.assertEqual(
            {"anomaly-investigation", "executive-report"},
            {activation.name for activation in activations},
        )
        executive = next(item for item in activations if item.name == "executive-report")
        self.assertEqual(
            ("work-items.prepare_issue", "work-items.commit_issue"),
            executive.required_tools,
        )

    def test_completion_contract_declares_alias_groups_and_report_priority(self):
        runtime = SkillRuntime(PROJECT_SKILLS)
        root = next(item for item in runtime.activate(
            "支付失败为什么增多，请读取样本",
        ) if item.name == "root-cause-analysis")
        self.assertIn(
            ("feedback_samples", "feedback.get_samples"),
            root.required_tool_groups,
        )
        self.assertEqual(
            (
                ("feedback_search", "feedback.query"),
                ("feedback_samples", "feedback.get_samples"),
            ),
            root.required_tool_plan,
        )
        report = runtime.activate("生成最近7天客户反馈周报，包含异常和样本")
        self.assertEqual("executive-report", report[0].name)
        self.assertEqual(
            {"executive-report", "anomaly-investigation"},
            {item.name for item in report},
        )
        self.assertEqual(3, len(report[0].required_tool_groups))

    def test_explicit_routing_has_priority_and_unknown_skill_is_rejected(self):
        runtime = SkillRuntime(PROJECT_SKILLS, max_active=1)
        activation = runtime.activate("请使用 $executive-report 分析异常")[0]
        self.assertEqual("executive-report", activation.name)
        self.assertEqual("explicit:$executive-report", activation.reason)
        with self.assertRaises(SkillError):
            runtime.activate("请使用 $not-installed-skill")

    def test_default_route_and_tool_policy(self):
        runtime = SkillRuntime(PROJECT_SKILLS)
        activations = runtime.activate("你好，请帮我看看")
        self.assertEqual(["feedback-triage"], [item.name for item in activations])
        tools = runtime.allowed_tools(activations, (
            "feedback_overview", "feedback_compare", "feedback_anomalies",
            "feedback_search", "feedback_samples",
        ))
        self.assertEqual(("feedback_overview", "feedback_search", "feedback_samples"), tools)

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
            agent = create_feedback_agent(
                llm, "skill-session", root / "sessions",
                FeedbackService(root / "feedback"), skill_dir=PROJECT_SKILLS,
            )
            events = []
            agent.hooks.append(events.append)
            response = agent.run("skill-session", "调查最近7天异常增长")

            self.assertEqual(("anomaly-investigation",), response.active_skills)
            exposed = {item["name"] for item in llm.tools}
            self.assertEqual({
                "feedback_compare", "feedback_anomalies", "feedback_search", "feedback_samples",
            }, exposed)
            system_text = "\n".join(item["content"] for item in llm.messages if item["role"] == "system")
            self.assertIn("客户反馈异常调查", system_text)
            activation_events = [event for event in events if event.event_type == "skill_activated"]
            self.assertEqual("anomaly-investigation", activation_events[0].data["skill"])
            self.assertTrue(activation_events[0].data["version"])

    def test_harness_resolves_completion_alias_and_blocks_early_final(self):
        class ContractLLM:
            def __init__(self):
                self.outputs = iter([
                    json.dumps({"thought": "too early", "final": "稍后读取样本"}),
                    json.dumps({"thought": "wrong order", "tool_call": {
                        "name": "feedback_samples", "arguments": {"query": "支付失败", "limit": 5},
                    }}),
                    json.dumps({"thought": "search evidence", "tool_call": {
                        "name": "feedback_search", "arguments": {"query": "支付失败", "limit": 10},
                    }}),
                    json.dumps({"thought": "still incomplete", "final": "已经检索和读取"}),
                    json.dumps({"thought": "complete ordered contract", "tool_call": {
                        "name": "feedback_samples", "arguments": {"query": "支付失败", "limit": 5},
                    }}),
                    json.dumps({"thought": "done", "final": "没有匹配样本"}),
                ])

            def complete(self, messages, tools):
                return next(self.outputs)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = create_feedback_agent(
                ContractLLM(), "contract-session", root / "sessions",
                FeedbackService(root / "feedback"), skill_dir=PROJECT_SKILLS,
            )
            events = []
            agent.hooks.append(events.append)
            response = agent.run("contract-session", "支付失败为什么增多？读取5条样本")
            self.assertEqual("completed", response.status)
            blocked = [event for event in events if event.event_type == "completion_blocked"]
            self.assertEqual(
                ["feedback_search", "feedback_samples"],
                blocked[0].data["missing_tools"],
            )
            self.assertEqual(["feedback_samples"], blocked[1].data["missing_tools"])
            self.assertEqual(["feedback_samples", "feedback_search", "feedback_samples"], [
                event.data.get("tool") for event in events if event.event_type == "tool_end"
            ])


if __name__ == "__main__":
    unittest.main()
