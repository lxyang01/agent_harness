# tests/test_storage_pg.py — PG 存储层测试;连接 compose PostgreSQL(127.0.0.1:5433)。
# 断言体移植自单进程套件:test_bills.py / test_auth.py / test_concurrency.py /
# test_mcp.py / test_session_owner.py / test_observability.py,构造参数换成 PG 版。
from __future__ import annotations

import hashlib
import threading
import unittest
from typing import Any, Callable

from psycopg_pool import ConnectionPool

from billguard.auth import AuthError
from billguard.bills import BillFilters, BillService
from billguard.policy import PolicyError, ToolPolicy
from billguard.types import Message, Session
from billguard.work_items import WorkItemError

from billguard.storage_pg import (
    PGApprovalStore,
    PGBillService,
    PGEvidenceStore,
    PGSessionStore,
    PGTraceStore,
    PGUserStore,
    PGWorkItemStore,
    new_pg_pool,
)

DSN = "postgresql://billguard:billguard@127.0.0.1:5433/billguard"

TABLES = ("tx_audits", "transactions", "categories", "subscriptions", "imports",
          "reports", "approvals", "wi_approvals", "issues", "sessions",
          "evidence", "traces", "users")


def pool() -> ConnectionPool:
    return ConnectionPool(DSN, min_size=1, max_size=4, open=True)


def clean(pool: ConnectionPool) -> None:
    with pool.connection() as db:
        for table in TABLES:
            db.execute(f"DELETE FROM {table}")


def demo_csv() -> str:
    # 移植自 tests/test_bills.py 的 DEMO 数据
    return (
        "tx_id,paid_at,merchant,category,amount,method,note\n"
        "TX-001,2026-07-05 10:00:00,饿了么,餐饮,35.5,支付宝,午餐\n"
        "TX-002,2026-07-05 21:00:00,腾讯视频,订阅,15.0,微信,月费\n"
        "TX-003,2026-08-05 21:00:00,腾讯视频,订阅,25.0,微信,月费\n"
        "TX-004,2026-08-06 10:00:00,百度网盘,订阅,18.0,支付宝,月费\n"
        "TX-005,2026-08-06 10:05:00,百度网盘,订阅,18.0,支付宝,月费,重复\n"
    )


def run_threaded(count: int, target: Callable[[], Any]) -> tuple[list[Any], list[Exception]]:
    """移植自 tests/test_concurrency.py:barrier 同时放行 count 个线程。"""
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


class PGTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = pool()
        clean(self.pool)
        self.addCleanup(self.pool.close)

    def store_pool(self) -> ConnectionPool:
        return self.pool


class NewPoolTests(PGTestCase):
    def test_new_pg_pool_sizes_and_lifecycle(self):
        with new_pg_pool(DSN) as fresh:
            self.assertEqual(2, fresh.min_size)
            self.assertEqual(8, fresh.max_size)
            with fresh.connection() as db:
                self.assertEqual(1, db.execute("SELECT 1").fetchone()[0])


class UserStoreTests(PGTestCase):
    # 与单进程 test_auth 同语义:建号→verify→set_disabled→verify 失败
    def test_create_verify_and_disable(self):
        users = PGUserStore(self.store_pool())
        created = users.create("alice", "alice-pass-123", "user")
        self.assertEqual("alice", created.username)
        self.assertEqual("user", created.role)
        self.assertEqual(1, users.count())
        self.assertEqual(users.get("alice"), created)
        verified = users.verify("alice", "alice-pass-123")
        self.assertEqual("user", verified.role)
        with self.assertRaises(AuthError):
            users.verify("alice", "wrong-pass-123")
        with self.assertRaises(AuthError):
            users.verify("nobody", "alice-pass-123")
        with self.assertRaises(AuthError):  # 重复建号
            users.create("alice", "other-pass-123", "user")
        self.assertTrue(users.set_disabled("alice", True).disabled)
        with self.assertRaises(AuthError):  # 禁用用户登录失败
            users.verify("alice", "alice-pass-123")
        users.set_disabled("alice", False)
        self.assertEqual("user", users.verify("alice", "alice-pass-123").role)
        # 密码重置后旧密码失效(移植自 test_auth.py test_reset_password)
        users.reset_password("alice", "new-pass-12345")
        with self.assertRaises(AuthError):
            users.verify("alice", "alice-pass-123")
        self.assertEqual("user", users.verify("alice", "new-pass-12345").role)

    def test_last_admin_guard(self):
        # 移植自 test_auth.py test_set_role_and_last_admin_guard / test_disable_guards
        users = PGUserStore(self.store_pool())
        users.create("root", "root-pass-1234", "admin")
        users.create("alice", "alice-pass-123", "admin")
        self.assertEqual("user", users.set_role("alice", "user").role)
        with self.assertRaises(AuthError):  # 最后一个启用中的 admin 不可降级
            users.set_role("root", "user")
        users.create("bob", "bob-pass-1234", "admin")
        users.set_role("root", "user")  # 有其他 admin 时允许
        with self.assertRaises(AuthError):  # 最后一个启用的 admin 不可禁用
            users.set_disabled("bob", True)


