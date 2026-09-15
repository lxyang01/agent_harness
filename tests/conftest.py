# tests/conftest.py — PG/Redis 测试夹具(纯 helper 模块)。
#
# 本套件运行于 unittest(非 pytest),此文件不会被自动加载;
# 各迁移测试文件按既有共享助手风格显式 import(同 tests.llm_doubles):
#     from tests import conftest  /  from tests.conftest import StoreTestCase
#
# 职责:
# - PG 连接池 / Redis 客户端工厂(compose:PG 5433 / Redis 6380/0)
# - clean_stores:清空全部业务表,保证共享库下测试可背靠背重跑(零状态残留)
# - sweep_redis:按前缀清扫测试产生的 Redis 键(会话锁/登录令牌/LLM 槽位)
# - seed_default_categories:播种 owner=NULL 的默认类别,对齐 SQLite 版
#   “空库播种”契约(PG 版按 spec 不做全局播种,存量语义由夹具补齐)
# - StoreTestCase:迁移测试文件的公共基类(setUp 建池+清表,清理期关池+扫键)
from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timezone

import redis
from psycopg_pool import ConnectionPool

from billguard.bills import DEFAULT_CATEGORIES

PG_DSN = "postgresql://billguard:billguard@127.0.0.1:5433/billguard"
REDIS_URL = "redis://127.0.0.1:6380/0"


def lock_key(session_id: str) -> str:
    """会话锁键(与 coordination.RedisSessionLock 同构),供测试跟踪/断言。"""
    return f"lock:session:{hashlib.sha256(session_id.encode()).hexdigest()}"


def token_key(token: str) -> str:
    """登录令牌键(与 coordination.RedisAuthSessions 同构)。"""
    return f"auth:token:{hashlib.sha256(token.encode()).hexdigest()}"

# 13 张业务表(docker/init.sql 预建);顺序无关,均为整表 DELETE
TABLES = ("tx_audits", "transactions", "categories", "subscriptions", "imports",
          "reports", "approvals", "wi_approvals", "issues", "sessions",
          "evidence", "traces", "users")

# 迁移测试会产生的 Redis 键前缀(套件串行运行,前缀清扫安全);
# auth:user:* 为 RedisAuthSessions 的按用户反向索引(改密/删户失效通道)
REDIS_KEY_PATTERNS = ("lock:session:*", "auth:token:*", "auth:user:*", "llm:slots")


def pg_pool() -> ConnectionPool:
    return ConnectionPool(PG_DSN, min_size=1, max_size=4, open=True)


def redis_client() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def clean_stores(pool: ConnectionPool) -> None:
    """清空全部业务表;每条用例 setUp 调用,保证可重复执行。"""
    with pool.connection() as db:
        for table in TABLES:
            db.execute(f"DELETE FROM {table}")


def sweep_redis(client: redis.Redis,
                patterns: tuple[str, ...] = REDIS_KEY_PATTERNS) -> int:
    """删除匹配前缀的全部 Redis 键(测试自清理,零残留)。"""
    keys: list[str] = []
    for pattern in patterns:
        keys.extend(client.scan_iter(match=pattern, count=100))
    if keys:
        client.delete(*keys)
    return len(keys)


def seed_default_categories(pool: ConnectionPool) -> None:
    """播种 owner=NULL 的默认类别副本。

    单进程 BillService 在空库时播种全局默认类别(bills.py _initialize),
    PG 版按 spec 不播种(storage_pg 注释:owner 首访副本由
    _ensure_user_categories 负责)。owner=None(存量/根句柄)导入在 PG 下
    依赖本夹具对齐该契约,使按类别名的既有断言语义不变。
    幂等:唯一索引 (COALESCE(owner,''), name) + ON CONFLICT DO NOTHING。
    """
    now = datetime.now(timezone.utc).isoformat()
    with pool.connection() as db:
        for name, keywords in DEFAULT_CATEGORIES:
            db.execute(
                "INSERT INTO categories(name, keywords, enabled, created_at, owner) "
                "VALUES (%s, %s, TRUE, %s, NULL) ON CONFLICT DO NOTHING",
                (name, json.dumps([word.strip() for word in keywords.split(",")],
                                  ensure_ascii=False), now))


class StoreTestCase(unittest.TestCase):
    """迁移测试文件公共基类:PG 池 + 清表 + Redis 客户端;清理期再清表、
    扫键、关池 —— 用例结束零残留,套件背靠背重跑零状态泄漏。"""

    def setUp(self) -> None:
        self.pool = pg_pool()
        clean_stores(self.pool)
        self.redis = redis_client()
        self.addCleanup(self._cleanup_backend)

    def _cleanup_backend(self) -> None:
        clean_stores(self.pool)
        sweep_redis(self.redis)
        self.redis.close()
        self.pool.close()
