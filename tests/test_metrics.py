# tests/test_metrics.py — 轻量指标(进程内计数/计时/仪表)+ GET /api/metrics 端点。
#
# 覆盖:
# - Metrics 单元:inc / observe / gauge_add / provider / snapshot 形状 / reset /
#   8 线程并发精确性
# - /api/metrics 端点:未登录 401;任意登录用户可读(无能力门槛);
#   响应自身不计入 http_request_seconds(抓取噪声隔离)
# - 埋点冒烟(全部穿过真实代码路径):
#   llm.py(OpenAICompatibleLLM + httpx.MockTransport)/ mcp_runtime.py(未连接
#   服务器失败路径 + _trip)/ engine.py(app 级 chat 里的工具成功与失败)/
#   web.py(423 锁冲突与 429 繁忙经 HTTP 层)
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

from billguard.auth import Authenticator, User
from billguard.coordination import (
    RedisAuthSessions, RedisLLMLimiter, RedisSessionLock,
)
from billguard.llm import OpenAICompatibleLLM
from billguard.metrics import METRICS, Metrics
from billguard.mcp_runtime import MCPClientManager, MCPError
from billguard.storage_pg import (
    PGBillService, PGEvidenceStore, PGSessionStore, PGTraceStore, PGUserStore,
)
from billguard.web import BillGuardApp, make_handler

from tests.llm_doubles import FinalLLM, ScriptedLLM
from tests.conftest import StoreTestCase

DEMO = ("tx_id,paid_at,merchant,category,amount,method,note" + chr(10)
        + "TX-001,2026-07-05 10:00:00,饿了么,餐饮,35.5,支付宝,午餐" + chr(10)
        + "TX-002,2026-08-05 21:00:00,腾讯视频,订阅,25.0,微信,月费" + chr(10))


class MetricsUnitTests(unittest.TestCase):
    """纯内存单元:不依赖任何外部服务。"""

    def setUp(self):
        self.metrics = Metrics()

    def test_inc_accumulates_and_snapshot_shape(self):
        self.metrics.inc("http_requests_total")
        self.metrics.inc("http_requests_total")
        self.metrics.inc("http_status_423")
        snapshot = self.metrics.snapshot()
        self.assertEqual(2, snapshot["counters"]["http_requests_total"])
        self.assertEqual(1, snapshot["counters"]["http_status_423"])
        for key in ("uptime_seconds", "collected_at", "counters", "gauges", "timings"):
            self.assertIn(key, snapshot)
        self.assertGreaterEqual(snapshot["uptime_seconds"], 0.0)

    def test_observe_records_count_and_sum(self):
        self.metrics.observe("llm_call_seconds", 0.5)
        self.metrics.observe("llm_call_seconds", 0.25)
        timing = self.metrics.snapshot()["timings"]["llm_call_seconds"]
        self.assertEqual(2, timing["count"])
        self.assertAlmostEqual(0.75, timing["sum"], places=6)

    def test_gauge_add_tracks_delta(self):
        self.metrics.gauge_add("llm_slots_in_use", 1)
        self.metrics.gauge_add("llm_slots_in_use", 1)
        self.metrics.gauge_add("llm_slots_in_use", -1)
        self.assertEqual(1, self.metrics.snapshot()["gauges"]["llm_slots_in_use"])

    def test_provider_gauges_merged_and_failures_swallowed(self):
        self.metrics.register_provider("pg_pool", lambda: {"pg_pool_size": 8, "pg_pool_in_use": 3})
        self.metrics.register_provider("broken", lambda: 1 / 0)  # 观测失败绝不影响快照
        gauges = self.metrics.snapshot()["gauges"]
        self.assertEqual(8, gauges["pg_pool_size"])
        self.assertEqual(3, gauges["pg_pool_in_use"])

    def test_reset_clears_counters_timings_gauges_providers(self):
        self.metrics.inc("x")
        self.metrics.observe("y", 1.0)
        self.metrics.gauge_add("z", 1)
        self.metrics.register_provider("p", lambda: {"q": 1})
        self.metrics.reset()
        snapshot = self.metrics.snapshot()
        self.assertEqual({}, snapshot["counters"])
        self.assertEqual({}, snapshot["timings"])
        self.assertEqual({}, snapshot["gauges"])

    def test_thread_safe_under_concurrent_inc(self):
        threads_count, per_thread = 8, 500
        barrier = threading.Barrier(threads_count)

        def worker() -> None:
            barrier.wait()
            for _ in range(per_thread):
                self.metrics.inc("http_requests_total")

        threads = [threading.Thread(target=worker) for _ in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(threads_count * per_thread,
                         self.metrics.snapshot()["counters"]["http_requests_total"])


class LLMInstrumentationTests(unittest.TestCase):
    """llm.py 埋点:经 httpx.MockTransport 走真实 complete() 代码路径。"""

    def setUp(self):
        METRICS.reset()
        self.addCleanup(METRICS.reset)

    @staticmethod
    def _llm(handler, max_retries: int = 0) -> OpenAICompatibleLLM:
        return OpenAICompatibleLLM(
            "test-model", api_key="test-key",
            transport=httpx.MockTransport(handler), max_retries=max_retries)

    @staticmethod
    def _ok_response() -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(
                {"thought": "done", "final": "好的。"}, ensure_ascii=False)}}],
            "usage": {"total_tokens": 1}, "model": "test-model"})

    def test_success_counts_call_and_seconds(self):
        llm = self._llm(lambda request: self._ok_response())
        llm.complete([{"role": "user", "content": "hi"}], [])
        snapshot = METRICS.snapshot()
        self.assertEqual(1, snapshot["counters"]["llm_calls_total"])
        self.assertEqual(0, snapshot["counters"].get("llm_failures_total", 0))
        self.assertEqual(0, snapshot["counters"].get("llm_retries_total", 0))
        self.assertEqual(1, snapshot["timings"]["llm_call_seconds"]["count"])

    def test_failure_counts_failure_and_call(self):
        llm = self._llm(lambda request: httpx.Response(500, text="boom"))
        with self.assertRaises(RuntimeError):
            llm.complete([{"role": "user", "content": "hi"}], [])
        snapshot = METRICS.snapshot()
        self.assertEqual(1, snapshot["counters"]["llm_failures_total"])
        self.assertEqual(1, snapshot["counters"]["llm_calls_total"])
        self.assertEqual(1, snapshot["timings"]["llm_call_seconds"]["count"])

    def test_retry_counted_then_succeeds(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, text="slow down")
            return self._ok_response()

        llm = self._llm(handler, max_retries=1)
        llm.complete([{"role": "user", "content": "hi"}], [])
        snapshot = METRICS.snapshot()
        self.assertEqual(1, snapshot["counters"]["llm_retries_total"])
        self.assertEqual(1, snapshot["counters"]["llm_calls_total"])
        self.assertEqual(0, snapshot["counters"].get("llm_failures_total", 0))


