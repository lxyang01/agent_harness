from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


ROLES = ("admin", "approver", "viewer")

# 能力常量沿用继承层命名(含 feedback_write):这是认证/角色层的内部标识,
# 服务端与前端 JS 一致引用,从不作为文案展示给用户,按"认证/角色继承不动"约束保留。
_CAPABILITY_BY_ROLE = {
    "viewer": frozenset(),
    "approver": frozenset({"report_write", "feedback_write", "approval_decide"}),
    "admin": frozenset({"report_write", "feedback_write", "approval_decide", "users_manage"}),
}

_USERNAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
_MIN_PASSWORD_LENGTH = 8
_PBKDF2_ITERATIONS = 200_000


class AuthError(ValueError):
    """401: credentials are missing, wrong, expired, or the account is disabled."""


class PermissionDenied(PermissionError):
    """403: authenticated but not allowed for this action or session."""


def can(role: str, capability: str) -> bool:
    return capability in _CAPABILITY_BY_ROLE.get(role, frozenset())


def validate_username(username: str) -> None:
    if not _USERNAME.fullmatch(username):
        raise AuthError("用户名须为 2-32 位小写字母/数字，可用 - _ 连接")


def validate_password(password: str) -> None:
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise AuthError(f"密码至少 {_MIN_PASSWORD_LENGTH} 位")


@dataclass(frozen=True)
class User:
    username: str
    role: str
    disabled: bool = False
    created_at: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)


# 未知用户名的等代价哈希,用于抹平 verify 的计时差
_DUMMY_SALT = b"billguard-timing-equalizer"
_DUMMY_HASH = _hash_password("billguard-dummy", _DUMMY_SALT).hex()