class BillServiceTests(PGTestCase):
    def test_public_api_parity(self):
        # PGBillService 必须是 BillService 的全量替换(供 Task 3 直接换入)
        single = {name for name in dir(BillService) if not name.startswith("_")}
        ported = {name for name in dir(PGBillService) if not name.startswith("_")}
        self.assertLessEqual(single, ported)

    def test_import_isolation_and_anomalies(self):
        # DEMO 导入 alice → bob 不可见 → for_user("alice").anomalies 检出 price_hike
        service = PGBillService(self.store_pool())
        alice = service.for_user("alice")
        result = alice.import_bills("demo.csv", demo_csv())
        self.assertEqual(5, result["imported_rows"])
        result2 = alice.import_bills("demo.csv", demo_csv())
        self.assertEqual(0, result2["imported_rows"])
        self.assertEqual(5, result2["duplicate_rows"])
        overview = alice.overview()
        self.assertEqual(111.5, round(overview["total_amount"], 2))
        self.assertEqual(5, overview["count"])
        self.assertIsInstance(overview["total_amount"], float)  # NUMERIC 读取边界已转 float

        # bob 视野隔离:空数据,看不到 alice 的任何行
        bob = service.for_user("bob")
        bob_overview = bob.overview()
        self.assertEqual(0, bob_overview["count"])
        self.assertEqual(0.0, bob_overview["total_amount"])
        self.assertEqual([], bob.query()["items"])

        # alice 订阅导入 + 涨价检测(移植自 test_bills.py SubscriptionTests)
        sub_result = alice.import_subscriptions(
            "subs.csv", "name,merchant,cycle,expected_amount\n腾讯视频,腾讯视频,月,15.0\n")
        self.assertEqual(1, sub_result["imported_rows"])
        self.assertEqual(1, len(alice.subscriptions()))
        anomalies = alice.anomalies(62, "price_hike", 10)
        # 腾讯视频预期 15,实扣 25 → 涨价异常
        self.assertTrue(any(item["name"] == "腾讯视频" for item in anomalies["items"]))

        # 工作流更新写审计(移植自 test_bills.py WorkflowAndMaskTests)
        updated = alice.update_workflow(["TX-001"], "alice", status="待核查", note="查一下")
        self.assertEqual(1, updated["count"])
        audits = alice.transaction_audits("TX-001")
        self.assertEqual("alice", audits[-1]["operator"])


class OverviewCoverageTests(PGTestCase):
    # 移植自 tests/test_bills.py OverviewCoverageTests:覆盖区间随过滤集走
    def test_overview_reports_data_coverage(self):
        service = PGBillService(self.store_pool())
        alice = service.for_user("alice")
        alice.import_bills("demo.csv", demo_csv())
        # 全量:demo 数据跨 2026-07-05..2026-08-06
        overview = alice.overview()
        self.assertEqual("2026-07-05", overview["data_from"])
        self.assertEqual("2026-08-06", overview["data_to"])
        # 非空过滤集:区间必须来自过滤后的行,而非全库
        july = alice.overview(BillFilters(date_from="2026-07-01", date_to="2026-07-31"))
        self.assertEqual(2, july["count"])
        self.assertEqual("2026-07-05", july["data_from"])
        self.assertEqual("2026-07-05", july["data_to"])

    def test_overview_coverage_none_when_no_rows(self):
        service = PGBillService(self.store_pool())
        alice = service.for_user("alice")
        alice.import_bills("demo.csv", demo_csv())
        # 过滤后为空(如 9 月无数据):无覆盖区间可报
        empty = alice.overview(BillFilters(date_from="2026-09-01", date_to="2026-09-30"))
        self.assertEqual(0, empty["count"])
        self.assertIsNone(empty["data_from"])
        self.assertIsNone(empty["data_to"])
        # bob 无任何数据:全量 overview 也无覆盖区间
        self.assertIsNone(service.for_user("bob").overview()["data_from"])


