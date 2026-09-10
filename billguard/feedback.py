from __future__ import annotations

import csv
import hashlib
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
from .guardrails import redact_pii


DEFAULT_TAGS = {
    "支付问题": ["支付失败", "扣款", "付款", "微信支付", "支付宝", "重复扣款"],
    "退款问题": ["退款", "退钱", "不到账", "退回"],
    "登录问题": ["登录", "验证码", "密码", "账号", "短信"],
    "订单问题": ["订单", "下单", "取消订单", "订单状态"],
    "性能问题": ["卡顿", "很慢", "加载", "闪退", "崩溃"],
    "功能建议": ["建议", "希望", "能不能", "增加", "功能"],
}


FIELD_ALIASES = {
    "ticket_id": ("ticket_id", "工单id", "工单_id", "工单编号", "id"),
    "created_at": ("created_at", "创建时间", "时间", "created_time"),
    "product_module": ("product_module", "产品模块", "模块", "module"),
    "content": ("content", "反馈内容", "反馈", "内容", "message"),
    "customer_tier": ("customer_tier", "客户等级", "客户级别", "tier"),
    "status": ("status", "处理状态", "状态"),
}


@dataclass(frozen=True)
class FeedbackFilters:
    date_from: str | None = None
    date_to: str | None = None
    product_module: str | None = None
    customer_tier: str | None = None
    status: str | None = None
    priority: str | None = None
    assignee: str | None = None
    tag: str | None = None
    query: str | None = None


