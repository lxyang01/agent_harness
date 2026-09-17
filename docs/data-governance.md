# 数据治理(distributed 分支)

本文回答四个问题:系统存了什么(清单)、敏感数据在哪些面可见(PII 边界)、
删除到底删掉什么(删除语义)、以及备份与保留期怎么办。**除标注"生产建议/
未实现"的段落外,均为当前代码的实际行为**。

- 角色模型:仅 `user` / `admin` 两角色(`billguard/auth.py` `ROLES`);
  业务表全部带 `owner` 戳,普通用户只见自己的行;**admin 的视野额外包含
  `owner IS NULL` 的存量行**(`storage_pg.py` `_owner_clause`),与单进程版
  语义一致。

## 1. 数据清单

### 1.1 PostgreSQL(13 张表,`migrations/V001_init.up.sql`)

| 表 | 内容 | 敏感性 | 保留期(现状) | 谁可读 |
| --- | --- | --- | --- | --- |
| `users` | username、PBKDF2 password_hash+salt、role、disabled、created_at | **高**(凭据材料) | 永久(无清理) | 管理员经 `/api/admin/users` 列表(**不含哈希**);DBA 直连全量 |
| `transactions` | tx_id、paid_at、merchant、**note(PII 原文)**、amount、method、status、owner | **高** | 永久 | 本人;admin(含 NULL 存量行);模型经工具只见**脱敏** note |
| `categories` | 类别名、关键词规则、enabled、owner | 低 | 永久 | 本人/admin |
| `subscriptions` | 名称、商户、周期、预期金额、**note(原文)**、owner | 中 | 永久 | 本人/admin(页面不渲染 note 列) |
| `tx_audits` | 交易/类别/订阅/清空操作审计:action、operator、new_value(含旧值)、owner | 中 | 永久 | 本人/admin(按 owner 戳过滤) |
| `imports` | 导入批次统计(文件名、行数、失败原因)、owner | 低 | 永久 | 本人/admin |
| `reports` | 分析报告标题与正文(用户/模型生成)、session_id、owner | 中 | 永久 | 本人/admin |
| `approvals` | 审批:工具名、**arguments(参数原文)**、checkpoint、状态流转、decided_by、decision_note、execution_result | 中 | 永久(会话删除时连带删) | 会话归属人/admin |
| `wi_approvals` | 工单域审批:payload(标题/描述/优先级)、状态、decided_by | 中 | 永久 | 经审批面(会话归属链) |
| `issues` | 工单:标题、描述、created_by、approval_id、payload | 中 | 永久 | 经工具/审批链 |
| `sessions` | 对话会话:owner、summary、**messages JSONB(用户输入与模型回答原文)** | 高 | 永久(无自动清理) | 会话归属人;无归属会话仅 admin |
| `evidence` | (session_id, answer_hash) → 证据条目(label/description/filters,来自工具聚合结果,**不含 note 原文**) | 低 | 永久(随会话删除) | 会话归属人 |
| `traces` | (session_id, trace_id) → events JSONB:**工具调用参数与完整结果、模型输出事件、token 用量/cost**(见 §3) | 中-高 | 永久(随会话删除) | 会话归属人 |

另有迁移账本 `schema_migrations`(version, applied_at)——纯元数据,
由 `billguard/migrate.py` 自建管理,不含业务数据。

### 1.2 Redis(5 类键,`billguard/coordination.py`)

| 键 | 内容 | TTL | 丢失影响 |
| --- | --- | --- | --- |
| `lock:session:{sha256(session_id)}` | 值=持有者 uuid(会话锁) | run_timeout+60s(默认 180s) | 锁提前消失(见并发文档 §3) |
| `llm:slots` | 集群在飞 LLM 请求数 | 120s(每次成功获取刷新) | 额度立即回满 |
| `auth:token:{sha256(token)}` | 值=username(**无令牌明文**) | 7 天,剩余<6 天滑动续期 | 该用户掉线重登 |
| `auth:user:{username}` | 该用户全部令牌键的 SET(反向索引) | 与令牌对齐并随续期同步 | 改密/删户的整批失效退化为逐键自然过期 |
| `login:fail:{sha256(username\|ip)}` | 登录失败计数 | ≤600s(首败起算固定窗口) | 节流计数清零,可重建 |

Redis 实例**无持久化卷、AOF 关闭(实测 `appendonly no`,RDB 为默认
save 点位但写在容器层)**——容器重建即全部丢失,逐键影响见上表,
`docs/operations.md` §4 有完整权衡。

### 1.3 其它落盘点

