# tests/test_web_distributed.py — 分布式装配(web 层)测试;
# 连接独立测试库 billguard_test(compose PG 127.0.0.1:5433)与 Redis(6380/0)。
# 覆盖:同 session 并发 → LockedError(HTTP 层 423)、LLM 限流 → BusyError、
# Authenticator×RedisAuthSessions×PGUserStore(含滑动续期)、
# PG 存储 end-to-end(会话/追踪/证据/删除)、F6 MCP 服务器存储工厂。
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import redis
from psycopg_pool import ConnectionPool

from billguard.auth import AuthError, Authenticator, User
from billguard.bills import BillService
from billguard.coordination import (
    LockedError, RedisAuthSessions, RedisLLMLimiter, RedisSessionLock,
)
from billguard.policy import PolicyGateway
from billguard.storage_pg import (
    PGApprovalStore, PGBillService, PGEvidenceStore, PGSessionStore,
    PGTraceStore, PGUserStore, PGWorkItemStore,
)
from billguard.web import BillGuardApp, BusyError, make_handler

from tests.llm_doubles import FinalLLM, ScriptedLLM

from tests.conftest import PG_DSN as DSN, REDIS_URL  # 测试库(与演示库分离)

TABLES = ("tx_audits", "transactions", "categories", "subscriptions", "imports",
          "reports", "approvals", "wi_approvals", "issues", "sessions",
          "evidence", "traces", "users")

DEMO = ("tx_id,paid_at,merchant,category,amount,method,note" + chr(10)
        + "TX-001,2026-07-05 10:00:00,饿了么,餐饮,35.5,支付宝,午餐" + chr(10)
        + "TX-002,2026-08-05 21:00:00,腾讯视频,订阅,25.0,微信,月费" + chr(10))


def lock_key(session_id: str) -> str:
    return f"lock:session:{hashlib.sha256(session_id.encode()).hexdigest()}"


def token_key(token: str) -> str:
    return f"auth:token:{hashlib.sha256(token.encode()).hexdigest()}"


class DistributedTestCase(unittest.TestCase):
    """PG/Redis 连接与自清理:每用例前清空 PG 表,结束后删除本用例用到的 Redis 键。"""

    def setUp(self) -> None:
        self.pool = ConnectionPool(DSN, min_size=1, max_size=4, open=True)
        with self.pool.connection() as db:
            for table in TABLES:
                db.execute(f"DELETE FROM {table}")
        self.client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        self.temp = tempfile.TemporaryDirectory()
        self._redis_keys: set[str] = {"llm:slots"}
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self.client.delete(*self._redis_keys)
        # 令牌键经 track 原始 DEL,不走 SREM;同步清按用户反向索引键
        # (auth:user:*,与 tests/conftest.sweep_redis 的扫除方式一致;空集不 DEL)
        index_keys = list(self.client.scan_iter(match="auth:user:*"))
        if index_keys:
            self.client.delete(*index_keys)
        self.pool.close()
        self.client.close()
        self.temp.cleanup()

    def track(self, *keys: str) -> None:
        """登记本用例创建的 Redis 键,清理阶段统一删除(零残留)。"""
        self._redis_keys.update(keys)

    def build_app(self, llm=None, max_concurrent_llm: int = 4,
                  authenticator=None) -> BillGuardApp:
        return BillGuardApp(
            Path(self.temp.name), Path(self.temp.name), llm or FinalLLM(),
            authenticator=authenticator,
            policy_gateway=PolicyGateway(PGApprovalStore(self.pool)),
            work_item_store=PGWorkItemStore(self.pool),
            max_concurrent_llm=max_concurrent_llm, run_timeout=30.0,
            bills=PGBillService(self.pool),
            session_store=PGSessionStore(self.pool),
            evidence_store=PGEvidenceStore(self.pool),
            trace_store=PGTraceStore(self.pool),
            redis_client=self.client,
        )


