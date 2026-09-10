from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from minimal_agent.auth import (
    AuthError, PermissionDenied, UserStore, can,
    validate_password, validate_username,
)
from minimal_agent.auth import (
    AuthSessionStore, Authenticator, clear_session_cookie,
    session_cookie, session_token_from_cookie,
)


def store(root: Path) -> UserStore:
    return UserStore(root / "auth")


class UserValidationTests(unittest.TestCase):
    def test_username_rules(self):
        validate_username("admin")
        validate_username("zhang-san_01")
        with self.assertRaises(AuthError):
            validate_username("Bad")
        with self.assertRaises(AuthError):
            validate_username("x")
        with self.assertRaises(AuthError):
            validate_username("-abc")

    def test_password_rules(self):
        validate_password("12345678")
        with self.assertRaises(AuthError):
            validate_password("1234567")


class CapabilityMatrixTests(unittest.TestCase):
    def test_matrix(self):
        self.assertFalse(can("viewer", "approval_decide"))
        self.assertFalse(can("viewer", "report_write"))
        self.assertFalse(can("viewer", "feedback_write"))
        self.assertFalse(can("viewer", "users_manage"))
        self.assertTrue(can("approver", "report_write"))
        self.assertTrue(can("approver", "feedback_write"))
        self.assertTrue(can("approver", "approval_decide"))
        self.assertFalse(can("approver", "users_manage"))
        for capability in ("report_write", "feedback_write", "approval_decide", "users_manage"):
            self.assertTrue(can("admin", capability))
        self.assertFalse(can("unknown-role", "report_write"))


class UserStoreTests(unittest.TestCase):
    def test_create_verify_and_wrong_password(self):
        with tempfile.TemporaryDirectory() as temp:
            users = store(Path(temp))
            created = users.create("alice", "alice-pass-123", "approver")
            self.assertEqual("alice", created.username)
            self.assertEqual("approver", created.role)
            self.assertEqual(1, users.count())
            self.assertEqual(users.get("alice"), created)
            verified = users.verify("alice", "alice-pass-123")
            self.assertEqual("approver", verified.role)
            with self.assertRaises(AuthError):
                users.verify("alice", "wrong-pass-123")
            with self.assertRaises(AuthError):
                users.verify("nobody", "alice-pass-123")

    def test_duplicate_and_invalid_create(self):
        with tempfile.TemporaryDirectory() as temp:
            users = store(Path(temp))
            users.create("alice", "alice-pass-123", "viewer")
            with self.assertRaises(AuthError):
                users.create("alice", "other-pass-123", "viewer")
            with self.assertRaises(AuthError):
                users.create("Bob", "bob-pass-1234", "viewer")
            with self.assertRaises(AuthError):
                users.create("bob", "short", "viewer")
            with self.assertRaises(AuthError):
                users.create("bob", "bob-pass-1234", "boss")

    def test_password_hash_not_plaintext(self):
        with tempfile.TemporaryDirectory() as temp:
            users = store(Path(temp))
            users.create("alice", "alice-pass-123", "viewer")
            import sqlite3
            conn = sqlite3.connect(users.db_path)
            row = conn.execute(
                "SELECT password_hash, salt FROM users WHERE username='alice'").fetchone()
            conn.close()  # Windows：显式关闭，否则临时目录清理时 auth.db 仍被占用
            self.assertNotIn("alice-pass-123", row)

    def test_set_role_and_last_admin_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            users = store(Path(temp))
            users.create("root", "root-pass-1234", "admin")
            users.create("alice", "alice-pass-123", "admin")
            self.assertEqual("viewer", users.set_role("alice", "viewer").role)
            with self.assertRaises(AuthError):  # 最后一个启用中的 admin 不可降级
                users.set_role("root", "viewer")
            users.create("bob", "bob-pass-1234", "admin")
            users.set_role("root", "viewer")  # 有其他 admin 时允许

    def test_disable_guards(self):
        with tempfile.TemporaryDirectory() as temp:
            users = store(Path(temp))
            users.create("root", "root-pass-1234", "admin")
            users.create("alice", "alice-pass-123", "viewer")
            self.assertTrue(users.set_disabled("alice", True).disabled)
            with self.assertRaises(AuthError):  # 最后一个启用的 admin 不可禁用
                users.set_disabled("root", True)
            with self.assertRaises(AuthError):
                users.verify("alice", "alice-pass-123")  # 禁用用户登录失败
            users.set_disabled("alice", False)
            self.assertEqual("viewer", users.verify("alice", "alice-pass-123").role)

    def test_reset_password(self):
        with tempfile.TemporaryDirectory() as temp:
            users = store(Path(temp))
            users.create("alice", "alice-pass-123", "viewer")
            users.reset_password("alice", "new-pass-12345")
            with self.assertRaises(AuthError):
                users.verify("alice", "alice-pass-123")
            self.assertEqual("viewer", users.verify("alice", "new-pass-12345").role)


