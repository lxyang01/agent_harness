from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


class WorkItemError(ValueError):
    pass


@dataclass(frozen=True)
class WorkItemStore:
    root: Path

    def __init__(self, root: str | Path) -> None:
        object.__setattr__(self, "root", Path(root))
        self.root.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def db_path(self) -> Path:
        return self.root / "work_items.db"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS issues (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_refs TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    decided_by TEXT,
                    decided_at TEXT,
                    issue_id TEXT
                );
            """)

    def prepare_issue(self, title: str, description: str,
                      priority: str = "medium", evidence_refs: list[str] | None = None) -> dict[str, Any]:
        title = title.strip()
        description = description.strip()
        if not title or len(title) > 200:
            raise WorkItemError("title must contain between 1 and 200 characters")
        if not description or len(description) > 5_000:
            raise WorkItemError("description must contain between 1 and 5000 characters")
        if priority not in {"low", "medium", "high", "urgent"}:
            raise WorkItemError("priority must be low, medium, high, or urgent")
        refs = list(dict.fromkeys(str(value).strip() for value in evidence_refs or [] if str(value).strip()))
        if len(refs) > 50:
            raise WorkItemError("evidence_refs cannot exceed 50 items")
        approval_id = f"APR-{uuid.uuid4().hex[:10].upper()}"
        now = datetime.now(timezone.utc)
        payload = {
            "title": title,
            "description": description,
            "priority": priority,
            "evidence_refs": refs,
        }
        import json
        with self._connect() as db:
            db.execute(
                "INSERT INTO approvals(id, action, payload, status, requested_at, expires_at) "
                "VALUES (?, 'issue.create', ?, 'pending', ?, ?)",
                (approval_id, json.dumps(payload, ensure_ascii=False), now.isoformat(),
                 (now + timedelta(minutes=30)).isoformat()),
            )
        return {
            "approval_id": approval_id,
            "status": "pending",
            "action": "issue.create",
            "payload": payload,
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
            "next_step": "A human must approve this request outside the MCP tool channel before commit_issue.",
        }

    def decide(self, approval_id: str, approved: bool, decided_by: str) -> dict[str, Any]:
        decided_by = decided_by.strip()
        if not decided_by:
            raise WorkItemError("decided_by is required")
        with self._connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None:
                raise WorkItemError(f"approval not found: {approval_id}")
            if row["status"] != "pending":
                raise WorkItemError(f"approval is already {row['status']}: {approval_id}")
            if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
                db.execute("UPDATE approvals SET status = 'expired' WHERE id = ?", (approval_id,))
                raise WorkItemError(f"approval has expired: {approval_id}")
            status = "approved" if approved else "rejected"
            # 条件 UPDATE 保证并发 decide 恰好一个生效(与 ApprovalStore.decide 同款)。
            cursor = db.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, decided_by, datetime.now(timezone.utc).isoformat(), approval_id),
            )
            if cursor.rowcount == 0:
                raise WorkItemError(f"approval is already decided: {approval_id}")
        return self.approval(approval_id)

    def commit_issue(self, approval_id: str) -> dict[str, Any]:
        import json
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None:
                raise WorkItemError(f"approval not found: {approval_id}")
            if row["status"] == "consumed" and row["issue_id"]:
                issue = db.execute("SELECT * FROM issues WHERE id = ?", (row["issue_id"],)).fetchone()
                return {"created": self._issue_dict(issue), "idempotent_replay": True}
            if row["status"] != "approved":
                raise WorkItemError(f"approval must be approved before commit: {row['status']}")
            payload = json.loads(row["payload"])
            sequence = db.execute("SELECT COUNT(*) + 1 AS value FROM issues").fetchone()["value"]
            issue_id = f"ISS-{sequence:04d}"
            db.execute(
                "INSERT INTO issues(id, title, description, priority, status, evidence_refs, created_by, created_at) "
                "VALUES (?, ?, ?, ?, 'open', ?, ?, ?)",
                (issue_id, payload["title"], payload["description"], payload["priority"],
                 json.dumps(payload["evidence_refs"], ensure_ascii=False), row["decided_by"],
                 datetime.now(timezone.utc).isoformat()),
            )
            db.execute(
                "UPDATE approvals SET status = 'consumed', issue_id = ? WHERE id = ?",
                (issue_id, approval_id),
            )
            issue = db.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        return {"created": self._issue_dict(issue), "idempotent_replay": False}

    def list_issues(self, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        if limit < 1 or limit > 200:
            raise WorkItemError("limit must be between 1 and 200")
        sql = "SELECT * FROM issues"
        parameters: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            parameters.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as db:
            rows = db.execute(sql, parameters).fetchall()
        return {"items": [self._issue_dict(row) for row in rows], "count": len(rows)}

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            raise WorkItemError(f"issue not found: {issue_id}")
        return self._issue_dict(row)

    def approval(self, approval_id: str) -> dict[str, Any]:
        import json
        with self._connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise WorkItemError(f"approval not found: {approval_id}")
        return {
            "approval_id": row["id"],
            "action": row["action"],
            "payload": json.loads(row["payload"]),
            "status": row["status"],
            "requested_at": row["requested_at"],
            "expires_at": row["expires_at"],
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
            "issue_id": row["issue_id"],
        }

    def pending_approvals(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id FROM approvals WHERE status = 'pending' ORDER BY requested_at"
            ).fetchall()
        return [self.approval(row["id"]) for row in rows]

    @staticmethod
    def _issue_dict(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise WorkItemError("issue disappeared during transaction")
        import json
        return {
            "id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "priority": row["priority"],
            "status": row["status"],
            "evidence_refs": json.loads(row["evidence_refs"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

