from __future__ import annotations

import unittest

from billguard.storage_pg import PGSessionStore
from billguard.types import Message, Session

from tests.conftest import StoreTestCase


class SessionOwnerTests(StoreTestCase):
    def test_owner_roundtrip_and_default(self):
        store = PGSessionStore(self.pool)
        self.assertIsNone(store.load("s1").owner)  # 不存在 → 空会话,owner=None
        store.save(Session("s1", [Message("user", "hi")], owner="alice"))
        self.assertEqual("alice", store.load("s1").owner)

    def test_legacy_file_without_owner_loads_as_none(self):
        # 原断言写无 owner 的旧版 JSON 文件(文件机制);PG 等价可观察量:
        # owner 列为 NULL 的存量行(旧版本/迁移写入)读回 owner=None
        with self.pool.connection() as db:
            db.execute(
                "INSERT INTO sessions(session_id, owner, summary, messages, updated_at) "
                "VALUES ('legacy', NULL, '', '[]', '2026-01-01T00:00:00+00:00')")
        self.assertIsNone(PGSessionStore(self.pool).load("legacy").owner)


if __name__ == "__main__":
    unittest.main()
