# 运维手册(distributed 分支)

面向部署者的一本手册:日常启停与发布顺序、备份恢复与演练、Redis
持久化权衡、升级流程、容量规划、监控告警、密钥轮换、故障排查速查。
命令均在仓库根目录(Git Bash)执行;**除标注"建议"的内容外,命令与
行为均为当前代码/编排的实际状态**。

配套:`docker/cluster-smoke.md`(集群冒烟验收)、`docs/security-baseline.md`
(安全)、`docs/data-governance.md`(数据)、`docs/concurrency-guarantees.md`
(并发语义)。

## 1. 拓扑与端口速查

```text
nginx :8080(ip_hash;max_fails=2 fail_timeout=10s)
 ├── web-1 :8001(无状态,容器内端口,不对外发布)
 └── web-2 :8002(无状态,容器内端口,不对外发布)
      ├── postgres :5432   13 张表,卷 pgdata;宿主机 127.0.0.1:5433
      ├── redis    :6379   5 类协调键,无卷;宿主机 127.0.0.1:6380
      ├── bill-server      :8010  账单 MCP(/health 健康检查)
      └── work-item-server :8020  工单 MCP(/health 健康检查)
```

健康检查:postgres `pg_isready`、redis `redis-cli ping`、MCP `/health`、
web `/api/health`(postgres/redis 3s、MCP/web 5s 间隔,各 10 次重试);
`docker compose ps` 的 healthy 即综合结果。

## 2. 日常操作与发布

### 2.1 启停

```bash
docker compose up -d        # 全量启动(缺镜像自动 build)
docker compose down         # 停止并删容器(保留 pgdata/billdata 卷与 admin 账号)
docker compose down -v      # 全量重置(卷一并删除,下次 up 需重新播种 admin)
docker compose kill web-1   # 故障转移演练:nginx 自动切 web-2
docker compose start web-1  # 恢复双实例
```

### 2.2 滚动发布顺序

依赖方向是 web/MCP → PG+Redis,所以**发布顺序**:

1. **PG**(仅当迁移有新版本):新迁移文件已合入 `migrations/` 后,
   `docker compose up -d --build postgres`——无需停其它服务;
   `BILLGUARD_AUTO_MIGRATE=1` 使 web/MCP/users 的 PG 入口在建池后发现
   落后于 `migrations/` 会**先自动补齐再启动**(`billguard/migrate.py`
   `require_current`);不设该变量则启动即退出并打印待应用版本与
   确切修复命令(启动门禁)。多容器并发首启安全:apply 事务先取
   `pg_advisory_xact_lock`(固定锁号),后到者等待并在锁内复查账本跳过;
2. **Redis**:镜像/配置变更时 `docker compose up -d redis`(注意:无卷,
   重建即清空全部键,影响见 §4;常规发布不动它);
3. **MCP**:`docker compose up -d --build bill-server work-item-server`
   (restart: unless-stopped,慢启动自动重试);
4. **web 与 nginx 一起**:`docker compose up -d --build web-1 web-2 nginx`。

**必记的坑**:web 容器被**重建**(`up --build` 或 down 后 up)会拿到
新容器 IP,而 nginx 的静态 upstream 只在 nginx 启动时解析一次 →
**502 Bad Gateway**。修复:`docker compose restart nginx` 重新解析。
`kill`/`start` 复用同一容器、IP 不变,无此问题。发布 web 后永远重启
一次 nginx(顺序里让它们一起 up 即可避免)。

### 2.3 回滚

- 代码回滚:`git checkout <旧版本>` 后重复 §2.2 的 build 步骤
  (迁移账本向后兼容检查:若旧代码缺少新迁移文件,门禁只看
  "库是否落后于目录",多出的已应用版本不阻塞启动);
- schema 精确回退:`python -m billguard.migrate --dsn
  postgresql://billguard:billguard@127.0.0.1:5433/billguard rollback`
  (一次回滚最新一个版本,跑其 `.down.sql`;V001 的 down 会 DROP
  全部业务表、**清空数据**,执行前先备份);
- 演示环境兜底:`down -v` 全量重置(下次 up 重新播种 admin)。

## 3. 备份与恢复

### 3.1 备份(现状一条命令)

```bash
docker compose exec -T postgres pg_dump -U billguard billguard > backup.sql
```

- 内容:**明文 SQL**,含全部 13 张表(用户哈希+盐、note/对话/trace
  原文)——保密要求见 `docs/data-governance.md` §6;