class FeedbackService:
    """SQLite-backed feedback store optimized for deterministic aggregation."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "feedback.db"
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

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    product_module TEXT NOT NULL DEFAULT '未分类',
                    content TEXT NOT NULL,
                    customer_tier TEXT NOT NULL DEFAULT '普通',
                    status TEXT NOT NULL DEFAULT '待处理',
                    priority TEXT NOT NULL DEFAULT 'medium',
                    assignee TEXT NOT NULL DEFAULT '',
                    internal_notes TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    source_file TEXT NOT NULL DEFAULT '',
                    imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    keywords TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS feedback_tags (
                    feedback_id INTEGER NOT NULL REFERENCES feedback(id) ON DELETE CASCADE,
                    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    source TEXT NOT NULL DEFAULT 'rule',
                    PRIMARY KEY (feedback_id, tag_id)
                );
                CREATE TABLE IF NOT EXISTS import_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    total_rows INTEGER NOT NULL,
                    imported_rows INTEGER NOT NULL,
                    duplicate_rows INTEGER NOT NULL,
                    failed_rows INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS tag_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
                    old_tags TEXT NOT NULL,
                    new_tags TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    changed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    old_value TEXT NOT NULL,
                    new_value TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    changed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tag_rule_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tag_id INTEGER,
                    action TEXT NOT NULL,
                    old_value TEXT NOT NULL,
                    new_value TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    changed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS insight_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at);
                CREATE INDEX IF NOT EXISTS idx_feedback_module ON feedback(product_module);
                CREATE INDEX IF NOT EXISTS idx_feedback_tier ON feedback(customer_tier);
                CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status);
                CREATE INDEX IF NOT EXISTS idx_feedback_tags_tag ON feedback_tags(tag_id);
            """)
            self._ensure_column(db, "feedback", "priority", "TEXT NOT NULL DEFAULT 'medium'")
            self._ensure_column(db, "feedback", "assignee", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "feedback", "internal_notes", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "feedback", "updated_at", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "tags", "enabled", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(db, "tags", "updated_at", "TEXT NOT NULL DEFAULT ''")
            for name, keywords in DEFAULT_TAGS.items():
                db.execute(
                    "INSERT INTO tags(name, keywords, enabled, updated_at) VALUES (?, ?, 1, ?) ON CONFLICT(name) DO NOTHING",
                    (name, json.dumps(keywords, ensure_ascii=False), self._utc_now()),
                )

    @staticmethod
    def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _normalize_header(name: str) -> str:
        return name.strip().lower().replace(" ", "_")

    def _column_map(self, headers: list[str]) -> dict[str, str]:
        normalized = {self._normalize_header(header): header for header in headers}
        result: dict[str, str] = {}
        for field, aliases in FIELD_ALIASES.items():
            match = next((normalized[a] for a in aliases if a in normalized), None)
            if match:
                result[field] = match
        missing = {"ticket_id", "created_at", "content"} - set(result)
        if missing:
            raise ToolError(f"CSV 缺少必要字段：{', '.join(sorted(missing))}")
        return result

    @staticmethod
    def _normalize_date(value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("创建时间为空")
        normalized = text.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).isoformat()
        except ValueError:
            for pattern in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"):
                try:
                    return datetime.strptime(text, pattern).isoformat()
                except ValueError:
                    continue
        raise ValueError(f"无法识别创建时间：{text}")

    @staticmethod
    def mask_pii(text: str) -> str:
        return redact_pii(text)[0]

    def _tag_rules(self, db: sqlite3.Connection) -> list[tuple[int, str, list[str]]]:
        rules = []
        for row in db.execute("SELECT id, name, keywords FROM tags WHERE enabled=1 ORDER BY id"):
            try:
                keywords = json.loads(row["keywords"])
            except json.JSONDecodeError:
                keywords = []
            rules.append((row["id"], row["name"], keywords))
        return rules

    def import_csv(self, filename: str, csv_text: str) -> dict[str, Any]:
        if not csv_text.strip():
            raise ToolError("CSV 内容为空")
        digest = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
        reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
        if not reader.fieldnames:
            raise ToolError("CSV 没有表头")
        columns = self._column_map(reader.fieldnames)
        total = imported = duplicates = failed = 0
        errors: list[str] = []
        now = self._utc_now()
        with self._connect() as db:
            rules = self._tag_rules(db)
            for line_number, row in enumerate(reader, start=2):
                total += 1
                try:
                    ticket_id = str(row.get(columns["ticket_id"], "")).strip()
                    content = str(row.get(columns["content"], "")).strip()
                    if not ticket_id or not content:
                        raise ValueError("工单 ID 或反馈内容为空")
                    created_at = self._normalize_date(str(row.get(columns["created_at"], "")))
                    module = str(row.get(columns.get("product_module", ""), "未分类")).strip() or "未分类"
                    tier = str(row.get(columns.get("customer_tier", ""), "普通")).strip() or "普通"
                    status = str(row.get(columns.get("status", ""), "待处理")).strip() or "待处理"
                    cursor = db.execute(
                        """INSERT INTO feedback(ticket_id, created_at, product_module, content, customer_tier,
                           status, source_file, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(ticket_id) DO NOTHING""",
                        (ticket_id, created_at, module, content, tier, status, filename, now),
                    )
                    if cursor.rowcount == 0:
                        duplicates += 1
                        continue
                    imported += 1
                    feedback_id = cursor.lastrowid
                    lowered = content.lower()
                    for tag_id, _, keywords in rules:
                        if any(str(keyword).lower() in lowered for keyword in keywords):
                            db.execute(
                                "INSERT OR IGNORE INTO feedback_tags(feedback_id, tag_id, source) VALUES (?, ?, 'rule')",
                                (feedback_id, tag_id),
                            )
                except (ValueError, TypeError) as exc:
                    failed += 1
                    if len(errors) < 20:
                        errors.append(f"第 {line_number} 行：{exc}")
            status = "completed" if failed == 0 else ("partial" if imported else "failed")
            db.execute(
                """INSERT INTO import_jobs(filename, file_hash, imported_at, total_rows, imported_rows,
                   duplicate_rows, failed_rows, status, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (filename, digest, now, total, imported, duplicates, failed, status, "\n".join(errors)),
            )
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
    def _where(filters: FeedbackFilters, alias: str = "f") -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        mapping = {
            "date_from": f"{alias}.created_at >= ?",
            "date_to": f"{alias}.created_at <= ?",
            "product_module": f"{alias}.product_module = ?",
            "customer_tier": f"{alias}.customer_tier = ?",
            "status": f"{alias}.status = ?",
            "priority": f"{alias}.priority = ?",
            "assignee": f"{alias}.assignee = ?",
        }
        for field, clause in mapping.items():
            value = getattr(filters, field)
            if value:
                clauses.append(clause)
                params.append(value)
        if filters.query:
            clauses.append(
                f"({alias}.content LIKE ? OR {alias}.ticket_id LIKE ? "
                f"OR {alias}.product_module LIKE ? OR EXISTS ("
                f"SELECT 1 FROM feedback_tags qx JOIN tags qt ON qt.id=qx.tag_id "
                f"WHERE qx.feedback_id={alias}.id AND qt.name LIKE ?))"
            )
            value = f"%{filters.query}%"
            params.extend((value, value, value, value))
        if filters.tag:
            clauses.append(
                f"EXISTS (SELECT 1 FROM feedback_tags wx JOIN tags tx ON tx.id=wx.tag_id "
                f"WHERE wx.feedback_id={alias}.id AND tx.name=?)"
            )
            params.append(filters.tag)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    def overview(self, filters: FeedbackFilters | None = None) -> dict[str, Any]:
        filters = filters or FeedbackFilters()
        where, params = self._where(filters)
        with self._connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM feedback f{where}", params).fetchone()[0]
            pending = db.execute(
                f"SELECT COUNT(*) FROM feedback f{where}{' AND' if where else ' WHERE'} f.status IN ('待处理','pending','未处理')",
                params,
            ).fetchone()[0]
            modules = [dict(row) for row in db.execute(
                f"SELECT f.product_module AS name, COUNT(*) AS count FROM feedback f{where} GROUP BY f.product_module ORDER BY count DESC LIMIT 10",
                params,
            )]
            tiers = [dict(row) for row in db.execute(
                f"SELECT f.customer_tier AS name, COUNT(*) AS count FROM feedback f{where} GROUP BY f.customer_tier ORDER BY count DESC",
                params,
            )]
            trend = [dict(row) for row in db.execute(
                f"SELECT substr(f.created_at,1,10) AS date, COUNT(*) AS count FROM feedback f{where} GROUP BY date ORDER BY date DESC LIMIT 90",
                params,
            )]
            trend.reverse()
            tag_where = where.replace(" WHERE ", " WHERE ", 1)
            tags = [dict(row) for row in db.execute(
                f"""SELECT t.name, COUNT(*) AS count FROM feedback_tags ft
                    JOIN tags t ON t.id=ft.tag_id JOIN feedback f ON f.id=ft.feedback_id
                    {tag_where} GROUP BY t.id ORDER BY count DESC LIMIT 10""",
                params,
            )]
            options = {
                "modules": [row[0] for row in db.execute("SELECT DISTINCT product_module FROM feedback ORDER BY product_module")],
                "tiers": [row[0] for row in db.execute("SELECT DISTINCT customer_tier FROM feedback ORDER BY customer_tier")],
                "statuses": [row[0] for row in db.execute("SELECT DISTINCT status FROM feedback ORDER BY status")],
                "tags": [row[0] for row in db.execute("SELECT name FROM tags ORDER BY name")],
            }
        return {"total": total, "pending": pending, "modules": modules, "tiers": tiers,
                "trend": trend, "top_tags": tags, "options": options}

    def query(self, filters: FeedbackFilters | None = None, page: int = 1, page_size: int = 30) -> dict[str, Any]:
        filters = filters or FeedbackFilters()
        page = max(1, page)
        page_size = min(max(1, page_size), 100)
        where, params = self._where(filters)
        with self._connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM feedback f{where}", params).fetchone()[0]
            rows = db.execute(
                f"""SELECT f.*, COALESCE(group_concat(t.name, '、'), '') AS tags
                    FROM feedback f LEFT JOIN feedback_tags ft ON ft.feedback_id=f.id
                    LEFT JOIN tags t ON t.id=ft.tag_id {where}
                    GROUP BY f.id ORDER BY f.created_at DESC LIMIT ? OFFSET ?""",
                [*params, page_size, (page - 1) * page_size],
            )
            items = [dict(row) for row in rows]
        result = {"items": items, "total": total, "page": page, "page_size": page_size}
        if total == 0 and (filters.query or filters.tag):
            keyword = filters.query or filters.tag or ""
            broader = keyword.removesuffix("问题").strip()
            result["retry_hint"] = (
                "当前筛选未命中数据。请改用已知标签，或缩短全文检索关键词后重试一次；"
                "重试仍为空时只能报告证据不足。"
            )
            if broader and broader != keyword:
                result["suggested_query"] = broader
        return result

    def samples(self, tag: str | None = None, query: str | None = None, limit: int = 10,
                date_from: str | None = None, date_to: str | None = None) -> dict[str, Any]:
        result = self.query(FeedbackFilters(tag=tag, query=query, date_from=date_from, date_to=date_to), 1, min(limit, 20))
        samples = [{**item, "content": self.mask_pii(item["content"])} for item in result["items"]]
        response = {"samples": samples, "matched": result["total"], "pii_masked": True}
        if result.get("retry_hint"):
            response["retry_hint"] = result["retry_hint"]
        if result.get("suggested_query"):
            response["suggested_query"] = result["suggested_query"]
        return response

    def compare(self, days: int = 7) -> dict[str, Any]:
        days = min(max(1, days), 90)
        with self._connect() as db:
            latest = db.execute("SELECT MAX(substr(created_at,1,10)) FROM feedback").fetchone()[0]
        anchor = datetime.fromisoformat(latest).date() if latest else datetime.now().date()
        current_start = anchor - timedelta(days=days - 1)
        previous_start = current_start - timedelta(days=days)
        previous_end = current_start - timedelta(days=1)
        current = self.overview(FeedbackFilters(date_from=current_start.isoformat(), date_to=f"{anchor.isoformat()}T23:59:59"))
        previous = self.overview(FeedbackFilters(date_from=previous_start.isoformat(), date_to=f"{previous_end.isoformat()}T23:59:59"))
        change = None if previous["total"] == 0 else round((current["total"] - previous["total"]) / previous["total"] * 100, 1)
        return {
            "days": days,
            "current_period": {"from": current_start.isoformat(), "to": anchor.isoformat(), "total": current["total"], "top_tags": current["top_tags"]},
            "previous_period": {"from": previous_start.isoformat(), "to": previous_end.isoformat(), "total": previous["total"], "top_tags": previous["top_tags"]},
            "change_percent": change,
        }

    def anomalies(self, days: int = 7, dimension: str = "tag", limit: int = 10) -> dict[str, Any]:
        """Find dimensions growing between the latest two complete data windows."""
        days = min(max(1, days), 90)
        if dimension not in {"tag", "module"}:
            raise ToolError("dimension 必须是 tag 或 module")
        with self._connect() as db:
            latest = db.execute("SELECT MAX(substr(created_at,1,10)) FROM feedback").fetchone()[0]
            if not latest:
                return {"dimension": dimension, "days": days, "items": [], "anchor_date": None}
            anchor = datetime.fromisoformat(latest).date()
            current_start = anchor - timedelta(days=days - 1)
            previous_end = current_start - timedelta(days=1)
            previous_start = previous_end - timedelta(days=days - 1)
            if dimension == "tag":
                rows = db.execute(
                    """SELECT t.name,
                       SUM(CASE WHEN substr(f.created_at,1,10) BETWEEN ? AND ? THEN 1 ELSE 0 END) AS current_count,
                       SUM(CASE WHEN substr(f.created_at,1,10) BETWEEN ? AND ? THEN 1 ELSE 0 END) AS previous_count
                       FROM tags t LEFT JOIN feedback_tags ft ON ft.tag_id=t.id
                       LEFT JOIN feedback f ON f.id=ft.feedback_id GROUP BY t.id""",
                    (current_start.isoformat(), anchor.isoformat(), previous_start.isoformat(), previous_end.isoformat()),
                )
            else:
                rows = db.execute(
                    """SELECT product_module AS name,
                       SUM(CASE WHEN substr(created_at,1,10) BETWEEN ? AND ? THEN 1 ELSE 0 END) AS current_count,
                       SUM(CASE WHEN substr(created_at,1,10) BETWEEN ? AND ? THEN 1 ELSE 0 END) AS previous_count
                       FROM feedback GROUP BY product_module""",
                    (current_start.isoformat(), anchor.isoformat(), previous_start.isoformat(), previous_end.isoformat()),
                )
            items = []
            for row in rows:
                current = int(row["current_count"] or 0)
                previous = int(row["previous_count"] or 0)
                if current == 0:
                    continue
                change = None if previous == 0 else round((current - previous) / previous * 100, 1)
                items.append({
                    "name": row["name"], "current_count": current, "previous_count": previous,
                    "change_percent": change, "is_new": previous == 0,
                    "delta": current - previous,
                })
            items.sort(key=lambda item: (item["delta"], item["current_count"]), reverse=True)
        return {
            "dimension": dimension, "days": days, "anchor_date": anchor.isoformat(),
            "current_period": {"from": current_start.isoformat(), "to": anchor.isoformat()},
            "previous_period": {"from": previous_start.isoformat(), "to": previous_end.isoformat()},
            "items": items[:min(max(1, limit), 30)],
        }

    def update_tags(self, ticket_id: str, tags: list[str], operator: str = "web-user") -> dict[str, Any]:
        cleaned = list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
        with self._connect() as db:
            feedback = db.execute("SELECT id FROM feedback WHERE ticket_id=?", (ticket_id,)).fetchone()
            if not feedback:
                raise ToolError(f"反馈不存在：{ticket_id}")
            old = [row[0] for row in db.execute(
                "SELECT t.name FROM feedback_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.feedback_id=? ORDER BY t.name",
                (feedback["id"],),
            )]
            db.execute("DELETE FROM feedback_tags WHERE feedback_id=?", (feedback["id"],))
            for name in cleaned:
                db.execute("INSERT INTO tags(name, keywords) VALUES (?, '[]') ON CONFLICT(name) DO NOTHING", (name,))
                tag_id = db.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()[0]
                db.execute("INSERT INTO feedback_tags(feedback_id, tag_id, source) VALUES (?, ?, 'manual')", (feedback["id"], tag_id))
            db.execute(
                "INSERT INTO tag_audit_logs(ticket_id, old_tags, new_tags, operator, changed_at) VALUES (?, ?, ?, ?, ?)",
                (ticket_id, json.dumps(old, ensure_ascii=False), json.dumps(cleaned, ensure_ascii=False), operator, self._utc_now()),
            )
        return {"ticket_id": ticket_id, "old_tags": old, "new_tags": cleaned, "operator": operator}

    def imports(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM import_jobs ORDER BY id DESC LIMIT ?", (limit,))]

    def tags(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""SELECT t.id, t.name, t.keywords, t.enabled, t.updated_at, COUNT(ft.feedback_id) AS count
                               FROM tags t LEFT JOIN feedback_tags ft ON ft.tag_id=t.id
                               GROUP BY t.id ORDER BY count DESC, t.name""")
            result = []
            for row in rows:
                item = dict(row)
                item["keywords"] = json.loads(item["keywords"])
                result.append(item)
            return result

    def save_tag_rule(self, name: str, keywords: list[str], enabled: bool = True,
                      tag_id: int | None = None, operator: str = "web-user") -> dict[str, Any]:
        name = name.strip()[:80]
        cleaned = list(dict.fromkeys(word.strip() for word in keywords if word.strip()))
        if not name:
            raise ToolError("标签名称不能为空")
        now = self._utc_now()
        with self._connect() as db:
            old: dict[str, Any] = {}
            action = "create"
            if tag_id is not None:
                row = db.execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone()
                if not row:
                    raise ToolError(f"标签不存在：{tag_id}")
                old = {"name": row["name"], "keywords": json.loads(row["keywords"]), "enabled": bool(row["enabled"])}
                try:
                    db.execute("UPDATE tags SET name=?, keywords=?, enabled=?, updated_at=? WHERE id=?",
                               (name, json.dumps(cleaned, ensure_ascii=False), int(enabled), now, tag_id))
                except sqlite3.IntegrityError as exc:
                    raise ToolError(f"标签名称已存在：{name}") from exc
                action = "update"
            else:
                try:
                    cursor = db.execute("INSERT INTO tags(name, keywords, enabled, updated_at) VALUES (?, ?, ?, ?)",
                                        (name, json.dumps(cleaned, ensure_ascii=False), int(enabled), now))
                except sqlite3.IntegrityError as exc:
                    raise ToolError(f"标签名称已存在：{name}") from exc
                tag_id = cursor.lastrowid
            new = {"name": name, "keywords": cleaned, "enabled": enabled}
            db.execute("""INSERT INTO tag_rule_audit_logs(tag_id, action, old_value, new_value, operator, changed_at)
                          VALUES (?, ?, ?, ?, ?, ?)""",
                       (tag_id, action, json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False), operator, now))
        return {"id": tag_id, **new, "updated_at": now}

    def delete_tag_rule(self, tag_id: int, operator: str = "web-user") -> dict[str, Any]:
        now = self._utc_now()
        with self._connect() as db:
            row = db.execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone()
            if not row:
                raise ToolError(f"标签不存在：{tag_id}")
            old = {"name": row["name"], "keywords": json.loads(row["keywords"]), "enabled": bool(row["enabled"])}
            db.execute("""INSERT INTO tag_rule_audit_logs(tag_id, action, old_value, new_value, operator, changed_at)
                          VALUES (?, 'delete', ?, '{}', ?, ?)""",
                       (tag_id, json.dumps(old, ensure_ascii=False), operator, now))
            db.execute("DELETE FROM tags WHERE id=?", (tag_id,))
        return {"deleted": {"id": tag_id, **old}}

    def tag_rule_audits(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM tag_rule_audit_logs ORDER BY id DESC LIMIT ?", (min(max(1, limit), 200),)
            )]

    def rematch_tags(self) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("DELETE FROM feedback_tags WHERE source='rule'")
            rules = self._tag_rules(db)
            feedback_count = matched_links = 0
            for row in db.execute("SELECT id, content FROM feedback"):
                feedback_count += 1
                lowered = row["content"].lower()
                for tag_id, _, keywords in rules:
                    if any(str(keyword).lower() in lowered for keyword in keywords):
                        cursor = db.execute(
                            "INSERT OR IGNORE INTO feedback_tags(feedback_id, tag_id, source) VALUES (?, ?, 'rule')",
                            (row["id"], tag_id),
                        )
                        matched_links += max(cursor.rowcount, 0)
        return {"feedback_count": feedback_count, "matched_links": matched_links, "rule_count": len(rules)}

    def update_workflow(self, ticket_ids: list[str], operator: str = "web-user", **updates: Any) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(item).strip() for item in ticket_ids if str(item).strip()))
        if not ids:
            raise ToolError("至少选择一条反馈")
        if len(ids) > 200:
            raise ToolError("单次最多处理 200 条反馈")
        allowed = {"status", "priority", "assignee", "internal_notes"}
        cleaned = {key: str(value).strip() for key, value in updates.items() if key in allowed and value is not None}
        if not cleaned:
            raise ToolError("至少更新一个处理字段")
        if "priority" in cleaned and cleaned["priority"] not in {"low", "medium", "high", "urgent"}:
            raise ToolError("priority 必须是 low、medium、high 或 urgent")
        now = self._utc_now()
        updated: list[str] = []
        with self._connect() as db:
            for ticket_id in ids:
                row = db.execute("SELECT * FROM feedback WHERE ticket_id=?", (ticket_id,)).fetchone()
                if not row:
                    continue
                old = {key: row[key] for key in cleaned}
                assignments = ", ".join(f"{key}=?" for key in cleaned)
                db.execute(f"UPDATE feedback SET {assignments}, updated_at=? WHERE ticket_id=?",
                           [*cleaned.values(), now, ticket_id])
                db.execute("""INSERT INTO feedback_audit_logs(ticket_id, action, old_value, new_value, operator, changed_at)
                              VALUES (?, 'workflow_update', ?, ?, ?, ?)""",
                           (ticket_id, json.dumps(old, ensure_ascii=False), json.dumps(cleaned, ensure_ascii=False), operator, now))
                updated.append(ticket_id)
        return {"updated_ticket_ids": updated, "count": len(updated), "changes": cleaned, "operator": operator}

    def feedback_audits(self, ticket_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM feedback_audit_logs WHERE ticket_id=? ORDER BY id DESC LIMIT ?",
                (ticket_id, min(max(1, limit), 200)),
            )]

    def save_report(self, session_id: str, title: str, content: str) -> dict[str, Any]:
        title = title.strip()[:160]
        content = content.strip()
        if not title or not content:
            raise ToolError("报告标题和内容不能为空")
        created_at = self._utc_now()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO insight_reports(session_id, title, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, title, content, created_at),
            )
            report_id = cursor.lastrowid
        return {"id": report_id, "session_id": session_id, "title": title, "content": content, "created_at": created_at}

    def reports(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id, session_id, title, content, created_at FROM insight_reports ORDER BY id DESC LIMIT ?",
                (min(max(1, limit), 200),),
            )]

    def delete_report(self, report_id: int) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT id, title FROM insight_reports WHERE id=?", (report_id,)).fetchone()
            if not row:
                raise ToolError(f"洞察报告不存在：{report_id}")
            db.execute("DELETE FROM insight_reports WHERE id=?", (report_id,))
        return {"deleted": {"id": row["id"], "title": row["title"]}}

    def export_csv(self, filters: FeedbackFilters | None = None) -> str:
        filters = filters or FeedbackFilters()
        where, params = self._where(filters)
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["ticket_id", "created_at", "product_module", "content", "customer_tier", "status",
                         "priority", "assignee", "internal_notes", "tags"])
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT f.ticket_id, f.created_at, f.product_module, f.content, f.customer_tier, f.status,
                    f.priority, f.assignee, f.internal_notes,
                    COALESCE(group_concat(t.name, '、'), '') AS tags FROM feedback f
                    LEFT JOIN feedback_tags ft ON ft.feedback_id=f.id LEFT JOIN tags t ON t.id=ft.tag_id
                    {where} GROUP BY f.id ORDER BY f.created_at DESC""",
                params,
            )
            for row in rows:
                writer.writerow(tuple(row))
        return "\ufeff" + output.getvalue()
