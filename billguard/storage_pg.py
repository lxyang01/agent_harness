"""PostgreSQL 存储层(distributed 分支)。

逐 store 平移单进程实现,公开方法同名同参,仅构造函数变化(接连接池而非目录):
- PGUserStore / PGBillService / PGApprovalStore / PGWorkItemStore /
  PGSessionStore / PGEvidenceStore / PGTraceStore
- new_pg_pool(dsn) 统一建池

方言差异(相对 SQLite 版):
- 表由 docker/init.sql 预建,本模块绝不 CREATE/DROP TABLE
- 占位符 ? → %s;JSONB 参数用 psycopg.types.json.Jsonb,读回已是 dict/list
- 金额 NUMERIC 读回 Decimal,每个读取边界 float(...) 归一
- 布尔列直接用 Python bool / SQL TRUE,FALSE(不再 0/1)
- lastrowid 不存在,插入取主键一律 RETURNING id
- 密码/校验/纯函数逻辑 import 自单进程模块,不复制(auth.py / bills.py / observability.py)
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import secrets
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .auth import (
    ROLES, AuthError, User, _DUMMY_HASH, _DUMMY_SALT, _hash_password,
    validate_password, validate_username,
)
from .bills import (
    DEFAULT_CATEGORIES, DUPLICATE_WINDOW_DAYS, EXPORT_HEADER, HIKE_MIN_ABS, HIKE_RATIO,
    OUTLIER_MIN, OUTLIER_RATIO, SPIKE_MIN, SPIKE_RATIO, WORKFLOW_STATUSES,
    BillFilters, BillService,
)
from .observability import TraceStore
from .policy import ApprovalRequest, PolicyError, ToolPolicy
from .session import SessionStore
from .tools import ToolError
from .types import Message, Session
from .work_items import WorkItemError

__all__ = [
    "new_pg_pool", "PGUserStore", "PGBillService", "PGApprovalStore",
    "PGWorkItemStore", "PGSessionStore", "PGEvidenceStore", "PGTraceStore",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_pg_pool(dsn: str) -> ConnectionPool:
    """spec 规定的统一建池参数;open=True 立即预热连接。"""
    return ConnectionPool(dsn, min_size=2, max_size=8, open=True)


class _PooledStore:
    """共享的连接获取模式:每方法一取一还;dict 行按列名取值(等价 sqlite3.Row)。"""

    pool: ConnectionPool

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection]:
        with self.pool.connection() as db:
            db.row_factory = dict_row
            yield db


class PGUserStore(_PooledStore):
    """PG 版 users 表;密码/校验逻辑复用 auth.py,SQL 仅换方言。"""

    def __init__(self, pool: ConnectionPool, sessions: Any = None) -> None:
        self.pool = pool
        # 注入会话后端(如 RedisAuthSessions)后,reset_password/delete 联动
        # 失效该用户全部服务端会话(等价单进程 auth.py 的 auth_sessions 清理);
        # PG schema 无 auth_sessions 表,会话由注入方管理。
        self._sessions = sessions
        with self._connect() as db:
            # 迁移 UPDATE 同单进程版;PG 布尔列用 TRUE 表达(语义等价 disabled=1)
            db.execute("UPDATE users SET role = 'user' WHERE role = 'approver'")
            db.execute("UPDATE users SET disabled = TRUE WHERE role = 'viewer'")

    def _invalidate_sessions(self, username: str) -> None:
        """改密/删户后失效该用户全部登录会话(单进程在 auth.py 内联实现)。

        按能力探测:后端提供 delete_by_user(如 RedisAuthSessions)时调用,
        否则静默跳过。set_disabled 不在此列:resolve_user 每次请求都会
        拒绝禁用用户,禁用本身就是 kill-switch,无需再清会话。
        """
        invalidate = getattr(self._sessions, "delete_by_user", None)
        if invalidate is not None:
            invalidate(username)

    def _row(self, db: psycopg.Connection, username: str) -> dict[str, Any] | None:
        return db.execute("SELECT * FROM users WHERE username = %s", (username,)).fetchone()

    def create(self, username: str, password: str, role: str) -> User:
        username = username.strip()
        validate_username(username)
        validate_password(password)
        if role not in ROLES:
            raise AuthError(f"角色必须是 {ROLES} 之一")
        salt = secrets.token_bytes(16)
        with self._connect() as db:
            try:
                db.execute(
                    "INSERT INTO users(username, password_hash, salt, role, disabled, created_at) "
                    "VALUES (%s, %s, %s, %s, FALSE, %s)",
                    (username, _hash_password(password, salt).hex(), salt.hex(), role, _now()),
                )
            except UniqueViolation as exc:
                raise AuthError(f"用户已存在：{username}") from exc
        return self.get(username)

    def get(self, username: str) -> User:
        with self._connect() as db:
            row = self._row(db, username)
        if row is None:
            raise AuthError(f"用户不存在：{username}")
        return User(row["username"], row["role"], bool(row["disabled"]), row["created_at"])

    def list(self) -> list[User]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT username, role, disabled, created_at FROM users ORDER BY username").fetchall()
        return [User(row["username"], row["role"], bool(row["disabled"]), row["created_at"])
                for row in rows]

    def count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"])

    def verify(self, username: str, password: str) -> User:
        with self._connect() as db:
            row = self._row(db, username.strip())
        # 统一失败信息，不区分“用户不存在”与“密码错误”
        if row is None:
            # 未知用户名也执行一次等代价哈希,避免通过响应时间枚举有效用户名
            hmac.compare_digest(_hash_password(password, _DUMMY_SALT).hex(), _DUMMY_HASH)
            raise AuthError("用户名或密码错误")
        candidate = _hash_password(password, bytes.fromhex(row["salt"]))
        if not hmac.compare_digest(candidate.hex(), row["password_hash"]):
            raise AuthError("用户名或密码错误")
        if row["disabled"]:
            raise AuthError("账号已被禁用")
        return User(row["username"], row["role"], bool(row["disabled"]), row["created_at"])

    def _enabled_admins(self, db: psycopg.Connection, exclude: str = "") -> int:
        return int(db.execute(
            "SELECT COUNT(*) AS total FROM users "
            "WHERE role = 'admin' AND disabled = FALSE AND username != %s",
            (exclude,)).fetchone()["total"])

    def set_role(self, username: str, role: str) -> User:
        if role not in ROLES:
            raise AuthError(f"角色必须是 {ROLES} 之一")
        with self._connect() as db:
            row = self._row(db, username)
            if row is None:
                raise AuthError(f"用户不存在:{username}")
            if row["role"] == "admin" and role != "admin" and not row["disabled"] \
                    and self._enabled_admins(db, exclude=username) == 0:
                raise AuthError("不能降级最后一个启用中的管理员")
            db.execute("UPDATE users SET role = %s WHERE username = %s", (role, username))
        return self.get(username)

    def reset_password(self, username: str, password: str) -> None:
        validate_password(password)
        salt = secrets.token_bytes(16)
        with self._connect() as db:
            if self._row(db, username) is None:
                raise AuthError(f"用户不存在:{username}")
            db.execute("UPDATE users SET password_hash = %s, salt = %s WHERE username = %s",
                       (_hash_password(password, salt).hex(), salt.hex(), username))
        self._invalidate_sessions(username)

    def delete(self, username: str) -> None:
        with self._connect() as db:
            row = self._row(db, username)
            if row is None:
                raise AuthError(f"用户不存在:{username}")
            if row["role"] == "admin" and not row["disabled"] \
                    and self._enabled_admins(db, exclude=username) == 0:
                raise AuthError("不能删除最后一个启用中的管理员")
            db.execute("DELETE FROM users WHERE username = %s", (username,))
        self._invalidate_sessions(username)

    def set_disabled(self, username: str, disabled: bool) -> User:
        with self._connect() as db:
            row = self._row(db, username)
            if row is None:
                raise AuthError(f"用户不存在:{username}")
            if row["role"] == "admin" and disabled and not row["disabled"] \
                    and self._enabled_admins(db, exclude=username) == 0:
                raise AuthError("不能禁用最后一个启用中的管理员")
            db.execute("UPDATE users SET disabled = %s WHERE username = %s",
                       (bool(disabled), username))
        return self.get(username)


class PGBillService(_PooledStore, BillService):
    """BillService 的 PG 全量替换:继承纯函数与组合方法,数据访问逐方法换方言。

    表结构以 docker/init.sql 为准;不建表、不播种全局默认类别
    (owner 首访的默认类别副本由 _ensure_user_categories 负责,与单进程版一致)。
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    # ---- owner 片段:%s 版;其余逻辑与单进程一字不差 ----

    @staticmethod
    def _owner_clause(owner: str, column: str = "owner") -> tuple[str, tuple]:
        if owner == "":
            return f"{column} IS NULL", ()
        if owner == "admin":
            return f"({column} = %s OR {column} IS NULL)", (owner,)
        return f"{column} = %s", (owner,)

    @staticmethod
    def _owner_where(owner: str | None, column: str = "owner") -> tuple[str, list[Any]]:
        if owner is None:
            return "", []
        clause, params = PGBillService._owner_clause(owner, column)
        return f" WHERE {clause}", list(params)

    @staticmethod
    def _owner_and(owner: str | None, column: str = "owner") -> tuple[str, list[Any]]:
        if owner is None:
            return "", []
        clause, params = PGBillService._owner_clause(owner, column)
        return f" AND {clause}", list(params)

    @staticmethod
    def _where(filters: BillFilters, alias: str = "t",
               owner: str | None = None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        mapping = {
            "merchant": f"{alias}.merchant = %s",
            "method": f"{alias}.method = %s",
            "status": f"{alias}.status = %s",
            "min_amount": f"{alias}.amount >= %s",
            "max_amount": f"{alias}.amount <= %s",
        }
        for field, clause in mapping.items():
            value = getattr(filters, field)
            if value not in (None, ""):
                clauses.append(clause)
                params.append(value)
        # 纯日期自动补全当天边界,避免整点时间被 date_to 排除
        if filters.date_from:
            clauses.append(f"{alias}.paid_at >= %s")
            params.append(f"{filters.date_from} 00:00:00" if len(filters.date_from) == 10 else filters.date_from)
        if filters.date_to:
            clauses.append(f"{alias}.paid_at <= %s")
            params.append(f"{filters.date_to} 23:59:59.999999" if len(filters.date_to) == 10 else filters.date_to)
        if filters.category:
            clauses.append(f"{alias}.category_id IN (SELECT id FROM categories WHERE name=%s)")
            params.append(filters.category)
        if filters.query:
            clauses.append(
                f"({alias}.tx_id LIKE %s OR {alias}.merchant LIKE %s OR {alias}.note LIKE %s "
                f"OR EXISTS (SELECT 1 FROM categories cx WHERE cx.id={alias}.category_id AND cx.name LIKE %s))"
            )
            value = f"%{filters.query}%"
            params.extend((value, value, value, value))
        if owner is not None:
            clause, owner_params = PGBillService._owner_clause(owner, f"{alias}.owner")
            clauses.append(clause)
            params.extend(owner_params)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    # ---- 类别规则 / 归类 ----

    @staticmethod
    def _category_rules(db: psycopg.Connection,
                        owner: str | None = None) -> list[tuple[int, str, list[str]]]:
        # 自动分类只看同一 owner 的启用规则
        if owner is None:
            rows = db.execute(
                "SELECT id, name, keywords FROM categories WHERE enabled = TRUE ORDER BY id").fetchall()
        else:
            clause, params = PGBillService._owner_clause(owner)
            rows = db.execute(
                f"SELECT id, name, keywords FROM categories WHERE enabled = TRUE AND {clause} ORDER BY id",
                params).fetchall()
        rules = []
        for row in rows:
            try:
                keywords = json.loads(row["keywords"])
            except ValueError:
                keywords = []
            rules.append((row["id"], row["name"], keywords))
        return rules

    @staticmethod
    def _ensure_category(db: psycopg.Connection, name: str, now: str,
                         owner: str | None = None) -> int:
        db.execute("INSERT INTO categories(name, keywords, enabled, created_at, owner)"
                   " VALUES (%s, '[]', TRUE, %s, %s) ON CONFLICT DO NOTHING",
                   (name, now, owner))
        if owner is None:
            return db.execute(
                "SELECT id FROM categories WHERE name=%s", (name,)).fetchone()["id"]
        clause, params = PGBillService._owner_clause(owner)
        return db.execute(
            f"SELECT id FROM categories WHERE name=%s AND {clause}", (name, *params)).fetchone()["id"]

    def _resolve_category(self, db: psycopg.Connection, rules: list[tuple[int, str, list[str]]],
                          category_name: str, merchant: str, note: str, now: str,
                          owner: str | None = None) -> int:
        if category_name:
            if owner is None:
                row = db.execute(
                    "SELECT id FROM categories WHERE name=%s", (category_name,)).fetchone()
            else:
                clause, params = self._owner_clause(owner)
                row = db.execute(
                    f"SELECT id FROM categories WHERE name=%s AND {clause}",
                    (category_name, *params)).fetchone()
            if row:
                return row["id"]
        haystack = f"{merchant} {note}".lower()
        for category_id, _, keywords in rules:
            if any(str(keyword).lower() in haystack for keyword in keywords):
                return category_id
        return self._ensure_category(db, "其他", now, owner)

    # ---- 导入 ----

    def import_bills(self, filename: str, csv_text: str, owner: str | None = None) -> dict[str, Any]:
        if not csv_text.strip():
            raise ToolError("CSV 内容为空")
        reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
        if not reader.fieldnames:
            raise ToolError("CSV 没有表头")
        columns = self._column_map(reader.fieldnames)
        total = imported = duplicates = failed = 0
        errors: list[str] = []
        now = _now()
        with self._connect() as db:
            rules = self._category_rules(db, owner)
            owner_and, owner_params = self._owner_and(owner)
            for line_number, row in enumerate(reader, start=2):
                total += 1
                try:
                    tx_id = str(row.get(columns["tx_id"], "")).strip()
                    merchant = str(row.get(columns["merchant"], "")).strip()
                    if not tx_id or not merchant:
                        raise ValueError("交易编号或商户为空")
                    paid_at = self._normalize_datetime(str(row.get(columns["paid_at"], "")))
                    amount = float(str(row.get(columns["amount"], "")).strip())
                    method = str(row.get(columns.get("method", ""), "未知")).strip() or "未知"
                    note = str(row.get(columns.get("note", ""), "")).strip()
                    category_name = str(row.get(columns.get("category", ""), "")).strip()
                    # 去重按 (owner, tx_id) 复合唯一:同号账单可在不同 owner 名下各自入库
                    cursor = db.execute(
                        """INSERT INTO transactions(tx_id, paid_at, merchant, note, amount, method, created_at, owner)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                        (tx_id, paid_at, merchant, note, amount, method, now, owner))
                    if cursor.rowcount == 0:
                        duplicates += 1
                        continue
                    imported += 1
                    category_id = self._resolve_category(db, rules, category_name, merchant, note, now, owner)
                    db.execute(f"UPDATE transactions SET category_id=%s WHERE tx_id=%s{owner_and}",
                               (category_id, tx_id, *owner_params))
                except (ValueError, TypeError) as exc:
                    failed += 1
                    if len(errors) < 20:
                        errors.append(f"第 {line_number} 行:{exc}")
            status = "completed" if failed == 0 else ("partial" if imported else "failed")
            db.execute(
                """INSERT INTO imports(filename, total_rows, imported_rows, duplicate_rows,
                   failed_rows, failed_reasons, status, imported_at, owner) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (filename, total, imported, duplicates, failed,
                 json.dumps(errors, ensure_ascii=False), status, now, owner))
        return {
            "filename": filename,
            "total_rows": total,
            "imported_rows": imported,
            "duplicate_rows": duplicates,
            "failed_rows": failed,
            "status": status,
            "errors": errors,
        }

    def import_subscriptions(self, filename: str, csv_text: str, owner: str | None = None) -> dict[str, Any]:
        if not csv_text.strip():
            raise ToolError("CSV 内容为空")
        reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
        if not reader.fieldnames:
            raise ToolError("CSV 没有表头")
        aliases = {
            "name": ("name", "订阅名称", "名称"),
            "merchant": ("merchant", "商户"),
            "cycle": ("cycle", "周期"),
            "expected_amount": ("expected_amount", "预期金额"),
        }
        normalized = {self._normalize_header(header): header for header in reader.fieldnames}
        columns = {field: next((normalized[a] for a in names if a in normalized), None)
                   for field, names in aliases.items()}
        missing = {"name", "merchant", "expected_amount"} - {k for k, v in columns.items() if v}
        if missing:
            raise ToolError(f"CSV 缺少必要字段:{', '.join(sorted(missing))}")
        total = imported = duplicates = failed = 0
        errors: list[str] = []
        now = _now()
        with self._connect() as db:
            owner_and, owner_params = self._owner_and(owner)
            for line_number, row in enumerate(reader, start=2):
                total += 1
                try:
                    name = str(row.get(columns["name"], "")).strip()
                    merchant = str(row.get(columns["merchant"], "")).strip()
                    if not name or not merchant:
                        raise ValueError("订阅名称或商户为空")
                    expected = float(str(row.get(columns["expected_amount"], "")).strip())
                    cycle = str(row.get(columns.get("cycle") or "", "月")).strip() or "月"
                    if cycle not in ("月", "年"):
                        raise ValueError(f"周期必须是 月 或 年:{cycle}")
                    exists = db.execute(
                        f"SELECT id FROM subscriptions WHERE name=%s AND merchant=%s{owner_and}",
                        (name, merchant, *owner_params)).fetchone()
                    if exists:
                        duplicates += 1
                        continue
                    db.execute(
                        """INSERT INTO subscriptions(name, merchant, cycle, expected_amount, created_at, owner)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (name, merchant, cycle, expected, now, owner))
                    imported += 1
                except (ValueError, TypeError) as exc:
                    failed += 1
                    if len(errors) < 20:
                        errors.append(f"第 {line_number} 行:{exc}")
            status = "completed" if failed == 0 else ("partial" if imported else "failed")
            db.execute(
                """INSERT INTO imports(filename, total_rows, imported_rows, duplicate_rows,
                   failed_rows, failed_reasons, status, imported_at, owner) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (filename, total, imported, duplicates, failed,
                 json.dumps(errors, ensure_ascii=False), status, now, owner))
        return {
            "filename": filename,
            "total_rows": total,
            "imported_rows": imported,
            "duplicate_rows": duplicates,
            "failed_rows": failed,
            "status": status,
            "errors": errors,
        }

    # ---- 查询 / 聚合 ----

    def overview(self, filters: BillFilters | None = None, owner: str | None = None) -> dict[str, Any]:
        filters = filters or BillFilters()
        where, params = self._where(filters, owner=owner)
        with self._connect() as db:
            row = db.execute(
                f"""SELECT COALESCE(SUM(amount), 0) AS total_amount, COUNT(*) AS count,
                    COUNT(DISTINCT substr(paid_at, 1, 10)) AS active_days,
                    MIN(substr(paid_at, 1, 10)) AS data_from, MAX(substr(paid_at, 1, 10)) AS data_to
                    FROM transactions t{where}""",
                params).fetchone()
            pending = db.execute(
                f"SELECT COUNT(*) AS pending FROM transactions t{where}"
                f"{' AND' if where else ' WHERE'} t.status IN ('待核查','核查中')",
                params).fetchone()["pending"]
            by_category = [{
                "name": item["name"], "amount": float(item["amount"]), "count": item["count"],
            } for item in db.execute(
                f"""SELECT COALESCE(c.name, '未分类') AS name, ROUND(SUM(t.amount), 2) AS amount, COUNT(*) AS count
                    FROM transactions t LEFT JOIN categories c ON c.id=t.category_id{where}
                    GROUP BY c.name ORDER BY amount DESC""", params)]
            top_merchants = [{
                "name": item["name"], "amount": float(item["amount"]), "count": item["count"],
            } for item in db.execute(
                f"""SELECT t.merchant AS name, ROUND(SUM(t.amount), 2) AS amount, COUNT(*) AS count
                    FROM transactions t{where} GROUP BY t.merchant ORDER BY amount DESC LIMIT 8""", params)]
            max_tx = db.execute(
                f"""SELECT t.tx_id, t.paid_at, t.merchant, COALESCE(c.name, '未分类') AS category, t.amount
                    FROM transactions t LEFT JOIN categories c ON c.id=t.category_id{where}
                    ORDER BY t.amount DESC, t.paid_at DESC LIMIT 1""", params).fetchone()
            trend_rows = [{
                "date": item["date"], "amount": float(item["amount"]),
            } for item in db.execute(
                f"""SELECT substr(t.paid_at, 1, 10) AS date, ROUND(SUM(t.amount), 2) AS amount
                    FROM transactions t{where} GROUP BY date ORDER BY date DESC LIMIT 90""", params)]
            trend_rows.reverse()
            owner_where, owner_params = self._owner_where(owner)
            options = {
                "categories": [row["name"] for row in db.execute(
                    f"SELECT name FROM categories{owner_where} ORDER BY name", owner_params)],
                "methods": [row["method"] for row in db.execute(
                    f"SELECT DISTINCT method FROM transactions{owner_where} ORDER BY method", owner_params)],
            }
        total_amount = float(row["total_amount"])
        active_days = row["active_days"]
        return {
            "total_amount": round(total_amount, 2),
            "count": row["count"],
            "pending": pending,
            "avg_daily": round(total_amount / active_days, 2) if active_days else 0.0,
            # 数据覆盖区间(min/max paid_at 的日期前缀):空结果集时为 None,
            # 给模型一个日期锚点,避免它在“本月无支出”类回答里编造年份/区间
            "data_from": row["data_from"],
            "data_to": row["data_to"],
            "by_category": by_category,
            "top_merchants": top_merchants,
            "max_tx": ({**max_tx, "amount": float(max_tx["amount"])} if max_tx else None),
            "trend": trend_rows,
            "options": options,
        }

    def compare(self, days: int = 7, owner: str | None = None) -> dict[str, Any]:
        days = min(max(1, days), 90)
        with self._connect() as db:
            owner_where, owner_params = self._owner_where(owner)
            # 锚点=该 owner 数据的最新日期,避免窗口被别人的数据带偏
            latest = db.execute(
                f"SELECT MAX(substr(paid_at, 1, 10)) AS latest FROM transactions{owner_where}",
                owner_params).fetchone()["latest"]
        anchor = datetime.fromisoformat(latest).date() if latest else datetime.now().date()
        current_start = anchor - timedelta(days=days - 1)
        previous_end = current_start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=days - 1)
        current = self.overview(BillFilters(date_from=current_start.isoformat(), date_to=anchor.isoformat()), owner=owner)
        previous = self.overview(BillFilters(date_from=previous_start.isoformat(), date_to=previous_end.isoformat()), owner=owner)
        change = None if previous["total_amount"] == 0 else round(
            (current["total_amount"] - previous["total_amount"]) / previous["total_amount"] * 100, 1)
        merged: dict[str, dict[str, Any]] = {}
        for item in current["by_category"]:
            merged.setdefault(item["name"], {"current": 0.0, "previous": 0.0})["current"] = item["amount"]
        for item in previous["by_category"]:
            merged.setdefault(item["name"], {"current": 0.0, "previous": 0.0})["previous"] = item["amount"]
        by_category = [{
            "name": name,
            "current": round(values["current"], 2),
            "previous": round(values["previous"], 2),
            "change_percent": None if values["previous"] == 0 else round(
                (values["current"] - values["previous"]) / values["previous"] * 100, 1),
        } for name, values in merged.items()]
        by_category.sort(key=lambda item: (-item["current"], item["name"]))
        return {
            "days": days,
            "current_period": {"from": current_start.isoformat(), "to": anchor.isoformat(), "total": current["total_amount"]},
            "previous_period": {"from": previous_start.isoformat(), "to": previous_end.isoformat(), "total": previous["total_amount"]},
            "change_percent": change,
            "by_category": by_category,
        }

    def anomalies(self, days: int = 7, dimension: str = "spike", limit: int = 10,
                  owner: str | None = None) -> dict[str, Any]:
        days = min(max(1, days), 90)
        if dimension not in {"spike", "duplicate", "price_hike", "outlier"}:
            raise ToolError("dimension 必须是 spike、duplicate、price_hike 或 outlier")
        thresholds = {
            "duplicate_window_days": DUPLICATE_WINDOW_DAYS, "spike_ratio": SPIKE_RATIO, "spike_min": SPIKE_MIN,
            "outlier_min": OUTLIER_MIN, "outlier_ratio": OUTLIER_RATIO,
            "hike_min_abs": HIKE_MIN_ABS, "hike_ratio": HIKE_RATIO,
        }
        with self._connect() as db:
            owner_where, owner_params = self._owner_where(owner)
            owner_and, _ = self._owner_and(owner)
            owner_and_tx, tx_params = self._owner_and(owner, "t.owner")
            # 锚点=该 owner 数据的最新日期,避免窗口被别人的数据带偏
            latest = db.execute(
                f"SELECT MAX(substr(paid_at, 1, 10)) AS latest FROM transactions{owner_where}",
                owner_params).fetchone()["latest"]
            if not latest:
                return {"dimension": dimension, "days": days, "current_period": None, "previous_period": None,
                        "thresholds": thresholds, "items": []}
            anchor = datetime.fromisoformat(latest).date()
            current_from = (anchor - timedelta(days=days - 1)).isoformat()
            current_to = anchor.isoformat()
            previous_end = anchor - timedelta(days=days)
            previous_from = (previous_end - timedelta(days=days - 1)).isoformat()
            previous_to = previous_end.isoformat()
            items: list[dict[str, Any]] = []
            if dimension == "spike":
                for row in self.compare(days, owner=owner)["by_category"]:
                    if row["current"] >= SPIKE_MIN and row["current"] >= row["previous"] * SPIKE_RATIO:
                        items.append({
                            "name": row["name"],
                            "detail": f"本期 ¥{row['current']:g},上期 ¥{row['previous']:g},达到 {SPIKE_RATIO:g} 倍",
                            "evidence": {"current_amount": row["current"], "previous_amount": row["previous"],
                                         "change_percent": row["change_percent"]},
                        })
                items.sort(key=lambda item: -item["evidence"]["current_amount"])
            elif dimension == "duplicate":
                rows = db.execute(
                    f"""SELECT tx_id, merchant, amount, paid_at FROM transactions
                        WHERE substr(paid_at, 1, 10) BETWEEN %s AND %s{owner_and} ORDER BY paid_at""",
                    (current_from, current_to, *owner_params)).fetchall()
                groups: dict[tuple[str, float], list[dict[str, Any]]] = {}
                for row in rows:
                    groups.setdefault((row["merchant"], round(float(row["amount"]), 2)), []).append(row)
                for (merchant, amount), group in groups.items():
                    if len(group) < 2:
                        continue
                    dates = sorted(datetime.fromisoformat(row["paid_at"]) for row in group)
                    gaps = [(later - earlier).total_seconds() / 86400 for earlier, later in zip(dates, dates[1:])]
                    min_gap = min(gaps)
                    if min_gap > DUPLICATE_WINDOW_DAYS:
                        continue
                    gap_text = f"{min_gap:.1f} 天" if min_gap >= 1 else f"{min_gap * 1440:.0f} 分钟"
                    items.append({
                        "name": f"{merchant} ¥{amount:g}",
                        "detail": f"{len(group)} 笔最近间隔 {gap_text}",
                        "evidence": {"tx_ids": [row["tx_id"] for row in group],
                                     "dates": [row["paid_at"] for row in group],
                                     "min_gap_days": round(min_gap, 4)},
                    })
                items.sort(key=lambda item: (-len(item["evidence"]["tx_ids"]), item["name"]))
            elif dimension == "price_hike":
                # 演示数据金额双峰,取窗口内该商户最近一笔实扣与预期比较
                for sub in db.execute(
                        f"SELECT * FROM subscriptions WHERE active = TRUE{owner_and} ORDER BY id", owner_params):
                    row = db.execute(
                        f"""SELECT amount, paid_at FROM transactions WHERE merchant=%s
                            AND substr(paid_at, 1, 10) BETWEEN %s AND %s{owner_and} ORDER BY paid_at DESC LIMIT 1""",
                        (sub["merchant"], current_from, current_to, *owner_params)).fetchone()
                    if not row:
                        continue
                    expected, actual = float(sub["expected_amount"]), float(row["amount"])
                    if abs(actual - expected) < max(HIKE_MIN_ABS, expected * HIKE_RATIO):
                        continue
                    direction = "上涨" if actual > expected else "回落"
                    items.append({
                        "name": sub["name"],
                        "detail": f"预期 ¥{expected:g} 实扣 ¥{actual:g},{direction}",
                        "evidence": {"expected_amount": expected, "actual_amount": actual,
                                     "latest_date": row["paid_at"], "merchant": sub["merchant"]},
                    })
                items.sort(key=lambda item: -abs(item["evidence"]["actual_amount"] - item["evidence"]["expected_amount"]))
            else:
                rows = [{**item, "amount": float(item["amount"])} for item in db.execute(
                    f"""SELECT t.tx_id, t.merchant, t.amount, t.paid_at, COALESCE(c.name, '未分类') AS category
                        FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
                        WHERE substr(t.paid_at, 1, 10) BETWEEN %s AND %s{owner_and_tx}""",
                    (current_from, current_to, *tx_params))]
                amounts: dict[str, list[float]] = {}
                for row in rows:
                    amounts.setdefault(row["category"], []).append(row["amount"])
                for row in rows:
                    if row["amount"] < OUTLIER_MIN:
                        continue
                    values = amounts[row["category"]]
                    category_mean = sum(values) / len(values)
                    if row["amount"] < category_mean * OUTLIER_RATIO:
                        continue
                    items.append({
                        "name": row["merchant"],
                        "detail": f"¥{row['amount']:g} 为类别均值 {row['amount'] / category_mean:.1f} 倍",
                        "evidence": {"tx_id": row["tx_id"], "category": row["category"], "amount": row["amount"],
                                     "category_mean": round(category_mean, 2)},
                    })
                items.sort(key=lambda item: -item["evidence"]["amount"])
        return {
            "dimension": dimension,
            "days": days,
            "current_period": {"from": current_from, "to": current_to},
            "previous_period": {"from": previous_from, "to": previous_to},
            "thresholds": thresholds,
            "items": items[:min(max(1, limit), 30)],
        }

    def query(self, filters: BillFilters | None = None, page: int = 1, page_size: int = 30,
              owner: str | None = None) -> dict[str, Any]:
        filters = filters or BillFilters()
        page = max(1, page)
        page_size = min(max(1, page_size), 100)
        where, params = self._where(filters, owner=owner)
        with self._connect() as db:
            total = db.execute(
                f"SELECT COUNT(*) AS total FROM transactions t{where}", params).fetchone()["total"]
            rows = db.execute(
                f"""SELECT t.tx_id, t.paid_at, t.merchant, COALESCE(c.name, '未分类') AS category, t.amount,
                    t.method, t.status, t.note, t.created_at
                    FROM transactions t LEFT JOIN categories c ON c.id=t.category_id{where}
                    ORDER BY t.paid_at DESC, t.tx_id LIMIT %s OFFSET %s""",
                [*params, page_size, (page - 1) * page_size])
            items = [{**row, "amount": float(row["amount"]),
                      "note": self.mask_pii(row["note"])[0]} for row in rows]
        result = {"items": items, "total": total, "page": page, "page_size": page_size}
        if total == 0 and (filters.query or filters.merchant):
            result["retry_hint"] = "当前筛选未命中数据。请放宽筛选条件或缩短关键词后重试一次;重试仍为空时只能报告证据不足。"
        return result

    def categories(self, owner: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            owner_where, owner_params = self._owner_where(owner, "c.owner")
            rows = db.execute(f"""SELECT c.id, c.name, c.keywords, c.enabled, c.created_at,
                                 COUNT(t.tx_id) AS count FROM categories c
                                 LEFT JOIN transactions t ON t.category_id=c.id{owner_where}
                                 GROUP BY c.id ORDER BY count DESC, c.name""", owner_params)
            return [{**row, "keywords": json.loads(row["keywords"]),
                     "enabled": bool(row["enabled"])} for row in rows]

    def save_category(self, name: str, keywords: list[str], enabled: bool = True,
                      category_id: int | None = None, operator: str = "web-user",
                      owner: str | None = None) -> dict[str, Any]:
        name = name.strip()
        cleaned = list(dict.fromkeys(word.strip() for word in keywords if str(word).strip()))
        if not name:
            raise ToolError("类别名称不能为空")
        if len(name) > 40:
            raise ToolError("类别名不能超过 40 字符")
        now = _now()
        with self._connect() as db:
            owner_and, owner_params = self._owner_and(owner)
            old: dict[str, Any] = {}
            action = "create"
            if category_id is not None:
                row = db.execute(
                    f"SELECT * FROM categories WHERE id=%s{owner_and}",
                    (category_id, *owner_params)).fetchone()
                if not row:
                    raise ToolError(f"类别不存在:{category_id}")
                old = {"name": row["name"], "keywords": json.loads(row["keywords"]), "enabled": bool(row["enabled"])}
                try:
                    db.execute(f"UPDATE categories SET name=%s, keywords=%s, enabled=%s WHERE id=%s{owner_and}",
                               (name, json.dumps(cleaned, ensure_ascii=False), bool(enabled),
                                category_id, *owner_params))
                except UniqueViolation as exc:
                    raise ToolError(f"类别名称已存在:{name}") from exc
                action = "update"
            else:
                try:
                    cursor = db.execute(
                        "INSERT INTO categories(name, keywords, enabled, created_at, owner) "
                        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                        (name, json.dumps(cleaned, ensure_ascii=False), bool(enabled), now, owner))
                    category_id = cursor.fetchone()["id"]
                except UniqueViolation as exc:
                    raise ToolError(f"类别名称已存在:{name}") from exc
            new = {"name": name, "keywords": cleaned, "enabled": enabled}
            db.execute(
                """INSERT INTO tx_audits(tx_id, action, operator, new_value, changed_at, owner)
                   VALUES ('', 'category', %s, %s, %s, %s)""",
                (operator, json.dumps({"rule_action": action, "id": category_id, "old": old, "new": new},
                                       ensure_ascii=False), now, owner))
        return {"id": category_id, **new, "created_at": now}

    def delete_category(self, category_id: int, operator: str = "web-user",
                        owner: str | None = None) -> dict[str, Any]:
        now = _now()
        with self._connect() as db:
            owner_and, owner_params = self._owner_and(owner)
            row = db.execute(
                f"SELECT * FROM categories WHERE id=%s{owner_and}",
                (category_id, *owner_params)).fetchone()
            if not row:
                raise ToolError(f"类别不存在:{category_id}")
            old = {"name": row["name"], "keywords": json.loads(row["keywords"]), "enabled": bool(row["enabled"])}
            # 引用该类别的交易置空,由 rematch_categories 或关键词规则重新归类
            affected = db.execute(
                f"SELECT tx_id FROM transactions WHERE category_id=%s{owner_and}",
                (category_id, *owner_params)).fetchall()
            db.execute(f"UPDATE transactions SET category_id=NULL WHERE category_id=%s{owner_and}",
                       (category_id, *owner_params))
            db.execute(
                """INSERT INTO tx_audits(tx_id, action, operator, new_value, changed_at, owner)
                   VALUES ('', 'category', %s, %s, %s, %s)""",
                (operator, json.dumps({"rule_action": "delete", "id": category_id, "old": old,
                                        "unassigned": [item["tx_id"] for item in affected]},
                                       ensure_ascii=False), now, owner))
            db.execute(f"DELETE FROM categories WHERE id=%s{owner_and}", (category_id, *owner_params))
        return {"deleted": {"id": category_id, **old}, "unassigned": len(affected)}

    def rematch_categories(self, operator: str = "web-user", owner: str | None = None) -> dict[str, Any]:
        now = _now()
        with self._connect() as db:
            rules = self._category_rules(db, owner)
            owner_where, owner_params = self._owner_where(owner)
            owner_and, _ = self._owner_and(owner)
            other_id = None
            changed = total = 0
            for row in db.execute(
                    f"SELECT tx_id, merchant, note, category_id FROM transactions{owner_where}",
                    owner_params).fetchall():
                total += 1
                haystack = f"{row['merchant']} {row['note']}".lower()
                new_id = next((category_id for category_id, _, keywords in rules
                               if any(str(keyword).lower() in haystack for keyword in keywords)), None)
                if new_id is None:
                    if other_id is None:
                        other_id = self._ensure_category(db, "其他", now, owner)
                    new_id = other_id
                if new_id != row["category_id"]:
                    db.execute(f"UPDATE transactions SET category_id=%s WHERE tx_id=%s{owner_and}",
                               (new_id, row["tx_id"], *owner_params))
                    db.execute(
                        """INSERT INTO tx_audits(tx_id, action, operator, new_value, changed_at, owner)
                           VALUES (%s, 'category', %s, %s, %s, %s)""",
                        (row["tx_id"], operator,
                         json.dumps({"category_id": new_id, "source": "rematch"}, ensure_ascii=False), now, owner))
                    changed += 1
        return {"transactions": total, "changed": changed, "rule_count": len(rules)}

    def update_workflow(self, tx_ids: list[str], operator: str = "web-user",
                        status: str | None = None, note: str = "",
                        owner: str | None = None) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(item).strip() for item in tx_ids if str(item).strip()))
        if not ids:
            raise ToolError("至少选择一条交易")
        if len(ids) > 200:
            raise ToolError("单次最多处理 200 条交易")
        if status not in WORKFLOW_STATUSES:
            raise ToolError(f"status 必须是{'、'.join(WORKFLOW_STATUSES)}")
        now = _now()
        changes = {"status": status, "note": str(note or "").strip()}
        updated: list[str] = []
        with self._connect() as db:
            owner_and, owner_params = self._owner_and(owner)
            for tx_id in ids:
                row = db.execute(
                    f"SELECT status FROM transactions WHERE tx_id=%s{owner_and}",
                    (tx_id, *owner_params)).fetchone()
                if not row:
                    continue
                db.execute(f"UPDATE transactions SET status=%s WHERE tx_id=%s{owner_and}",
                           (status, tx_id, *owner_params))
                db.execute(
                    """INSERT INTO tx_audits(tx_id, action, operator, new_value, changed_at, owner)
                       VALUES (%s, 'workflow', %s, %s, %s, %s)""",
                    (tx_id, operator,
                     json.dumps({"old_status": row["status"], **changes}, ensure_ascii=False), now, owner))
                updated.append(tx_id)
        return {"updated_tx_ids": updated, "count": len(updated), "changes": changes, "operator": operator}

    def transaction_audits(self, tx_id: str, limit: int = 50,
                           owner: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            # 审计行自带 owner 戳,必须按 owner 过滤:同号交易可在多个 owner
            # 名下,只按交易归属过滤会把他人审计(operator/note)一并带出
            audit_and, audit_params = self._owner_and(owner, "a.owner")
            owner_and, owner_params = self._owner_and(owner, "t.owner")
            # EXISTS 保证审计仍对应本视野内存在的交易,且避免 JOIN 拉出重复审计行
            return [dict(row) for row in db.execute(
                f"""SELECT a.* FROM tx_audits a WHERE a.tx_id=%s{audit_and} AND EXISTS
                    (SELECT 1 FROM transactions t WHERE t.tx_id=a.tx_id{owner_and})
                    ORDER BY a.id LIMIT %s""",
                (tx_id, *audit_params, *owner_params, min(max(1, limit), 200)))]

    def recent_audits(self, limit: int = 50, owner: str | None = None) -> list[dict[str, Any]]:
        """最近的全量审计记录(类别规则 / 核查状态 / 订阅),供看板展示。"""
        with self._connect() as db:
            owner_where, owner_params = self._owner_where(owner)
            return [dict(row) for row in db.execute(
                f"SELECT * FROM tx_audits{owner_where} ORDER BY id DESC LIMIT %s",
                (*owner_params, min(max(1, limit), 200)))]

    def imports(self, limit: int = 20, owner: str | None = None) -> list[dict[str, Any]]:
        """最近导入批次;账单与订阅 CSV 共用 imports 表。"""
        with self._connect() as db:
            owner_where, owner_params = self._owner_where(owner)
            return [dict(row) for row in db.execute(
                f"SELECT * FROM imports{owner_where} ORDER BY id DESC LIMIT %s",
                (*owner_params, min(max(1, limit), 100),))]

    def update_transaction_category(self, tx_id: str, category: str,
                                    operator: str = "web-user",
                                    owner: str | None = None) -> dict[str, Any]:
        """人工改判单笔交易的类别;类别不存在则即时创建,并写入审计。"""
        tx_id = str(tx_id).strip()
        category = str(category).strip()
        if not tx_id or not category:
            raise ToolError("tx_id 和 category 不能为空")
        if len(category) > 40:
            raise ToolError("类别名不能超过 40 字符")
        now = _now()
        with self._connect() as db:
            # 别名片段只用于带 t 别名的 SELECT;UPDATE 语句无别名,须用裸 owner 片段
            owner_and, owner_params = self._owner_and(owner, "t.owner")
            owner_and_plain, plain_params = self._owner_and(owner)
            row = db.execute(
                f"""SELECT t.tx_id, COALESCE(c.name, '未分类') AS category FROM transactions t
                   LEFT JOIN categories c ON c.id=t.category_id WHERE t.tx_id=%s{owner_and}""",
                (tx_id, *owner_params)).fetchone()
            if not row:
                raise ToolError(f"交易不存在:{tx_id}")
            category_id = self._ensure_category(db, category, now, owner)
            db.execute(f"UPDATE transactions SET category_id=%s WHERE tx_id=%s{owner_and_plain}",
                       (category_id, tx_id, *plain_params))
            db.execute(
                """INSERT INTO tx_audits(tx_id, action, operator, new_value, changed_at, owner)
                   VALUES (%s, 'category', %s, %s, %s, %s)""",
                (tx_id, operator,
                 json.dumps({"old_category": row["category"], "new_category": category,
                              "source": "manual"}, ensure_ascii=False), now, owner))
        return {"tx_id": tx_id, "old_category": row["category"], "new_category": category,
                "operator": operator}

    def subscriptions(self, owner: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            owner_where, owner_params = self._owner_where(owner, "s.owner")
            owner_and_tx, tx_params = self._owner_and(owner, "t.owner")
            rows = db.execute(
                f"""SELECT s.*, (SELECT MAX(t.paid_at) FROM transactions t
                    WHERE t.merchant=s.merchant{owner_and_tx}) AS last_paid_at
                   FROM subscriptions s{owner_where} ORDER BY s.id""", [*tx_params, *owner_params])
            return [{**row, "expected_amount": float(row["expected_amount"]),
                     "active": bool(row["active"])} for row in rows]

    def set_subscription_active(self, subscription_id: int, active: bool, operator: str = "web-user",
                                owner: str | None = None) -> dict[str, Any]:
        now = _now()
        with self._connect() as db:
            owner_and, owner_params = self._owner_and(owner)
            row = db.execute(
                f"SELECT * FROM subscriptions WHERE id=%s{owner_and}",
                (subscription_id, *owner_params)).fetchone()
            if not row:
                raise ToolError(f"订阅不存在:{subscription_id}")
            db.execute(f"UPDATE subscriptions SET active=%s WHERE id=%s{owner_and}",
                       (bool(active), subscription_id, *owner_params))
            db.execute(
                """INSERT INTO tx_audits(tx_id, action, operator, new_value, changed_at, owner)
                   VALUES ('', 'subscription', %s, %s, %s, %s)""",
                (operator, json.dumps({"id": subscription_id, "name": row["name"],
                                        "active": bool(active)}, ensure_ascii=False), now, owner))
            return {**row, "expected_amount": float(row["expected_amount"]),
                    "active": bool(active)}

    def save_report(self, session_id: str, title: str, content: str,
                    owner: str | None = None) -> dict[str, Any]:
        title = title.strip()[:160]
        content = content.strip()
        if not title or not content:
            raise ToolError("报告标题和内容不能为空")
        created_at = _now()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO reports(session_id, title, content, created_at, owner) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (session_id, title, content, created_at, owner))
            report_id = cursor.fetchone()["id"]
        return {"id": report_id, "session_id": session_id, "title": title, "content": content, "created_at": created_at}

    def reports(self, limit: int = 50, owner: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            owner_where, owner_params = self._owner_where(owner)
            return [dict(row) for row in db.execute(
                f"SELECT id, session_id, title, content, created_at FROM reports{owner_where}"
                " ORDER BY id DESC LIMIT %s",
                (*owner_params, min(max(1, limit), 200)))]

    def delete_report(self, report_id: int, owner: str | None = None) -> dict[str, Any]:
        with self._connect() as db:
            owner_and, owner_params = self._owner_and(owner)
            row = db.execute(
                f"SELECT id, title FROM reports WHERE id=%s{owner_and}",
                (report_id, *owner_params)).fetchone()
            if not row:
                raise ToolError(f"报告不存在:{report_id}")
            db.execute(f"DELETE FROM reports WHERE id=%s{owner_and}", (report_id, *owner_params))
        return {"deleted": {"id": row["id"], "title": row["title"]}}

    def export_csv(self, filters: BillFilters | None = None, owner: str | None = None) -> str:
        filters = filters or BillFilters()
        where, params = self._where(filters, owner=owner)
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(EXPORT_HEADER)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT t.tx_id, t.paid_at, t.merchant, COALESCE(c.name, '未分类') AS category, t.amount,
                    t.method, t.note FROM transactions t LEFT JOIN categories c ON c.id=t.category_id{where}
                    ORDER BY t.paid_at DESC, t.tx_id""", params)
            for row in rows:
                writer.writerow([float(row["amount"]) if name == "amount" else row[name]
                                 for name in EXPORT_HEADER])
        return "\ufeff" + output.getvalue()

    def purge_owner(self, owner: str, operator: str = "", note: str = "") -> dict:
        """删除用户的全部业务数据(账号删除/自助清空时调用);不触碰 NULL 存量行。"""
        # admin 的可见范围包含 NULL 存量行,清空语义与其视图一致:连带清除
        where, params = (("owner = %s OR owner IS NULL", (owner,))
                         if owner == "admin" else ("owner = %s", (owner,)))
        with self._connect() as db:
            counts = {
                "transactions": db.execute(
                    f"DELETE FROM transactions WHERE {where}", params).rowcount,
                "subscriptions": db.execute(
                    f"DELETE FROM subscriptions WHERE {where}", params).rowcount,
                "categories": db.execute(
                    f"DELETE FROM categories WHERE {where}", params).rowcount,
                "reports": db.execute(
                    f"DELETE FROM reports WHERE {where}", params).rowcount,
            }
            db.execute(f"DELETE FROM tx_audits WHERE {where}", params)
            db.execute(f"DELETE FROM imports WHERE {where}", params)
            if operator:  # 清空后留一条审计标记,证明发生过自助清空
                db.execute(
                    "INSERT INTO tx_audits(tx_id, action, operator, new_value, changed_at, owner) "
                    "VALUES ('__purge__', 'purge', %s, %s, %s, %s)",
                    (operator, note[:200], _now(), owner))
            return counts

    def _ensure_user_categories(self, owner: str) -> None:
        """首次访问时为无任何类别的 owner 播种默认类别副本(带 owner 戳)。"""
        with self._connect() as db:
            clause, params = self._owner_clause(owner)
            if db.execute(f"SELECT 1 AS present FROM categories WHERE {clause} LIMIT 1", params).fetchone():
                return
            now = _now()
            # psycopg3 的 executemany 在 Cursor 上;默认类别仅 6 行,逐条执行等价
            for name, keywords in DEFAULT_CATEGORIES:
                db.execute(
                    "INSERT INTO categories(name, keywords, enabled, created_at, owner) "
                    "VALUES (%s, %s, TRUE, %s, %s)",
                    (name, json.dumps([x.strip() for x in keywords.split(",")], ensure_ascii=False),
                     now, owner))

    # for_user / scoped_or_legacy / samples / mask_pii / CSV 解析等纯函数与
    # 组合方法继承自 BillService,自动落到本类的数据访问实现上。


class PGApprovalStore(_PooledStore):
    """PG 版审批存储;列名对齐 docker/init.sql(arguments/policy_reason/checkpoint/execution_result)。"""

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    def request(self, session_id: str, trace_id: str, step: int,
                tool_name: str, arguments: dict[str, Any], policy: ToolPolicy,
                checkpoint: dict[str, Any]) -> ApprovalRequest:
        if policy.risk_level == "forbidden":
            raise PolicyError(f"forbidden tool cannot request approval: {tool_name}")
        approval_id = f"POL-{uuid.uuid4().hex[:12].upper()}"
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                "INSERT INTO approvals(id, session_id, trace_id, step, tool_name, arguments, "
                "risk_level, requires_approval, policy_reason, checkpoint, status, requested_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)",
                (approval_id, session_id, trace_id, step, tool_name, Jsonb(arguments),
                 policy.risk_level, bool(policy.requires_approval), policy.reason,
                 Jsonb(checkpoint), now),
            )
        return self.get(approval_id)

    def get(self, approval_id: str) -> ApprovalRequest:
        with self._connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id = %s", (approval_id,)).fetchone()
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
            clauses.append("session_id = %s")
            parameters.append(session_id)
        if statuses:
            placeholders = ",".join("%s" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            parameters.extend(statuses)
        sql = "SELECT * FROM approvals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY requested_at DESC LIMIT %s"
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
            row = db.execute("SELECT status FROM approvals WHERE id = %s", (approval_id,)).fetchone()
            if row is None:
                raise PolicyError(f"approval not found: {approval_id}")
            if row["status"] != "pending":
                raise PolicyError(f"approval is already {row['status']}: {approval_id}")
            status = "approved" if approved else "rejected"
            # 条件 UPDATE 是并发下的唯一裁决者:SELECT 与 UPDATE 之间存在竞态窗口,
            # 恰好一个线程的 rowcount=1,其余在提交前被拒绝。
            cursor = db.execute(
                "UPDATE approvals SET status = %s, decided_at = %s, decided_by = %s, decision_note = %s "
                "WHERE id = %s AND status = 'pending'",
                (status, datetime.now(timezone.utc).isoformat(), decided_by,
                 note.strip()[:1000], approval_id),
            )
            if cursor.rowcount == 0:
                raise PolicyError(f"approval is already decided: {approval_id}")
        return self.get(approval_id)

    def mark_execution(self, approval_id: str, succeeded: bool,
                       error: str = "") -> ApprovalRequest:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            row = db.execute("SELECT status FROM approvals WHERE id = %s", (approval_id,)).fetchone()
            if row is None:
                raise PolicyError(f"approval not found: {approval_id}")
            if row["status"] != "approved":
                raise PolicyError(f"only approved requests can be executed: {row['status']}")
            db.execute(
                "UPDATE approvals SET status = %s, execution_result = %s, execution_error = %s "
                "WHERE id = %s",
                ("executed" if succeeded else "failed",
                 # PG schema 无 executed_at 列;执行时刻记入 execution_result JSONB
                 Jsonb({"succeeded": bool(succeeded), "executed_at": now}),
                 error[:4000] if error else None, approval_id),
            )
        return self.get(approval_id)

    # brief 命名(mark_executed)与单进程命名(mark_execution)等价提供
    mark_executed = mark_execution

    def delete_session(self, session_id: str) -> int:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM approvals WHERE session_id = %s", (session_id,))
            return cursor.rowcount

    @staticmethod
    def _from_row(row: dict[str, Any]) -> ApprovalRequest:
        execution_result = row["execution_result"]
        executed_at = execution_result.get("executed_at") \
            if isinstance(execution_result, dict) else None
        return ApprovalRequest(
            id=row["id"], session_id=row["session_id"], trace_id=row["trace_id"],
            step=row["step"], tool_name=row["tool_name"],
            arguments=row["arguments"], risk_level=row["risk_level"],
            reason=row["policy_reason"], status=row["status"],
            checkpoint=row["checkpoint"], requested_at=row["requested_at"],
            decided_at=row["decided_at"], decided_by=row["decided_by"],
            decision_note=row["decision_note"], executed_at=executed_at,
            execution_error=row["execution_error"],
        )


class PGWorkItemStore(_PooledStore):
    """PG 版工单存储;issues 主键为 BIGSERIAL,对外仍呈现 ISS-XXXX 编号(单进程同构)。"""

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    @staticmethod
    def _issue_label(numeric_id: int) -> str:
        return f"ISS-{numeric_id:04d}"

    @staticmethod
    def _issue_number(issue_id: str | int) -> int:
        text = str(issue_id).strip()
        if text.upper().startswith("ISS-"):
            text = text[4:]
        try:
            return int(text)
        except ValueError:
            raise WorkItemError(f"issue not found: {issue_id}") from None

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
        next_step = ("A human must approve this request outside the MCP tool channel "
                     "before commit_issue.")
        payload = {
            "title": title,
            "description": description,
            "priority": priority,
            "evidence_refs": refs,
        }
        with self._connect() as db:
            db.execute(
                "INSERT INTO wi_approvals(id, action, payload, status, requested_at, expires_at, next_step) "
                "VALUES (%s, 'issue.create', %s, 'pending', %s, %s, %s)",
                (approval_id, Jsonb(payload), now.isoformat(),
                 (now + timedelta(minutes=30)).isoformat(), next_step),
            )
        return {
            "approval_id": approval_id,
            "status": "pending",
            "action": "issue.create",
            "payload": payload,
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
            "next_step": next_step,
        }

    def decide(self, approval_id: str, approved: bool, decided_by: str) -> dict[str, Any]:
        decided_by = decided_by.strip()
        if not decided_by:
            raise WorkItemError("decided_by is required")
        with self._connect() as db:
            row = db.execute("SELECT * FROM wi_approvals WHERE id = %s", (approval_id,)).fetchone()
            if row is None:
                raise WorkItemError(f"approval not found: {approval_id}")
            if row["status"] != "pending":
                raise WorkItemError(f"approval is already {row['status']}: {approval_id}")
            if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
                db.execute("UPDATE wi_approvals SET status = 'expired' WHERE id = %s", (approval_id,))
                raise WorkItemError(f"approval has expired: {approval_id}")
            status = "approved" if approved else "rejected"
            # 条件 UPDATE 保证并发 decide 恰好一个生效(与 ApprovalStore.decide 同款)。
            cursor = db.execute(
                "UPDATE wi_approvals SET status = %s, decided_by = %s, decided_at = %s "
                "WHERE id = %s AND status = 'pending'",
                (status, decided_by, datetime.now(timezone.utc).isoformat(), approval_id),
            )
            if cursor.rowcount == 0:
                raise WorkItemError(f"approval is already decided: {approval_id}")
        return self.approval(approval_id)

    def commit_issue(self, approval_id: str) -> dict[str, Any]:
        with self._connect() as db:
            # FOR UPDATE 行锁等价单进程 BEGIN IMMEDIATE:并发 commit 只建一单
            row = db.execute(
                "SELECT * FROM wi_approvals WHERE id = %s FOR UPDATE", (approval_id,)).fetchone()
            if row is None:
                raise WorkItemError(f"approval not found: {approval_id}")
            if row["status"] == "consumed" and row["issue_id"]:
                issue = db.execute(
                    "SELECT * FROM issues WHERE id = %s", (row["issue_id"],)).fetchone()
                return {"created": self._issue_dict(issue), "idempotent_replay": True}
            if row["status"] != "approved":
                raise WorkItemError(f"approval must be approved before commit: {row['status']}")
            payload = row["payload"]
            cursor = db.execute(
                "INSERT INTO issues(title, description, priority, created_by, approval_id, created_at, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (payload["title"], payload["description"], payload["priority"], row["decided_by"],
                 approval_id, datetime.now(timezone.utc).isoformat(),
                 # schema 无 status/evidence_refs 列:工单态与证据引用记入 payload JSONB
                 Jsonb({"status": "open", "evidence_refs": payload.get("evidence_refs", [])})))
            numeric_id = cursor.fetchone()["id"]
            db.execute(
                "UPDATE wi_approvals SET status = 'consumed', issue_id = %s WHERE id = %s",
                (numeric_id, approval_id),
            )
            issue = db.execute("SELECT * FROM issues WHERE id = %s", (numeric_id,)).fetchone()
        return {"created": self._issue_dict(issue), "idempotent_replay": False}

    def list_issues(self, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        if limit < 1 or limit > 200:
            raise WorkItemError("limit must be between 1 and 200")
        sql = "SELECT * FROM issues"
        parameters: list[Any] = []
        if status:
            sql += " WHERE payload ->> 'status' = %s"
            parameters.append(status)
        sql += " ORDER BY created_at DESC LIMIT %s"
        parameters.append(limit)
        with self._connect() as db:
            rows = db.execute(sql, parameters).fetchall()
        return {"items": [self._issue_dict(row) for row in rows], "count": len(rows)}

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM issues WHERE id = %s",
                             (self._issue_number(issue_id),)).fetchone()
        if row is None:
            raise WorkItemError(f"issue not found: {issue_id}")
        return self._issue_dict(row)

    def approval(self, approval_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM wi_approvals WHERE id = %s", (approval_id,)).fetchone()
        if row is None:
            raise WorkItemError(f"approval not found: {approval_id}")
        return {
            "approval_id": row["id"],
            "action": row["action"],
            "payload": row["payload"],
            "status": row["status"],
            "requested_at": row["requested_at"],
            "expires_at": row["expires_at"],
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
            "issue_id": self._issue_label(row["issue_id"]) if row["issue_id"] else None,
        }

    def pending_approvals(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id FROM wi_approvals WHERE status = 'pending' ORDER BY requested_at"
            ).fetchall()
        return [self.approval(row["id"]) for row in rows]

    @staticmethod
    def _issue_dict(row: dict[str, Any] | None) -> dict[str, Any]:
        if row is None:
            raise WorkItemError("issue disappeared during transaction")
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        return {
            "id": PGWorkItemStore._issue_label(row["id"]),
            "title": row["title"],
            "description": row["description"],
            "priority": row["priority"],
            "status": payload.get("status", "open"),
            "evidence_refs": payload.get("evidence_refs", []),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }


class PGSessionStore(_PooledStore):
    """PG 版会话存储;原子文件写换 upsert,读 messages JSONB。"""

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    # 校验与哈希复用单进程版(空/超长 session id 拒绝)
    _key = staticmethod(SessionStore._key)

    def _path(self, session_id: str) -> tuple[str, ...]:
        """存储寻址返回组合键(本表为单列主键),不再是文件路径。"""
        self._key(session_id)  # 保留非法 session id 的拒绝副作用
        return (session_id,)

    def load(self, session_id: str) -> Session:
        with self._connect() as db:
            row = db.execute(
                "SELECT summary, owner, messages FROM sessions WHERE session_id = %s",
                (session_id,)).fetchone()
        if row is None:
            return Session(session_id=session_id)
        try:
            return Session(session_id=session_id, summary=row["summary"] or "",
                           owner=row["owner"],
                           messages=[Message.from_dict(item) for item in row["messages"]])
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"cannot load session {session_id}: {exc}") from exc

    def exists(self, session_id: str) -> bool:
        """会话是否已存在(web 归属检查用;对应文件版 _path().exists())。"""
        self._key(session_id)  # 与 load 一致,非法 session id 先拒绝
        with self._connect() as db:
            return db.execute(
                "SELECT 1 AS present FROM sessions WHERE session_id = %s LIMIT 1",
                (session_id,)).fetchone() is not None

    def list(self) -> list[dict[str, Any]]:
        """全部会话行(侧栏列表用):session_id/owner/messages/updated_at。"""
        with self._connect() as db:
            rows = db.execute(
                "SELECT session_id, owner, messages, updated_at FROM sessions "
                "ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def save(self, session: Session) -> None:
        # 历史压缩由 HarnessEngine.compress_history 按 AgentSpec 阈值负责;
        # 存储层只做持久化,不再隐藏改写会话。
        with self._connect() as db:
            db.execute(
                "INSERT INTO sessions(session_id, owner, summary, messages, updated_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (session_id) DO UPDATE SET "
                "owner = EXCLUDED.owner, summary = EXCLUDED.summary, "
                "messages = EXCLUDED.messages, updated_at = EXCLUDED.updated_at",
                (session.session_id, session.owner, session.summary,
                 Jsonb([message.as_dict() for message in session.messages]), _now()))

    def delete(self, session_id: str) -> int:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
            return cursor.rowcount


class PGEvidenceStore(_PooledStore):
    """PG 版证据存储;替代 web.py 的 _load_evidence/_save_evidence 文件模式。

    evidence 为 dict:键 = answer 的 sha256(web._answer_key 同构),值 = 证据条目列表;
    save 按 (session_id, answer_hash) 主键 upsert,天然保留“合并”语义。
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    @staticmethod
    def _answer_key(answer: str) -> str:
        return hashlib.sha256(answer.encode("utf-8")).hexdigest()

    def load(self, session_id: str) -> dict[str, list[dict[str, Any]]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT answer_hash, evidence FROM evidence WHERE session_id = %s",
                (session_id,)).fetchall()
        return {row["answer_hash"]: row["evidence"] for row in rows}

    def save(self, session_id: str, answer: str, evidence: list[dict[str, Any]]) -> None:
        if not evidence:
            return
        with self._connect() as db:
            db.execute(
                "INSERT INTO evidence(session_id, answer_hash, evidence) VALUES (%s, %s, %s) "
                "ON CONFLICT (session_id, answer_hash) DO UPDATE SET evidence = EXCLUDED.evidence",
                (session_id, self._answer_key(answer), Jsonb(evidence)))

    def delete(self, session_id: str) -> int:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM evidence WHERE session_id = %s", (session_id,))
            return cursor.rowcount


class PGTraceStore(_PooledStore):
    """PG 版追踪存储;JSONL 追加换整行 events JSONB 读-改-写(upsert)。

    汇总逻辑(_summary/_duration_ms)复用单进程 observability.TraceStore,不复制。
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    def append_event(self, session_id: str, trace_id: str, agent: str,
                     event: dict[str, Any]) -> None:
        now = _now()
        with self._connect() as db:
            # FOR UPDATE 锁住既有行,避免并发读-改-写互相覆盖事件
            row = db.execute(
                "SELECT events FROM traces WHERE session_id = %s AND trace_id = %s FOR UPDATE",
                (session_id, trace_id)).fetchone()
            events = list(row["events"]) if row else []
            events.append(event)
            db.execute(
                "INSERT INTO traces(session_id, trace_id, agent, events, created_at) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (session_id, trace_id) DO UPDATE SET events = EXCLUDED.events",
                (session_id, trace_id, agent or "", Jsonb(events), now))

    @staticmethod
    def _with_session(events: Any, session_id: str) -> list[dict[str, Any]]:
        # 单进程 _read_events 会在读取时补 session_id;此处保持同构
        return [dict(item, session_id=session_id) if isinstance(item, dict) else item
                for item in events]

    def list_runs(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("run list limit must be between 1 and 200")
        with self._connect() as db:
            rows = db.execute(
                "SELECT trace_id, events FROM traces WHERE session_id = %s",
                (session_id,)).fetchall()
        runs = [TraceStore._summary(row["trace_id"], self._with_session(row["events"], session_id))
                for row in rows]
        runs.sort(key=lambda item: item["started_at"], reverse=True)
        return runs[:limit]

    def get_run(self, session_id: str, trace_id: str) -> dict[str, Any]:
        trace_id = trace_id.strip()
        if not trace_id:
            raise ValueError("trace_id is required")
        with self._connect() as db:
            row = db.execute(
                "SELECT events FROM traces WHERE session_id = %s AND trace_id = %s",
                (session_id, trace_id)).fetchone()
        if row is None:
            raise ValueError(f"run not found: {trace_id}")
        events = self._with_session(row["events"], session_id)
        return {"summary": TraceStore._summary(trace_id, events), "events": events}

    def delete(self, session_id: str) -> int:
        """删除该会话全部 Trace(web.delete_session 用;对应删除 traces/*.jsonl)。"""
        with self._connect() as db:
            cursor = db.execute("DELETE FROM traces WHERE session_id = %s", (session_id,))
            return cursor.rowcount
