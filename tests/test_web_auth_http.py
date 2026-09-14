from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from billguard.agents import BillMockLLM
from billguard.auth import AuthSessionStore, Authenticator, UserStore
from billguard.web import BusyError, BillGuardApp, make_handler
from http.server import ThreadingHTTPServer


class HttpAuthTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        users = UserStore(root / "auth")
        users.create("admin", "admin-pass-1234", "admin")
        users.create("user1", "user-pass-123", "user")
        self.app = BillGuardApp(
            root / "web", root / "docs", BillMockLLM(),
            authenticator=Authenticator(users, AuthSessionStore(root / "auth")))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._temp.cleanup()

    def post(self, path: str, body: dict):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener.open(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def get(self, path: str):
        try:
            with self.opener.open(self.base + path) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_unauthenticated_api_returns_401(self):
        bodies = {"/api/chat": {"message": "hi"},
                  "/api/approvals/decide": {"approval_id": "POL-X", "decision": "approve"},
                  "/api/bills/import": {"filename": "f.csv", "csv_text": "x"}}
        for path in ("/api/snapshot", "/api/chat", "/api/approvals/decide", "/api/bills/import"):
            with self.subTest(path=path):
                status, _ = self.post(path, bodies.get(path, {}))
                self.assertEqual(401, status)

    def test_login_sets_cookie_and_grants_access(self):
        status, data = self.post("/api/auth/login",
                                 {"username": "admin", "password": "wrong-pass-1"})
        self.assertEqual(401, status)
        status, data = self.post("/api/auth/login",
                                 {"username": "admin", "password": "admin-pass-1234"})
        self.assertEqual(200, status)
        self.assertEqual("admin", data["user"]["username"])
        self.assertTrue(any(cookie.name == "session" for cookie in self.jar))
        status, data = self.post("/api/snapshot", {})
        self.assertEqual(200, status)
        status, data = self.get("/api/auth/me")
        self.assertEqual(200, status)
        self.assertEqual("admin", data["username"])

    def test_user_capabilities_and_admin_gate(self):
        self.post("/api/auth/login", {"username": "user1", "password": "user-pass-123"})
        # user 拥有全部业务能力(报告写入/导出/导入/对话)
        for path, body in (
            ("/api/reports/save", {"title": "t", "content": "c"}),
            ("/api/bills/export", {"filters": {}}),
        ):
            with self.subTest(path=path):
                status, _ = self.post(path, body)
                self.assertEqual(200, status)
        nl = chr(10)
        csv_text = ("tx_id,paid_at,merchant,category,amount,method,note" + nl
                    + "TX-1,2026-08-05 10:00:00,测试商户,餐饮,10.0,微信," + nl)
        status, _ = self.post("/api/bills/import", {"filename": "f.csv", "csv_text": csv_text})
        self.assertEqual(200, status)
        status, _ = self.post("/api/chat", {"message": "最近 7 天的问题"})
        self.assertEqual(200, status)
        # 用户管理仍为 admin 专属
        status, _ = self.post("/api/admin/users",
                              {"username": "u", "password": "12345678", "role": "user"})
        self.assertEqual(403, status)
        status, _ = self.get("/api/admin/users")
        self.assertEqual(403, status)

    def test_export_bills_allowed_for_writer_roles(self):
        self.post("/api/auth/login", {"username": "user1", "password": "user-pass-123"})
        status, data = self.post("/api/bills/export", {"filters": {}})
        self.assertEqual(200, status)
        self.assertEqual("bills-export.csv", data["filename"])
        self.post("/api/auth/logout", {})
        self.post("/api/auth/login", {"username": "admin", "password": "admin-pass-1234"})
        status, data = self.post("/api/bills/export", {"filters": {}})
        self.assertEqual(200, status)

    def test_unknown_api_returns_chinese_404_envelope(self):
        self.post("/api/auth/login", {"username": "admin", "password": "admin-pass-1234"})
        status, data = self.post("/api/no-such-endpoint", {})
        self.assertEqual(404, status)
        self.assertEqual("接口不存在", data["error"])

    def test_logout_invalidates_session(self):
        self.post("/api/auth/login", {"username": "admin", "password": "admin-pass-1234"})
        status, _ = self.post("/api/auth/logout", {})
        self.assertEqual(200, status)
        status, _ = self.post("/api/snapshot", {})
        self.assertEqual(401, status)

    def test_admin_user_management_endpoints(self):
        self.post("/api/auth/login", {"username": "admin", "password": "admin-pass-1234"})
        status, data = self.post("/api/admin/users",
                                 {"username": "newbie", "password": "12345678", "role": "user"})
        self.assertEqual(200, status)
        status, data = self.get("/api/admin/users")
        usernames = [item["username"] for item in data["users"]]
        self.assertIn("newbie", usernames)
        self.assertNotIn("password_hash", data["users"][0])  # 不泄露哈希
        status, _ = self.post("/api/admin/users/role", {"username": "newbie", "role": "user"})
        self.assertEqual(200, status)
        status, _ = self.post("/api/admin/users/password",
                              {"username": "newbie", "password": "abcd12345"})
        self.assertEqual(200, status)
        status, _ = self.post("/api/admin/users/disable",
                              {"username": "admin", "disabled": True})
        self.assertEqual(400, status)  # 不能禁用自己
        status, _ = self.post("/api/admin/users/disable",
                              {"username": "newbie", "disabled": True})
        self.assertEqual(200, status)


    def test_busy_error_maps_to_429(self):
        self.post("/api/auth/login", {"username": "admin", "password": "admin-pass-1234"})
        original_chat = self.app.chat

        def busy_chat(*args, **kwargs):
            raise BusyError("服务繁忙,请稍后重试")

        self.app.chat = busy_chat
        try:
            status, data = self.post("/api/chat", {"message": "hi"})
        finally:
            self.app.chat = original_chat
        self.assertEqual(429, status)
        self.assertIn("繁忙", data["error"])


if __name__ == "__main__":
    unittest.main()
