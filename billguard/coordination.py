from __future__ import annotations

import hashlib
import secrets
import uuid

SESSION_TTL_DAYS = 7
_REFRESH_THRESHOLD_DAYS = 6

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0"""

_INCR_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current < tonumber(ARGV[1]) then
    redis.call('INCR', KEYS[1])
    redis.call('EXPIRE', KEYS[1], ARGV[2])
    return 1
end
return 0"""


class LockedError(RuntimeError):
    """423: 另一实例正在处理该会话。"""


class RedisSessionLock:
    def __init__(self, client, session_id: str, ttl_ms: int) -> None:
        self._client = client
        self._key = f"lock:session:{hashlib.sha256(session_id.encode()).hexdigest()}"
        self._ttl_ms = ttl_ms
        self._holder = uuid.uuid4().hex

    def acquire(self) -> bool:
        return bool(self._client.set(self._key, self._holder, nx=True, px=self._ttl_ms))

    def release(self) -> None:
        self._client.eval(_RELEASE_LUA, 1, self._key, self._holder)


def acquire_session_lock(client, session_id: str, ttl_ms: int) -> RedisSessionLock | None:
    """获取会话锁:成功返回已持有的锁对象,失败(被占用)返回 None。"""
    lock = RedisSessionLock(client, session_id, ttl_ms)
    return lock if lock.acquire() else None


class RedisLLMLimiter:
    def __init__(self, client, limit: int, key: str = "llm:slots") -> None:
        self._client = client
        self._limit = limit
        self._key = key

    def acquire(self) -> bool:
        return bool(self._client.eval(_INCR_LUA, 1, self._key,
                                      str(self._limit), "120"))

    def release(self) -> None:
        # DECR 带地板:不为负
        self._client.eval(
            "local v = tonumber(redis.call('GET', KEYS[1]) or '0') "
            "if v > 0 then return redis.call('DECR', KEYS[1]) end return 0",
            1, self._key)


class RedisAuthSessions:
    def __init__(self, client) -> None:
        self._client = client

    @staticmethod
    def _key(token: str) -> str:
        return f"auth:token:{hashlib.sha256(token.encode()).hexdigest()}"

    def create(self, username: str, ttl_days: int = SESSION_TTL_DAYS) -> str:
        token = secrets.token_urlsafe(32)
        self._client.set(self._key(token), username, ex=ttl_days * 86400)
        return token

    def resolve(self, token: str) -> str | None:
        username = self._client.get(self._key(token))
        if username is None:
            return None
        ttl = self._client.ttl(self._key(token))
        if 0 < ttl < (_REFRESH_THRESHOLD_DAYS * 86400):
            self._client.expire(self._key(token), SESSION_TTL_DAYS * 86400)
        return username

    def delete(self, token: str) -> None:
        self._client.delete(self._key(token))
