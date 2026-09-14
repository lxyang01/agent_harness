from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .tools import ToolError


WORKFLOW_STATUSES = ("正常", "待核查", "核查中", "已确认", "已忽略")
DEFAULT_CATEGORIES = (
    ("餐饮", "饿了么,美团,肯德基,麦当劳,咖啡,午餐,晚餐,奶茶"),
    ("交通", "滴滴,地铁,公交,高铁,加油,停车"),
    ("购物", "淘宝,京东,拼多多,天猫,超市"),
    ("订阅", "会员,订阅,月费,年费,自动续费"),
    ("娱乐", "电影,游戏,Steam,演出"),
    ("居住", "房租,水电,物业,燃气"),
)

# 阈值常量:四类异常共用,便于 UI 展示与调参
DUPLICATE_WINDOW_DAYS = 3
SPIKE_RATIO = 2
SPIKE_MIN = 100
OUTLIER_MIN = 200
OUTLIER_RATIO = 5
HIKE_MIN_ABS = 1
HIKE_RATIO = 0.2

# counts 的键用中文标签,与页面文案一致
_PII_PATTERNS = (
    ("订单号", re.compile(r"\b(?:SO|ORD|NO)[-_]?[A-Za-z0-9-]{5,}\b", re.I), "[订单号]"),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    ("邮箱", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[邮箱]"),
)

FIELD_ALIASES = {
    "tx_id": ("tx_id", "交易编号", "交易id", "交易_id", "订单号", "id"),
    "paid_at": ("paid_at", "时间", "交易时间", "支付时间", "created_at"),
    "merchant": ("merchant", "商户", "商户名称"),
    "category": ("category", "类别", "分类"),
    "amount": ("amount", "金额"),
    "method": ("method", "方式", "支付方式"),
    "note": ("note", "备注"),
}

EXPORT_HEADER = ["tx_id", "paid_at", "merchant", "category", "amount", "method", "note"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class BillFilters:
    date_from: str = ""
    date_to: str = ""
    category: str = ""
    merchant: str = ""
    method: str = ""
    status: str = ""
    min_amount: float | None = None
    max_amount: float | None = None
    query: str = ""


class BillService:
    """SQLite-backed bills / categories / subscriptions store with audited workflow."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "bills.db"
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _owner_clause(owner: str, column: str = "owner") -> tuple[str, tuple]:
        """owner 过滤子句:普通用户 = 本名行;admin 视野包含存量 NULL 行(与 Session 归属同构);
        空串 = 仅存量 NULL 行(独立 CLI 探针用)。根服务(owner=None)不加子句。"""
        if owner == "":
            return f"{column} IS NULL", ()
        if owner == "admin":
            return f"({column} = ? OR {column} IS NULL)", (owner,)
        return f"{column} = ?", (owner,)

    @staticmethod
    def _owner_where(owner: str | None, column: str = "owner") -> tuple[str, list[Any]]:
        """独立 WHERE 子句(查询原本没有条件时使用);owner=None 时不加过滤。"""
        if owner is None:
            return "", []
        clause, params = BillService._owner_clause(owner, column)
        return f" WHERE {clause}", list(params)

    @staticmethod
    def _owner_and(owner: str | None, column: str = "owner") -> tuple[str, list[Any]]:
        """拼接到既有 WHERE 末尾的 AND 片段;owner=None 时不加过滤。"""
        if owner is None:
            return "", []
        clause, params = BillService._owner_clause(owner, column)
        return f" AND {clause}", list(params)

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS categories(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    keywords TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, owner TEXT);
                CREATE TABLE IF NOT EXISTS transactions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, tx_id TEXT NOT NULL, paid_at TEXT NOT NULL,
                    merchant TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', category_id INTEGER REFERENCES categories(id),
                    amount REAL NOT NULL, method TEXT NOT NULL DEFAULT '未知',
                    status TEXT NOT NULL DEFAULT '正常', created_at TEXT NOT NULL, owner TEXT);
                CREATE INDEX IF NOT EXISTS idx_tx_paid_at ON transactions(paid_at);
                CREATE INDEX IF NOT EXISTS idx_tx_merchant ON transactions(merchant);
                CREATE TABLE IF NOT EXISTS subscriptions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    merchant TEXT NOT NULL, cycle TEXT NOT NULL DEFAULT '月',
                    expected_amount REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tx_audits(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, tx_id TEXT NOT NULL,
                    action TEXT NOT NULL, operator TEXT NOT NULL,
                    new_value TEXT NOT NULL DEFAULT '', changed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS imports(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL,
                    total_rows INTEGER, imported_rows INTEGER, duplicate_rows INTEGER,
                    failed_rows INTEGER, failed_reasons TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL, imported_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reports(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                    content TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL);
            """)
            # 阶段 3:业务表补 owner 列(存量行保持 NULL = 仅 admin 可见)
            tx_had_surrogate = False
            for table in ("transactions", "categories", "subscriptions",
                          "tx_audits", "imports", "reports"):
                columns = {row["name"] for row in db.execute(
                    f"PRAGMA table_info({table})")}
                if table == "transactions":
                    tx_had_surrogate = "id" in columns
                if "owner" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN owner TEXT")
            # 旧库 categories.name 是全局唯一,须重建为按 (owner, name) 唯一,
            # 否则各 owner 无法各自持有同名默认类别
            legacy_unique_name = any(
                row["origin"] == "u" for row in db.execute("PRAGMA index_list(categories)"))
            if legacy_unique_name:
                db.execute("PRAGMA foreign_keys=OFF")
                db.execute("DROP TABLE IF EXISTS categories_rebuild")
                db.execute("""CREATE TABLE categories_rebuild(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    keywords TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, owner TEXT)""")
                db.execute("""INSERT INTO categories_rebuild(id, name, keywords, enabled, created_at, owner)
                    SELECT id, name, keywords, enabled, created_at, owner FROM categories""")
                db.execute("DROP TABLE categories")
                db.execute("ALTER TABLE categories_rebuild RENAME TO categories")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_owner_name"
                       " ON categories(COALESCE(owner, ''), name)")
            # 旧库 transactions.tx_id 是全局主键,须重建为代理主键 + (owner, tx_id) 复合唯一,
            # 否则不同 owner 无法各自导入相同编号的账单
            if not tx_had_surrogate:
                db.execute("PRAGMA foreign_keys=OFF")
                db.execute("DROP TABLE IF EXISTS transactions_rebuild")
                db.execute("""CREATE TABLE transactions_rebuild(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, tx_id TEXT NOT NULL, paid_at TEXT NOT NULL,
                    merchant TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                    category_id INTEGER REFERENCES categories(id), amount REAL NOT NULL,
                    method TEXT NOT NULL DEFAULT '未知', status TEXT NOT NULL DEFAULT '正常',
                    created_at TEXT NOT NULL, owner TEXT)""")
                db.execute("""INSERT INTO transactions_rebuild(tx_id, paid_at, merchant, note, category_id,
                    amount, method, status, created_at, owner)
                    SELECT tx_id, paid_at, merchant, note, category_id, amount, method, status, created_at, owner
                    FROM transactions""")
                db.execute("DROP TABLE transactions")
                db.execute("ALTER TABLE transactions_rebuild RENAME TO transactions")
                db.execute("CREATE INDEX IF NOT EXISTS idx_tx_paid_at ON transactions(paid_at)")
                db.execute("CREATE INDEX IF NOT EXISTS idx_tx_merchant ON transactions(merchant)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_owner_tx"
                       " ON transactions(COALESCE(owner, ''), tx_id)")
            # 仅在空库时播种默认类别,避免删光后重启又复活
            if db.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
                now = _now()
                db.executemany(
                    "INSERT INTO categories(name, keywords, enabled, created_at) VALUES (?,?,1,?)",
                    [(name, json.dumps([x.strip() for x in k.split(",")], ensure_ascii=False), now)
                     for name, k in DEFAULT_CATEGORIES])

    @staticmethod
    def _normalize_header(name: str) -> str:
        return name.strip().lower().replace(" ", "_")

    def _column_map(self, headers: list[str]) -> dict[str, str]:
        normalized = {self._normalize_header(header): header for header in headers}
        result: dict[str, str] = {}
        for field_name, aliases in FIELD_ALIASES.items():
            match = next((normalized[alias] for alias in aliases if alias in normalized), None)
            if match:
                result[field_name] = match
        missing = {"tx_id", "paid_at", "merchant", "amount"} - set(result)
        if missing:
            raise ToolError(f"CSV 缺少必要字段:{', '.join(sorted(missing))}")
        return result

    @staticmethod
    def _normalize_datetime(value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("交易时间为空")
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(sep=" ")
        except ValueError:
            for pattern in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(text, pattern).isoformat(sep=" ")
                except ValueError:
                    continue
        raise ValueError(f"无法识别交易时间:{text}")

    @staticmethod
    def mask_pii(text: str) -> tuple[str, dict[str, int]]:
        masked, counts = text, {}
        for name, pattern, replacement in _PII_PATTERNS:
            masked, count = pattern.subn(replacement, masked)
            if count:
                counts[name] = count
        return masked, counts

    @staticmethod
    def _category_rules(db: sqlite3.Connection,
                        owner: str | None = None) -> list[tuple[int, str, list[str]]]:
        # 自动分类只看同一 owner 的启用规则
        if owner is None:
            rows = db.execute("SELECT id, name, keywords FROM categories WHERE enabled=1 ORDER BY id")
        else:
            clause, params = BillService._owner_clause(owner)
            rows = db.execute(
                f"SELECT id, name, keywords FROM categories WHERE enabled=1 AND {clause} ORDER BY id",
                params)
        rules = []
        for row in rows:
            try:
                keywords = json.loads(row["keywords"])
            except json.JSONDecodeError:
                keywords = []
            rules.append((row["id"], row["name"], keywords))
        return rules

    @staticmethod
    def _ensure_category(db: sqlite3.Connection, name: str, now: str,
                         owner: str | None = None) -> int:
        db.execute("INSERT INTO categories(name, keywords, enabled, created_at, owner)"
                   " VALUES (?, '[]', 1, ?, ?) ON CONFLICT DO NOTHING",
                   (name, now, owner))
        if owner is None:
            return db.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()[0]
        clause, params = BillService._owner_clause(owner)
        return db.execute(
            f"SELECT id FROM categories WHERE name=? AND {clause}", (name, *params)).fetchone()[0]

    def _resolve_category(self, db: sqlite3.Connection, rules: list[tuple[int, str, list[str]]],
                          category_name: str, merchant: str, note: str, now: str,
                          owner: str | None = None) -> int:
        if category_name:
            if owner is None:
                row = db.execute("SELECT id FROM categories WHERE name=?", (category_name,)).fetchone()
            else:
                clause, params = BillService._owner_clause(owner)
                row = db.execute(
                    f"SELECT id FROM categories WHERE name=? AND {clause}",
                    (category_name, *params)).fetchone()
            if row:
                return row[0]
        haystack = f"{merchant} {note}".lower()
        for category_id, _, keywords in rules:
            if any(str(keyword).lower() in haystack for keyword in keywords):
                return category_id
        return self._ensure_category(db, "其他", now, owner)

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
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                        (tx_id, paid_at, merchant, note, amount, method, now, owner))
                    if cursor.rowcount == 0:
                        duplicates += 1
                        continue
                    imported += 1
                    category_id = self._resolve_category(db, rules, category_name, merchant, note, now, owner)
                    db.execute(f"UPDATE transactions SET category_id=? WHERE tx_id=?{owner_and}",
                               (category_id, tx_id, *owner_params))
                except (ValueError, TypeError) as exc:
                    failed += 1
                    if len(errors) < 20:
                        errors.append(f"第 {line_number} 行:{exc}")
            status = "completed" if failed == 0 else ("partial" if imported else "failed")
            db.execute(
                """INSERT INTO imports(filename, total_rows, imported_rows, duplicate_rows,
                   failed_rows, failed_reasons, status, imported_at, owner) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                        f"SELECT id FROM subscriptions WHERE name=? AND merchant=?{owner_and}",
                        (name, merchant, *owner_params)).fetchone()
                    if exists:
                        duplicates += 1
                        continue
                    db.execute(
                        """INSERT INTO subscriptions(name, merchant, cycle, expected_amount, created_at, owner)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (name, merchant, cycle, expected, now, owner))
                    imported += 1
                except (ValueError, TypeError) as exc:
                    failed += 1
                    if len(errors) < 20:
                        errors.append(f"第 {line_number} 行:{exc}")
            status = "completed" if failed == 0 else ("partial" if imported else "failed")
            db.execute(
                """INSERT INTO imports(filename, total_rows, imported_rows, duplicate_rows,
                   failed_rows, failed_reasons, status, imported_at, owner) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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

    @staticmethod
    def _where(filters: BillFilters, alias: str = "t",
               owner: str | None = None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        mapping = {
            "merchant": f"{alias}.merchant = ?",
            "method": f"{alias}.method = ?",
            "status": f"{alias}.status = ?",
            "min_amount": f"{alias}.amount >= ?",
            "max_amount": f"{alias}.amount <= ?",
        }
        for field, clause in mapping.items():
            value = getattr(filters, field)
            if value not in (None, ""):
                clauses.append(clause)
                params.append(value)
        # 纯日期自动补全当天边界,避免整点时间被 date_to 排除
        if filters.date_from:
            clauses.append(f"{alias}.paid_at >= ?")
            params.append(f"{filters.date_from} 00:00:00" if len(filters.date_from) == 10 else filters.date_from)
        if filters.date_to:
            clauses.append(f"{alias}.paid_at <= ?")
            params.append(f"{filters.date_to} 23:59:59.999999" if len(filters.date_to) == 10 else filters.date_to)
        if filters.category:
            clauses.append(f"{alias}.category_id IN (SELECT id FROM categories WHERE name=?)")
            params.append(filters.category)
        if filters.query:
            clauses.append(
                f"({alias}.tx_id LIKE ? OR {alias}.merchant LIKE ? OR {alias}.note LIKE ? "
                f"OR EXISTS (SELECT 1 FROM categories cx WHERE cx.id={alias}.category_id AND cx.name LIKE ?))"
            )
            value = f"%{filters.query}%"
            params.extend((value, value, value, value))
        if owner is not None:
            clause, owner_params = BillService._owner_clause(owner, f"{alias}.owner")
            clauses.append(clause)
            params.extend(owner_params)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    def overview(self, filters: BillFilters | None = None, owner: str | None = None) -> dict[str, Any]:
        filters = filters or BillFilters()
        where, params = self._where(filters, owner=owner)
        with self._connect() as db:
            row = db.execute(
                f"""SELECT COALESCE(SUM(amount), 0) AS total_amount, COUNT(*) AS count,
                    COUNT(DISTINCT substr(paid_at, 1, 10)) AS active_days FROM transactions t{where}""",
                params).fetchone()
            pending = db.execute(
                f"SELECT COUNT(*) FROM transactions t{where}{' AND' if where else ' WHERE'} t.status IN ('待核查','核查中')",
                params).fetchone()[0]
            by_category = [dict(item) for item in db.execute(
                f"""SELECT COALESCE(c.name, '未分类') AS name, ROUND(SUM(t.amount), 2) AS amount, COUNT(*) AS count
                    FROM transactions t LEFT JOIN categories c ON c.id=t.category_id{where}
                    GROUP BY c.name ORDER BY amount DESC""", params)]
            top_merchants = [dict(item) for item in db.execute(
                f"""SELECT t.merchant AS name, ROUND(SUM(t.amount), 2) AS amount, COUNT(*) AS count
                    FROM transactions t{where} GROUP BY t.merchant ORDER BY amount DESC LIMIT 8""", params)]
            max_tx = db.execute(
                f"""SELECT t.tx_id, t.paid_at, t.merchant, COALESCE(c.name, '未分类') AS category, t.amount
                    FROM transactions t LEFT JOIN categories c ON c.id=t.category_id{where}
                    ORDER BY t.amount DESC, t.paid_at DESC LIMIT 1""", params).fetchone()
            trend_rows = [dict(item) for item in db.execute(
                f"""SELECT substr(t.paid_at, 1, 10) AS date, ROUND(SUM(t.amount), 2) AS amount
                    FROM transactions t{where} GROUP BY date ORDER BY date DESC LIMIT 90""", params)]
            trend_rows.reverse()
            owner_where, owner_params = self._owner_where(owner)
            options = {
                "categories": [item[0] for item in db.execute(
                    f"SELECT name FROM categories{owner_where} ORDER BY name", owner_params)],
                "methods": [item[0] for item in db.execute(
                    f"SELECT DISTINCT method FROM transactions{owner_where} ORDER BY method", owner_params)],
            }
        active_days = row["active_days"]
        return {
            "total_amount": round(row["total_amount"], 2),
            "count": row["count"],
            "pending": pending,
            "avg_daily": round(row["total_amount"] / active_days, 2) if active_days else 0.0,
            "by_category": by_category,
            "top_merchants": top_merchants,
            "max_tx": dict(max_tx) if max_tx else None,
            "trend": trend_rows,
            "options": options,
        }

    def compare(self, days: int = 7, owner: str | None = None) -> dict[str, Any]:
        days = min(max(1, days), 90)
        with self._connect() as db:
            owner_where, owner_params = self._owner_where(owner)
            # 锚点=该 owner 数据的最新日期,避免窗口被别人的数据带偏
            latest = db.execute(
                f"SELECT MAX(substr(paid_at, 1, 10)) FROM transactions{owner_where}",
                owner_params).fetchone()[0]
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
                f"SELECT MAX(substr(paid_at, 1, 10)) FROM transactions{owner_where}",
                owner_params).fetchone()[0]
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
                        WHERE substr(paid_at, 1, 10) BETWEEN ? AND ?{owner_and} ORDER BY paid_at""",
                    (current_from, current_to, *owner_params)).fetchall()
                groups: dict[tuple[str, float], list[sqlite3.Row]] = {}
                for row in rows:
                    groups.setdefault((row["merchant"], round(row["amount"], 2)), []).append(row)
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
                        f"SELECT * FROM subscriptions WHERE active=1{owner_and} ORDER BY id", owner_params):
                    row = db.execute(
                        f"""SELECT amount, paid_at FROM transactions WHERE merchant=?
                            AND substr(paid_at, 1, 10) BETWEEN ? AND ?{owner_and} ORDER BY paid_at DESC LIMIT 1""",
                        (sub["merchant"], current_from, current_to, *owner_params)).fetchone()
                    if not row:
                        continue
                    expected, actual = sub["expected_amount"], row["amount"]
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
                rows = [dict(item) for item in db.execute(
                    f"""SELECT t.tx_id, t.merchant, t.amount, t.paid_at, COALESCE(c.name, '未分类') AS category
                        FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
                        WHERE substr(t.paid_at, 1, 10) BETWEEN ? AND ?{owner_and_tx}""",
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
            total = db.execute(f"SELECT COUNT(*) FROM transactions t{where}", params).fetchone()[0]
            rows = db.execute(
                f"""SELECT t.tx_id, t.paid_at, t.merchant, COALESCE(c.name, '未分类') AS category, t.amount,
                    t.method, t.status, t.note, t.created_at
                    FROM transactions t LEFT JOIN categories c ON c.id=t.category_id{where}
                    ORDER BY t.paid_at DESC, t.tx_id LIMIT ? OFFSET ?""",
                [*params, page_size, (page - 1) * page_size])
            items = [{**dict(row), "note": self.mask_pii(row["note"])[0]} for row in rows]
        result = {"items": items, "total": total, "page": page, "page_size": page_size}
        if total == 0 and (filters.query or filters.merchant):
            result["retry_hint"] = "当前筛选未命中数据。请放宽筛选条件或缩短关键词后重试一次;重试仍为空时只能报告证据不足。"
        return result

    def samples(self, merchant: str | None = None, category: str | None = None, query: str | None = None,
                limit: int = 10, date_from: str | None = None, date_to: str | None = None,
                owner: str | None = None) -> dict[str, Any]:
        # 检索优先级:商户精确 > 类别 > 关键词
        if merchant:
            filters = BillFilters(merchant=merchant, date_from=date_from or "", date_to=date_to or "")
        elif category:
            filters = BillFilters(category=category, date_from=date_from or "", date_to=date_to or "")
        else:
            filters = BillFilters(query=query or "", date_from=date_from or "", date_to=date_to or "")
        result = self.query(filters, 1, min(max(1, limit), 20), owner=owner)
        samples = [{key: item[key] for key in ("tx_id", "paid_at", "merchant", "category", "amount", "note")}
                   for item in result["items"]]
        response = {"samples": samples, "matched": result["total"], "pii_masked": True}
        if result.get("retry_hint"):
            response["retry_hint"] = result["retry_hint"]
        return response

    def categories(self, owner: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            owner_where, owner_params = self._owner_where(owner, "c.owner")
            rows = db.execute(f"""SELECT c.id, c.name, c.keywords, c.enabled, c.created_at,
                                 COUNT(t.tx_id) AS count FROM categories c
                                 LEFT JOIN transactions t ON t.category_id=c.id{owner_where}
                                 GROUP BY c.id ORDER BY count DESC, c.name""", owner_params)
            return [{**dict(row), "keywords": json.loads(row["keywords"])} for row in rows]

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
                    f"SELECT * FROM categories WHERE id=?{owner_and}",
                    (category_id, *owner_params)).fetchone()
                if not row:
                    raise ToolError(f"类别不存在:{category_id}")
                old = {"name": row["name"], "keywords": json.loads(row["keywords"]), "enabled": bool(row["enabled"])}
                try:
                    db.execute(f"UPDATE categories SET name=?, keywords=?, enabled=? WHERE id=?{owner_and}",
                               (name, json.dumps(cleaned, ensure_ascii=False), int(enabled),
                                category_id, *owner_params))
                except sqlite3.IntegrityError as exc:
                    raise ToolError(f"类别名称已存在:{name}") from exc
                action = "update"
            else:
                try:
                    cursor = db.execute(
                        "INSERT INTO categories(name, keywords, enabled, created_at, owner) VALUES (?, ?, ?, ?, ?)",
                        (name, json.dumps(cleaned, ensure_ascii=False), int(enabled), now, owner))
                except sqlite3.IntegrityError as exc:
                    raise ToolError(f"类别名称已存在:{name}") from exc
                category_id = cursor.lastrowid
            new = {"name": name, "keywords": cleaned, "enabled": enabled}
            db.execute(
                """INSERT INTO tx_audits(tx_id, action, operator, new_value, changed_at, owner)
                   VALUES ('', 'category', ?, ?, ?, ?)""",
                (operator, json.dumps({"rule_action": action, "id": category_id, "old": old, "new": new},
                                      ensure_ascii=False), now, owner))
        return {"id": category_id, **new, "created_at": now}

    def delete_category(self, category_id: int, operator: str = "web-user",
                        owner: str | None = None) -> dict[str, Any]:
        now = _now()
        with self._connect() as db:
            owner_and, owner_params = self._owner_and(owner)
            row = db.execute(
                f"SELECT * FROM categories WHERE id=?{owner_and}",
                (category_id, *owner_params)).fetchone()
            if not row:
                raise ToolError(f"类别不存在:{category_id}")
            old = {"name": row["name"], "keywords": json.loads(row["keywords"]), "enabled": bool(row["enabled"])}
            # 引用该类别的交易置空,由 rematch_categories 或关键词规则重新归类
            affected = db.execute(
                f"SELECT tx_id FROM transactions WHERE category_id=?{owner_and}",
                (category_id, *owner_params)).fetchall()
            db.execute(f"UPDATE transactions SET category_id=NULL WHERE category_id=?{owner_and}",
                       (category_id, *owner_params))
            db.execute(
                """INSERT INTO tx_audits(tx_id, action, operator, new_value, changed_at, owner)
                   VALUES ('', 'category', ?, ?, ?, ?)""",
                (operator, json.dumps({"rule_action": "delete", "id": category_id, "old": old,
                                       "unassigned": [item["tx_id"] for item in affected]}, ensure_ascii=False), now, owner))
            db.execute(f"DELETE FROM categories WHERE id=?{owner_and}", (category_id, *owner_params))
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
                    db.execute(f"UPDATE transactions SET category_id=? WHERE tx_id=?{owner_and}",
                               (new_id, row["tx_id"], *owner_params))
                    db.execute(
                        """INSERT INTO tx_audits(tx_id, action, operator, new_value, changed_at, owner)
                           VALUES (?, 'category', ?, ?, ?, ?)""",
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
                    f"SELECT status FROM transactions WHERE tx_id=?{owner_and}",
                    (tx_id, *owner_params)).fetchone()
                if not row:
                    continue
                db.execute(f"UPDATE transactions SET status=? WHERE tx_id=?{owner_and}",
                           (status, tx_id, *owner_params))
                db.execute(
                    """INSERT INTO tx_audits(tx_id, action, operator, new_value, changed_at, owner)
                       VALUES (?, 'workflow', ?, ?, ?, ?)""",
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
                f"""SELECT a.* FROM tx_audits a WHERE a.tx_id=?{audit_and} AND EXISTS
                    (SELECT 1 FROM transactions t WHERE t.tx_id=a.tx_id{owner_and})
                    ORDER BY a.id LIMIT ?""",
                (tx_id, *audit_params, *owner_params, min(max(1, limit), 200)))]

    def recent_audits(self, limit: int = 50, owner: str | None = None) -> list[dict[str, Any]]:
        """最近的全量审计记录(类别规则 / 核查状态 / 订阅),供看板展示。"""
        with self._connect() as db:
            owner_where, owner_params = self._owner_where(owner)
            return [dict(row) for row in db.execute(
                f"SELECT * FROM tx_audits{owner_where} ORDER BY id DESC LIMIT ?",
                (*owner_params, min(max(1, limit), 200)))]

    def imports(self, limit: int = 20, owner: str | None = None) -> list[dict[str, Any]]:
        """最近导入批次;账单与订阅 CSV 共用 imports 表。"""
        with self._connect() as db:
            owner_where, owner_params = self._owner_where(owner)
            return [dict(row) for row in db.execute(
                f"SELECT * FROM imports{owner_where} ORDER BY id DESC LIMIT ?",
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
                   LEFT JOIN categories c ON c.id=t.category_id WHERE t.tx_id=?{owner_and}""",
                (tx_id, *owner_params)).fetchone()
            if not row:
                raise ToolError(f"交易不存在:{tx_id}")
            category_id = self._ensure_category(db, category, now, owner)
            db.execute(f"UPDATE transactions SET category_id=? WHERE tx_id=?{owner_and_plain}",
                       (category_id, tx_id, *plain_params))
            db.execute(
                """INSERT INTO tx_audits(tx_id, action, operator, new_value, changed_at, owner)
                   VALUES (?, 'category', ?, ?, ?, ?)""",
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
            return [{**dict(row), "active": bool(row["active"])} for row in rows]

    def set_subscription_active(self, subscription_id: int, active: bool, operator: str = "web-user",
                                owner: str | None = None) -> dict[str, Any]:
        now = _now()
        with self._connect() as db:
            owner_and, owner_params = self._owner_and(owner)
            row = db.execute(
                f"SELECT * FROM subscriptions WHERE id=?{owner_and}",
                (subscription_id, *owner_params)).fetchone()
            if not row:
                raise ToolError(f"订阅不存在:{subscription_id}")
            db.execute(f"UPDATE subscriptions SET active=? WHERE id=?{owner_and}",
                       (int(bool(active)), subscription_id, *owner_params))
            db.execute(
                """INSERT INTO tx_audits(tx_id, action, operator, new_value, changed_at, owner)
                   VALUES ('', 'subscription', ?, ?, ?, ?)""",
                (operator, json.dumps({"id": subscription_id, "name": row["name"],
                                       "active": bool(active)}, ensure_ascii=False), now, owner))
            return {**dict(row), "active": bool(active)}

    def save_report(self, session_id: str, title: str, content: str,
                    owner: str | None = None) -> dict[str, Any]:
        title = title.strip()[:160]
        content = content.strip()
        if not title or not content:
            raise ToolError("报告标题和内容不能为空")
        created_at = _now()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO reports(session_id, title, content, created_at, owner) VALUES (?, ?, ?, ?, ?)",
                (session_id, title, content, created_at, owner))
            report_id = cursor.lastrowid
        return {"id": report_id, "session_id": session_id, "title": title, "content": content, "created_at": created_at}

    def reports(self, limit: int = 50, owner: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            owner_where, owner_params = self._owner_where(owner)
            return [dict(row) for row in db.execute(
                f"SELECT id, session_id, title, content, created_at FROM reports{owner_where}"
                " ORDER BY id DESC LIMIT ?",
                (*owner_params, min(max(1, limit), 200)))]

    def delete_report(self, report_id: int, owner: str | None = None) -> dict[str, Any]:
        with self._connect() as db:
            owner_and, owner_params = self._owner_and(owner)
            row = db.execute(
                f"SELECT id, title FROM reports WHERE id=?{owner_and}",
                (report_id, *owner_params)).fetchone()
            if not row:
                raise ToolError(f"报告不存在:{report_id}")
            db.execute(f"DELETE FROM reports WHERE id=?{owner_and}", (report_id, *owner_params))
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
                writer.writerow(tuple(row))
        return "\ufeff" + output.getvalue()

    def purge_owner(self, owner: str, operator: str = "", note: str = "") -> dict:
        """删除用户的全部业务数据(账号删除/自助清空时调用);不触碰 NULL 存量行。"""
        # admin 的可见范围包含 NULL 存量行,清空语义与其视图一致:连带清除
        where, params = (("owner = ? OR owner IS NULL", (owner,))
                         if owner == "admin" else ("owner = ?", (owner,)))
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
                    "VALUES ('__purge__', 'purge', ?, ?, ?, ?)",
                    (operator, note[:200], _now(), owner))
            return counts

    def for_user(self, owner: str) -> "_ScopedBills":
        owner = owner.strip()
        if not owner:
            raise ToolError("owner 不能为空")
        self._ensure_user_categories(owner)
        return _ScopedBills(self, owner)

    def scoped_or_legacy(self, owner: str = "") -> "_ScopedBills":
        """MCP bill 服务器的数据边界原语。

        非空 owner 与 for_user 同义(本人视图 + 默认类别播种);空串在这里是
        合法边界——“仅存量 NULL 行”(_owner_clause 的空串分支),对应服务器端
        未注入身份时的默认形态。for_user 拒绝空 owner(web 层的真实身份不允许
        为空),所以这里直接构造 _ScopedBills(self, "") 表达 NULL-only 语义,
        仅限服务端注入链路使用,不经用户输入直取。"""
        owner = str(owner or "").strip()
        if owner:
            return self.for_user(owner)
        return _ScopedBills(self, "")

    def _ensure_user_categories(self, owner: str) -> None:
        """首次访问时为无任何类别的 owner 播种默认类别副本(带 owner 戳)。"""
        with self._connect() as db:
            clause, params = self._owner_clause(owner)
            if db.execute(f"SELECT 1 FROM categories WHERE {clause} LIMIT 1", params).fetchone():
                return
            now = _now()
            db.executemany(
                "INSERT INTO categories(name, keywords, enabled, created_at, owner) VALUES (?, ?, 1, ?, ?)",
                [(name, json.dumps([x.strip() for x in k.split(",")], ensure_ascii=False), now, owner)
                 for name, k in DEFAULT_CATEGORIES])


class _ScopedBills:
    """BillService 的按用户受限视图:查询自动过滤,写入自动盖戳。"""

    _METHODS = ("overview", "compare", "anomalies", "query", "samples",
                "categories", "save_category", "delete_category",
                "rematch_categories", "update_workflow", "transaction_audits",
                "subscriptions", "set_subscription_active", "import_bills",
                "import_subscriptions", "reports", "save_report",
                "delete_report", "export_csv", "update_transaction_category",
                "imports", "recent_audits")

    def __init__(self, service: "BillService", owner: str) -> None:
        self._service = service
        self._owner = owner

    def mask_pii(self, text: str) -> tuple[str, dict[str, int]]:
        """脱敏是与 owner 无关的纯文本函数,直接透传(不注入 owner)。"""
        return BillService.mask_pii(text)

    def __getattr__(self, name: str):
        if name not in _ScopedBills._METHODS:
            raise AttributeError(name)
        method = getattr(self._service, name)

        def bound(*args, **kwargs):
            kwargs["owner"] = self._owner
            return method(*args, **kwargs)

        return bound