class ApprovalStoreTests(PGTestCase):
    def test_concurrent_decide_exactly_one_winner(self):
        # 移植自 tests/test_concurrency.py ApprovalStoreRaceTests
        store = PGApprovalStore(self.store_pool())
        approval = store.request("s", "t", 1, "tool.x", {},
                                 ToolPolicy("high_write", True, "race probe"), {})
        results, errors = run_threaded(
            8, lambda: store.decide(approval.id, True, "alice", "note"))
        self.assertEqual(1, len(results), f"winners={len(results)}")
        self.assertEqual(7, len(errors))
        for exc in errors:
            self.assertIsInstance(exc, PolicyError)
        self.assertEqual("approved", store.get(approval.id).status)

    def test_request_get_list_and_execution_lifecycle(self):
        store = PGApprovalStore(self.store_pool())
        approval = store.request("s1", "t1", 2, "tool.y", {"k": [1, 2]},
                                 ToolPolicy("low_write", False, "probe"), {"ckpt": {"a": 1}})
        self.assertEqual("pending", approval.status)
        self.assertEqual({"k": [1, 2]}, store.get(approval.id).arguments)  # JSONB 往返
        self.assertEqual({"ckpt": {"a": 1}}, store.get(approval.id).checkpoint)
        self.assertEqual([approval.id], [item.id for item in store.list("s1")])
        decided = store.decide(approval.id, True, "reviewer", "ok")
        self.assertEqual("approved", decided.status)
        self.assertEqual("reviewer", decided.decided_by)
        executed = store.mark_execution(approval.id, True)
        self.assertEqual("executed", executed.status)
        self.assertEqual(1, store.delete_session("s1"))
        self.assertEqual([], store.list("s1"))


class WorkItemStoreTests(PGTestCase):
    def test_prepare_decide_commit_idempotent(self):
        # 移植自 tests/test_mcp.py 人审在工具通道外的 prepare→decide→commit 流程
        store = PGWorkItemStore(self.store_pool())
        prepared = store.prepare_issue("排查支付扣款失败", "根据反馈样本确认支付失败但已扣款。",
                                       "high", ["MCP-001"])
        approval_id = prepared["approval_id"]
        self.assertEqual("pending", prepared["status"])
        self.assertEqual("issue.create", prepared["action"])
        self.assertEqual(["MCP-001"], prepared["payload"]["evidence_refs"])
        self.assertIn(approval_id, [item["approval_id"]
                                    for item in store.pending_approvals()])
        with self.assertRaises(WorkItemError):  # 未审批先 commit 被拒
            store.commit_issue(approval_id)
        store.decide(approval_id, True, "product-owner")
        created = store.commit_issue(approval_id)
        self.assertFalse(created["idempotent_replay"])
        self.assertTrue(created["created"]["id"].startswith("ISS-"))
        self.assertEqual("product-owner", created["created"]["created_by"])
        self.assertEqual("open", created["created"]["status"])
        self.assertEqual(["MCP-001"], created["created"]["evidence_refs"])

        replay = store.commit_issue(approval_id)  # 幂等重放不重复建单
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(created["created"]["id"], replay["created"]["id"])
        listed = store.list_issues()
        self.assertEqual(1, listed["count"])
        self.assertEqual(created["created"]["id"], listed["items"][0]["id"])
        # commit 后审批单进入 consumed(与单进程版一致),issue_id 回指所建工单
        final = store.approval(approval_id)
        self.assertEqual("consumed", final["status"])
        self.assertEqual(created["created"]["id"], final["issue_id"])


class SessionStoreTests(PGTestCase):
    def test_save_load_roundtrip_with_owner(self):
        # 移植自 tests/test_session_owner.py
        store = PGSessionStore(self.store_pool())
        self.assertIsNone(store.load("s1").owner)  # 不存在 → 空会话,owner=None
        store.save(Session("s1", [Message("user", "hi")], owner="alice"))
        loaded = store.load("s1")
        self.assertEqual("alice", loaded.owner)
        self.assertEqual([Message("user", "hi")], loaded.messages)
        # 覆盖保存(替换语义,与原子文件写一致)
        store.save(Session("s1", [Message("user", "hi"), Message("assistant", "hello")],
                           summary="两点", owner="alice"))
        again = store.load("s1")
        self.assertEqual(2, len(again.messages))
        self.assertEqual("两点", again.summary)
        # 组合键寻址与删除
        key = store._key("s1")
        self.assertEqual(hashlib.sha256(b"s1").hexdigest(), key)
        self.assertEqual(("s1",), store._path("s1"))
        store.delete("s1")
        self.assertEqual([], store.load("s1").messages)