- 频率建议:演示按需;生产每日一次 + 保留 30 天滚动 + 加密存储;
- **RPO(现状)**:手动执行 ⇒ 数据丢失窗口=距上次备份的时长,
  **没有自动定时任务**;生产建议 cron/CI 定时后 RPO=24h(或更短);
- **RTO(定位)**:恢复=起空 PG + 迁移自动建表 + psql 重放,
  全程分钟级(数据量大时取决于重放时长);集群无副本,PG 实例故障
  即停写,恢复路径只有备份重放——**单实例是当前可用性上限**,
  不做主备(见 §10)。

### 3.2 恢复步骤

```bash
docker compose down                 # 停 web(防写入竞争);PG/Redis 可留
docker compose up -d postgres       # pgdata 卷保留时:
#   a) 库还在、只是数据坏了:清库重放
docker compose exec -T postgres psql -U billguard -d billguard -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
docker compose exec -T postgres psql -U billguard -d billguard < backup.sql
#   b) 卷也坏了:down -v 后重来,migrations 自动建表再重放
docker compose up -d                # 起全集群;users 表非空即正常启动
docker compose exec -T postgres psql -U billguard -d billguard -c "SELECT COUNT(*) FROM users;"
```

重放后 schema_migrations 与 migrations/ 目录不一致时,启动门禁会
拦截(AUTO_MIGRATE=1 则自动补齐)——重放的是"当时"的 schema,
若代码已前进,自动迁移会补上新版本。

### 3.3 恢复演练(建议每季度一次)

1. 挑低峰,执行 §3.1 备份得到 `backup.sql`(记录字节数与 MD5);
2. `docker compose down` + `docker volume rm
   feedback-agent-runtime_pgdata`(模拟卷灾难);
3. 按 §3.2(b) 路径恢复;
4. 核验:`/api/health` ok、登录旧账号成功、交易笔数与演练前一致
   (overview 接口或 psql COUNT)、抽查 1 条对话会话可打开;
5. 填写演练记录:

```markdown
## 恢复演练记录
- 日期/执行人:2026-__-__ / ____
- 备份文件:backup-<date>.sql(<size> MB,md5 <hash>)
- 故障模拟:pgdata 卷删除
- 耗时:停服 ___ 分;恢复(PG 起+重放+全集群起)___ 分;核验 ___ 分
- RTO 实测 / RPO 实测:___ / 距上次备份 ___
- 核验结果:health / 登录 / 数据量 / 抽查会话:□ 全部通过
- 偏差与改进:____
```

## 4. Redis 持久化权衡

现状(实测):`appendonly no`(AOF 关),RDB 为默认 save 点位
(`3600 1 / 300 100 / 60 10000`)但 **compose 未给 redis 挂卷**——
快照写在容器层,容器重建即丢。结论:**当前 Redis 视为纯易失内存**,
逐键论证丢失可接受性:

| 键 | 丢失后果 | 为什么可接受 / 可重建 |
| --- | --- | --- |
| `lock:session:*` | 全部会话锁瞬间消失 | 锁本来 TTL ≤180s;清空只把"崩溃自愈"提前。理论双主窗口见并发文档 §3.3(要求同会话双在飞,概率极低) |
| `llm:slots` | 额度立即回满 | 计数器可重建(下一个 acquire 重新起算);瞬间多放≈在飞请求数,量级个位数 |
| `auth:token:*` | 全员登录态失效 | 用户重新登录即重建;无数据损失(会话不在 Redis);代价是体验(全员掉线一次) |
| `auth:user:*` | 反向索引消失 | 随令牌一起消失;下次登录重建;期间改密/删户的"整批失效"退化为逐键自然过期(≤7 天)——短暂弱化,非破坏 |
| `login:fail:*` | 节流计数清零 | 攻击者至多重得 5 次尝试窗口;计数随失败自动重建 |

全部键要么可丢(锁/计数)、要么可重建(令牌/索引),**AOF 关闭是
合理现状**,不是缺陷。若要求"重启不掉登录态":给 redis 挂卷
(`volumes: [redisdata:/data]`)+ 默认 RDB 即可,仍不必开 AOF
(RDB 对这几类键的丢失容忍同样成立,只是窗口从"容器重建"缩到
"最后一次快照")。持久业务数据永远只在 PG。

## 5. 升级与配置变更流程

1. **代码升级**:合入 → §2.2 顺序 build/up → 冒烟(`docker/cluster-smoke.md`
   A 部分,无 Key 可跑)→ 观察 `/api/metrics`;
2. **迁移纪律**(`migrations/`):改表**永远发新版本文件**
   (V<序号>_<名称>.up.sql + .down.sql 成对),不改历史文件;
   存量数据建索引用 `CREATE INDEX CONCURRENTLY`——它不能在事务块内
   跑,与本 runner「每版本一事务」冲突,需单独 psql 执行;
