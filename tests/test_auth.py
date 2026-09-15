from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from billguard.auth import (
    AuthError, PermissionDenied, UserStore, can,
    validate_password, validate_username,
)
from billguard.auth import (
    AuthSessionStore, Authenticator, clear_session_cookie,
    session_cookie, session_token_from_cookie,
)
from billguard.coordination import RedisAuthSessions
from billguard.storage_pg import PGUserStore

from tests.conftest import StoreTestCase, token_key


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
        self.assertTrue(can("user", "approval_decide"))
        self.assertTrue(can("user", "report_write"))
        self.assertTrue(can("user", "bills_write"))
        self.assertFalse(can("user", "users_manage"))
        self.assertTrue(can("user", "report_write"))
        self.assertTrue(can("user", "bills_write"))
        self.assertTrue(can("user", "approval_decide"))
        self.assertFalse(can("user", "users_manage"))
        for capability in ("report_write", "bills_write", "approval_decide", "users_manage"):
            self.assertTrue(can("admin", capability))
        self.assertFalse(can("unknown-role", "report_write"))
        self.assertFalse(can("admin", "bills_write_legacy"))  # 未定义能力一律拒绝


class UserStoreTests(StoreTestCase):
    def store(self) -> PGUserStore:
        return PGUserStore(self.pool)

    def test_create_verify_and_wrong_password(self):
        users = self.store()
        created = users.create("alice", "alice-pass-123", "user")
        self.assertEqual("alice", created.username)
        self.assertEqual("user", created.role)
        self.assertEqual(1, users.count())
        self.assertEqual(users.get("alice"), created)
        verified = users.verify("alice", "alice-pass-123")
        self.assertEqual("user", verified.role)
        with self.assertRaises(AuthError):
            users.verify("alice", "wrong-pass-123")
        with self.assertRaises(AuthError):
            users.verify("nobody", "alice-pass-123")

    def test_duplicate_and_invalid_create(self):
        users = self.store()
        users.create("alice", "alice-pass-123", "user")
        with self.assertRaises(AuthError):
            users.create("alice", "other-pass-123", "user")
        with self.assertRaises(AuthError):
            users.create("Bob", "bob-pass-1234", "user")
        with self.assertRaises(AuthError):
            users.create("bob", "short", "user")
        with self.assertRaises(AuthError):
            users.create("bob", "bob-pass-1234", "boss")

    def test_password_hash_not_plaintext(self):
        users = self.store()
        users.create("alice", "alice-pass-123", "user")
        # 原断言读 SQLite auth.db 文件(文件机制);PG 等价可观察量:
        # 直接读 users 行的 password_hash/salt,证明库中无明文密码
        with self.pool.connection() as db:
            row = db.execute(
                "SELECT password_hash, salt FROM users WHERE username = 'alice'").fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn("alice-pass-123", f"{row['password_hash']}{row['salt']}")

    def test_set_role_and_last_admin_guard(self):
        users = self.store()
        users.create("root", "root-pass-1234", "admin")
        users.create("alice", "alice-pass-123", "admin")
        self.assertEqual("user", users.set_role("alice", "user").role)
        with self.assertRaises(AuthError):  # 最后一个启用中的 admin 不可降级
            users.set_role("root", "user")
        users.create("bob", "bob-pass-1234", "admin")
        users.set_role("root", "user")  # 有其他 admin 时允许

    def test_disable_guards(self):
        users = self.store()
        users.create("root", "root-pass-1234", "admin")
        users.create("alice", "alice-pass-123", "user")
        self.assertTrue(users.set_disabled("alice", True).disabled)
        with self.assertRaises(AuthError):  # 最后一个启用的 admin 不可禁用
            users.set_disabled("root", True)
        with self.assertRaises(AuthError):
            users.verify("alice", "alice-pass-123")  # 禁用用户登录失败
        users.set_disabled("alice", False)
        self.assertEqual("user", users.verify("alice", "alice-pass-123").role)

    def test_reset_password(self):
        users = self.store()
        users.create("alice", "alice-pass-123", "user")
        users.reset_password("alice", "new-pass-12345")
        with self.assertRaises(AuthError):
            users.verify("alice", "alice-pass-123")
        self.assertEqual("user", users.verify("alice", "new-pass-12345").role)


