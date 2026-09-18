from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from email.message import Message
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests.llm_doubles import FinalLLM
from billguard.auth import (
    Authenticator, clear_session_cookie, session_cookie,
)
from billguard.coordination import RedisAuthSessions
from billguard.storage_pg import (
    PGBillService, PGEvidenceStore, PGSessionStore, PGTraceStore, PGUserStore,
)
from billguard.web import BillGuardApp, _same_origin, make_handler

from tests.conftest import StoreTestCase


def _headers(**values: str) -> Message:
    """构造与 http.server 同型的 headers 对象(email.message.Message)。"""
    message = Message()
    for key, value in values.items():
        message[key] = value
    return message


class SameOriginHelperTests(unittest.TestCase):
    """_same_origin 单元测试:Origin 优先 / Referer 兜底 / 双缺失放行。"""

    def test_matching_origin_passes(self):
        self.assertTrue(_same_origin(
            _headers(Host="example.com", Origin="http://example.com")))

    def test_port_significant_in_match(self):
        self.assertTrue(_same_origin(
            _headers(Host="localhost:8080", Origin="http://localhost:8080")))
        self.assertFalse(_same_origin(
            _headers(Host="localhost:8080", Origin="http://localhost:9999")))

    def test_mismatched_origin_rejected(self):
        self.assertFalse(_same_origin(
            _headers(Host="example.com", Origin="http://evil.example")))

    def test_host_comparison_case_insensitive(self):
        self.assertTrue(_same_origin(
            _headers(Host="EXAMPLE.com", Origin="http://example.com")))

    def test_referer_fallback_matches(self):
        self.assertTrue(_same_origin(
            _headers(Host="example.com", Referer="http://example.com/page")))

    def test_referer_fallback_mismatch_rejected(self):
        self.assertFalse(_same_origin(
            _headers(Host="example.com", Referer="http://evil.example/attack")))

    def test_origin_takes_precedence_over_referer(self):
        # Origin 与 Referer 并存时以 Origin 为准:Origin 伪造即拒绝,
        # 即使 Referer 看起来同源(Referer 可能被浏览器策略裁剪,不可信)。
        self.assertFalse(_same_origin(
            _headers(Host="example.com", Origin="http://evil.example",
                     Referer="http://example.com/page")))

    def test_absent_headers_allowed(self):
        # 权衡(设计决策):两头均缺失 → 放行。浏览器对跨站 POST 必带
        # Origin,缺失即非浏览器客户端(curl/测试),不在 CSRF 威胁模型内。
        self.assertTrue(_same_origin(_headers(Host="example.com")))
        self.assertTrue(_same_origin(Message()))

    def test_origin_without_host_header_rejected(self):
        self.assertFalse(_same_origin(_headers(Origin="http://example.com")))


class SecureCookieTests(unittest.TestCase):
    """BILLGUARD_SECURE_COOKIES(函数内读 env,用例可随时翻转)。"""

    def setUp(self):
        self._previous = os.environ.get("BILLGUARD_SECURE_COOKIES")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._previous is None:
            os.environ.pop("BILLGUARD_SECURE_COOKIES", None)
        else:
            os.environ["BILLGUARD_SECURE_COOKIES"] = self._previous

    def test_default_off_no_secure_attribute(self):
        os.environ.pop("BILLGUARD_SECURE_COOKIES", None)
        self.assertNotIn("Secure", session_cookie("token-123"))
        self.assertNotIn("Secure", clear_session_cookie())

    def test_env_on_appends_secure(self):
        for value in ("1", "true", "TRUE", "True"):
            with self.subTest(value=value):
                os.environ["BILLGUARD_SECURE_COOKIES"] = value
                self.assertIn("Secure", session_cookie("token-123"))
                self.assertIn("Secure", clear_session_cookie())

    def test_env_off_values_do_not_append_secure(self):
        for value in ("0", "false", ""):
            with self.subTest(value=value):
                os.environ["BILLGUARD_SECURE_COOKIES"] = value
                self.assertNotIn("Secure", session_cookie("token-123"))
                self.assertNotIn("Secure", clear_session_cookie())

    def test_cookie_attributes_preserved_when_secure(self):
        os.environ["BILLGUARD_SECURE_COOKIES"] = "1"
        cookie = session_cookie("token-123")
        for attribute in ("session=token-123", "HttpOnly", "SameSite=Strict", "Secure"):
            self.assertIn(attribute, cookie)
        cleared = clear_session_cookie()
        for attribute in ("session=", "HttpOnly", "SameSite=Strict", "Max-Age=0", "Secure"):
            self.assertIn(attribute, cleared)


