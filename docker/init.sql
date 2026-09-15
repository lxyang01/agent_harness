-- init.sql:由 spec §3.1 表清单逐张建表;列定义从 SQLite 方言平移。
-- 关键差异:INTEGER→BIGINT,金额 NUMERIC(12,2),messages/events/evidence 用 JSONB。
CREATE TABLE IF NOT EXISTS users(
    username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
    salt TEXT NOT NULL, role TEXT NOT NULL,
    disabled BOOLEAN NOT NULL DEFAULT FALSE, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transactions(
    id BIGSERIAL PRIMARY KEY, tx_id TEXT NOT NULL, paid_at TEXT NOT NULL,
    merchant TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
    category_id INTEGER, amount NUMERIC(12,2) NOT NULL,
    method TEXT NOT NULL DEFAULT '未知', status TEXT NOT NULL DEFAULT '正常',
    created_at TEXT NOT NULL, owner TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tx_owner_tx
    ON transactions(COALESCE(owner, ''), tx_id);
CREATE INDEX IF NOT EXISTS idx_tx_paid_at ON transactions(paid_at);
CREATE INDEX IF NOT EXISTS idx_tx_merchant ON transactions(merchant);
CREATE TABLE IF NOT EXISTS categories(
    id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '[]', enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TEXT NOT NULL, owner TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cat_owner_name
    ON categories(COALESCE(owner, ''), name);
CREATE TABLE IF NOT EXISTS subscriptions(
    id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, merchant TEXT NOT NULL,
    cycle TEXT NOT NULL DEFAULT '月', expected_amount NUMERIC(12,2) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE, note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, owner TEXT);
CREATE TABLE IF NOT EXISTS tx_audits(
    id BIGSERIAL PRIMARY KEY, tx_id TEXT NOT NULL, action TEXT NOT NULL,
    operator TEXT NOT NULL, new_value TEXT NOT NULL DEFAULT '',
    changed_at TEXT NOT NULL, owner TEXT);
CREATE TABLE IF NOT EXISTS imports(
    id BIGSERIAL PRIMARY KEY, filename TEXT NOT NULL,
    total_rows INTEGER, imported_rows INTEGER, duplicate_rows INTEGER,
    failed_rows INTEGER, failed_reasons TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL, imported_at TEXT NOT NULL, owner TEXT);
CREATE TABLE IF NOT EXISTS reports(
    id BIGSERIAL PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, owner TEXT);
CREATE TABLE IF NOT EXISTS approvals(
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, trace_id TEXT NOT NULL,
    step INTEGER NOT NULL, tool_name TEXT NOT NULL, arguments JSONB NOT NULL,
    risk_level TEXT NOT NULL, requires_approval BOOLEAN NOT NULL,
    policy_reason TEXT NOT NULL, checkpoint JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT NOT NULL, decided_at TEXT, decided_by TEXT,
    decision_note TEXT, execution_error TEXT, execution_result JSONB,
    work_item_ref TEXT);
CREATE TABLE IF NOT EXISTS wi_approvals(
    id TEXT PRIMARY KEY, action TEXT NOT NULL, payload JSONB NOT NULL,
    status TEXT NOT NULL, requested_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    decided_by TEXT, decided_at TEXT, issue_id INTEGER, next_step TEXT);
CREATE TABLE IF NOT EXISTS issues(
    id BIGSERIAL PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
    priority TEXT NOT NULL, created_by TEXT NOT NULL,
    approval_id TEXT NOT NULL, created_at TEXT NOT NULL, payload JSONB);
CREATE TABLE IF NOT EXISTS sessions(
    session_id TEXT PRIMARY KEY, owner TEXT,
    summary TEXT NOT NULL DEFAULT '', messages JSONB NOT NULL DEFAULT '[]',
    updated_at TEXT);
CREATE TABLE IF NOT EXISTS evidence(
    session_id TEXT NOT NULL, answer_hash TEXT NOT NULL,
    evidence JSONB NOT NULL, PRIMARY KEY(session_id, answer_hash));
CREATE TABLE IF NOT EXISTS traces(
    trace_id TEXT NOT NULL, session_id TEXT NOT NULL,
    agent TEXT NOT NULL, events JSONB NOT NULL DEFAULT '[]',
    created_at TEXT, PRIMARY KEY(session_id, trace_id));
