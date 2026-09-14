from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from billguard.auth import User
from billguard.policy import ApprovalStore, PolicyError, PolicyGateway, ToolPolicy
from billguard.session import SessionStore
from billguard.tools import Tool, ToolRegistry
from billguard.web import BusyError, BillGuardApp
from billguard.work_items import WorkItemError, WorkItemStore


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


class _SlowScriptedLLM:
    """第 1 次调用发起 commit_issue(触发暂停);第 2 次调用睡眠后给最终答案;
    第 3 次调用(审批恢复)再给一个最终答案。睡眠前设置 event 便于主线程对齐。"""

    def __init__(self, remote_approval_id: str, in_flight: threading.Event) -> None:
        self.remote_approval_id = remote_approval_id
        self.in_flight = in_flight
        self.calls = 0

    def complete(self, messages, tools) -> str:
        self.calls += 1
        if self.calls == 1:
            return json.dumps({"thought": "commit", "tool_call": {
                "name": "work-items.commit_issue",
                "arguments": {"approval_id": self.remote_approval_id}}}, ensure_ascii=False)
        if self.calls == 2:
            self.in_flight.set()
            time.sleep(0.4)
            return json.dumps({"thought": "done", "final": "慢速回答"}, ensure_ascii=False)
        return json.dumps({"thought": "done", "final": "审批后回答"}, ensure_ascii=False)


class _CommitManager:
    def snapshots(self):
        return [SimpleNamespace(name="work-items", transport="test", server_name="t",
                                server_version="t", protocol_version="t",
                                tools=(), resources=(), prompts=())]

    def register_tools(self, registry: ToolRegistry, server_name: str, store=None):
        registry.register(Tool(
            "work-items.commit_issue", "Commit approved issue",
            {"type": "object", "properties": {"approval_id": {"type": "string"}},
             "required": ["approval_id"], "additionalProperties": False},
            store.commit_issue,
            policy=ToolPolicy("high_write", True, "Creates durable work item")))
        # 默认 skill(bill-triage)只信任账单读工具;注册一个同名只读工具,
        # 让普通消息的 skill 白名单与注册表有交集,模型调用才能发生。
        registry.register(Tool(
            "bill_overview", "Overview placeholder",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            lambda: {}))
        return registry.names()


class SessionLockRaceTests(unittest.TestCase):
    def test_concurrent_chat_and_approval_do_not_lose_session_updates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_items = WorkItemStore(root / "work-items")
            remote = work_items.prepare_issue("Fix checkout", "desc", "high")
            in_flight = threading.Event()
            llm = _SlowScriptedLLM(remote["approval_id"], in_flight)
            gateway = PolicyGateway(ApprovalStore(root / "web" / "policy"))

            manager = _CommitManager()
            manager_store = work_items
            original_register = manager.register_tools
            manager.register_tools = lambda registry, name: original_register(
                registry, name, store=manager_store)

            app = BillGuardApp(root / "web", root / "docs", llm, manager,
                               gateway, work_items)
            alice = User("alice", "user")

            # 准备:第一次 chat 触发 high_write 暂停,产生 pending 审批
            paused = app.chat(alice, "s", "$monthly-guard-report create issue")
            self.assertEqual("approval_pending", paused["status"])

            # 并发:慢速 chat 与审批恢复同时在同一 session 上运行
            chat_result: dict[str, Any] = {}

            def run_chat() -> None:
                try:
                    # "hi" 不触发隐式 Skill 路由,避免 fake 注册表与 skill 白名单冲突
                    chat_result["response"] = app.chat(alice, "s", "hi")
                except Exception as exc:  # 记录异常便于失败诊断
                    chat_result["error"] = exc

            thread = threading.Thread(target=run_chat)
            thread.start()
            self.assertTrue(in_flight.wait(timeout=5))  # chat 已持锁、模型睡眠中
            decision = app.decide_approval(alice, "s", {
                "approval_id": paused["approval"]["id"], "decision": "approve"})
            thread.join(timeout=10)

            self.assertIsNone(chat_result.get("error"))
            self.assertEqual("completed", chat_result["response"]["status"])
            self.assertEqual("completed", decision["status"])
            session = SessionStore(root / "web" / "billguard" / "sessions").load("s")
            contents = [message.content for message in session.messages]
            self.assertTrue(any("慢速回答" in content for content in contents),
                            f"chat 结果丢失: {contents}")
            self.assertTrue(any("审批后回答" in content for content in contents),
                            f"审批恢复结果丢失: {contents}")


class _BlockingLLM:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, messages, tools) -> str:
        self.started.set()
        self.release.wait(timeout=5)
        return json.dumps({"thought": "done", "final": "ok"}, ensure_ascii=False)


class _BillReadManager:
    """只注册一个 bill.* 只读工具,让默认 skill(bill-triage)的白名单与
    fake 注册表相交,本地演示链路的模型调用才能发生。"""

    def snapshots(self):
        return [SimpleNamespace(name="bill", transport="test", server_name="t",
                                server_version="t", protocol_version="t",
                                tools=(), resources=(), prompts=())]

    def register_tools(self, registry: ToolRegistry, server_name: str):
        registry.register(Tool(
            "bill.aggregate", "Aggregate bills",
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            lambda: {"count": 0, "total_amount": 0}))
        return registry.names()


class LlmSemaphoreTests(unittest.TestCase):
    def test_over_limit_chat_gets_busy_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            llm = _BlockingLLM()
            app = BillGuardApp(root / "web", root / "docs", llm, _BillReadManager(),
                               max_concurrent_llm=1)
            alice = User("alice", "user")
            result: dict[str, Any] = {}

            def run_chat() -> None:
                try:
                    result["response"] = app.chat(alice, "s1", "hi")
                except Exception as exc:
                    result["error"] = exc

            thread = threading.Thread(target=run_chat)
            thread.start()
            self.assertTrue(llm.started.wait(timeout=5))  # 独占槽位的模型调用进行中
            with self.assertRaises(BusyError):
                app.chat(alice, "s2", "hi")  # 不同 session,不与会话锁冲突
            llm.release.set()
            thread.join(timeout=10)
            self.assertIsNone(result.get("error"))
            self.assertEqual("completed", result["response"]["status"])


if __name__ == "__main__":
    unittest.main()


class TaskFileAtomicityTests(unittest.TestCase):
    def test_concurrent_task_writes_never_expose_partial_file(self):
        from billguard.tools import TaskService
        with tempfile.TemporaryDirectory() as temp:
            tasks = TaskService(Path(temp))
            stop = threading.Event()
            errors: list[Exception] = []

            def writer() -> None:
                index = 0
                while not stop.is_set() and index < 60:
                    try:
                        tasks.create("s1", f"task-{index}")
                    except Exception as exc:
                        errors.append(exc)
                        return
                    index += 1

            def reader() -> None:
                while not stop.is_set():
                    try:
                        tasks.list("s1")
                    except Exception as exc:
                        errors.append(exc)
                        return

            writers = [threading.Thread(target=writer) for _ in range(3)]
            readers = [threading.Thread(target=reader) for _ in range(3)]
            for thread in writers + readers:
                thread.start()
            for thread in writers:
                thread.join(timeout=15)
            stop.set()
            for thread in readers:
                thread.join(timeout=5)
            self.assertEqual([], errors)
            self.assertGreaterEqual(len(tasks.list("s1")["tasks"]), 1)