| 位置 | 内容 | 说明 |
| --- | --- | --- |
| Docker 卷 `pgdata` | 全部 PG 数据文件 | 含上面 13 张表;`docker compose down -v` 即抹除 |
| Docker 卷 `billdata` | MCP 服务的 `--data-dir` | 分布式模式下 MCP 走 PG,该卷基本空置(留给 SQLite 回退模式) |
| 容器 stdout | web 访问日志(每请求一行)、迁移/启动日志 | `docker compose logs`;宿主机 json-log 文件按 Docker 默认轮转 |
| 宿主机 `.sessions/` | 单进程模式的本地存储 | 分布式模式不用;已 gitignore |
| 已导出文件 | 用户经 `/api/bills/export` 下载的 CSV(**note 原文**) | 落在用户浏览器/本机,系统无法召回 |

## 2. PII 边界

### 2.1 脱敏规则(`billguard/bills.py` `mask_pii` / `_PII_PATTERNS`)

交易备注(note)在进入模型面与页面表格前,按三条正则替换:

| 类别 | 模式(概述) | 替换为 |
| --- | --- | --- |
| 订单号 | `\b(?:SO\|ORD\|NO)[-_]?[A-Za-z0-9-]{5,}\b` | `[订单号]` |
| 手机号 | `(?<!\d)1[3-9]\d{9}(?!\d)` | `[手机号]` |
| 邮箱 | 标准邮箱正则 | `[邮箱]` |

注:`billguard/guardrails.py` 另有一套 `redact_pii`(评测/展示路径用,
订单号前缀集合略不同:`ORD|ORDER|NO`)。两套实现各自独立,规则不完全
一致是现状;统一收敛列入生产方向,不在本分支做。

### 2.2 哪些面看到 note 原文(如实)

| 面 | note 形态 | 依据 |
| --- | --- | --- |
| PostgreSQL 库内 | **原文** | 导入即原文入库,任何脱敏都不发生在存储层 |
| 本人导出 CSV(`/api/bills/export`) | **原文** | `export_csv` 直出 `t.note`;该接口按 `bills_write` 能力保护(所有登录用户都有),`web.py` 能力表有注释明示"导出含未脱敏 note" |
| 页面交易表格 | **脱敏** | `/api/bills/query` → `PGBillService.query` 每行 `mask_pii`(`storage_pg.py`) |
| 模型(Agent 工具面) | **脱敏** | `bill.query`/`get_samples` 工具在返回前脱敏(`billguard/agents/bills.py` `_masked_search`、`billguard/mcp_servers/bill_server.py`);异常检测(anomalies)结果不含 note(工具自述:"anomaly items never expose raw notes") |
| 页面订阅区 | 不渲染 note 列 | `web_static/app.js` 订阅卡片仅名称/周期/商户/最近扣款/预期金额;`subscriptions.note` 原文仅存于库内 |

用户手输文本(对话消息、核查工作流备注 ≤200 字、报告内容)按定义是
用户自己的话,不脱敏,原文进 sessions/tx_audits/reports 并在页面回显。

**模型还能看到什么**:用户消息原文、工具返回(已脱敏)、自身历史回答。
工具结果进入模型会话前还有 1,500 字符/条的截断(`AgentSpec.tool_result_
context_limit`,门禁校验用完整值,截断只影响模型可见面)。

## 3. Trace / Evidence / 报告是否含原文(如实)

- **traces.events**(`PGTraceStore.append_event`):记录 `tool_start`
  (工具名+**参数原文**)、`tool_end`(**完整工具结果**=模型可见的值,
  即已脱敏后的结果——脱敏发生在工具实现内部,先于事件记录)、模型调用
  事件与 token 用量、最终答案文本。结论:**trace 存"模型可见文本"与
  工具结果,不存 note 原文**(除非用户自己把 PII 写进对话消息);
- **evidence**:证据条目由工具结果聚合而来(类别/金额/商户/筛选条件),
  构造点在 `web.py` `_build_evidence`,**不含 note**;
- **reports**:用户保存的报告正文原样入库(`save_report` 仅去首尾空白、
  标题截 160 字),内容含什么取决于用户/模型写了什么——报告里的数字
  受数字门禁约束(必须来自工具证据),但文本本身不脱敏;
- **sessions.messages**:用户输入与助手回答**原文**。
- **approvals.arguments / checkpoint**:模型请求的工具参数与运行检查点
  (含 user_input 原文),原文入库。

## 4. 删除语义

### 4.1 `purge_my_data`(`/api/bills/purge`,自助清空)

调用 `PGBillService.purge_owner(username, operator=username, note=...)`,
**删除且仅删除**该 owner 的:`transactions`、`subscriptions`、
`categories`、`reports`、`tx_audits`、`imports`(admin 清空时额外含
NULL 存量行,与其视野一致),并留一条 `__purge__` 审计标记
(operator、note 截 200 字)证明发生过清空。