class EvidenceTraceTests(PGTestCase):
    def test_evidence_roundtrip_merges_by_answer(self):
        # 语义移植自 web.py _load_evidence/_save_evidence:按 answer-hash 键合并
        store = PGEvidenceStore(self.store_pool())
        self.assertEqual({}, store.load("s1"))  # 不存在 → 空
        store.save("s1", "answer-1", [{"label": "饿了么", "description": "¥35.5"}])
        store.save("s1", "answer-2", [{"label": "腾讯视频", "description": "¥25"}])
        value = store.load("s1")
        self.assertEqual(2, len(value))
        expected_key = hashlib.sha256(b"answer-1").hexdigest()
        self.assertIn(expected_key, value)
        self.assertEqual([{"label": "饿了么", "description": "¥35.5"}], value[expected_key])
        store.save("s1", "answer-1", [{"label": "新证据"}])  # 同 answer 覆盖
        self.assertEqual([{"label": "新证据"}], store.load("s1")[expected_key])
        self.assertEqual(2, len(store.load("s1")))  # 其他键保留(合并语义)
        store.save("s1", "answer-3", [])  # 空证据不落盘(移植 _save_evidence no-op)
        self.assertEqual(2, len(store.load("s1")))

    def test_trace_roundtrip_summarizes_runs(self):
        # 事件与断言移植自 tests/test_observability.py TraceStoreTests
        store = PGTraceStore(self.store_pool())
        events = [
            {"timestamp": "2026-08-05T00:00:00+00:00", "event": "run_start", "trace_id": "trace-a", "step": 0},
            {"timestamp": "2026-08-05T00:00:00.010000+00:00", "event": "skill_activated", "trace_id": "trace-a", "step": 0, "skill": "anomaly-investigation"},
            {"timestamp": "2026-08-05T00:00:00.110000+00:00", "event": "model_output", "trace_id": "trace-a", "step": 1, "latency_ms": 100, "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50, "cost": 0.001}},
            {"timestamp": "2026-08-05T00:00:00.120000+00:00", "event": "tool_start", "trace_id": "trace-a", "step": 1, "tool": "bill.compare_periods"},
            {"timestamp": "2026-08-05T00:00:00.145000+00:00", "event": "tool_end", "trace_id": "trace-a", "step": 1, "tool": "bill.compare_periods", "latency_ms": 25},
            {"timestamp": "2026-08-05T00:00:00.200000+00:00", "event": "approval_pending", "trace_id": "trace-a", "step": 2},
            {"timestamp": "2026-08-05T00:01:00+00:00", "event": "run_resume", "trace_id": "trace-a", "step": 2},
            {"timestamp": "2026-08-05T00:01:00.100000+00:00", "event": "run_end", "trace_id": "trace-a", "step": 3, "status": "completed"},
        ]
        for event in events:
            store.append_event("session-a", "trace-a", "bill-triage", event)

        runs = store.list_runs("session-a")
        self.assertEqual(1, len(runs))
        run = runs[0]
        self.assertEqual("trace-a", run["trace_id"])
        self.assertEqual("completed", run["status"])
        self.assertTrue(run["resumed"])
        self.assertEqual(["anomaly-investigation"], run["skills"])
        self.assertEqual(["bill.compare_periods"], run["tools"])
        self.assertEqual(125, run["active_time_ms"])
        self.assertEqual(60_100, run["wall_time_ms"])
        self.assertEqual(50, run["token_usage"]["total_tokens"])
        self.assertEqual(0.001, run["cost"])

        detail = store.get_run("session-a", "trace-a")
        self.assertEqual(8, len(detail["events"]))
        self.assertTrue(all(item["session_id"] == "session-a" for item in detail["events"]))

        # 缺失与非法入参(移植自 test_missing_and_invalid_trace_records_fail_safely)
        self.assertEqual([], store.list_runs("missing"))
        with self.assertRaises(ValueError):
            store.get_run("missing", "trace")
        with self.assertRaises(ValueError):
            store.list_runs("session-a", 0)


if __name__ == "__main__":
    unittest.main()
