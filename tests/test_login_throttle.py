# tests/test_login_throttle.py — 登录暴力破解防护:Redis/内存双实现 + HTTP 层锁定。
# Redis 单元用例连 compose Redis(127.0.0.1:6380/0),tearDown 按精确键名删除
# 本类用到的 login:fail:* 桶,套件结束后零残留(背靠背重跑不受影响)。
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path

import redis

from tests.llm_doubles import FinalLLM
from tests.conftest import StoreTestCase
from billguard.auth import Authenticator
from billguard.coordination import RedisAuthSessions, RedisLoginThrottle
from billguard.storage_pg import (
    PGBillService, PGEvidenceStore, PGSessionStore, PGTraceStore, PGUserStore,
)
from billguard.web import BillGuardApp, MemoryLoginThrottle, make_handler

CLIENT = redis.Redis.from_url("redis://127.0.0.1:6380/0", decode_responses=True)


def login_fail_key(username: str, ip: str) -> str:
    """登录失败计数键(与 coordination.RedisLoginThrottle 同构),供清理/断言。"""
    digest = hashlib.sha256(f"{username}|{ip}".encode()).hexdigest()
    return f"login:fail:{digest}"


# Redis 用例覆盖的 (username, ip) 桶:换用户名/换 IP 的独立性各需对照桶
_REDIS_PAIRS = (
    ("alice", "1.2.3.4"), ("alice", "5.6.7.8"), ("bob", "1.2.3.4"),
    ("carol", "9.9.9.9"), ("dave", "4.4.4.4"),
)


class RedisLoginThrottleTests(unittest.TestCase):
    """Redis 实现:计数锁定 / 清零 / 分桶独立 / 固定窗口到期自愈。"""

    KEYS = tuple(login_fail_key(username, ip) for username, ip in _REDIS_PAIRS)

    def tearDown(self) -> None:
        CLIENT.delete(*self.KEYS)

    def test_lockout_after_max_failures_and_reset_clears(self):
        throttle = RedisLoginThrottle(CLIENT, max_failures=5, window_seconds=600)
        self.assertTrue(throttle.allowed("alice", "1.2.3.4"))
        for _ in range(5):
            throttle.record_failure("alice", "1.2.3.4")
        self.assertFalse(throttle.allowed("alice", "1.2.3.4"))
        throttle.reset("alice", "1.2.3.4")  # 成功登录清零
        self.assertTrue(throttle.allowed("alice", "1.2.3.4"))

    def test_username_and_ip_buckets_are_independent(self):
        throttle = RedisLoginThrottle(CLIENT, max_failures=5, window_seconds=600)
        for _ in range(5):
            throttle.record_failure("alice", "1.2.3.4")
        self.assertFalse(throttle.allowed("alice", "1.2.3.4"))
        self.assertTrue(throttle.allowed("alice", "5.6.7.8"))  # 换 IP = 新桶
        self.assertTrue(throttle.allowed("bob", "1.2.3.4"))    # 换用户名 = 新桶

    def test_window_expiry_re_allows(self):
        throttle = RedisLoginThrottle(CLIENT, max_failures=3, window_seconds=1)
        for _ in range(3):
            throttle.record_failure("carol", "9.9.9.9")
        self.assertFalse(throttle.allowed("carol", "9.9.9.9"))
        time.sleep(1.1)  # 固定窗口从首次失败起算,过期即整键消失
        self.assertTrue(throttle.allowed("carol", "9.9.9.9"))


class MemoryLoginThrottleTests(unittest.TestCase):
    """进程内实现:与 Redis 版语义一致(固定窗口,同三方法调用面)。"""

    def test_lockout_after_max_failures_and_reset_clears(self):
        throttle = MemoryLoginThrottle(max_failures=5, window_seconds=600)
        self.assertTrue(throttle.allowed("alice", "1.2.3.4"))
        for _ in range(5):
            throttle.record_failure("alice", "1.2.3.4")
        self.assertFalse(throttle.allowed("alice", "1.2.3.4"))
        throttle.reset("alice", "1.2.3.4")
        self.assertTrue(throttle.allowed("alice", "1.2.3.4"))

    def test_username_and_ip_buckets_are_independent(self):
        throttle = MemoryLoginThrottle(max_failures=5, window_seconds=600)
        for _ in range(5):
            throttle.record_failure("alice", "1.2.3.4")
        self.assertFalse(throttle.allowed("alice", "1.2.3.4"))
        self.assertTrue(throttle.allowed("alice", "5.6.7.8"))
        self.assertTrue(throttle.allowed("bob", "1.2.3.4"))

    def test_window_expiry_re_allows(self):
        throttle = MemoryLoginThrottle(max_failures=3, window_seconds=1)
        for _ in range(3):
            throttle.record_failure("dave", "4.4.4.4")
        self.assertFalse(throttle.allowed("dave", "4.4.4.4"))
        time.sleep(1.1)
        self.assertTrue(throttle.allowed("dave", "4.4.4.4"))


class HttpLoginThrottleTests(StoreTestCase):
    """HTTP 层登录节流:阈值内失败 401,达阈值后锁定 429,成功登录清零。"""

    def setUp(self):
        super().setUp()
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)
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
            redis_client=self.redis,
            login_throttle=MemoryLoginThrottle(max_failures=3, window_seconds=60),
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def tearDown(self):
        # LIFO:先 shutdown 再 server_close(顺序颠倒会在 Windows 上产生套接字竞态噪声)
        self.server.shutdown()
        self.server.server_close()

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

    def test_locked_out_after_threshold_even_with_correct_password(self):
        for index in range(3):
            status, _ = self.login("admin", f"wrong-pass-{index}")
            self.assertEqual(401, status)
        # 第 4 次即使密码正确:计数已达上限,窗口内一律 429
        status, data = self.login("admin", "admin-pass-1234")
        self.assertEqual(429, status)
        self.assertIn("登录失败次数过多", data["error"])
        # 不同用户名是独立桶,不受牵连
        status, _ = self.login("user1", "user-pass-123")
        self.assertEqual(200, status)

    def test_success_resets_counter(self):
        self.assertEqual(401, self.login("admin", "wrong-pass-1")[0])
        self.assertEqual(401, self.login("admin", "wrong-pass-2")[0])
        self.assertEqual(200, self.login("admin", "admin-pass-1234")[0])
        # 清零生效:需再满 3 次失败才锁(若未清零,第 2 次失败后即 429)
        self.assertEqual(401, self.login("admin", "wrong-pass-3")[0])
        self.assertEqual(401, self.login("admin", "wrong-pass-4")[0])
        self.assertEqual(401, self.login("admin", "wrong-pass-5")[0])
        status, _ = self.login("admin", "admin-pass-1234")
        self.assertEqual(429, status)


if __name__ == "__main__":
    unittest.main()
