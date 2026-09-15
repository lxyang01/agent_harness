# tests/test_coordination.py — Redis 协调层测试;连接 compose Redis(127.0.0.1:6380/0)。
# 断言体移植自任务简报;按控制器 F4 裁定:acquire() 返回 bool,
# 失败断言用 assertFalse(简报原文为 assertIsNone),成功仍用 assertTrue。
from __future__ import annotations

import threading, unittest
import redis
from billguard.coordination import (
    RedisSessionLock, RedisLLMLimiter, RedisAuthSessions, LockedError,
    acquire_session_lock)

CLIENT = redis.Redis.from_url("redis://127.0.0.1:6380/0", decode_responses=True)


class SessionLockTests(unittest.TestCase):
    def test_exclusive_and_cross_instance(self):
        # 锁 A 持有时,新客户端(模拟另一实例)acquire 返回 False
        lock = RedisSessionLock(CLIENT, "s1", ttl_ms=5000)
        self.assertTrue(lock.acquire())
        other = redis.Redis.from_url("redis://127.0.0.1:6380/0", decode_responses=True)
        self.assertFalse(RedisSessionLock(other, "s1", ttl_ms=5000).acquire())
        lock.release()
        self.assertTrue(RedisSessionLock(other, "s1", ttl_ms=5000).acquire())

    def test_ttl_expires(self):
        lock = RedisSessionLock(CLIENT, "s2", ttl_ms=100)
        self.assertTrue(lock.acquire())
        import time; time.sleep(0.15)
        # 过期自动释放
        self.assertTrue(RedisSessionLock(CLIENT, "s2", ttl_ms=100).acquire())

    def test_release_only_own_lock(self):
        # 超时后他人持锁,原持有者 release 不得误删
        a = RedisSessionLock(CLIENT, "s3", ttl_ms=80)
        self.assertTrue(a.acquire())
        import time; time.sleep(0.12)
        b = RedisSessionLock(CLIENT, "s3", ttl_ms=5000)
        self.assertTrue(b.acquire())
        a.release()   # 不应删掉 b 的锁
        self.assertFalse(RedisSessionLock(CLIENT, "s3", ttl_ms=5000).acquire())


class LLMLimiterTests(unittest.TestCase):
    def test_limit_and_release(self):
        CLIENT.delete("llm:test")
        limiter = RedisLLMLimiter(CLIENT, 2, key="llm:test")
        self.assertTrue(limiter.acquire())
        self.assertTrue(limiter.acquire())
        self.assertFalse(limiter.acquire())  # 第三个被拒
        limiter.release()
        self.assertTrue(RedisLLMLimiter(CLIENT, 2, key="llm:test").acquire())


class AuthSessionTests(unittest.TestCase):
    def test_create_resolve_delete(self):
        CLIENT.delete("auth:token:*")
        sessions = RedisAuthSessions(CLIENT)
        token = sessions.create("alice")
        self.assertEqual("alice", sessions.resolve(token))
        self.assertIsNone(sessions.resolve("no-such-token"))
        sessions.delete(token)
        self.assertIsNone(sessions.resolve(token))


class AcquireSessionLockTests(unittest.TestCase):
    def test_helper_acquire_release_cycle(self):
        # F4 补充:helper 成功返回锁对象,占用期返回 None,释放后可再获取
        lock = acquire_session_lock(CLIENT, "helper1", ttl_ms=5000)
        try:
            self.assertIsNotNone(lock)
            self.assertIsNone(acquire_session_lock(CLIENT, "helper1", ttl_ms=5000))
        finally:
            lock.release()
        relock = acquire_session_lock(CLIENT, "helper1", ttl_ms=5000)
        try:
            self.assertIsNotNone(relock)
        finally:
            relock.release()


class LockedErrorTests(unittest.TestCase):
    def test_is_runtime_error(self):
        # 423 信号:web 层按 RuntimeError 子类映射状态码
        self.assertTrue(issubclass(LockedError, RuntimeError))


if __name__ == "__main__":
    unittest.main()