class HttpCsrfTests(StoreTestCase):
    """HTTP 层 CSRF 防线:伪造 Origin/Referer 的 POST 一律 403。

    既有 HTTP 测试(urllib)不发送 Origin/Referer → 走"非浏览器客户端"
    放行分支,全部保持 200/401 语义不变。"""

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
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._close_server)
        self._token = ""
        self.cookie = ""
        # 登录一次拿会话 Cookie(CSRF 攻击寄生于受害者的登录态)
        status, _ = self._login({"username": "user1", "password": "user-pass-123"}, {})
        self.assertEqual(200, status)
        self.assertTrue(self.cookie)

    def _close_server(self):
        # LIFO:先 shutdown 再 server_close(Windows 套接字竞态防护,同 HTTP 测试基类)
        self.server.shutdown()
        self.server.server_close()

    def post(self, path: str, body: dict, headers: dict | None = None):
        # 与 test_web_auth_http 相同的裸 urllib 形态,但显式带 Cookie 头,
        # 便于构造"受害者浏览器"的跨站请求(不依赖 CookieJar 自动附带)
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cookie": self.cookie,
                     **(headers or {})},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _login(self, body: dict, headers: dict):
        """登录 POST(不带 Cookie),成功时记下会话令牌供后续请求复用。"""
        request = urllib.request.Request(
            self.base + "/api/auth/login", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                self._token = response.headers.get(
                    "Set-Cookie", "").split(";", 1)[0].split("=", 1)[1]
                self.cookie = f"session={self._token}"
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_forged_origin_rejected(self):
        status, data = self.post("/api/bills/export", {"filters": {}},
                                 {"Origin": "http://evil.example"})
        self.assertEqual(403, status)
        self.assertEqual("跨站请求被拒绝", data["error"])

    def test_forged_referer_rejected_without_origin(self):
        status, _ = self.post("/api/bills/export", {"filters": {}},
                              {"Referer": "http://evil.example/attack"})
        self.assertEqual(403, status)

    def test_same_origin_headers_pass(self):
        # 合法浏览器流量:Origin 与自身 Host(缺省即目标 netloc)一致 → 放行
        status, _ = self.post("/api/bills/export", {"filters": {}},
                              {"Origin": self.base})
        self.assertEqual(200, status)

    def test_absent_headers_pass_through(self):
        # 非浏览器客户端(curl/测试脚本)不带 Origin/Referer → 不受影响
        status, _ = self.post("/api/bills/export", {"filters": {}})
        self.assertEqual(200, status)

    def test_login_with_forged_origin_rejected(self):
        # 登录 CSRF(把受害者登进攻击者账号)同样拦截
        status, data = self._login(
            {"username": "admin", "password": "admin-pass-1234"},
            {"Origin": "http://evil.example"})
        self.assertEqual(403, status)
        self.assertEqual("跨站请求被拒绝", data["error"])

    def test_login_empty_credentials_validated_before_origin(self):
        # 空凭据校验先于同源校验:同为非法请求时优先报 400
        status, _ = self._login({"username": "", "password": ""},
                                {"Origin": "http://evil.example"})
        self.assertEqual(400, status)

    def test_unauthenticated_forged_request_still_401(self):
        # 无有效会话的请求先经认证(401),CSRF 检查不改变未登录语义
        request = urllib.request.Request(
            self.base + "/api/bills/export", data=b"{}",
            headers={"Content-Type": "application/json",
                     "Origin": "http://evil.example"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.read()
        self.assertEqual(401, status)


if __name__ == "__main__":
    unittest.main()
