from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from billguard.agents import create_bill_agent
from billguard.bills import BillService
from billguard.harness.context import ContextBuilder
from billguard.harness.spec import AgentSpec
from billguard.session import SessionStore
from billguard.tools import Tool, ToolRegistry
from billguard.types import Message
from tests.llm_doubles import ScriptedLLM


class _HugeTool:
    """返回体 > 3000 字符,关键数字 9999 藏在尾部(截断必然丢失)。"""

    def __call__(self, **kwargs) -> dict:
        return {"padding": "x" * 3000, "late_number": 9999}


def huge_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(
        "bill_overview", "huge", {"type": "object", "properties": {},
                                  "required": [], "additionalProperties": False},
        _HugeTool()))
    return registry


class ToolResultTruncationTests(unittest.TestCase):
    def _agent(self, temp: str):
        from billguard.harness.engine import HarnessEngine
        spec = AgentSpec(name="t", instructions="测试", tool_names=("bill_overview",))
        return HarnessEngine(
            spec, ScriptedLLM([
                {"thought": "查", "tool_call": {"name": "bill_overview", "arguments": {}}},
                {"thought": "done", "final": "尾部关键数字是 9999。"},
            ]), huge_registry(), SessionStore(Path(temp) / "sessions"))

    def test_truncated_in_session_but_grounding_uses_full(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = self._agent(temp)
            response = agent.run("s1", "查一下")
            # 门禁用完整结果:尾部的 9999 必须被认可,而非误拦
            self.assertEqual("completed", response.status, response.answer)
            persisted = SessionStore(Path(temp) / "sessions").load("s1")
            tool_messages = [m for m in persisted.messages if m.role == "tool"]
            self.assertTrue(tool_messages)
            content = tool_messages[0].content
            self.assertLessEqual(len(content), 1800)  # 截断生效(限值+标注)
            self.assertIn("已截断", content)
            self.assertNotIn("x" * 100, content.replace("xxx", ""))  # 大垫片不进历史
            self.assertNotIn("9999", content)  # 完整值只在 Trace/门禁,不重复进历史

    def test_small_result_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            from billguard.harness.engine import HarnessEngine
            spec = AgentSpec(name="t", instructions="t", tool_names=("bill_overview",))
            small = ToolRegistry()
            small.register(Tool("bill_overview", "s",
                                {"type": "object", "properties": {}, "required": [],
                                 "additionalProperties": False},
                                lambda **kw: {"count": 3}))
            agent = HarnessEngine(spec, ScriptedLLM([
                {"thought": "查", "tool_call": {"name": "bill_overview", "arguments": {}}},
                {"thought": "done", "final": "共 3 条。"},
            ]), small, SessionStore(Path(temp) / "sessions"))
            response = agent.run("s1", "查")
            self.assertEqual("completed", response.status)
            persisted = SessionStore(Path(temp) / "sessions").load("s1")
            tool_content = [m for m in persisted.messages if m.role == "tool"][0].content
            self.assertNotIn("已截断", tool_content)


class ContextBudgetTests(unittest.TestCase):
    def test_budget_drops_oldest_keeps_latest_user(self):
        spec = AgentSpec(name="t", instructions="系统指令", tool_names=(),
                         max_context_chars=600)
        messages = [Message("user", f"消息{i}:" + "内" * 120) for i in range(1, 9)]
        messages.append(Message("user", "最新问题"))
        context = ContextBuilder().build(spec, "", messages)
        rendered = [m.content for m in context]
        joined = "".join(rendered)
        self.assertLessEqual(len(joined), 900)  # 预算内(系统块+最新消息为下限)
        self.assertIn("最新问题", joined)
        self.assertNotIn("消息1:", joined)  # 最旧的先丢
        self.assertNotIn("消息5:", joined)

    def test_no_budget_keeps_everything(self):
        spec = AgentSpec(name="t", instructions="t", tool_names=())
        messages = [Message("user", f"m{i}") for i in range(20)]
        context = ContextBuilder().build(spec, "", messages)
        self.assertEqual(21, len(context))  # system + 20


if __name__ == "__main__":
    unittest.main()
