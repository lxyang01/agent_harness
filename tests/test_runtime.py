from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from billguard.agents import create_bill_agent
from billguard.bills import BillService


class _NeverFinalLLM:
    """每步都发起一次工具调用且每次都睡 0.3 秒,永不给最终答案。"""

    def complete(self, messages, tools) -> str:
        time.sleep(0.3)
        return json.dumps({"thought": "loop", "tool_call": {
            "name": "bill_overview", "arguments": {}}}, ensure_ascii=False)


class RunTimeoutTests(unittest.TestCase):
    def test_run_timeout_stops_safely(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp) / "bills")
            agent = create_bill_agent(_NeverFinalLLM(), "s1", temp, service,
                                      run_timeout=1.0)
            response = agent.run("s1", "hi")
            self.assertEqual("failed", response.status)
            self.assertIn("最大执行时间", response.answer)


if __name__ == "__main__":
    unittest.main()