class DistributedChatTests(DistributedTestCase):
    def test_same_session_concurrent_chat_raises_locked(self):
        app = self.build_app()
        alice = User("alice", "user")
        session_id = "dist-lock-chat"
        self.track(lock_key(session_id))
        holder = RedisSessionLock(self.client, session_id, ttl_ms=30_000)
        self.assertTrue(holder.acquire())  # 模拟另一实例正在处理该会话
        try:
            with self.assertRaises(LockedError) as ctx:
                app.chat(alice, session_id, "总结问题")
            self.assertIn("另一会话操作正在进行", str(ctx.exception))
        finally:
            holder.release()
        # 锁释放后同一会话可继续(锁确被释放,无残留占用)
        self.assertEqual("completed", app.chat(alice, session_id, "总结问题")["status"])

    def test_decide_approval_shares_session_lock(self):
        # 审批恢复/驳回与 chat 共用同一把会话锁:持锁期间 decide 也返回 423
        app = self.build_app()
        session_id = "dist-lock-decide"
        self.track(lock_key(session_id))
        holder = RedisSessionLock(self.client, session_id, ttl_ms=30_000)
        self.assertTrue(holder.acquire())
        try:
            with self.assertRaises(LockedError):
                app.decide_approval(User("alice", "user"), session_id,
                                    {"approval_id": "POL-NOPE", "decision": "approve"})
        finally:
            holder.release()

    def test_llm_limit_returns_busy(self):
        app = self.build_app(max_concurrent_llm=1)
        session_id = "dist-busy"
        self.track(lock_key(session_id))
        saturated = RedisLLMLimiter(self.client, 1)  # 与 app 同一默认键 llm:slots
        self.assertTrue(saturated.acquire())
        try:
            with self.assertRaises(BusyError) as ctx:
                app.chat(User("alice", "user"), session_id, "总结问题")
            self.assertIn("服务繁忙", str(ctx.exception))
        finally:
            saturated.release()
        free_session = "dist-busy-ok"
        self.track(lock_key(free_session))
        self.assertEqual("completed",
                         app.chat(User("alice", "user"), free_session, "总结问题")["status"])


class DistributedPersistenceTests(DistributedTestCase):
    def test_chat_writes_session_trace_evidence_to_pg(self):
        app = self.build_app(llm=ScriptedLLM([
            {"thought": "先看总览", "tool_call": {"name": "bill_overview", "arguments": {}}},
            {"thought": "done", "final": "本月支出已梳理。"},
        ]))
        alice = User("alice", "user")
        app.import_bills(alice, {"filename": "demo.csv", "csv_text": DEMO})
        session_id = "dist-e2e"
        self.track(lock_key(session_id))
        result = app.chat(alice, session_id, "总结问题")
        self.assertEqual("completed", result["status"])
        # runs 列表来自 PG(而非本地 JSONL)
        runs = result["runs"]
        self.assertEqual(1, len(runs))
        self.assertEqual("completed", runs[0]["status"])
        self.assertEqual(["bill_overview"], runs[0]["tools"])
        detail = app.run_detail(alice, session_id, {"trace_id": runs[0]["trace_id"]})
        self.assertTrue(any(item.get("event") == "tool_end" for item in detail["events"]))
        # 会话消息与归属落在 PG
        session = PGSessionStore(self.pool).load(session_id)
        self.assertEqual("alice", session.owner)
        self.assertTrue(any(message.role == "assistant" and not message.tool_call_id
                            for message in session.messages))
        # 证据经 PGEvidenceStore,snapshot 可见
        snapshot = app.snapshot(alice, session_id)
        self.assertTrue(snapshot["messages"][-1]["evidence"])
        self.assertIn(session_id, [item["id"] for item in snapshot["sessions"]])
        # 分布式模式不得写本地会话/追踪/证据文件
        guard = Path(self.temp.name) / "billguard"
        self.assertEqual([], [path for path in guard.rglob("*")
                              if path.suffix in {".json", ".jsonl"}])

    def test_delete_session_removes_pg_rows(self):
        app = self.build_app()
        alice = User("alice", "user")
        session_id = "dist-del"
        self.track(lock_key(session_id))
        self.assertEqual("completed", app.chat(alice, session_id, "总结问题")["status"])
        self.assertTrue(PGSessionStore(self.pool).exists(session_id))

        result = app.delete_session(alice, session_id)

        self.assertTrue(result["deleted"])
        self.assertFalse(PGSessionStore(self.pool).exists(session_id))
        self.assertEqual([], PGTraceStore(self.pool).list_runs(session_id))
        self.assertEqual({}, PGEvidenceStore(self.pool).load(session_id))