class MCPInstrumentationTests(unittest.TestCase):
    """mcp_runtime.py 埋点:call_tool 失败路径(未连接)+ _trip 熔断计数。"""

    def setUp(self):
        METRICS.reset()
        self.addCleanup(METRICS.reset)

    def test_call_tool_counts_total_failures_seconds(self):
        manager = MCPClientManager(request_timeout=5)
        self.addCleanup(manager.close)
        with self.assertRaises(MCPError):
            manager.call_tool("bill", "aggregate", {})  # 未连接的服务器
        snapshot = METRICS.snapshot()
        self.assertEqual(1, snapshot["counters"]["mcp_calls_total"])
        self.assertEqual(1, snapshot["counters"]["mcp_call_failures_total"])
        self.assertEqual(1, snapshot["timings"]["mcp_call_seconds"]["count"])

    def test_trip_counts_circuit_open(self):
        manager = MCPClientManager(request_timeout=5)
        self.addCleanup(manager.close)
        manager._trip("bill", MCPError("断连"))
        self.assertEqual(1, METRICS.snapshot()["counters"]["circuit_opens_total"])


class EngineToolInstrumentationTests(StoreTestCase):
    """engine.py 埋点:app 级 chat(本地 Agent 路径)里的工具成功/失败计数。"""

    def setUp(self):
        super().setUp()
        METRICS.reset()
        self.addCleanup(METRICS.reset)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def build_app(self, llm=None) -> BillGuardApp:
        return BillGuardApp(
            self.root, self.root / "docs", llm or FinalLLM(),
            bills=PGBillService(self.pool),
            session_store=PGSessionStore(self.pool),
            evidence_store=PGEvidenceStore(self.pool),
            trace_store=PGTraceStore(self.pool),
            redis_client=self.redis, run_timeout=30.0,
        )

    def test_tool_calls_and_failures_counted(self):
        alice = User("alice", "user")
        self.build_app().import_bills(alice, {"filename": "demo.csv", "csv_text": DEMO})
        ok = self.build_app(llm=ScriptedLLM([
            {"thought": "先看总览", "tool_call": {"name": "bill_overview", "arguments": {}}},
            {"thought": "done", "final": "总览已获取。"},
        ]))
        ok.chat(alice, "metrics-tools-ok", "总结问题")
        snapshot = METRICS.snapshot()
        self.assertEqual(1, snapshot["counters"]["tool_calls_total"])
        self.assertEqual(0, snapshot["counters"].get("tool_failures_total", 0))

        failed = self.build_app(llm=ScriptedLLM([
            {"thought": "查异常", "tool_call": {"name": "bill_anomalies",
                                               "arguments": {"days": 7, "dimension": "bogus", "limit": 5}}},
            {"thought": "done", "final": "已尝试检测异常。"},
        ]))
        failed.chat(alice, "metrics-tools-fail", "异常?")
        snapshot = METRICS.snapshot()
        self.assertEqual(2, snapshot["counters"]["tool_calls_total"])
        self.assertEqual(1, snapshot["counters"]["tool_failures_total"])