class FakeHeaders:
    def __init__(self, cookie: str = "") -> None:
        self.cookie = cookie

    def get(self, name: str, default: str = "") -> str:
        return self.cookie if name.lower() == "cookie" else default


class AuthSessionTests(unittest.TestCase):
    def test_token_roundtrip_and_hashed_storage(self):
        with tempfile.TemporaryDirectory() as temp:
            sessions = AuthSessionStore(Path(temp) / "auth")
            token = sessions.create("alice")
            self.assertNotIn(token[:8], sqlite3_text(Path(temp) / "auth" / "auth.db"))
            self.assertEqual("alice", sessions.consume(token))
            self.assertEqual("alice", sessions.consume(token))  # 可重复使用

    def test_logout_invalidates(self):
        with tempfile.TemporaryDirectory() as temp:
            sessions = AuthSessionStore(Path(temp) / "auth")
            token = sessions.create("alice")
            sessions.delete(token)
            self.assertIsNone(sessions.consume(token))

    def test_expired_token_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            sessions = AuthSessionStore(Path(temp) / "auth")
            token = sessions.create("alice", ttl_days=-1)  # 构造已过期
            self.assertIsNone(sessions.consume(token))

    def test_resolve_requires_cookie_and_checks_user(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "auth"
            users = UserStore(root)
            users.create("alice", "alice-pass-123", "approver")
            auth = Authenticator(users, AuthSessionStore(root))
            with self.assertRaises(AuthError):
                auth.resolve_user(FakeHeaders(""))
            user, token = auth.login("alice", "alice-pass-123")
            self.assertEqual("alice", user.username)
            resolved = auth.resolve_user(FakeHeaders(f"other=1; session={token}"))
            self.assertEqual("alice", resolved.username)
            users.set_disabled("alice", True)
            with self.assertRaises(AuthError):  # 禁用后存量会话立即失效
                auth.resolve_user(FakeHeaders(f"session={token}"))

    def test_logout_and_cookie_helpers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "auth"
            users = UserStore(root)
            users.create("alice", "alice-pass-123", "viewer")
            auth = Authenticator(users, AuthSessionStore(root))
            _, token = auth.login("alice", "alice-pass-123")
            self.assertEqual(token, session_token_from_cookie(
                f"session={token}; other=x"))
            self.assertIn("HttpOnly", session_cookie(token))
            self.assertIn("Max-Age=0", clear_session_cookie())
            auth.logout(FakeHeaders(f"session={token}"))
            with self.assertRaises(AuthError):
                auth.resolve_user(FakeHeaders(f"session={token}"))


def sqlite3_text(path: Path) -> str:
    import sqlite3
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT token_hash FROM auth_sessions").fetchall()
    finally:
        connection.close()
    return " ".join(row[0] for row in rows)


if __name__ == "__main__":
    unittest.main()


class PasswordResetTests(unittest.TestCase):
    def test_password_reset_invalidates_existing_sessions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "auth"
            users = UserStore(root)
            users.create("alice", "alice-pass-123", "viewer")
            auth = Authenticator(users, AuthSessionStore(root))
            _, token = auth.login("alice", "alice-pass-123")
            self.assertEqual("alice", auth.resolve_user(FakeHeaders(f"session={token}")).username)
            users.reset_password("alice", "new-pass-12345")
            with self.assertRaises(AuthError):
                auth.resolve_user(FakeHeaders(f"session={token}"))
