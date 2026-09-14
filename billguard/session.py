from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .types import Message, Session


class SessionStore:
    def __init__(self, root: str | Path = ".sessions") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key(session_id: str) -> str:
        if not session_id or len(session_id) > 200:
            raise ValueError("invalid session id")
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def _path(self, session_id: str) -> Path:
        return self.root / f"{self._key(session_id)}.json"

    def load(self, session_id: str) -> Session:
        path = self._path(session_id)
        if not path.exists():
            return Session(session_id=session_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return Session(session_id=session_id, summary=value.get("summary", ""),
                           owner=value.get("owner"),
                           messages=[Message.from_dict(item) for item in value.get("messages", [])])
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError(f"cannot load session {session_id}: {exc}") from exc

    def save(self, session: Session) -> None:
        # 历史压缩由 HarnessEngine.compress_history 按 AgentSpec 阈值负责;
        # 存储层只做持久化,不再隐藏改写会话。
        payload = {"session_id": session.session_id, "summary": session.summary,
                   "owner": session.owner,
                   "messages": [message.as_dict() for message in session.messages]}
        fd, tmp_name = tempfile.mkstemp(prefix="session-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self._path(session.session_id))
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