class AuthenticatorDistributedTests(DistributedTestCase):
    def test_login_resolve_logout_via_redis(self):
        users = PGUserStore(self.pool)
        users.create("david", "david-pass-123", "user")
        authenticator = Authenticator(users, RedisAuthSessions(self.client))
        user, token = authenticator.login("david", "david-pass-123")
        self.track(token_key(token))
        self.assertEqual("david", user.username)
        headers = {"Cookie": f"session={token}"}
        resolved = authenticator.resolve_user(headers)
        self.assertEqual("david", resolved.username)
        self.assertEqual("user", resolved.role)
        authenticator.logout(headers)
        with self.assertRaises(AuthError):  # 登出后 token 失效
            authenticator.resolve_user(headers)

    def test_wrong_password_rejected(self):
        users = PGUserStore(self.pool)
        users.create("david", "david-pass-123", "user")
        authenticator = Authenticator(users, RedisAuthSessions(self.client))
        with self.assertRaises(AuthError):
            authenticator.login("david", "wrong-pass-123")

    def test_resolve_refreshes_expiry_under_threshold(self):
        # 滑动续期:剩余 TTL < 6 天时,经 Authenticator.resolve 后恢复到 7 天
        users = PGUserStore(self.pool)
        users.create("erin", "erin-pass-1234", "user")
        authenticator = Authenticator(users, RedisAuthSessions(self.client))
        _, token = authenticator.login("erin", "erin-pass-1234")
        key = token_key(token)
        self.track(key)
        self.client.expire(key, 5 * 86400)  # 压到剩余 5 天
        self.assertEqual(
            "erin",
            authenticator.resolve_user({"Cookie": f"session={token}"}).username)
        self.assertGreaterEqual(self.client.ttl(key), 6 * 86400)  # 滑回 ≥6 天


class DistributedHttpTests(DistributedTestCase):
    """HTTP 层:分布式装配下 LockedError → 423;登录走 Redis 会话。"""

    def setUp(self) -> None:
        super().setUp()
        users = PGUserStore(self.pool)
        users.create("alice", "alice-pass-123", "user")
        self.app = self.build_app(
            authenticator=Authenticator(users, RedisAuthSessions(self.client)))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        # LIFO:先 shutdown 再 server_close(顺序颠倒会在 Windows 上产生套接字竞态噪声)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener.open(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_chat_returns_423_when_session_locked(self):
        self.assertEqual(200, self.post("/api/auth/login",
                                        {"username": "alice", "password": "alice-pass-123"})[0])
        for cookie in self.jar:
            if cookie.name == "session":
                self.track(token_key(cookie.value))
        session_id = "http-locked"
        self.track(lock_key(session_id))
        holder = RedisSessionLock(self.client, session_id, ttl_ms=30_000)
        self.assertTrue(holder.acquire())
        try:
            status, payload = self.post("/api/chat",
                                        {"session_id": session_id, "message": "总结问题"})
            self.assertEqual(423, status)
            self.assertIn("另一会话操作正在进行", payload["error"])
        finally:
            holder.release()
        status, _ = self.post("/api/chat",
                              {"session_id": session_id, "message": "总结问题"})
        self.assertEqual(200, status)  # 释放后可正常对话


class BillServerStoreFactoryTests(unittest.TestCase):
    """F6:bill_server 存储工厂按 BILLGUARD_PG_DSN 选择 PG/SQLite。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def tearDown(self) -> None:
        import billguard.mcp_servers.bill_server as module
        if module._PG_POOL is not None:
            module._PG_POOL.close()
            module._PG_POOL = None

    def test_pg_service_when_env_set(self):
        import billguard.mcp_servers.bill_server as module
        with mock.patch.dict(os.environ, {"BILLGUARD_PG_DSN": DSN}):
            service = module._build_service(Path(self.temp.name))
            self.assertIsInstance(service, PGBillService)
            self.assertIsNotNone(module._PG_POOL)  # 进程级连接池只建一次

    def test_sqlite_service_when_env_absent(self):
        import billguard.mcp_servers.bill_server as module
        with mock.patch.dict(os.environ):
            os.environ.pop("BILLGUARD_PG_DSN", None)
            service = module._build_service(Path(self.temp.name) / "bills")
            self.assertIsInstance(service, BillService)
            self.assertNotIsInstance(service, PGBillService)
            self.assertIsNone(module._PG_POOL)


class WorkItemServerStoreFactoryTests(unittest.TestCase):
    """F6:work_item_server 存储工厂按 BILLGUARD_PG_DSN 选择 PG/SQLite。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def tearDown(self) -> None:
        import billguard.mcp_servers.work_item_server as module
        if module._PG_POOL is not None:
            module._PG_POOL.close()
            module._PG_POOL = None

    def test_pg_store_when_env_set(self):
        import billguard.mcp_servers.work_item_server as module
        from billguard.work_items import WorkItemStore
        with mock.patch.dict(os.environ, {"BILLGUARD_PG_DSN": DSN}):
            store = module._build_store(Path(self.temp.name))
            self.assertIsInstance(store, PGWorkItemStore)
            self.assertIsNotNone(module._PG_POOL)
        with mock.patch.dict(os.environ):
            os.environ.pop("BILLGUARD_PG_DSN", None)
            self.assertIsInstance(module._build_store(Path(self.temp.name) / "wi"),
                                  WorkItemStore)


if __name__ == "__main__":
    unittest.main()