**不覆盖**:sessions/traces/evidence(分析会话与 Trace 明确保留——
`web.py` `purge_my_data` docstring)、approvals、users 行、Redis 键、
备份、已导出文件。

### 4.2 `admin_delete_user`(`/api/admin/users/delete`)

1. `PGUserStore.delete`:删 `users` 行,并经 Redis 反向索引**整批失效
   该用户全部登录令牌**;
2. `purge_owner(username)`(不带 operator)——删 §4.1 同一组业务表,
   **不留 purge 审计标记**(operator 为空则跳过插入)。

**不覆盖(级联缺口,如实)**:`sessions`(owner=该用户的对话行仍在)、
`traces`、`evidence`、`approvals`、`wi_approvals`/`issues`
(created_by 关联)、`login:fail` 键(≤600s 自然过期)、备份、已导出文件。
被删用户的会话行没有归属人后成为"无归属会话"——**仅 admin 可见**
(`_require_session_access` 的 owner=None 分支)。生产多租户化时应把
用户删除级联扩到 sessions/traces/evidence;本分支不做(见 §8)。

### 4.3 `delete_session`(`/api/session/delete`)

删该 session_id 的 `sessions` + `traces` + `evidence` 行 +
`approvals`(该会话)。与 chat/decide 共用会话锁,防并发删写。

### 4.4 系统性不覆盖项(任何删除都不碰)

| 项 | 原因 | 缓解 |
| --- | --- | --- |
| pg_dump 备份 | 备份是独立文件 | 备份保留期与销毁流程(§6、ops 文档 §3) |
| 已导出 CSV 文件 | 已离开系统边界 | 无;导出面按登录+能力保护 |
| Redis 登录失败键 | 短 TTL | 600s 内自然消失 |
| 容器访问日志 | stdout/宿主机日志文件 | 日志轮转策略(ops 文档) |

## 5. data_note(空库确定性说明)

overview 工具/接口在结果集为空时返回 `data_note` 字段
(`storage_pg.py` `overview`):整库为空→「账单库为空…请先导入 CSV」;
仅筛选落空→「当前筛选条件下无交易记录」。这是给模型/前端的确定性
锚点,防止在空数据上编造;不属于治理数据,列在此处只因批量文档时
常被问到。

## 6. 备份与加密:现状与生产建议

- **现状**:`docker compose exec -T postgres pg_dump -U billguard billguard
  > backup.sql`——**明文 SQL**,内含 PBKDF2 哈希+盐(可离线爆破)、
  全部 note/对话/trace 原文。无定时任务、无加密、无异地;
- **生产建议(未实现,按序落地)**:
  1. **卷加密 at rest**:pgdata 卷所在磁盘/卷加密(LUKS/云盘加密),
     先堵"拿到磁盘镜像"这条路;
  2. **备份加密**:`pg_dump | gpg --encrypt -r <key>`(或云 KMS 托管),
     备份文件权限 600,与解密密钥分开放;
  3. **备份保留与销毁**:定期(建议每日)+ 保留窗口(建议 30 天)+
     到期销毁,与 §7 保留期表对齐;
  4. **恢复演练**:见 `docs/operations.md` §3(含演练记录模板)。

## 7. 保留期建议表(演示默认 vs 生产建议)

| 数据 | 演示默认(现状) | 生产建议 |
| --- | --- | --- |
| transactions/subscriptions/categories/imports | 永久 | 按业务需要;个人数据场景建议提供导出后自助清空(已有) |
| sessions(对话原文) | 永久 | 180 天后归档或删除(合规通知用户) |
| traces(运行全量) | 永久 | 90 天(审计价值集中在近期;过期可只留汇总) |
| evidence | 随会话删除,否则永久 | 随会话 |
| tx_audits / approvals | 永久 | 审计类建议 ≥1 年,或按合规要求 |
| users 凭据材料 | 永久 | 删户即删(已做);哈希不外发 |
| Redis 各键 | TTL 自愈(见 §1.2) | 无需调整 |
| pg_dump 备份 | 手动、无期限 | 每日 + 30 天滚动 + 加密 |
| 访问日志 | Docker 默认轮转 | 30 天,超期清掉 |

现状没有任何自动清理任务(无 cron/定时器),上表右列全部是**建议**。

## 8. 非目标 / 明确不做

- **不做列级加密/TDE**:note、messages、traces 明文存 PG,靠卷加密与
  访问控制(见安全基线 §9);
- **不做自动保留期执行器**:保留期表是运维约定,不实现清理 job;
- **不做跨系统数据删除编排**(外部工单系统同步擦除):工单数据就在本库
  `issues`/`wi_approvals`,无外部同步;
- **不做用户级数据驻留/分区**(按地区分库):单库单集群形态;
- **不做匿名化/差分隐私发布**:评测报告聚合的是评测夹具,不含用户数据。
