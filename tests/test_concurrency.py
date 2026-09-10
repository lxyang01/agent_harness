from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Callable

from minimal_agent.policy import ApprovalStore, PolicyError, ToolPolicy
from minimal_agent.work_items import WorkItemError, WorkItemStore


def run_threaded(count: int, target: Callable[[], Any]) -> tuple[list[Any], list[Exception]]:
    """Run `target` on `count` threads released simultaneously by a barrier."""
    barrier = threading.Barrier(count)
    results: list[Any] = []
    errors: list[Exception] = []

    def worker() -> None:
        barrier.wait()
        try:
            results.append(target())
        except Exception as exc:  # collected for winner-count assertions
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


class ApprovalStoreRaceTests(unittest.TestCase):
    def test_concurrent_decide_yields_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ApprovalStore(Path(temp))
            approval = store.request("s", "t", 1, "tool.x", {},
                                     ToolPolicy("high_write", True, "race probe"), {})
            results, errors = run_threaded(
                8, lambda: store.decide(approval.id, True, "alice", "note"))
            self.assertEqual(1, len(results), f"winners={len(results)}")
            self.assertEqual(7, len(errors))
            for exc in errors:
                self.assertIsInstance(exc, PolicyError)
            self.assertEqual("approved", store.get(approval.id).status)


class WorkItemStoreRaceTests(unittest.TestCase):
    def test_concurrent_decide_yields_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as temp:
            store = WorkItemStore(Path(temp))
            remote = store.prepare_issue("Fix checkout", "desc", "high")
            results, errors = run_threaded(
                8, lambda: store.decide(remote["approval_id"], True, "alice"))
            self.assertEqual(1, len(results), f"winners={len(results)}")
            self.assertEqual(7, len(errors))
            for exc in errors:
                self.assertIsInstance(exc, WorkItemError)


if __name__ == "__main__":
    unittest.main()
