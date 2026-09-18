# BillGuard 集群冒烟验收手册

对应集群拓扑:`nginx(ip_hash :8080) → web-1(:8001)/web-2(:8002)`,MCP
`bill-server(:8010)`、`work-item-server(:8020)`(streamable-http),数据在
PostgreSQL(`pgdata` 卷),会话锁/登录会话/LLM 限流在 Redis。

手册分两部分:

- **A. 自动化冒烟(无需真实 API Key)**:基础设施级验证,任何时间可跑。
- **B. 手工验收(需要真实 API Key)**:真实对话链路 + 并发竞争 + 故障转移连续性。

> 关于 API Key:compose 对 web 服务做了 `OPENROUTER_API_KEY` /
> `OPENAI_API_KEY` 透传,宿主机未设置时注入占位值 `e2e-smoke-key`。web 启动
> 只检查变量**存在**,因此占位值足以让全部容器启动,完成 A 部分冒烟;
> **占位 Key 无法完成真实模型调用**,对话请求会以 LLM 401 收场(实测返回
> HTTP 200、status="failed",answer 里是模型错误信息;视错误类型也可能是
> 5xx)——这是预期行为,不是缺陷。B 部分必须设置真实 Key。

---

## A. 自动化冒烟(无 Key,基础设施验证)

全部命令在仓库根目录(Git Bash / PowerShell 均可)执行。

### A0. 启动基建并播种管理员

分布式模式要求 users 表非空,否则 web 进程启动即退出。先起 PG/Redis、播种
admin,再拉全集群:

```bash
docker compose up --build -d postgres redis
# 播种管理员(经 web-1 镜像内 users CLI,走 PG 分支;pgdata 卷保留时只需做一次)
# 注意:镜像 ENTRYPOINT 已是 python,compose run 的命令不再写 python 前缀
echo 'Smoke-Admin-1' | docker compose run --rm web-1 \
  -m billguard.users add admin --role admin --password-stdin
docker compose up --build -d
docker compose ps   # 七个服务均应 Up(postgres/redis 显示 healthy)
```

表结构不再由 init.sql 挂载预建:users CLI / web / MCP 的 PG 入口都带启动门禁,
compose 已注入 `BILLGUARD_AUTO_MIGRATE=1` —— 全新 pgdata 卷上首次执行上面任一
命令都会先自动应用 `migrations/` 全部迁移(输出「自动迁移:已应用版本 …」),
存量旧卷则被 V001 幂等补账。宿主机跑测试套件也不再影响集群:套件连独立测试库
`billguard_test`(见 README 测试节),users 表不会被清空。

### A1. 健康检查(经 nginx 入口)

```bash
curl -s http://127.0.0.1:8080/api/health
# 期望:{"ok": true}(HTTP 200)
```

### A2. 跨实例会话锁:持锁 → 423,释放 → 不再 423

先在 Redis 中直接持有某会话的锁(模拟另一实例正在处理),再用真实登录
token 经 nginx 发起对话:锁检查发生在任何模型调用之前,因此**占位 Key 不
影响本步骤**。

