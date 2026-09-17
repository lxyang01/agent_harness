# tests/test_migrate.py — 迁移 runner(TDD)测试。
#
# 目标库:conftest.PG_DSN(billguard_test,与演示库 billguard 物理隔离);
# 用例自备干净起点:先回滚到空库再验证 apply/rollback/门禁,类级清理把库
# 恢复到"全部已应用"基线,套件内其他文件背靠背重跑不受影响。
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from psycopg_pool import ConnectionPool

from tests import conftest

from billguard import migrate

# V001 建立的 13 张业务表(schema_migrations 账本由 runner 自建,不计入)
BUSINESS_TABLES = ("users", "transactions", "categories", "subscriptions",
                   "tx_audits", "imports", "reports", "approvals", "wi_approvals",
                   "issues", "sessions", "evidence", "traces")


def table_names(pool: ConnectionPool) -> set[str]:
    with pool.connection() as db:
        return {row[0] for row in db.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")}


class MigrateTestCase(unittest.TestCase):
    """迁移 runner 行为:apply 幂等、rollback 可逆、启动门禁两种模式。"""

    @classmethod
    def setUpClass(cls) -> None:
        conftest.ensure_test_database()  # 幂等引导:建库(如缺)+ 全量应用
        cls.pool = ConnectionPool(conftest.PG_DSN, min_size=1, max_size=2,
                                  open=True)
        cls.addClassCleanup(cls._restore_applied_state)

    @classmethod
    def _restore_applied_state(cls) -> None:
        try:
            migrate.apply_all(cls.pool)  # 无论用例把版本摆成什么样,回到全应用基线
        finally:
            cls.pool.close()

    def _rollback_to_empty(self) -> None:
        while migrate.applied_versions(self.pool):
            migrate.rollback_one(self.pool)

    def test_apply_on_empty_database(self) -> None:
        """空库全量 apply:账本记录齐全、13 张业务表建成、账本表自建。"""
        self._rollback_to_empty()
        self.assertEqual(migrate.applied_versions(self.pool), [])
        applied = migrate.apply_all(self.pool)
        expected = [mig.version for mig in migrate.discover()]
        self.assertEqual(applied, expected)  # 本次全部新应用
        self.assertEqual(set(migrate.applied_versions(self.pool)), set(expected))
        tables = table_names(self.pool)
        for name in BUSINESS_TABLES:
            self.assertIn(name, tables)
        self.assertIn("schema_migrations", tables)  # 账本由 runner 自建
        business = tables - {"schema_migrations"}
        self.assertEqual(len(business), 13)

    def test_reapply_is_noop(self) -> None:
        """已应用版本重复 apply:跳过不重放,账本不变。"""
        migrate.apply_all(self.pool)
        before = migrate.applied_versions(self.pool)
        self.assertEqual(migrate.apply_all(self.pool), [])
        self.assertEqual(migrate.applied_versions(self.pool), before)

    def test_rollback_one_then_reapply(self) -> None:
        """rollback_one 只回滚最新版本:down 跑过、记录删除、再 apply 恢复。"""
        migrate.apply_all(self.pool)
        versions = [mig.version for mig in migrate.discover()]
        rolled = migrate.rollback_one(self.pool)
        self.assertEqual(rolled, versions[-1])  # 最新已应用版本
        self.assertNotIn(rolled, migrate.applied_versions(self.pool))
        tables = table_names(self.pool)
        for name in BUSINESS_TABLES:  # 唯一版本的 down 已拆除全部业务表
            self.assertNotIn(name, tables)
        self.assertEqual(migrate.apply_all(self.pool), [rolled])  # 重新应用恢复
        self.assertIn(rolled, migrate.applied_versions(self.pool))
        for name in BUSINESS_TABLES:
            self.assertIn(name, table_names(self.pool))

    def test_require_current_gate(self) -> None:
        """启动门禁:落后 + auto=False 时 SystemExit(1) 并点名版本与命令;
        auto=True 时先补齐再放行。"""
        self._rollback_to_empty()
        pending = [mig.version for mig in migrate.discover()]
        self.assertTrue(pending)
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            migrate.require_current(self.pool, auto=False)
        self.assertEqual(ctx.exception.code, 1)
        text = out.getvalue()
        for version in pending:
            self.assertIn(version, text)  # 点名待应用版本
        self.assertIn("python -m billguard.migrate", text)  # 给出确切命令
        self.assertIn("apply", text)
        # 门禁拒绝时不得动库
        self.assertEqual([mig.version for mig in migrate.pending(self.pool)],
                         pending)
        with redirect_stdout(io.StringIO()) as applied_log:
            migrate.require_current(self.pool, auto=True)
        for version in pending:  # 自动迁移逐版本留痕
            self.assertIn(version, applied_log.getvalue())
        self.assertEqual(migrate.pending(self.pool), [])
        self.assertEqual(len(migrate.applied_versions(self.pool)), len(pending))
        migrate.require_current(self.pool, auto=False)  # 已最新:静默放行


if __name__ == "__main__":
    unittest.main()
