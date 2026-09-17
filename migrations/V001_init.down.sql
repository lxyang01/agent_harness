-- V001_init.down.sql:按依赖安全顺序拆除 V001 建立的全部对象。
-- 审计/证据/轨迹先于主表,evidence/traces 先于 sessions,wi_approvals 先于
-- issues,users 最后;索引随所属表自动删除。
DROP TABLE IF EXISTS tx_audits;
DROP TABLE IF EXISTS transactions;
DROP TABLE IF EXISTS categories;
DROP TABLE IF EXISTS subscriptions;
DROP TABLE IF EXISTS imports;
DROP TABLE IF EXISTS reports;
DROP TABLE IF EXISTS evidence;
DROP TABLE IF EXISTS traces;
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS wi_approvals;
DROP TABLE IF EXISTS issues;
DROP TABLE IF EXISTS approvals;
DROP TABLE IF EXISTS users;
