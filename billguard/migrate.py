"""billguard/migrate.py — 手写版本化迁移 runner(不引入第三方迁移框架)。

约定:
- 迁移文件放仓库根 migrations/,成对出现:V<零填充序号>_<名称>.up.sql 与
  同名 .down.sql;纯 SQL、可含多条语句(无参数时 psycopg 走 simple query
  协议,单次 execute 即可执行多语句),不要求一文件一语句。
- 版本账本 schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT NOT
  NULL)由本 runner 自建,不出现在任何迁移文件里。
- apply 的每个版本独立事务:up SQL 与版本记录同事务提交,失败整体回滚。

CLI(中文输出,失败非零退出):
    python -m billguard.migrate --dsn <dsn> status      # 查看已应用/待应用
    python -m billguard.migrate --dsn <dsn> apply       # 应用全部待应用版本
    python -m billguard.migrate --dsn <dsn> rollback    # 回滚最新已应用版本
    python -m billguard.migrate --dsn <dsn> ensure-database
                                                        # 建库(如缺)+ 全量应用

ensure-database 连同一服务器的 postgres 管理库,目标库不存在则 CREATE
DATABASE(重复创建错误吞掉,幂等),再对目标库应用全部迁移 —— 供 CI 与
测试套件引导独立的 billguard_test 库。

启动门禁:web/MCP/users 等 PG 模式入口在建池后调用 require_current(pool,
auto);库落后于 migrations/ 时 auto=False 打印待应用版本与确切修复命令后
SystemExit(1),auto=True(环境变量 BILLGUARD_AUTO_MIGRATE=1)先自动补齐。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import DuplicateDatabase
from psycopg_pool import ConnectionPool

# 仓库根(billguard/ 的上一级);与 web.py serve() 的 project_root 同一解析方式,
# 保证任意 CWD 启动(宿主机/容器/compose run)都定位到同一份 migrations/。
REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"

# 文件名形如 V001_init.up.sql;序号零填充,字典序 == 数值序
_NAME_RE = re.compile(r"^V(?P<num>\d+)_(?P<name>.+)\.up\.sql$")


@dataclass(frozen=True)
class Migration:
    """一个迁移版本:version 为文件名去掉 .up.sql/.down.sql 的部分。"""

    version: str
    order: int
    up_path: Path
    down_path: Path


def _sort_key(version: str) -> tuple[int, str]:
    match = re.match(r"^V(\d+)_", version)
    return (int(match.group(1)) if match else -1, version)


def discover(migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    """发现 migrations/ 下全部 V*.up.sql 并按零填充版本号升序排序。

    校验:文件名符合 V<数字>_<名称>.up.sql;每个 up 都有配对 .down.sql;
    数值序号不重复(否则账本主键会静默顶替,属迁移文件命名事故)。
    """
    found: list[Migration] = []
    seen_orders: dict[int, str] = {}
    for up_path in sorted(migrations_dir.glob("V*.up.sql")):
        match = _NAME_RE.match(up_path.name)
        if match is None:
            raise RuntimeError(f"迁移文件名不合规(期望 V<序号>_<名称>.up.sql):{up_path}")
        order = int(match.group("num"))
        if order in seen_orders:
            raise RuntimeError(
                f"迁移序号重复:{seen_orders[order]} 与 {up_path.name} 都是 V{order}")
        seen_orders[order] = up_path.name
        down_path = up_path.with_name(up_path.name[: -len(".up.sql")] + ".down.sql")
        if not down_path.exists():
            raise RuntimeError(f"迁移 {up_path.name} 缺少配对的 {down_path.name}")
        found.append(Migration(version=up_path.name[: -len(".up.sql")],
                               order=order, up_path=up_path, down_path=down_path))
    if not found:
        raise RuntimeError(f"{migrations_dir} 下未发现任何 V*.up.sql 迁移文件。")
    return sorted(found, key=lambda mig: (mig.order, mig.version))


def _ensure_bookkeeping(db: psycopg.Connection) -> None:
    """账本表由 runner 自建(不在迁移文件里),幂等。"""
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations("
               "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")


def applied_versions(pool: ConnectionPool) -> list[str]:
    """已应用版本列表,按与 discover() 相同的序号规则升序。"""
    with pool.connection() as db:
        _ensure_bookkeeping(db)
        versions = [row[0] for row in db.execute(
            "SELECT version FROM schema_migrations").fetchall()]
    return sorted(versions, key=_sort_key)


def pending(pool: ConnectionPool,
            migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    """目录里有、账本里没有的迁移,按序升序。"""
    applied = set(applied_versions(pool))
    return [mig for mig in discover(migrations_dir) if mig.version not in applied]


def apply_all(pool: ConnectionPool,
              migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """应用全部待应用版本;每个版本独立事务(up SQL + 账本记录同提交),
    已应用的跳过。返回本次新应用的版本号列表(空列表 = 已是最新)。"""
    applied_now: list[str] = []
    for mig in pending(pool, migrations_dir):
        sql = mig.up_path.read_text(encoding="utf-8")
        with pool.connection() as db:  # 连接上下文:正常退出即提交,异常即回滚
            _ensure_bookkeeping(db)
            db.execute(sql)
            db.execute("INSERT INTO schema_migrations(version, applied_at) "
                       "VALUES (%s, %s)",
                       (mig.version, datetime.now(timezone.utc).isoformat()))
        applied_now.append(mig.version)
    return applied_now


def rollback_one(pool: ConnectionPool,
                 migrations_dir: Path = MIGRATIONS_DIR) -> str:
    """回滚最新已应用版本:跑其 .down.sql 并删除账本记录(同一事务)。
    返回被回滚的版本号;没有已应用版本时报错。"""
    applied = applied_versions(pool)
    if not applied:
        raise RuntimeError("没有已应用的迁移,无需回滚。")
    known = {mig.version: mig for mig in discover(migrations_dir)}
    latest = applied[-1]  # applied_versions 已按序号升序排好
    mig = known.get(latest)
    if mig is None:
        raise RuntimeError(f"最新已应用版本 {latest} 在 {migrations_dir} 中"
                           "找不到对应迁移文件,无法回滚。")
    sql = mig.down_path.read_text(encoding="utf-8")
    with pool.connection() as db:
        db.execute(sql)
        db.execute("DELETE FROM schema_migrations WHERE version = %s", (latest,))
    return latest


def require_current(pool: ConnectionPool, auto: bool,
                    migrations_dir: Path = MIGRATIONS_DIR) -> None:
    """启动门禁:比较已应用版本与 migrations/ 目录。

    - 已最新:静默放行。
    - 落后 + auto=False:打印待应用版本与确切修复命令,SystemExit(1)。
    - 落后 + auto=True(BILLGUARD_AUTO_MIGRATE=1):先自动应用全部待应用
      版本(逐版本打印),再放行。
    """
    waiting = pending(pool, migrations_dir)
    if not waiting:
        return
    versions = "、".join(mig.version for mig in waiting)
    if auto:
        for version in apply_all(pool, migrations_dir):
            print(f"自动迁移:已应用版本 {version}(BILLGUARD_AUTO_MIGRATE=1)")
        return
    dsn_hint = os.environ.get("BILLGUARD_PG_DSN") \
        or getattr(pool, "conninfo", None) or "<PG DSN>"
    print(f"数据库 schema 落后于 migrations/ 目录,待应用版本:{versions}。")
    print(f"请先执行:python -m billguard.migrate --dsn \"{dsn_hint}\" apply"
          " 完成迁移后再启动服务(或设置环境变量 BILLGUARD_AUTO_MIGRATE=1)。")
    raise SystemExit(1)


def ensure_database(dsn: str,
                    migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """目标库不存在则建(连同一服务器的 postgres 管理库执行 CREATE DATABASE,
    重复创建错误吞掉),随后对目标库应用全部迁移;返回本次新应用版本。"""
    params = conninfo_to_dict(dsn)
    target = params.get("dbname") or os.environ.get("PGDATABASE") or ""
    if not target:
        raise RuntimeError("ensure-database:DSN 未指定目标库名(dbname)。")
    admin_dsn = make_conninfo(**{**params, "dbname": "postgres"})
    with psycopg.connect(admin_dsn, autocommit=True) as admin_conn:
        # CREATE DATABASE 不能在事务里跑,必须 autocommit
        try:
            admin_conn.execute(f'CREATE DATABASE "{target}"')
            print(f"已创建数据库 {target}。")
        except DuplicateDatabase:
            pass  # 已存在,幂等
    pool = ConnectionPool(dsn, min_size=1, max_size=2, open=True)
    try:
        return apply_all(pool, migrations_dir)
    finally:
        pool.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m billguard.migrate",
        description="BillGuard 版本化迁移 runner(migrations/ 目录)")
    parser.add_argument("--dsn", required=True, help="目标 PostgreSQL DSN")
    parser.add_argument("command", default="status", nargs="?",
                        choices=("status", "apply", "rollback", "ensure-database"),
                        help="status=查看状态 apply=全量应用 rollback=回滚最新 "
                             "ensure-database=建库(如缺)+全量应用")
    args = parser.parse_args(argv)
    try:
        if args.command == "ensure-database":
            for version in ensure_database(args.dsn):
                print(f"已应用版本:{version}")
            print(f"ensure-database 完成(目标库已就绪且 schema 最新)。")
            return 0
        pool = ConnectionPool(args.dsn, min_size=1, max_size=2, open=True)
        try:
            if args.command == "status":
                applied = applied_versions(pool)
                waiting = [mig.version for mig in pending(pool)]
                print(f"已应用版本({len(applied)}):{'、'.join(applied) or '无'}")
                print(f"待应用版本({len(waiting)}):{'、'.join(waiting) or '无'}")
            elif args.command == "apply":
                applied = apply_all(pool)
                for version in applied:
                    print(f"已应用版本:{version}")
                if not applied:
                    print("数据库已是最新,无待应用迁移。")
                else:
                    print(f"apply 完成:本次新应用 {len(applied)} 个版本。")
            elif args.command == "rollback":
                version = rollback_one(pool)
                print(f"已回滚版本:{version}")
        finally:
            pool.close()
        return 0
    except Exception as exc:
        print(f"迁移失败:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
