from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from billguard.adversarial_evaluation import (
    _AuthFakeManager,
    AdversarialEvaluator,
    QueueLLM,
    save_adversarial_report,
)
from billguard.auth import User
from billguard.storage_pg import (
    PGBillService, PGEvidenceStore, PGSessionStore, PGTraceStore,
    PGUserStore, PGWorkItemStore,
)

from tests.conftest import PG_DSN, TABLES, pg_pool, redis_client


class AdversarialEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 评测后端钉在测试库:模块缺省 DSN 指向演示库 billguard(供
        # adversarial_eval 独立跑演示集群),测试里必须显式覆写为
        # conftest.PG_DSN —— 否则 run() 清理的是演示库、断言查的是测试库。
        cls._prev_dsn = os.environ.get("BILLGUARD_PG_DSN")
        os.environ["BILLGUARD_PG_DSN"] = PG_DSN
        cls.report = AdversarialEvaluator().run()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._prev_dsn is None:
            os.environ.pop("BILLGUARD_PG_DSN", None)
        else:
            os.environ["BILLGUARD_PG_DSN"] = cls._prev_dsn

    def test_case_registry_loads(self):
        cases = AdversarialEvaluator().cases()
        self.assertEqual(24, len(cases))
        self.assertEqual([f"adv-{index:03d}" for index in range(1, 25)],
                         [case.id for case in cases])
        self.assertEqual("billguard-adversarial-v1", AdversarialEvaluator.BENCHMARK)
        self.assertTrue(all(callable(case.probe) for case in cases))

    def test_after_run_zero_residue(self):
        # 评测自带起止夹具清理:12 张业务表零行、夹具用户(alice/mallory)
        # 不存在、Redis 无会话锁/LLM 限流键(自清理契约,可背靠背重跑)。
        # adv-024 额外自清登录失败计数与对照登录令牌:login:fail:* /
        # auth:token:* / auth:user:* 同样零残留
        report = AdversarialEvaluator().run()
        self.assertEqual(24, report["metrics"]["passed"])
        pool = pg_pool()
        try:
            with pool.connection() as db:
                for table in TABLES:
                    if table == "users":
                        names = [row[0] for row in db.execute("SELECT username FROM users")]
                        self.assertEqual([], [name for name in names
                                              if name in ("alice", "mallory")])
                    else:
                        self.assertEqual(0, db.execute(
                            f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            pool.close()
        client = redis_client()
        try:
            keys = [key for pattern in ("lock:session:*", "llm:slots",
                                        "login:fail:*", "auth:token:*", "auth:user:*")
                    for key in client.scan_iter(match=pattern)]
            self.assertEqual([], keys)
        finally:
            client.close()

    def test_probe_setup_builds_against_pg(self):
        # 代表性探针(adv-019/021/022 共用)的分布式装配对 PG/Redis 真实构建:
        # PG 七件套存储 + Redis 会话锁/登录会话,一次 chat 端到端跑通
        evaluator = AdversarialEvaluator()
        with evaluator.backend():
            evaluator._reset_fixtures()
            users = PGUserStore(evaluator._pool)
            evaluator._ensure_user(users, "alice", "probe-pass-123", "user")
            work_items = PGWorkItemStore(evaluator._pool)
            app = evaluator._distributed_app(
                Path(tempfile.mkdtemp()),
                QueueLLM([{"thought": "done", "final": "好的,已处理。"}]),
                _AuthFakeManager(work_items), work_items, users,
            )
            self.assertIsInstance(app.bills, PGBillService)
            self.assertIsInstance(app.session_store, PGSessionStore)
            self.assertIsInstance(app.evidence_store, PGEvidenceStore)
            self.assertIsInstance(app.trace_store, PGTraceStore)
            self.assertIsNotNone(app._redis_client)
            result = app.chat(User("alice", "user"), "probe-setup", "总结问题")
            self.assertEqual("completed", result["status"])
            self.assertTrue(PGSessionStore(evaluator._pool).exists("probe-setup"))
            evaluator._reset_fixtures()  # 本用例自清理,零残留

    def test_fixed_attack_surface_and_honest_known_gaps(self):
        self.assertEqual("billguard-adversarial-v1", self.report["benchmark"])
        self.assertEqual(24, self.report["dataset_size"])
        self.assertEqual(0, self.report["metrics"]["probe_errors"])
        results = {item["id"]: item for item in self.report["results"]}
        self.assertTrue(results["adv-003"]["passed"])
        self.assertTrue(results["adv-010"]["passed"])
        self.assertTrue(results["adv-014"]["passed"])
        self.assertTrue(results["adv-020"]["passed"])
        self.assertTrue(results["adv-015"]["passed"])
        self.assertTrue(results["adv-016"]["passed"])
        self.assertTrue(results["adv-017"]["passed"])
        self.assertTrue(results["adv-018"]["passed"])
        self.assertTrue(results["adv-019"]["passed"])
        self.assertTrue(results["adv-021"]["passed"])
        self.assertTrue(results["adv-022"]["passed"])
        self.assertTrue(results["adv-023"]["passed"])
        self.assertTrue(results["adv-024"]["passed"])
        self.assertEqual(24, self.report["metrics"]["passed"])
        self.assertEqual(0, self.report["metrics"]["failed"])

    def test_metrics_match_results(self):
        passed = sum(item["passed"] for item in self.report["results"])
        self.assertEqual(passed, self.report["metrics"]["passed"])
        self.assertEqual(24 - passed, self.report["metrics"]["failed"])
        self.assertAlmostEqual(passed / 24, self.report["metrics"]["defense_rate"])

    def test_cross_tenant_leak_probe_evidence(self):
        result = next(item for item in self.report["results"]
                      if item["id"] == "adv-022")
        self.assertEqual("isolation", result["category"])
        self.assertEqual("critical", result["severity"])
        # 到达数据层的 owner 是注入的登录身份,伪造 owner/_owner 均未生效
        self.assertEqual(["mallory"], result["evidence"]["owner_reaching_service"])
        self.assertEqual([{"owner": "mallory"}], result["evidence"]["handler_arguments"])
        # mallory 聚合为空视图,alice 自己的数据不受影响
        self.assertEqual(0, result["evidence"]["mallory_aggregate_count"])
        self.assertEqual(1, result["evidence"]["alice_own_count"])

    def test_report_contains_failures_and_reproduction_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = save_adversarial_report(
                self.report, root / "evaluations", root / "canonical.md",
            )
            markdown = Path(paths["canonical_markdown"]).read_text(encoding="utf-8")
            self.assertIn("对抗评测与失败案例报告", markdown)
            self.assertIn("adv-015", markdown)
            self.assertIn("python -m billguard.adversarial_eval", markdown)
            self.assertTrue(Path(paths["json"]).is_file())


if __name__ == "__main__":
    unittest.main()
