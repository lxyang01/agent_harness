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

_LOGIN_FAIL_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current"""


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
    """Redis 登录会话;token 只以 sha256 摘要落键名,值存 username。

    另维护反向索引 auth:user:{username}(SET,成员=该用户的令牌键):
    改密/删户经 delete_by_user 全量失效(等价单进程 auth.py 的
    DELETE FROM auth_sessions WHERE username = ?)。索引 TTL 与令牌对齐,
    且随滑动续期同步续期——索引不会先于任何活跃令牌过期。
    """

    def __init__(self, client) -> None:
        self._client = client

    @staticmethod
    def _key(token: str) -> str:
        return f"auth:token:{hashlib.sha256(token.encode()).hexdigest()}"

    @staticmethod
    def _user_index_key(username: str) -> str:
        return f"auth:user:{username}"

    def create(self, username: str, ttl_days: int = SESSION_TTL_DAYS) -> str:
        token = secrets.token_urlsafe(32)
        key = self._key(token)
        self._client.set(key, username, ex=ttl_days * 86400)
        index = self._user_index_key(username)
        self._client.sadd(index, key)
        self._client.expire(index, ttl_days * 86400)
        return token

    def resolve(self, token: str) -> str | None:
        key = self._key(token)
        username = self._client.get(key)
        if username is None:
            return None
        ttl = self._client.ttl(key)
        if 0 < ttl < (_REFRESH_THRESHOLD_DAYS * 86400):
            self._client.expire(key, SESSION_TTL_DAYS * 86400)
            # 令牌被续期即仍在活跃使用:反向索引同步续期,不得先于令牌过期
            self._client.expire(self._user_index_key(username), SESSION_TTL_DAYS * 86400)
        return username

    def delete(self, token: str) -> None:
        key = self._key(token)
        username = self._client.get(key)
        self._client.delete(key)
        if username:
            # 索引同步摘除;SET 弹空时 Redis 自动删键,登出零残留
            self._client.srem(self._user_index_key(username), key)

    def delete_by_user(self, username: str) -> int:
        """失效该用户的全部登录令牌,返回清除的令牌数。

        改密/删户联动入口:SMEMBERS 反查令牌键后整批 DEL,索引随删;
        成员若已先期过期,DEL 不存在的键是无害空操作。
        """
        index = self._user_index_key(username)
        keys = list(self._client.smembers(index))
        if keys:
            self._client.delete(*keys)
        self._client.delete(index)
        return len(keys)


class RedisLoginThrottle:
    """登录暴力破解防护:按 (username, ip) 失败计数,达 max_failures 次锁定。

    固定窗口:窗口从该组合的**第一次失败**起算 window_seconds 秒,不做
    滑动续期——实现简单且防御足够(攻击者至多在窗口边界附近多得少量
    尝试次数)。计数键 login:fail:{sha256(username|ip)},窗口过期整键
    消失即自动解锁;成功登录 DEL 清零。record_failure 用 Lua 保证
    INCR 与首次 EXPIRE 原子(只设一次 TTL,后续失败不续窗,也不会
    留下无 TTL 的常驻计数键)。
    """

    def __init__(self, client, max_failures: int = 5, window_seconds: int = 600) -> None:
        self._client = client
        self._max_failures = max_failures
        self._window_seconds = window_seconds

    @staticmethod
    def _key(username: str, ip: str) -> str:
        return f"login:fail:{hashlib.sha256(f'{username}|{ip}'.encode()).hexdigest()}"

    def allowed(self, username: str, ip: str) -> bool:
        count = self._client.get(self._key(username, ip))
        return (int(count) if count is not None else 0) < self._max_failures

    def record_failure(self, username: str, ip: str) -> None:
        self._client.eval(_LOGIN_FAIL_LUA, 1, self._key(username, ip),
                          str(self._window_seconds))

    def reset(self, username: str, ip: str) -> None:
        self._client.delete(self._key(username, ip))
