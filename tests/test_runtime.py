from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
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


class _SlowHandler(BaseHTTPRequestHandler):
    started: threading.Event | None = None

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if _SlowHandler.started is not None:
            _SlowHandler.started.set()
        time.sleep(0.4)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


class BoundedServerTests(unittest.TestCase):
    def test_saturation_returns_503(self):
        from billguard.web import BoundedHTTPServer
        _SlowHandler.started = threading.Event()
        server = BoundedHTTPServer(("127.0.0.1", 0), _SlowHandler,
                                   max_threads=1, queue_capacity=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        holder = []

        def hold():
            with urllib.request.urlopen(base + "/a") as response:
                holder.append(response.read())

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(_SlowHandler.started.wait(timeout=2))  # 唯一工人已被占用
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(base + "/b")  # 容量 0 → 503
        body = caught.exception.read().decode("utf-8")
        self.assertEqual(503, caught.exception.code)
        self.assertIn("服务繁忙", body)
        thread.join(timeout=5)
        self.assertEqual([b"ok"], holder)


if __name__ == "__main__":
    unittest.main()