class UserStore:
    """SQLite-backed users with salted PBKDF2 password hashing."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "auth.db"
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
                salt TEXT NOT NULL, role TEXT NOT NULL,
                disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
            # 密码重置需联动清理会话;即使 AuthSessionStore 尚未初始化也保证表存在
            db.execute("""CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL)""")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row  # brief 缺失此行：按列名取值需要 Row 工厂
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _row(self, db: sqlite3.Connection, username: str) -> sqlite3.Row | None:
        return db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    def create(self, username: str, password: str, role: str) -> User:
        username = username.strip()
        validate_username(username)
        validate_password(password)
        if role not in ROLES:
            raise AuthError(f"角色必须是 {ROLES} 之一")
        salt = secrets.token_bytes(16)
        with self._connect() as db:
            try:
                db.execute(
                    "INSERT INTO users(username, password_hash, salt, role, disabled, created_at) "
                    "VALUES (?, ?, ?, ?, 0, ?)",
                    (username, _hash_password(password, salt).hex(), salt.hex(), role,
                     _now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise AuthError(f"用户已存在：{username}") from exc
        return self.get(username)

    def get(self, username: str) -> User:
        with self._connect() as db:
            row = self._row(db, username)
        if row is None:
            raise AuthError(f"用户不存在：{username}")
        return User(row["username"], row["role"], bool(row["disabled"]), row["created_at"])

    def list(self) -> list[User]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT username, role, disabled, created_at FROM users ORDER BY username").fetchall()
        return [User(row["username"], row["role"], bool(row["disabled"]), row["created_at"])
                for row in rows]

    def count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def verify(self, username: str, password: str) -> User:
        with self._connect() as db:
            row = self._row(db, username.strip())
        # 统一失败信息，不区分“用户不存在”与“密码错误”
        if row is None:
            # 未知用户名也执行一次等代价哈希,避免通过响应时间枚举有效用户名
            hmac.compare_digest(_hash_password(password, _DUMMY_SALT).hex(), _DUMMY_HASH)
            raise AuthError("用户名或密码错误")
        candidate = _hash_password(password, bytes.fromhex(row["salt"]))
        if not hmac.compare_digest(candidate.hex(), row["password_hash"]):
            raise AuthError("用户名或密码错误")
        if row["disabled"]:
            raise AuthError("账号已被禁用")
        return User(row["username"], row["role"], bool(row["disabled"]), row["created_at"])

    def _enabled_admins(self, db: sqlite3.Connection, exclude: str = "") -> int:
        return int(db.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0 AND username != ?",
            (exclude,)).fetchone()[0])

    def set_role(self, username: str, role: str) -> User:
        if role not in ROLES:
            raise AuthError(f"角色必须是 {ROLES} 之一")
        with self._connect() as db:
            row = self._row(db, username)
            if row is None:
                raise AuthError(f"用户不存在:{username}")
            if row["role"] == "admin" and role != "admin" and not row["disabled"] \
                    and self._enabled_admins(db, exclude=username) == 0:
                raise AuthError("不能降级最后一个启用中的管理员")
            db.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
        return self.get(username)

    def reset_password(self, username: str, password: str) -> None:
        validate_password(password)
        salt = secrets.token_bytes(16)
        with self._connect() as db:
            if self._row(db, username) is None:
                raise AuthError(f"用户不存在:{username}")
            db.execute("UPDATE users SET password_hash = ?, salt = ? WHERE username = ?",
                       (_hash_password(password, salt).hex(), salt.hex(), username))
            # 密码已重置,旧凭证对应的存量会话一并失效
            db.execute("DELETE FROM auth_sessions WHERE username = ?", (username,))

    def set_disabled(self, username: str, disabled: bool) -> User:
        with self._connect() as db:
            row = self._row(db, username)
            if row is None:
                raise AuthError(f"用户不存在:{username}")
            if row["role"] == "admin" and disabled and not row["disabled"] \
                    and self._enabled_admins(db, exclude=username) == 0:
                raise AuthError("不能禁用最后一个启用中的管理员")
            db.execute("UPDATE users SET disabled = ? WHERE username = ?",
                       (1 if disabled else 0, username))
        return self.get(username)


SESSION_TTL_DAYS = 7
_REFRESH_THRESHOLD_DAYS = 6
_COOKIE_NAME = "session"


class AuthSessionStore:
    """Server-side session tokens; only SHA-256 hashes are persisted."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "auth.db"
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL)""")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row  # brief 缺失此行：按列名取值需要 Row 工厂
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, username: str, ttl_days: int = SESSION_TTL_DAYS) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        expires = now + timedelta(days=ttl_days)
        with self._connect() as db:
            db.execute(
                "INSERT INTO auth_sessions(token_hash, username, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (self._hash(token), username, now.isoformat(), expires.isoformat()))
        return token

    def consume(self, token: str) -> str | None:
        """Return the username for a valid token; slide expiry when < threshold left."""
        token_hash = self._hash(token)
        with self._connect() as db:
            row = db.execute(
                "SELECT username, expires_at FROM auth_sessions WHERE token_hash = ?",
                (token_hash,)).fetchone()
            if row is None:
                return None
            expires = datetime.fromisoformat(row["expires_at"])
            now = _now()
            if now >= expires:
                db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))
                return None
            if expires - now < timedelta(days=_REFRESH_THRESHOLD_DAYS):
                db.execute("UPDATE auth_sessions SET expires_at = ? WHERE token_hash = ?",
                           ((now + timedelta(days=SESSION_TTL_DAYS)).isoformat(), token_hash))
            return row["username"]

    def delete(self, token: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (self._hash(token),))


def session_token_from_cookie(header: str) -> str | None:
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == _COOKIE_NAME and value:
            return value
    return None


def session_cookie(token: str) -> str:
    return (f"{_COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; Path=/; "
            f"Max-Age={SESSION_TTL_DAYS * 86400}")


def clear_session_cookie() -> str:
    return f"{_COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"


class Authenticator:
    """Single identity-resolution boundary; swap this class for SSO later."""

    def __init__(self, users: UserStore, sessions: AuthSessionStore) -> None:
        self.users = users
        self.sessions = sessions

    def login(self, username: str, password: str) -> tuple[User, str]:
        user = self.users.verify(username, password)
        return user, self.sessions.create(user.username)

    def logout(self, headers) -> None:
        token = session_token_from_cookie(headers.get("Cookie", ""))
        if token:
            self.sessions.delete(token)

    def resolve_user(self, headers) -> User:
        token = session_token_from_cookie(headers.get("Cookie", ""))
        if not token:
            raise AuthError("未登录或会话已失效")
        username = self.sessions.consume(token)
        if username is None:
            raise AuthError("登录已过期,请重新登录")
        user = self.users.get(username)  # 用户被删除则失败
        if user.disabled:
            raise AuthError("账号已被禁用")
        return user