class FakeHeaders:
    def __init__(self, cookie: str = "") -> None:
        self.cookie = cookie

    def get(self, name: str, default: str = "") -> str:
        return self.cookie if name.lower() == "cookie" else default


class AuthSessionTests(StoreTestCase):
    """登录会话走 Redis(RedisAuthSessions):分布式模式的会话后端。"""

    def sessions(self) -> RedisAuthSessions:
        return RedisAuthSessions(self.redis)

    def users(self) -> PGUserStore:
        return PGUserStore(self.pool)

    def test_token_roundtrip_and_hashed_storage(self):
        sessions = self.sessions()
        token = sessions.create("alice")
        # 原断言读 SQLite auth.db 的 token_hash(文件机制);Redis 等价可观察量:
        # 键为 token 的 sha256 摘要(auth:token:<hash>),原 token 不落任何键名
        self.assertTrue(self.redis.exists(token_key(token)))
        self.assertEqual([], list(self.redis.scan_iter(match=f"*{token}*")))
        self.assertEqual("alice", sessions.resolve(token))
        self.assertEqual("alice", sessions.resolve(token))  # 可重复使用

    def test_logout_invalidates(self):
        sessions = self.sessions()
        token = sessions.create("alice")
        sessions.delete(token)
        self.assertIsNone(sessions.resolve(token))

    def test_expired_token_rejected(self):
        sessions = self.sessions()
        token = sessions.create("alice")
        # 原断言以 ttl_days=-1 构造已过期(SQLite 可写过去时间戳);Redis 的
        # ex 不接受负值,等价可观察量:把键 TTL 压到 60ms 后等待其自然过期
        self.redis.pexpire(token_key(token), 60)
        time.sleep(0.1)
        self.assertIsNone(sessions.resolve(token))

    def test_resolve_requires_cookie_and_checks_user(self):
        users = self.users()
        users.create("alice", "alice-pass-123", "user")
        auth = Authenticator(users, self.sessions())
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
        self.users().create("alice", "alice-pass-123", "user")
        auth = Authenticator(self.users(), self.sessions())
        _, token = auth.login("alice", "alice-pass-123")
        self.assertEqual(token, session_token_from_cookie(
            f"session={token}; other=x"))
        self.assertIn("HttpOnly", session_cookie(token))
        self.assertIn("Max-Age=0", clear_session_cookie())
        auth.logout(FakeHeaders(f"session={token}"))
        with self.assertRaises(AuthError):
            auth.resolve_user(FakeHeaders(f"session={token}"))


class PasswordResetTests(unittest.TestCase):
    def test_password_reset_invalidates_existing_sessions(self):
        # 单进程契约(UserStore+AuthSessionStore 文件版):改密联动清理服务端会话。
        # 分布式后端(PGUserStore+RedisAuthSessions)按 T1/T2 决策不做联动
        # (storage_pg 注释:PG schema 无 auth_sessions 表),本语义仅由
        # 单进程路径保障,故保留文件版构造(见任务报告 concerns)。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "auth"
            users = UserStore(root)
            users.create("alice", "alice-pass-123", "user")
            auth = Authenticator(users, AuthSessionStore(root))
            _, token = auth.login("alice", "alice-pass-123")
            self.assertEqual("alice", auth.resolve_user(FakeHeaders(f"session={token}")).username)
            users.reset_password("alice", "new-pass-12345")
            with self.assertRaises(AuthError):
                auth.resolve_user(FakeHeaders(f"session={token}"))


if __name__ == "__main__":
    unittest.main()