3. **环境变量变更**(`.env` 或 compose):改完 `docker compose up -d`
   即可,compose 会只重建受影响(环境变化的)容器;web 重建后记得
   nginx(§2.2 的坑);
4. **回滚**:§2.3。

## 6. 容量规划

| 维度 | 公式/现状 | 默认值 | 上限/说明 |
| --- | --- | --- | --- |
| PG 连接 | (web 实例数 + MCP 实例数) × max 8(池上限,`new_pg_pool` min2/max8) | 2+2 个池 × 8 = **32** | PG `max_connections=100`(实测);再算 users CLI/测试的临时池(各≤8)。扩 web 实例按每实例 +8 换算:11 个 web 实例(11+2 个池 × 8 = 104)即超过 100,需调 PG `max_connections` 或引入 pgbouncer |
| HTTP 线程 | 每实例 16 线程 + 32 排队 | 容量 48/实例 | 超出立即 503;全局 = 实例数 × 48 |
| LLM 并发 | `--max-concurrent-llm`,Redis 集群共享 | 4 | 超限 429;实例间配置应一致(并发文档 §2) |
| 内存(粗估,以实测为准) | web 进程(Python+httpx/psycopg/redis/mcp)≈150-250MB/实例;PG16 缺省 shared_buffers 128MB、常驻 300-500MB;redis 空载数 MB + 键量(极小);nginx alpine ≈10MB | 2C2G 可跑通演示,4C4G 留有余量 | trace/sessions JSONB 增长才吃 PG 内存,量级取决于对话量 |
| LLM 成本控制 | 并发上限 × 单次 run 成本 × 周转率;run 内模型调用数 ≤ max_steps=8(`AgentSpec`),每次调用上下文 ≤80k 字符 | 4 并发 | 硬上限=并发不超发(除 TTL 边界 1-2 个,并发文档 §2);每次 run 的 token 用量与 cost 记入 trace 汇总(`/api/runs/detail`),可据此核算单价 |

## 7. 监控与告警

### 7.1 指标清单(`GET /api/metrics`,`billguard/metrics.py`)

- **计数器**:`http_requests_total`、`http_status_{code}`(含 401/423/429/503…)、
  `llm_calls_total`/`llm_failures_total`/`llm_retries_total`、
  `tool_calls_total`/`tool_failures_total`、`mcp_calls_total`/
  `mcp_call_failures_total`、`circuit_opens_total`、
  `lock_conflicts_total`、`login_failures_total`、
  `login_throttle_blocks_total`、`approvals_decided_total`;
- **计时**:`http_request_seconds`、`llm_call_seconds`、
  `mcp_call_seconds`(count+sum,均值=sum/count,无分桶);
- **仪表**:`llm_slots_in_use`、`pg_pool_size`/`pg_pool_in_use`、
  `uptime_seconds`。
- **语义(如实)**:**每实例独立**(进程内计数,不跨实例聚合),
  **实例重启即清零**;`instance` 字段标识来源(`host:port`);
  `http_*` 只统计 JSON API(静态文件与静态 404 不计);
  `/api/metrics` 自身不计时时延(抓取间隔不反馈进统计),但状态码
  照常计数;503 快速拒绝路径的计数在 `BoundedHTTPServer` 内直埋,
  同样可见。读该端点需登录(401 拒匿名)。

### 7.2 建议告警规则(表达)

| 告警 | 表达(单实例或聚合) | 阈值建议 |
| --- | --- | --- |
| 429 率(rate) | `sum(rate(http_status_429[5m])) / sum(rate(http_requests_total[5m]))` | >5% 持续 10 分钟 → LLM 限额吃紧,考虑加并发或扩实例 |
| 503 率 | 同上,取 `http_status_503` | >1% → 线程池打满 |
| 熔断 | `increase(circuit_opens_total[10m])` | >0 即告警(MCP 服务不稳) |
| 5xx | `sum(rate(http_status_500[5m]))` | >0 持续 5 分钟 |
| PG 池占用 | `pg_pool_in_use / pg_pool_size` | >0.8 持续 10 分钟 → 连接池逼近上限(§6 公式) |
| 登录失败 | `increase(login_failures_total[10m])` | 突增 → 撞库尝试(节流已限速,告警供观察) |
| 实例消失 | 抓取目标 `up == 0` 或 `uptime_seconds` 突降为 0 | 即时 |

### 7.3 Prometheus 接入路径