```bash
# 1) 登录 admin,保存 cookie
curl -s -c /tmp/bg-cookie -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"Smoke-Admin-1"}' \
  http://127.0.0.1:8080/api/auth/login
# 期望:{"username": "admin", "role": "admin"}

# 2) 同源校验经 nginx 入口(浏览器形态:同源 Origin 放行、伪造 Origin 拒绝)
curl -s -o /dev/null -w 'same-origin: %{http_code}\n' -X POST \
  -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:8080' \
  -d '{"username":"admin","password":"Smoke-Admin-1"}' \
  http://127.0.0.1:8080/api/auth/login
# 期望:same-origin: 200 —— 同时证明 nginx 以 $http_host 原样透传 Host(含端口)。
# 「旧 nginx($host 丢端口)+ 新 web」的部分滚动会让此处变 403,冒烟当场失败,
# 而不是等到浏览器全部 POST 被拒才发现
curl -s -w '\nforged: %{http_code}\n' -X POST \
  -H 'Content-Type: application/json' -H 'Origin: http://evil.example' \
  -d '{"username":"admin","password":"Smoke-Admin-1"}' \
  http://127.0.0.1:8080/api/auth/login
# 期望:forged: 403,{"error": "跨站请求被拒绝"}(403 在节流与验密之前,不计数)

# 3) 持有会话 smoke-lock-423 的 Redis 锁(宿主机直连 6380)
python -X utf8 -c "import redis,hashlib; c=redis.Redis.from_url('redis://127.0.0.1:6380/0'); k='lock:session:'+hashlib.sha256('smoke-lock-423'.encode()).hexdigest(); print('held' if c.set(k,'smoke-holder',nx=True,px=120000) else 'already-held')"

# 4) 该会话发起对话 → 期望 HTTP 423 + 中文错误
curl -s -b /tmp/bg-cookie -H 'Content-Type: application/json' \
  -d '{"session_id":"smoke-lock-423","message":"冒烟测试"}' \
  -w '\nHTTP %{http_code}\n' http://127.0.0.1:8080/api/chat
# 期望:HTTP 423,{"error": "另一会话操作正在进行,请稍后重试"}

# 5) 释放锁后再发一次 → 不再是 423
python -X utf8 -c "import redis,hashlib; c=redis.Redis.from_url('redis://127.0.0.1:6380/0'); c.delete('lock:session:'+hashlib.sha256('smoke-lock-423'.encode()).hexdigest())"
curl -s -b /tmp/bg-cookie -H 'Content-Type: application/json' \
  -d '{"session_id":"smoke-lock-423","message":"冒烟测试"}' \
  -w '\nHTTP %{http_code}\n' http://127.0.0.1:8080/api/chat
# 期望:非 423 即通过(锁已放行、请求进入模型调用阶段)。实测占位 Key 下
# 为 HTTP 200(运行时把 LLM 401 包进 status="failed" 的正常响应返回);
# 真实 Key 下同为 200 且 status="completed"。不同版本也可能以 500
# ({"error": "请求失败:…"})呈现 —— 断言只看“离开 423”。
```

> 断言要点:持锁时严格 423 且错误文案为上述中文;释放后状态码离开 423。
> 锁键由 web 实例经 Redis 抢占,宿主机脚本与容器进程天然互斥,等价于
> “另一实例正在处理该会话”的跨实例场景。

### A3. 故障转移:kill web-1 后 nginx 仍可服务

```bash
docker compose kill web-1
curl -s -w '\nHTTP %{http_code}\n' http://127.0.0.1:8080/api/health
# 期望:HTTP 200 {"ok": true}(nginx 将流量切到 web-2)
docker compose start web-1   # 恢复双实例
```

### A4. 集群内对抗评测(验收头条:25/25 在容器内对 PG/Redis 通过)

```bash
docker compose exec web-1 python -m billguard.adversarial_eval
# 期望输出 metrics:"total": 25, "passed": 25, "failed": 0, "probe_errors": 0
```

评测为确定性本地探针(ScriptedLLM 恶意模型替身),不需要真实 Key。它会
**清空业务表**(users 只删夹具用户 alice/mallory,不影响 admin),结束时
自清理,可背靠背重跑。

### A5. 零残留核验(可选)

```bash
docker compose exec postgres psql -U billguard -d billguard -tAc \
  "SELECT table_name, (xpath('/row/c/text()', query_to_xml(format('SELECT COUNT(*) AS c FROM %I', table_name), false, true, '')))[1]::text FROM information_schema.tables WHERE table_schema='public'"
# 期望:业务表全部为 0;users 仅剩既有真实账号(admin)
docker compose exec redis redis-cli --scan --pattern 'lock:session:*'   # 期望:无输出
docker compose exec redis redis-cli --scan --pattern 'llm:slots'        # 期望:无输出
```

### A6. 收尾

```bash
docker compose down        # 保留 pgdata 卷(保留 admin 账号)
docker compose up -d postgres redis   # 宿主机测试套件依赖这两个端口
```

---

## B. 手工验收(需要真实 API Key)

前置:`export OPENROUTER_API_KEY=sk-or-...`(或 `OPENAI_API_KEY`)。

```bash
docker compose down
docker compose up --build -d   # 重建环境,注入真实 Key
```

### B1. 登录与真实对话

