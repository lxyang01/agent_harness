from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from billguard.skills import SkillError, SkillRuntime

PROJECT_SKILLS = Path(__file__).resolve().parents[1] / "skills"


def _skill(root: Path, name: str, body: str = "工作流:照做。") -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 测试技能。\n---\n\n{body}",
        encoding="utf-8")


def _routes(root: Path, payload: dict) -> None:
    (root / "routes.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class CoOccurrenceTriggerTests(unittest.TestCase):
    def _runtime(self) -> SkillRuntime:
        return SkillRuntime(PROJECT_SKILLS)

    def test_natural_word_orders_route_to_guard_report(self):
        runtime = self._runtime()
        cases = {
            "帮我取消腾讯视频订阅": "取消+订阅",
            "取消掉这个腾讯视频的订阅": "取消+订阅",
            "把腾讯视频的订阅取消掉": "取消+订阅",
            "取消腾讯会员的自动续费": "取消+续费",
            "取消腾讯视频的自动续费": "取消+续费",
            "把腾讯视频的自动续费关掉": "自动续费",
            "腾讯视频到期后不续费了": "不续费",
            "退订腾讯视频": "退订",
            "把这个服务取消掉": "取消+服务",
            "会员退掉吧": "退掉",
            "停止自动扣款": "自动扣款",
            "把钱退给我": "退钱",
        }
        for text, note in cases.items():
            with self.subTest(f"{note} | {text}"):
                activated = runtime.activate(text)
                self.assertIn("monthly-guard-report", [a.name for a in activated])

    def test_bare_cancel_without_object_falls_to_default(self):
        runtime = self._runtime()
        activated = runtime.activate("把这个取消掉")
        self.assertEqual(["bill-triage"], [a.name for a in activated])

    def test_isolated_cancel_word_no_longer_overtriggers(self):
        runtime = self._runtime()
        activated = runtime.activate("取消我刚才说的第一句话")
        self.assertNotIn("monthly-guard-report", [a.name for a in activated])

    def test_other_skill_trigger_gaps_covered(self):
        runtime = self._runtime()
        self.assertIn("bill-triage", [a.name for a in runtime.activate("最近开销好大")])
        self.assertIn("root-cause-analysis",
                      [a.name for a in runtime.activate("这笔账单咋回事")])

    def test_string_trigger_still_substring(self):
        runtime = self._runtime()
        activated = runtime.activate("申请退款")
        self.assertIn("monthly-guard-report", [a.name for a in activated])


class OutputContractDataTests(unittest.TestCase):
    def test_guard_report_activation_carries_output_contract(self):
        runtime = SkillRuntime(PROJECT_SKILLS)
        activation = runtime.activate("生成本月守卫报告")[0]
        contract = activation.output_contract
        self.assertEqual(("支出事实", "异常清单", "根因推测", "行动计划"),
                         tuple(contract["sections"]))
        for term in ("守卫报告", "月报", "周报", "汇报"):
            self.assertIn(term, contract["gate_terms"])

    def test_wide_routing_words_do_not_gate_sections(self):
        # 宽路由词(报告/总结)路由到 Skill,但不应武装四章节门禁
        runtime = SkillRuntime(PROJECT_SKILLS)
        activation = runtime.activate("总结一下当前支出")[0] if False else None
        activated = runtime.activate("总结一下当前支出")
        guard = next((a for a in activated if a.name == "monthly-guard-report"), None)
        self.assertIsNotNone(guard)
        self.assertFalse(any(term in "总结一下当前支出"
                             for term in guard.output_contract["gate_terms"]))

    def test_routes_json_invalid_contract_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _skill(root, "demo")
            _routes(root, {"default_skill": "demo",
                           "routes": [{"skill": "demo", "triggers": ["x"],
                                       "allowed_tools": ["bill_overview"],
                                       "output_contract": {"sections": []}}]})
            with self.assertRaises(SkillError):
                SkillRuntime(root)


class SkillBodyLimitTests(unittest.TestCase):
    def test_oversized_skill_body_fails_safely_on_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _skill(root, "demo", body="灌水" * 8000)
            _routes(root, {"default_skill": "demo",
                           "routes": [{"skill": "demo", "triggers": ["x"],
                                       "allowed_tools": ["bill_overview"]}]})
            runtime = SkillRuntime(root)
            with self.assertRaises(SkillError):
                runtime.activate("x")


if __name__ == "__main__":
    unittest.main()
