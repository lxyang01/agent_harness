from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal


RiskLevel = Literal["read", "low_write", "high_write", "forbidden"]
ApprovalStatus = Literal["pending", "approved", "rejected", "executed", "failed"]


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ToolPolicy:
    risk_level: RiskLevel = "read"
    requires_approval: bool = False
    reason: str = "Read-only operation"

    def __post_init__(self) -> None:
        if self.risk_level not in {"read", "low_write", "high_write", "forbidden"}:
            raise PolicyError(f"invalid risk level: {self.risk_level}")
        if self.risk_level == "forbidden" and not self.requires_approval:
            object.__setattr__(self, "requires_approval", True)


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    session_id: str
    trace_id: str
    step: int
    tool_name: str
    arguments: dict[str, Any]
    risk_level: RiskLevel
    reason: str
    status: ApprovalStatus
    checkpoint: dict[str, Any]
    requested_at: str
    decided_at: str | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    executed_at: str | None = None
    execution_error: str | None = None

    def as_dict(self, include_checkpoint: bool = False) -> dict[str, Any]:
        value = {
            "id": self.id,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "step": self.step,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "status": self.status,
            "requested_at": self.requested_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "decision_note": self.decision_note,
            "executed_at": self.executed_at,
            "execution_error": self.execution_error,
        }
        if include_checkpoint:
            value["checkpoint"] = self.checkpoint
        return value


class ApprovalStore:
    """SQLite-backed approval state and resumable Harness checkpoints."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "approvals.db"
        self._initialize()

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
            db.execute("""
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    checkpoint_json TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    decided_at TEXT,
                    decided_by TEXT,
                    decision_note TEXT,
                    executed_at TEXT,
                    execution_error TEXT
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_session_status "
                "ON approvals(session_id, status, requested_at)"
            )

    def request(self, session_id: str, trace_id: str, step: int,
                tool_name: str, arguments: dict[str, Any], policy: ToolPolicy,
                checkpoint: dict[str, Any]) -> ApprovalRequest:
        if policy.risk_level == "forbidden":
            raise PolicyError(f"forbidden tool cannot request approval: {tool_name}")
        approval_id = f"POL-{uuid.uuid4().hex[:12].upper()}"
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                "INSERT INTO approvals(id, session_id, trace_id, step, tool_name, arguments_json, "
                "risk_level, reason, status, checkpoint_json, requested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (approval_id, session_id, trace_id, step, tool_name,
                 json.dumps(arguments, ensure_ascii=False), policy.risk_level, policy.reason,
                 json.dumps(checkpoint, ensure_ascii=False), now),
            )
        return self.get(approval_id)

    def get(self, approval_id: str) -> ApprovalRequest:
        with self._connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise PolicyError(f"approval not found: {approval_id}")
        return self._from_row(row)

    def list(self, session_id: str | None = None,
             statuses: tuple[str, ...] | None = None,
             limit: int = 100) -> list[ApprovalRequest]:
        if limit < 1 or limit > 500:
            raise PolicyError("approval list limit must be between 1 and 500")
        clauses: list[str] = []
        parameters: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            parameters.extend(statuses)
        sql = "SELECT * FROM approvals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY requested_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as db:
            rows = db.execute(sql, parameters).fetchall()
        return [self._from_row(row) for row in rows]

    def decide(self, approval_id: str, approved: bool, decided_by: str,
               note: str = "") -> ApprovalRequest:
        decided_by = decided_by.strip()
        if not decided_by:
            raise PolicyError("decided_by is required")
        with self._connect() as db:
            row = db.execute("SELECT status FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None:
                raise PolicyError(f"approval not found: {approval_id}")
            if row["status"] != "pending":
                raise PolicyError(f"approval is already {row['status']}: {approval_id}")
            status = "approved" if approved else "rejected"
            # 条件 UPDATE 是并发下的唯一裁决者:SELECT 与 UPDATE 之间存在竞态窗口,
            # 恰好一个线程的 rowcount=1,其余在提交前被拒绝。
            cursor = db.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, decision_note = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, datetime.now(timezone.utc).isoformat(), decided_by,
                 note.strip()[:1000], approval_id),
            )
            if cursor.rowcount == 0:
                raise PolicyError(f"approval is already decided: {approval_id}")
        return self.get(approval_id)

    def mark_execution(self, approval_id: str, succeeded: bool,
                       error: str = "") -> ApprovalRequest:
        with self._connect() as db:
            row = db.execute("SELECT status FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None:
                raise PolicyError(f"approval not found: {approval_id}")
            if row["status"] != "approved":
                raise PolicyError(f"only approved requests can be executed: {row['status']}")
            db.execute(
                "UPDATE approvals SET status = ?, executed_at = ?, execution_error = ? WHERE id = ?",
                ("executed" if succeeded else "failed", datetime.now(timezone.utc).isoformat(),
                 error[:4000] if error else None, approval_id),
            )
        return self.get(approval_id)

    def delete_session(self, session_id: str) -> int:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM approvals WHERE session_id = ?", (session_id,))
            return cursor.rowcount

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ApprovalRequest:
        return ApprovalRequest(
            id=row["id"], session_id=row["session_id"], trace_id=row["trace_id"],
            step=row["step"], tool_name=row["tool_name"],
            arguments=json.loads(row["arguments_json"]), risk_level=row["risk_level"],
            reason=row["reason"], status=row["status"],
            checkpoint=json.loads(row["checkpoint_json"]), requested_at=row["requested_at"],
            decided_at=row["decided_at"], decided_by=row["decided_by"],
            decision_note=row["decision_note"], executed_at=row["executed_at"],
            execution_error=row["execution_error"],
        )


class PolicyGateway:
    def __init__(self, store: ApprovalStore) -> None:
        self.store = store

    @staticmethod
    def enforce(policy: ToolPolicy, tool_name: str) -> Literal["allow", "approval_required"]:
        if policy.risk_level == "forbidden":
            raise PolicyError(f"tool is forbidden by policy: {tool_name}")
        if policy.requires_approval or policy.risk_level == "high_write":
            return "approval_required"
        return "allow"

