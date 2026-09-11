from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .policy import PolicyError
from .session import SessionStore


class TraceStore:
    """Read-only projection over append-only Harness JSONL traces."""

    def __init__(self, session_root: str | Path) -> None:
        self.session_root = Path(session_root)
        self.trace_root = self.session_root / "traces"

    def list_runs(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("run list limit must be between 1 and 200")
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in self._read_events(session_id):
            trace_id = str(event.get("trace_id", ""))
            if trace_id:
                groups[trace_id].append(event)
        runs = [self._summary(trace_id, events) for trace_id, events in groups.items()]
        runs.sort(key=lambda item: item["started_at"], reverse=True)
        return runs[:limit]

    def get_run(self, session_id: str, trace_id: str) -> dict[str, Any]:
        trace_id = trace_id.strip()
        if not trace_id:
            raise ValueError("trace_id is required")
        events = [
            event for event in self._read_events(session_id)
            if event.get("trace_id") == trace_id
        ]
        if not events:
            raise ValueError(f"run not found: {trace_id}")
        return {"summary": self._summary(trace_id, events), "events": events}

    def _read_events(self, session_id: str) -> list[dict[str, Any]]:
        path = self.trace_root / f"{SessionStore._key(session_id)}.jsonl"
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        events.append({
                            "timestamp": "", "event": "trace_parse_error",
                            "line": line_number, "error": "invalid JSONL record",
                        })
                        continue
                    if isinstance(value, dict):
                        value.setdefault("session_id", session_id)
                        events.append(value)
        except OSError as exc:
            raise PolicyError(f"cannot read trace: {exc}") from exc
        return events

    @classmethod
    def _summary(cls, trace_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        ordered = sorted(events, key=lambda item: str(item.get("timestamp", "")))
        started_at = str(ordered[0].get("timestamp", "")) if ordered else ""
        ended_at = str(ordered[-1].get("timestamp", "")) if ordered else ""
        status = "running"
        for event in ordered:
            name = event.get("event")
            if name == "approval_pending":
                status = "approval_pending"
            elif name == "approval_rejected":
                status = "rejected"
            elif name in {"run_error", "max_steps"}:
                status = "failed"
            elif name == "run_end":
                status = str(event.get("status", "completed"))

        skills = list(dict.fromkeys(
            str(event.get("skill")) for event in ordered
            if event.get("event") == "skill_activated" and event.get("skill")
        ))
        tools = [
            str(event.get("tool")) for event in ordered
            if event.get("event") == "tool_start" and event.get("tool")
        ]
        active_ms = round(sum(
            float(event.get("latency_ms", 0) or 0) for event in ordered
            if event.get("event") in {"model_output", "tool_end", "tool_error"}
        ), 2)
        token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        cost = 0.0
        for event in ordered:
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            for key in token_usage:
                token_usage[key] += int(usage.get(key, 0) or 0)
            cost += float(usage.get("cost", 0) or 0)
        return {
            "trace_id": trace_id,
            "session_id": next((str(item.get("session_id")) for item in ordered
                                if item.get("session_id")), ""),
            "agent": next((str(item.get("agent")) for item in ordered if item.get("agent")), ""),
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "wall_time_ms": cls._duration_ms(started_at, ended_at),
            "active_time_ms": active_ms,
            "steps": max((int(item.get("step", 0) or 0) for item in ordered), default=0),
            "model_calls": sum(item.get("event") == "model_start" for item in ordered),
            "tool_calls": len(tools),
            "tools": tools,
            "skills": skills,
            "completion_blocks": sum(item.get("event") == "completion_blocked" for item in ordered),
            "resumed": any(item.get("event") == "run_resume" for item in ordered),
            "token_usage": token_usage,
            "cost": round(cost, 8),
        }

    @staticmethod
    def _duration_ms(started_at: str, ended_at: str) -> float:
        if not started_at or not ended_at:
            return 0.0
        try:
            return round(max(0.0, (
                datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)
            ).total_seconds() * 1000), 2)
        except ValueError:
            return 0.0
