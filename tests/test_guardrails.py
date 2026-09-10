from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from minimal_agent.guardrails import (
    MAX_MODEL_OUTPUT_CHARS,
    MAX_USER_INPUT_CHARS,
    GuardrailError,
    redact_pii,
    unsupported_numeric_claims,
)
from minimal_agent.harness import AgentSpec, HarnessEngine
from minimal_agent.session import SessionStore
from minimal_agent.tools import Tool, ToolRegistry


class QueueLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)

    def complete(self, messages, tools):
        return self.outputs.pop(0)


class GuardrailTests(unittest.TestCase):
    def _engine(self, root: Path, outputs, registry=None, tools=(), max_steps=3):
        events = []
        engine = HarnessEngine(
            AgentSpec("guard-test", "Use evidence.", tools, max_steps),
            QueueLLM(outputs), registry or ToolRegistry(),
            SessionStore(root / "sessions"), hooks=[events.append],
        )
        return engine, events

    def test_input_limit_rejects_before_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            engine, events = self._engine(Path(temp), [])
            with self.assertRaises(GuardrailError):
                engine.run("s", "A" * (MAX_USER_INPUT_CHARS + 1))
            self.assertEqual([], events)

    def test_model_output_limit_fails_safely_and_is_traced(self):
        with tempfile.TemporaryDirectory() as temp:
            engine, events = self._engine(
                Path(temp), ["B" * (MAX_MODEL_OUTPUT_CHARS + 1)], max_steps=1,
            )
            response = engine.run("s", "test")
            self.assertEqual("failed", response.status)
            self.assertTrue(any(e.event_type == "model_output_blocked" for e in events))

    def test_pii_is_redacted_before_session_trace_and_response(self):
        raw = json.dumps({
            "final": "联系 13800138000 或 alice@example.com，订单 ORD-ABC12345"
        }, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine, events = self._engine(root, [raw], max_steps=1)
            response = engine.run("s", "show contact")
            self.assertEqual("completed", response.status)
            self.assertNotIn("13800138000", response.answer)
            self.assertNotIn("alice@example.com", response.answer)
            self.assertIn("[手机号]", response.answer)
            self.assertTrue(any(e.event_type == "output_redacted" for e in events))
            saved = SessionStore(root / "sessions").load("s")
            self.assertNotIn("alice@example.com", saved.messages[-1].content)

    def test_unsupported_number_is_blocked_then_model_can_rewrite(self):
        outputs = [
            json.dumps({"final": "当前共有 999 条异常反馈。"}, ensure_ascii=False),
            json.dumps({"final": "当前没有工具证据，无法给出异常数量。"}, ensure_ascii=False),
        ]
        with tempfile.TemporaryDirectory() as temp:
            engine, events = self._engine(Path(temp), outputs, max_steps=2)
            response = engine.run("s", "直接告诉我异常数量")
            self.assertEqual("completed", response.status)
            self.assertNotIn("999", response.answer)
            self.assertTrue(any(e.event_type == "grounding_blocked" for e in events))

    def test_tool_evidence_supports_numeric_final(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "stats", "Read stats",
            {"type": "object", "properties": {}, "required": [],
             "additionalProperties": False},
            lambda: {"total": 20},
        ))
        outputs = [
            json.dumps({"tool_call": {"name": "stats", "arguments": {}}}),
            json.dumps({"final": "当前共有 20 条反馈。"}, ensure_ascii=False),
        ]
        with tempfile.TemporaryDirectory() as temp:
            engine, events = self._engine(
                Path(temp), outputs, registry, ("stats",), max_steps=2,
            )
            response = engine.run("s", "查询反馈总量")
            self.assertEqual("completed", response.status)
            self.assertIn("20", response.answer)
            self.assertFalse(any(e.event_type == "grounding_blocked" for e in events))

    def test_guardrail_helpers(self):
        masked, counts = redact_pii("13800138000 a@example.com")
        self.assertEqual("[手机号] [邮箱]", masked)
        self.assertEqual({"phone": 1, "email": 1}, counts)
        self.assertEqual([999.0], unsupported_numeric_claims("共 999 条", [{"total": 20}]))


if __name__ == "__main__":
    unittest.main()
