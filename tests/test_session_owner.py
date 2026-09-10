from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from billguard.session import SessionStore
from billguard.types import Message, Session


class SessionOwnerTests(unittest.TestCase):
    def test_owner_roundtrip_and_default(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SessionStore(temp)
            self.assertIsNone(store.load("s1").owner)  # 不存在 → 空会话,owner=None
            store.save(Session("s1", [Message("user", "hi")], owner="alice"))
            self.assertEqual("alice", store.load("s1").owner)

    def test_legacy_file_without_owner_loads_as_none(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = SessionStore(temp)._path("legacy")
            path.write_text('{"session_id": "legacy", "summary": "", "messages": []}',
                            encoding="utf-8")
            self.assertIsNone(SessionStore(temp).load("legacy").owner)


if __name__ == "__main__":
    unittest.main()
