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
        # ex 不接受负值,等价可观察量:把键 TTL 压到 500ms 后等待其自然过期
        # (pexpire 500ms + sleep 700ms,留 200ms 余量抗调度抖动)
        self.redis.pexpire(token_key(token), 500)
        time.sleep(0.7)
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
        # 单进程契约(UserStore+AuthSessionStore 文件版):改密联动清理服务端会话
        # (auth.py 内联 DELETE FROM auth_sessions)。分布式等价语义见
        # DistributedSessionInvalidationTests(PGUserStore×RedisAuthSessions)。
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


class DistributedSessionInvalidationTests(StoreTestCase):
    """分布式联动(修复轮 1):PGUserStore 注入 RedisAuthSessions 后,
    改密/删户经反向索引(auth:user:{username})立即失效该用户全部
    Redis 登录令牌——等价单进程 auth.py 的 auth_sessions 联动清理。
    set_disabled 不联动:resolve_user 每次请求都拒绝禁用用户(天然 kill-switch)。
    """

    def _login_twice(self, username: str = "alice",
                     password: str = "alice-pass-123") -> tuple[PGUserStore, Authenticator, list[str]]:
        sessions = RedisAuthSessions(self.redis)
        users = PGUserStore(self.pool, sessions=sessions)
        users.create(username, password, "user")
        auth = Authenticator(users, sessions)
        tokens = [auth.login(username, password)[1] for _ in range(2)]
        return users, auth, tokens

    def test_password_reset_invalidates_all_existing_sessions(self):
        users, auth, tokens = self._login_twice()
        for token in tokens:  # 两个并发登录均有效
            self.assertEqual("alice",
                             auth.resolve_user(FakeHeaders(f"session={token}")).username)
        users.reset_password("alice", "new-pass-12345")
        for token in tokens:  # 改密后全部立即失效(不止当前请求持有的那个)
            with self.assertRaises(AuthError):
                auth.resolve_user(FakeHeaders(f"session={token}"))
        # 反查索引与令牌键一并清除,零残留
        self.assertEqual([], list(self.redis.scan_iter(match="auth:user:alice")))
        self.assertEqual([], [key for token in tokens
                              for key in self.redis.scan_iter(match=token_key(token))])

    def test_delete_user_invalidates_existing_sessions(self):
        users, auth, tokens = self._login_twice()
        users.create("boss", "boss-pass-1234", "admin")  # 保证库里有其他账号
        self.assertEqual("alice",
                         auth.resolve_user(FakeHeaders(f"session={tokens[0]}")).username)
        users.delete("alice")
        for token in tokens:  # 删户后全部令牌立即失效
            with self.assertRaises(AuthError):
                auth.resolve_user(FakeHeaders(f"session={token}"))

    def test_renewal_refreshes_reverse_index_ttl(self):
        # 滑动续期同步续反向索引:令牌仍活跃时,索引不得先于令牌过期
        sessions = RedisAuthSessions(self.redis)
        token = sessions.create("erin")
        key = token_key(token)
        self.redis.expire(key, 5 * 86400)  # 压到剩余 5 天(< 6 天阈值)
        self.redis.expire("auth:user:erin", 5 * 86400)
        self.assertEqual("erin", sessions.resolve(token))
        self.assertGreaterEqual(self.redis.ttl(key), 6 * 86400)  # 令牌滑回 ≥6 天
        self.assertGreaterEqual(self.redis.ttl("auth:user:erin"),
                                6 * 86400)  # 索引同步续期


if __name__ == "__main__":
    unittest.main()