class MetricsEndpointTests(StoreTestCase):
    """GET /api/metrics:登录必需;任意登录用户可读;自身不计时。"""

    def setUp(self):
        super().setUp()
        METRICS.reset()
        self.addCleanup(METRICS.reset)
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        users = PGUserStore(self.pool)
        users.create("admin", "admin-pass-1234", "admin")
        users.create("user1", "user-pass-123", "user")
        self.app = BillGuardApp(
            root / "web", root / "docs", FinalLLM(),
            authenticator=Authenticator(users, RedisAuthSessions(self.redis)),
            bills=PGBillService(self.pool),
            session_store=PGSessionStore(self.pool),
            evidence_store=PGEvidenceStore(self.pool),
            trace_store=PGTraceStore(self.pool),
            redis_client=self.redis, max_concurrent_llm=1, run_timeout=30.0,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        # LIFO:先 shutdown 再 server_close(Windows 套接字竞态,同既有套件)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.anon_opener = urllib.request.build_opener()

    def get(self, path: str, opener=None):
        try:
            with (opener or self.opener).open(self.base + path) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post(self, path: str, body: dict):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener.open(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def login(self, username: str, password: str):
        return self.post("/api/auth/login",
                         {"username": username, "password": password})

    def test_metrics_requires_login(self):
        status, body = self.get("/api/metrics", opener=self.anon_opener)
        self.assertEqual(401, status)
        self.assertEqual(1, METRICS.snapshot()["counters"].get("http_status_401", 0))

    def test_any_logged_in_user_reads_instance_snapshot(self):
        self.assertEqual(200, self.login("user1", "user-pass-123")[0])  # 普通用户即可
        status, body = self.get("/api/metrics")
        self.assertEqual(200, status)
        for key in ("instance", "uptime_seconds", "collected_at",
                    "counters", "gauges", "timings", "note"):
            self.assertIn(key, body)
        self.assertTrue(body["instance"])
        self.assertIn("Prometheus", body["note"])
        # 到此为止共 2 个 JSON 响应(登录 200 + 本端点 200),全部计数;
        # 快照在本端点 _json 之前取,故体内只见登录那 1 次
        self.assertEqual(1, body["counters"].get("http_status_200", 0))
        self.assertEqual(1, body["counters"].get("http_requests_total", 0))
        # 计时同样只含登录 POST:/api/metrics 自身不计时(抓取噪声隔离)
        self.assertEqual(1, body["timings"].get("http_request_seconds", {}).get("count", 0))
        # 本端点自身的 200 事后也计入状态计数(它是真实 HTTP 响应):登录 + 本端点
        self.assertEqual(2, METRICS.snapshot()["counters"].get("http_status_200", 0))

    def test_423_and_429_counters_via_http(self):
        self.assertEqual(200, self.login("admin", "admin-pass-1234")[0])
        # 423:另一实例持会话锁 → LockedError → HTTP 423 + lock_conflicts_total
        session_id = "metrics-locked"
        holder = RedisSessionLock(self.redis, session_id, ttl_ms=30_000)
        self.assertTrue(holder.acquire())
        try:
            status, payload = self.post("/api/chat",
                                        {"session_id": session_id, "message": "总结问题"})
            self.assertEqual(423, status)
        finally:
            holder.release()
        snapshot = METRICS.snapshot()
        self.assertEqual(1, snapshot["counters"].get("http_status_423", 0))
        self.assertEqual(1, snapshot["counters"].get("lock_conflicts_total", 0))
        # 429:LLM 槽位占满(max_concurrent_llm=1)→ BusyError → HTTP 429
        saturated = RedisLLMLimiter(self.redis, 1)
        self.assertTrue(saturated.acquire())
        try:
            status, payload = self.post("/api/chat",
                                        {"session_id": "metrics-busy", "message": "总结问题"})
            self.assertEqual(429, status)
        finally:
            saturated.release()
        snapshot = METRICS.snapshot()
        self.assertEqual(1, snapshot["counters"].get("http_status_429", 0))
        # 423/429 各计时一次(http_request_seconds 覆盖全部业务请求)
        self.assertEqual(3, snapshot["timings"].get("http_request_seconds", {}).get("count", 0))


if __name__ == "__main__":
    unittest.main()