```bash
curl -s -c /tmp/bg-cookie -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"Smoke-Admin-1"}' \
  http://127.0.0.1:8080/api/auth/login
curl -s -b /tmp/bg-cookie -H 'Content-Type: application/json' \
  -d '{"session_id":"manual-e2e","message":"帮我导入后的账单做个总览"}' \
  http://127.0.0.1:8080/api/chat
# 期望:HTTP 200,answer 为模型真实回答;浏览器打开 http://127.0.0.1:8080 等价
```

### B2. 同会话并发竞争(一 200 一 423)

模型响应需要时间,趁首个请求在途并发第二个:

```bash
curl -s -b /tmp/bg-cookie -H 'Content-Type: application/json' \
  -d '{"session_id":"manual-race","message":"详细分析异常"}' \
  -o /tmp/race1.json -w 'first: %{http_code}\n' \
  http://127.0.0.1:8080/api/chat &
sleep 0.5
curl -s -b /tmp/bg-cookie -H 'Content-Type: application/json' \
  -d '{"session_id":"manual-race","message":"详细分析异常"}' \
  -o /tmp/race2.json -w 'second: %{http_code}\n' \
  http://127.0.0.1:8080/api/chat
wait
# 期望:first: 200,second: 423(另一会话操作正在进行,请稍后重试)
```

> 同一宿主机经 ip_hash 两请求会落到同一 web 实例,Redis 锁依旧互斥;
> 跨实例等价性由 A2(持锁方为另一进程)与 B3(实例切换后会话仍在)共同覆盖。

### B3. kill web-1 会话连续性(会话在 PG,不在实例内存)

```bash
# 先在 manual-e2e 会话完成至少一轮对话(见 B1),然后:
docker compose kill web-1
curl -s -b /tmp/bg-cookie -H 'Content-Type: application/json' \
  -d '{"session_id":"manual-e2e","message":"继续刚才的话题,总结一下"}' \
  -w '\nHTTP %{http_code}\n' http://127.0.0.1:8080/api/chat
# 期望:HTTP 200,回答能延续上文(历史消息从 PG sessions 表加载,
# 登录态在 Redis,均与实例无关;nginx 自动改路由到 web-2)
docker compose start web-1
```

### B4. 路由评测(集群内)

```bash
docker compose exec web-1 python -m billguard.eval
# 期望:输出 baseline/skills/full 三变体指标 JSON 与报告路径
```

### B5. 清理重置

```bash
docker compose down -v   # 连同 pgdata 卷删除:下次 up 需重新播种 admin(A0)
docker compose up -d postgres redis   # 恢复宿主机测试依赖
```

---

## 已知边界

- 占位 Key(`e2e-smoke-key`)下一切对话类接口无法完成真实模型调用(实测
  HTTP 200 + status="failed",answer 为模型错误信息;也可能 5xx),属预期;
  A1–A4 不受影响。
- **web 与 nginx 必须一起滚动**:`docker compose up --build -d` 整批重建。
  新 web 的同源校验依赖 nginx 以 `$http_host` 原样透传 Host(含端口);旧
  nginx 的 `$host` 会丢端口,部分滚动(只换 web 不换 nginx)会让全部合法
  浏览器 POST(含登录)被 403 误拒 —— A2 第 2 步专门拦截这种部分滚动。
- **web 容器重建后需重启 nginx**:web-1/web-2 被重建(`up --build`/`down` 后
  `up`)会拿到新容器 IP,而 nginx.conf 的静态 upstream(`server web-1:8001`)
  只在 nginx 启动时解析一次,之后仍指向旧 IP → 502 Bad Gateway。A0 的恢复
  路径(播种 admin 后 `up -d web-1 web-2`)正好会踩到:此时执行
  `docker compose restart nginx` 重新解析即可。`kill`/`start` 复用同一容器、
  IP 不变,无此问题(A3 的故障转移不踩)。
- `ip_hash` 按客户端 IP 粘滞:单一宿主机压测始终命中同一 web 实例;
  如需强制分流,可从不同机器/网卡发起,或临时改 `docker/nginx.conf` 为轮询。
- 集群内对抗评测会清空业务表(users 仅删 alice/mallory 夹具),不要在
  存有真实数据的库上执行;`down -v` 可完全重置。
