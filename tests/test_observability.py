from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from minimal_agent.observability import TraceStore
from minimal_agent.session import SessionStore


class TraceStoreTests(unittest.TestCase):
    def test_groups_events_and_aggregates_latency_usage_and_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trace_dir = root / "traces"
            trace_dir.mkdir()
            path = trace_dir / f"{SessionStore._key('session-a')}.jsonl"
            events = [
                {"timestamp": "2026-08-05T00:00:00+00:00", "event": "run_start", "trace_id": "trace-a", "step": 0},
                {"timestamp": "2026-08-05T00:00:00.010000+00:00", "event": "skill_activated", "trace_id": "trace-a", "step": 0, "skill": "anomaly-investigation"},
                {"timestamp": "2026-08-05T00:00:00.110000+00:00", "event": "model_output", "trace_id": "trace-a", "step": 1, "latency_ms": 100, "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50, "cost": 0.001}},
                {"timestamp": "2026-08-05T00:00:00.120000+00:00", "event": "tool_start", "trace_id": "trace-a", "step": 1, "tool": "feedback.compare_periods"},
                {"timestamp": "2026-08-05T00:00:00.145000+00:00", "event": "tool_end", "trace_id": "trace-a", "step": 1, "tool": "feedback.compare_periods", "latency_ms": 25},
                {"timestamp": "2026-08-05T00:00:00.200000+00:00", "event": "approval_pending", "trace_id": "trace-a", "step": 2},
                {"timestamp": "2026-08-05T00:01:00+00:00", "event": "run_resume", "trace_id": "trace-a", "step": 2},
                {"timestamp": "2026-08-05T00:01:00.100000+00:00", "event": "run_end", "trace_id": "trace-a", "step": 3, "status": "completed"},
            ]
            path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")

            store = TraceStore(root)
            runs = store.list_runs("session-a")
            self.assertEqual(1, len(runs))
            run = runs[0]
            self.assertEqual("completed", run["status"])
            self.assertTrue(run["resumed"])
            self.assertEqual(["anomaly-investigation"], run["skills"])
            self.assertEqual(["feedback.compare_periods"], run["tools"])
            self.assertEqual(125, run["active_time_ms"])
            self.assertEqual(60_100, run["wall_time_ms"])
            self.assertEqual(50, run["token_usage"]["total_tokens"])
            self.assertEqual(0.001, run["cost"])

            detail = store.get_run("session-a", "trace-a")
            self.assertEqual(8, len(detail["events"]))
            self.assertTrue(all(item["session_id"] == "session-a" for item in detail["events"]))

    def test_missing_and_invalid_trace_records_fail_safely(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual([], TraceStore(root).list_runs("missing"))
            with self.assertRaises(ValueError):
                TraceStore(root).get_run("missing", "trace")

            trace_dir = root / "traces"
            trace_dir.mkdir()
            path = trace_dir / f"{SessionStore._key('broken')}.jsonl"
            path.write_text("not-json\n", encoding="utf-8")
            self.assertEqual([], TraceStore(root).list_runs("broken"))


if __name__ == "__main__":
    unittest.main()
