from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from billguard.agents import create_bill_agent
from billguard.harness.spec import AgentSpec
from billguard.session import SessionStore
from billguard.types import Message, Session
from tests.llm_doubles import ScriptedLLM

DEMO = ("tx_id,paid_at,merchant,category,amount,method,note" + chr(10)
        + "TX-1,2026-08-05 21:00:00,腾讯视频,订阅,25.0,微信,月费" + chr(10))


def long_history(rounds: int) -> list[Message]:
    """构造 rounds 轮对话:每轮 user + 工具调用/结果(噪音)+ assistant final。"""
    messages: list[Message] = []
    for index in range(1, rounds + 1):
        messages.append(Message("user", f"问题{index}:本月支出情况"))
        messages.append(Message("assistant", '{"thought":"查","tool_call":{"name":"bill_overview","arguments":{}}}',
                                tool_call_id=f"c{index}"))
        messages.append(Message("tool", '{"count": 408}', name="bill_overview", tool_call_id=f"c{index}"))
        messages.append(Message("assistant", f"第{index}轮回答:共408笔。"))
    return messages


class CompressorTests(unittest.TestCase):
    def test_over_threshold_compresses_and_drops_tool_noise(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SessionStore(temp)
            session = Session("s1", long_history(15))  # 60 条
            store.save(session)

            from billguard.harness.engine import HarnessEngine
            compressed = HarnessEngine.compress_history(session, keep_recent=12)

            self.assertLessEqual(len(compressed.messages), 12)  # 只留近期
            self.assertIn("问题1:本月支出情况", compressed.summary)  # 早期进摘要
            self.assertIn("第1轮回答", compressed.summary)
            self.assertNotIn('"tool_call"', compressed.summary)  # 工具噪音不进摘要
            self.assertEqual(session.messages, long_history(15))  # 原对象不被就地篡改

    def test_run_below_threshold_does_not_compress(self):
        # 阈值由 AgentSpec.summary_threshold 控制:20 条 < 40,run() 不应压缩
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp) / "sessions"
            agent = create_bill_agent(
                ScriptedLLM([{"thought": "done", "final": "答"}]), "s1", data_dir)
            agent.run("s1", "你好")
            persisted = SessionStore(data_dir).load("s1")
            self.assertEqual(2, len(persisted.messages))  # user + assistant 原样
            self.assertEqual("", persisted.summary)


class EngineIntegrationTests(unittest.TestCase):
    def test_long_session_run_compresses_and_persists(self):
        with tempfile.TemporaryDirectory() as temp:
            service_root = Path(temp) / "bills"
            from billguard.bills import BillService
            service = BillService(service_root)
            service.import_bills("d.csv", DEMO, owner="alice")

            data_dir = Path(temp) / "sessions"
            store = SessionStore(data_dir)
            store.save(Session("s1", long_history(15)))

            agent = create_bill_agent(
                ScriptedLLM([{"thought": "done", "final": "最新回答"}]),
                "s1", data_dir, service.for_user("alice"))
            response = agent.run("s1", "新问题")

            self.assertEqual("completed", response.status)
            persisted = store.load("s1")
            self.assertLessEqual(len(persisted.messages), 14)  # 12 + 本轮 user/assistant
            self.assertTrue(persisted.summary)  # 摘要已落盘

    def test_short_session_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp) / "sessions"
            agent = create_bill_agent(
                ScriptedLLM([{"thought": "done", "final": "答"}]), "s1", data_dir)
            response = agent.run("s1", "你好")
            self.assertEqual("completed", response.status)
            self.assertEqual("", SessionStore(data_dir).load("s1").summary)


if __name__ == "__main__":
    unittest.main()
