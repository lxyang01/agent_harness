# BillGuard · 可审计的账单守卫 Agent(分布式版)

[![CI](https://github.com/lxyang01/agent_harness/actions/workflows/ci.yml/badge.svg?branch=distributed)](https://github.com/lxyang01/agent_harness/actions/workflows/ci.yml)

> 本分支(distributed)将 BillGuard 平移到无状态多实例部署:nginx + web×2 + PostgreSQL + Redis + 共享 MCP,一条命令起全集群。**永不合并回 main**;单进程零依赖版见 **main** 分支,两分支功能一致,差异在部署形态、并发原语与登录防护。

BillGuard 是一个**框架无关的可审计 Agent Harness**:模型只能调用注册过的工具,不执行任意 SQL/Shell;金额与结论必须来自工具返回的真实数据;高风险写操作走三阶段审批(准备 → 人工批准 → 提交)。**25 条对抗探针**(零费用、确定性)验证提示注入、越权参数、伪造数字、跨用户数据窃取等攻击全部被拦截。业务载体是个人账单守卫:从账单与订阅 CSV 中发现涨价、重复扣费和大额离群,输出带证据的行动计划。

本分支在集群形态下保留全部防线,并把并发防御重建在分布式原语上(Redis 会话锁、全集群 LLM 限额、PG 行锁审批恰好一次、Redis 登录失败计数),248 项单元测试与 25 条对抗探针在 PG/Redis 后端下全部通过(宿主机与集群内两端验证)。

## 功能

与单进程版功能相同:账单/订阅 CSV 导入、支出概览与多维明细、四类异常检测(spike / duplicate / price_hike / outlier)、守卫 Agent 对话(4 Skill 路由 + 三阶段审批)、多用户角色与按用户数据隔离、对抗/路由/真实模型评测。本分支把这些搬到多实例无状态部署下:

- **多实例无状态**:web-1 / web-2 任意实例可服务任意请求,kill 任一实例服务不中断,对话会话与登录态跨实例延续
- **登录暴力破解防护**:按用户名+IP 失败计数,5 次锁定 10 分钟(Redis 计数;单进程模式为进程内计数),成功登录即清零;集群内经 nginx 的 X-Real-IP 识别客户端(nginx 强制覆写该头,web 端口不对外,不可伪造)
- **全量验证**:248 项单元测试与 25 条对抗探针全部在 PG/Redis 后端下通过(宿主机与集群内两端验证)
- **一条命令拉起集群**:`docker compose up` 起 nginx + web×2 + PostgreSQL + Redis + 两个 MCP 服务

## 架构与技术

### 集群拓扑

```text
nginx :8080(ip_hash 负载均衡)
 ├── web-1 :8001(无状态)
 └── web-2 :8002(无状态,可水平扩展)
      ├── postgres :5432   全部持久业务数据(13 张表)
      ├── redis    :6379   会话锁 / LLM 并发计数 / 登录会话 / 登录失败计数
      ├── bill-server      :8010  账单 MCP(streamable-http)
      └── work-item-server :8020  工单 MCP(streamable-http)
```

- web 实例间零直接通信,一切经 Redis/PG;compose 服务名即服务发现
- 宿主机端口:nginx `8080`(所有接口;如需仅本机访问可改为 `127.0.0.1:8080:80`);PG `127.0.0.1:5433`、Redis `127.0.0.1:6380` 仅本机监听,供宿主机测试/评测直连

### 存储分工

| 层 | 承载 | 内容 |
| --- | --- | --- |
| PostgreSQL(持久) | 13 张表,`migrations/` 版本化迁移建表 | users / transactions / categories / subscriptions / tx_audits / imports / reports / approvals / wi_approvals / issues,以及原 JSON/JSONL 文件改成的 sessions / evidence / traces 三张表(JSONB) |
| Redis(协调) | 5 类键 | `lock:session:{sha256}` 会话锁、`llm:slots` LLM 并发计数、`auth:token:{sha256}` 登录令牌、`auth:user:{username}` 令牌反向索引、`login:fail:{sha256}` 登录失败计数(5 次锁定 10 分钟,成功登录清零) |

存储层 `billguard/storage_pg.py` 公开方法与单进程版同名同参(构造参数从目录换为连接池);表结构由 `migrations/` 迁移预建(见下方「备份与容量」),运行时不做任何 DDL。每 web 实例一个 psycopg3 连接池(min 2 / max 8)。

### 与 main(单进程版)逐项对比

| 维度 | main(单进程,零依赖) | distributed(本分支) |
| --- | --- | --- |
| 存储 | SQLite + JSON/JSONL 文件(`.sessions/` 目录) | PostgreSQL 13 张表;消息/事件/证据用 JSONB,金额 NUMERIC |
| 会话锁 | 进程内 `threading.Lock`(per-session),同会话并发请求排队等待 | Redis `SET NX PX` 分布式锁,TTL = run_timeout + 60s;冲突**立即 423**,不排队;释放走 Lua 持有者校验,只删自己的锁 |
| LLM 限流 | 进程内 `BoundedSemaphore`(上限 4),超限 429 | Redis Lua 原子 check-and-incr(`llm:slots`),全集群共享额度,超限 429;键 TTL 120s 自愈 |
| 登录态 | 文件会话库,7 天滑动过期,改密/删户清理本地记录 | Redis 令牌键(键名即 token 的 sha256 摘要,原文不落盘)7 天 TTL、剩余 <6 天滑动续期;`auth:user:*` 反向索引让改密/删户整批失效该用户全部令牌 |
| 登录防护 | 无(仅 distributed 提供;单进程模式为进程内计数) | 按用户名+IP 失败计数,5 次锁定 10 分钟;Redis Lua 原子 INCR+EXPIRE(`login:fail:{sha256}`,固定窗口从首次失败起算),两实例共享计数,任一实例记满即全集群锁定;成功登录清零 |
| 审批恰好一次 | SQLite 条件 UPDATE(`WHERE status='pending'` + rowcount 判定) | **同一条 SQL 一字不改**(仅占位符 `?`→`%s`),PG 行锁保证并发 decide 恰好一个生效 |
| 线程池 | `BoundedHTTPServer` 有界线程池(16 线程 / 32 排队,满载 503) | 原样保留,每实例独立;全局容量 = 实例数 × max_threads |
| MCP | stdio 子进程随 web 自动拉起 | streamable-http 独立容器(bill-server :8010 / work-item-server :8020) |

### 环境变量

`BILLGUARD_PG_DSN` 与 `BILLGUARD_REDIS_URL` 同时设置时,`python -m billguard.web` 进入分布式模式(此时两个 MCP URL 必须给出,否则启动退出);两者缺任一则退回单进程模式。compose 已内置下列值:

| 变量 | compose 值 | 说明 |
| --- | --- | --- |
| `BILLGUARD_PG_DSN` | `postgresql://billguard:billguard@postgres:5432/billguard` | PG 连接串 |
| `BILLGUARD_REDIS_URL` | `redis://redis:6379/0` | Redis 地址 |
| `BILLGUARD_BILL_MCP_URL` | `http://bill-server:8010/mcp` | 账单 MCP(streamable-http) |
| `BILLGUARD_WORK_ITEM_MCP_URL` | `http://work-item-server:8020/mcp` | 工单 MCP(streamable-http) |
| `BILLGUARD_SECURE_COOKIES` | —(缺省关闭) | 设为 `1`/`true` 时会话 Cookie 追加 `Secure` 属性(仅经 https 发送;https 部署建议开启,演示为 http 故缺省关闭) |
| `API_KEY`(或 `OPENROUTER_API_KEY` / `OPENAI_API_KEY`) | `.env` / 宿主机透传 | 真实对话必填;**推荐写进仓库根目录 `.env`**(参照 `docker/.env.example`,compose 自动读取,已 gitignore)。全缺时 compose 注入占位值 `e2e-smoke-key`,容器可启动并完成基础设施冒烟,但不能真实调用模型 |
| `BILLGUARD_LLM_MODEL` / `BILLGUARD_LLM_BASE_URL` | `openai/gpt-4.1-mini` / `https://openrouter.ai/api/v1` | 模型与端点(在 `.env` 里覆盖即可换模型;用 OpenAI 官方 Key 时端点改为 `https://api.openai.com/v1`) |

> 注:spec §5.1 所列环境变量 `BILLGUARD_MAX_CONCURRENT_LLM` 未实现;集群 LLM 并发上限经 `--max-concurrent-llm` 参数传入(默认 4,与 spec §5.1 默认一致)。

### 指标

每个 web 实例内置**进程内轻量指标**(`billguard/metrics.py`,纯标准库,零新增依赖),经 `GET /api/metrics` 输出 JSON 快照:计数器(`http_requests_total`、`http_status_{code}`、`llm_calls/failures/retries_total`、`tool_calls/failures_total`、`mcp_calls_total`、`mcp_call_failures_total`、`circuit_opens_total`、`lock_conflicts_total`、`login_failures_total`、`login_throttle_blocks_total`、`approvals_decided_total`)、计时(`http/llm/mcp_call_seconds`,count+sum 可求均值)与仪表(`llm_slots_in_use`、`pg_pool_size`/`pg_pool_in_use`、`uptime_seconds`)。指标是**每实例独立**的(进程内计数,不聚合);`instance` 字段标识来源实例。

鉴权:与其它 API 一致要求登录,但不设能力门槛——指标是运行操作数据而非敏感业务数据,任何登录用户可读(未登录 401,避免匿名探测)。本端点自身不计入 `http_request_seconds`(抓取间隔不反馈进时延统计),但状态计数照常记录。

```bash
curl -s -b "session=<登录 Cookie>" http://localhost:8080/api/metrics | python -m json.tool
# 集群内指定实例:docker compose exec web-1 curl -s -H "Cookie: ..." http://127.0.0.1:8000/api/metrics
```

生产 Prometheus/OTel 路径:抓取该 JSON 转换为 Prometheus 文本暴露格式即可(如经 nginx 侧 exporter 定期抓取各实例并聚合),本分支不引入抓取库——做法详见 `docs/operations.md`。

### 设计决策

- **为什么 423 而不是排队**:分布式锁不加等待队列——跨实例的等待队列要处理排队者生命周期(实例死亡时的清理、公平性、超时传递),复杂度与收益不成比例。立即 423 让客户端显式重试,语义简单可预测;锁 TTL(run_timeout + 60s)兜底实例崩溃,不会永久占锁。
- **TTL 自愈的代价**:`llm:slots` 计数器设 120s TTL,实例崩溃后最迟 120s 恢复满额度;代价是 TTL 到期边界可能短暂多放 1-2 个请求(演示定位可接受)。会话锁同理:崩溃后最迟 run_timeout + 60s 自动释放,期间该会话请求 423。
- **审批 SQL 为何一字不改**:恰好一次语义由"条件 UPDATE + rowcount==0 即拒绝"表达,与存储引擎无关;SQLite→PG 只换占位符,PG 行锁天然串行化并发 decide。不动这条 SQL,意味着"并发审批双提交"对抗探针验证的就是同一份逻辑,行为可证等价。
- **ip_hash 的作用**:同一客户端 IP 固定路由到同一 web 实例,减少 Redis 锁竞争与 423 概率;非必需——正确性只依赖 Redis/PG 的互斥,轮询负载均衡同样正确。

## 启动与命令

以下命令均在仓库根目录执行;集群冒烟验收的完整步骤见 `docker/cluster-smoke.md`。

### 首次启动

```bash
docker compose up --build -d postgres redis
# 播种管理员(镜像 ENTRYPOINT 已是 python,命令不写 python 前缀;
# pgdata 卷保留时只需做一次)
echo 'Smoke-Admin-1' | docker compose run --rm web-1 -m billguard.users add admin --role admin --password-stdin
echo "API_KEY=sk-完整key" > .env
docker compose up --build -d
docker compose ps   # 全部服务 Up,postgres/redis 显示 healthy
```

真实对话:把 Key 写进 `.env`(上一步的 `API_KEY=...`;compose 自动读取,已 gitignore 不会提交,写 `OPENROUTER_API_KEY=...` 同样有效)。本地访问 `http://localhost:8080`(若浏览器走了代理打不开,关闭系统代理);健康检查 `curl -s http://localhost:8080/api/health` 期望 `{"ok": true}`。

### 日常启停

```bash
docker compose up -d      # 启动全部服务
docker compose down       # 停止并删容器(保留 pgdata 卷与 admin 账号)
docker compose down -v    # 全量重置(下次 up 需重新播种 admin)
docker compose kill web-1 # 故障转移演练:nginx 自动切到 web-2
docker compose start web-1   # 恢复双实例
docker compose restart nginx # web 容器重建后必做(见下)
```

web-1/web-2 被**重建**(`up --build` 或 `down` 后再 `up`)会拿到新容器 IP,而 nginx 的静态 upstream 只在 nginx 启动时解析一次 → 502 Bad Gateway;此时 `docker compose restart nginx` 重新解析即可。`kill`/`start` 复用同一容器、IP 不变,无此问题。

### 备份与容量

备份一条命令;恢复:清库后用 `psql` 重放 `backup.sql`,或 `docker compose down -v` 后由迁移自动重建表结构再重放。

```bash
docker compose exec -T postgres pg_dump -U billguard billguard > backup.sql
```

容量:每个 web 实例 PG 连接池 min 2 / max 8,默认部署 2 实例 = 最多 16 连接(PG 默认上限 100);水平扩实例时按此换算连接占用量。

表结构演化走版本化迁移(`migrations/` 目录,`billguard/migrate.py` 手写 runner,无第三方迁移框架依赖):

- 每个版本一对纯 SQL 文件 `V<零填充序号>_<名称>.up.sql` / `.down.sql`;已应用版本记录在库内账本表 `schema_migrations`(由 runner 自建),apply 逐版本独立事务。
- 常用命令:`python -m billguard.migrate --dsn postgresql://billguard:billguard@127.0.0.1:5433/billguard status|apply|rollback`(rollback 一次回滚最新一个版本;`ensure-database` 子命令可建库+全量应用,测试库 `billguard_test` 即由它引导)。
- 集群自迁移:compose 已注入 `BILLGUARD_AUTO_MIGRATE=1`,web / MCP / users CLI 的 PG 入口建池后发现落后于 `migrations/` 会先自动应用再启动;不设该变量则启动即退出并打印待应用版本与确切修复命令。存量 pgdata 卷下次启动会被 V001 自动补账(V001 全程 `IF NOT EXISTS`,重放幂等)。
- 回滚策略:演示环境优先 `down -v` 全量重置;需要精确回退时用 `rollback` 逐版本执行 `.down.sql`(V001 的 down 会 DROP 全部业务表,**清空数据**,执行前先备份)。改表永远发新版本文件,不改历史文件。
- 索引变更注意:在存量数据上新建索引应在迁移 SQL 里使用 `CREATE INDEX CONCURRENTLY`(避免长事务锁表);CONCURRENTLY 不能在事务块内跑,与本 runner「每版本一事务」冲突,如需使用请单独用 psql 执行。

### 测试(宿主机)

前置:`docker compose up -d postgres redis`(套件连 PG `127.0.0.1:5433` / Redis `127.0.0.1:6380`)。

```bash
python -X utf8 -m unittest discover -s tests
# 期望:Ran 278 tests ... OK
```

套件连的是**独立测试库 `billguard_test`**(`tests/conftest.py` 的 `ensure_test_database` 自动建库并应用全部迁移,幂等),与演示集群的 `billguard` 库物理隔离 —— 跑测试不再清空演示库的 users/账单数据。对抗评测(`python -m billguard.adversarial_eval`)缺省仍指向演示库 `billguard`(宿主机/集群内运行时如此,可用 `BILLGUARD_PG_DSN` 覆盖),会清空其夹具表;CI 里该步骤用 env 显式钉在 `billguard_test`(CI 的演示库是零表空壳)。

### 对抗评测

```bash
# 宿主机(缺省连 compose 宿主机端口 PG 5433 / Redis 6380)
python -X utf8 -m billguard.adversarial_eval
# 集群内(容器网络)
docker compose exec web-1 python -m billguard.adversarial_eval
# 期望:metrics "total": 25, "passed": 25, "failed": 0
```

评测为确定性本地探针(恶意脚本模型替身),不需要真实 Key;会清空业务表(users 仅删夹具 alice/mallory,不影响 admin),不要在存有真实数据的库上执行。

### 集群冒烟验收

完整手册见 `docker/cluster-smoke.md`:A 部分自动化冒烟(无 Key)覆盖健康检查、跨实例会话锁 423、kill web-1 故障转移、集群内对抗评测、零残留核验;B 部分手工验收(真实 Key)覆盖真实对话、同会话并发竞争(一 200 一 423)、实例切换后的会话连续性。路由评测在集群内执行:`docker compose exec web-1 python -m billguard.eval`。