端点是 JSON 不是 Prometheus 文本格式,接入方式:**部署一个轻量
exporter(compose 网络内),登录一次拿会话 Cookie → 定期抓取各实例
`/api/metrics`(直连 `http://web-1:8001/api/metrics`、
`http://web-2:8002/...`)→ 把 counters/timings/gauges 翻译成
`billguard_<name>` 文本暴露格式(counter 带 `instance` 标签)→
Prometheus 抓 exporter**。要点:端点要求登录,exporter 需持有
服务账号的 Cookie 并在其过期前重登;timings 的 count/sum 可导出为
`_count` 与 `_sum_seconds`(客户端可算均值,分桶暂无);
重启清零语义下,counter 增量计算要用 rate 而非裸值差。本分支刻意
不引入抓取库(零新增依赖约束),converter 数十行脚本可覆盖。

## 8. 密钥轮换步骤

- **模型 API Key**:改 `.env` 的 `API_KEY` → `docker compose up -d`
  (web 容器环境变化自动重建)→ 冒烟一次真实对话。旧 Key 在供应商侧
  立即作废即可,系统内无缓存(每次调用经 httpx header 携带);
- **用户密码**:`echo '新密码' | docker compose run --rm web-1 -m
  billguard.users reset-password <username> --password-stdin`
  ——reset 即经 Redis 反向索引**整批失效该用户全部登录令牌**(全端
  踢出),这是预期的轮换语义;
- **PG 口令(生产化后)**:`ALTER USER billguard WITH PASSWORD '…'` →
  同步改 compose 两处(`POSTGRES_PASSWORD` 与 `BILLGUARD_PG_DSN`)→
  `up -d` 重建;现状口令是 dev 值(安全基线 §4),轮换前先完成密钥
  管理改造。

## 9. 故障排查速查表

| 症状 | 首查 | 常见原因与处置 |
| --- | --- | --- |
| 全站 502 Bad Gateway | `docker compose ps` | web 容器被重建后 nginx upstream 解析过期 → `docker compose restart nginx`(§2.2 的坑) |
| web 容器反复重启(崩溃循环) | `docker compose logs web-1 \| tail -50` | 日志「用户库为空…」→ 先播种 admin(README「首次启动」);「数据库 schema 落后于 migrations/…」→ 迁移门禁拦截,按提示 `apply` 或确认 `BILLGUARD_AUTO_MIGRATE=1` |
| 对话返回 200 但内容是模型报错(假 Key) | `.env` 是否有真实 `API_KEY` | compose 缺省注入占位值 `e2e-smoke-key`:容器能起、冒烟能过,真实对话以 LLM 401 收场(status=failed)——写 `.env` 后 `up -d` 重建 web |
| 大量 423(同会话) | 是否同会话并发/上一请求未结束 | 会话锁立即拒绝语义(并发文档 §3),客户端稍后重试;持续 423 超过 3 分钟 → 持有者可能崩溃,锁 TTL(默认 180s)到期自愈 |
| 全部 API 500,静态页正常 | `docker compose ps redis` | Redis 不可达:会话解析失败即 500(并发文档 §3.4)→ 恢复 Redis;重建后全员需重新登录(§4) |
| 429 密集 | `/api/metrics` 的 `llm_slots_in_use` 与 429 计数 | LLM 限额打满:评估 `--max-concurrent-llm` 与实例配比 |
| MCP 工具返回「服务暂时不可用(熔断中)」 | `docker compose logs bill-server` | 熔断 OPEN(60s 冷却)自动恢复;持续出现按 MCP 服务日志排查根因 |
| 登录 429「失败次数过多」 | `/api/metrics` 的 `login_throttle_blocks_total` | 节流触发(5 次/600s,按用户名+IP),成功登录清零;若正常用户被锁,等 10 分钟窗口过期 |
| 测试跑完演示库数据没了 | 跑的是哪个库 | 套件连独立 `billguard_test`(tests/conftest.py);`adversarial_eval` 缺省连演示库 `billguard` 且清夹具表——别在存真数据的库上跑(README「测试」) |

## 10. 非目标 / 明确不做

- **不做 PG 主备/副本/自动故障转移**:单实例 + 备份恢复,可用性上限
  即 PG 实例(§3 RTO 定位);
- **不做自动定时备份任务**(给命令与建议,不内置 cron);
- **不做 Prometheus/Alertmanager 栈内置**:给出接入路径(§7.3),
  抓取与告警栈由部署侧自备;
- **不做自动密钥轮换**:轮换是手册步骤(§8);
- **不做多环境配置体系**(values/helm 之类):单 compose 文件 +
  `.env` 覆盖。
