# 安全基线(distributed 分支)

本文描述 BillGuard 集群形态下的安全现状:已实现的机制、各自的代码位置与参数、
明确的设计权衡,以及生产化方向。**除特别标注"生产方向/未实现"的段落外,
全部内容均为当前代码的实际行为**,可对照 `billguard/` 源码逐条核验。

- 相关文档:`docs/data-governance.md`(数据面)、`docs/concurrency-guarantees.md`(并发面)、
  `docs/operations.md`(运维面)。
- 单进程模式(main 分支)与本分支的差异见 README「与 main 逐项对比」;本文以
  distributed 分支(nginx + web×2 + PostgreSQL + Redis + MCP×2)为对象。

## 1. 认证与会话

### 1.1 密码存储(`billguard/auth.py`、`billguard/storage_pg.py`)

| 项 | 现状 |
| --- | --- |
| 哈希算法 | PBKDF2-HMAC-SHA256,200,000 轮(`_PBKDF2_ITERATIONS = 200_000`) |
| 盐 | 每用户独立随机 16 字节(`secrets.token_bytes(16)`),hex 存储 |
| 比较 | `hmac.compare_digest` 恒时比较 |
| 未知用户名 | 同样执行一次等代价哈希(`_DUMMY_SALT`/`_DUMMY_HASH`),抹平响应时间差,防用户名枚举 |
| 失败信息 | 统一「用户名或密码错误」,不区分"用户不存在"与"密码错误" |
| 密码策略 | 仅最小长度 8 位(`_MIN_PASSWORD_LENGTH`),无复杂度/历史/过期要求 |
| 用户名规则 | `^[a-z0-9][a-z0-9_-]{1,31}$`(2-32 位小写字母/数字,`-`/`_` 连接) |

两种部署共用同一套纯函数:PG 版 `PGUserStore.verify` 与 SQLite 版
`UserStore.verify` 的哈希/比较逻辑逐行相同,只有 SQL 方言不同。

**密码策略生产方向(未实现)**:最小长度提到 12、加常见弱口令黑名单、
 breached-password 校验、管理员强制周期轮换。当前仅长度校验,
 强度依赖用户自觉,见「已知边界」。

### 1.2 会话令牌(`billguard/coordination.py` `RedisAuthSessions`)

- 令牌生成:`secrets.token_urlsafe(32)`(约 256 位熵),仅在登录响应的
  `Set-Cookie` 中出现一次明文。
- 落盘形态:**令牌明文从不持久化**。Redis 键名即 `auth:token:{sha256(token)}`,
  键值为 username;拿到 Redis 快照也无法还原出可用令牌(单向摘要)。
- 生命周期:7 天 TTL(`SESSION_TTL_DAYS = 7`);每次请求解析时若剩余
  有效期 < 6 天(`_REFRESH_THRESHOLD_DAYS = 6`)则滑动续期到 7 天。
- 反向索引 `auth:user:{username}`(SET,成员=该用户的令牌键名):
  - 改密(`reset_password`)与删户(`delete`)调用 `delete_by_user`,
    经索引**整批失效该用户全部登录令牌**——包括其它设备/其它实例上的会话;
  - 索引 TTL 与令牌对齐并随续期同步续期,不会先于任何活跃令牌过期;
  - 登出时同步摘除成员,SET 弹空即删键,登出零残留。
- 禁用账号(`set_disabled`)不清会话:`resolve_user` 每次请求都会拒绝
  禁用用户,禁用本身就是即时的 kill-switch。
- 服务端会话可随时失效是本设计的核心优点:一切吊销动作(改密/删户/禁用)
  在下一次请求即生效,无需等待 Cookie 过期。

### 1.3 Cookie 属性(`billguard/auth.py` `session_cookie`)

```
session=<token>; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800
[; Secure]
```

- `HttpOnly`:JS 不可读,防 XSS 窃取令牌;
- `SameSite=Strict`:跨站请求不携带 Cookie(CSRF 第一层防线);
- `Secure` 开关:环境变量 `BILLGUARD_SECURE_COOKIES` 为 `1`/`true`
  (大小写不敏感)时追加。**缺省关闭,因为演示部署是 http**;启用 TLS
  后必须开启(见 §3),否则 `Secure` Cookie 在 http 下无法下发/保留。

### 1.4 请求输入上限(`billguard/guardrails.py`)

- 用户输入 ≤ 32,000 字符(`MAX_USER_INPUT_CHARS`),模型输出 ≤ 64,000 字符
  (`MAX_MODEL_OUTPUT_CHARS`,超限拦截并计 `model_output_blocked` 事件);
- HTTP 请求体 ≤ 16 MiB(`MAX_HTTP_REQUEST_BYTES`),超出立即 400,
  防超大报文打满线程池与内存。

## 2. 登录防护

### 2.1 失败计数节流(`billguard/coordination.py` `RedisLoginThrottle`)

| 参数 | 值 | 出处 |
| --- | --- | --- |
| 键 | `login:fail:{sha256(username\|ip)}` | `_key()`(摘要键名,Redis 快照不泄露用户名明文) |
| 阈值 | 同一 (username, ip) 5 次失败 | `max_failures=5` |
| 窗口 | 600 秒(10 分钟),**固定窗口**:从该组合第一次失败起算,后续失败不续窗 | `window_seconds=600` + `_LOGIN_FAIL_LUA` |
| 原子性 | Lua 脚本 `INCR` + 仅当计数为 1 时 `EXPIRE`(不会留下无 TTL 的常驻键) | `_LOGIN_FAIL_LUA` |
| 达限行为 | HTTP 429「登录失败次数过多,请稍后再试」,并计 `login_throttle_blocks_total` | `web.py` 登录分支 |
| 成功登录 | `DEL` 清零计数 | `reset()` |
| 窗口过期 | 整键消失即自动解锁 | Redis TTL |

两台 web 实例共享同一份 Redis 计数:任一实例记满 5 次,全集群对该
(username, ip) 组合锁定。单进程模式退回进程内 `MemoryLoginThrottle`
(`web.py`),语义相同(固定窗口、成功清零、过期作废)。

固定窗口的已知代价:攻击者在窗口边界附近至多多得少量尝试次数
(例如第 4:59 与 5:01 各 4 次),相对滑动窗口的防御强度略低,
实现简单且无需为每次失败续期,演示与一般生产场景够用。

### 2.2 客户端 IP 的信任链(`web.py` 登录分支)

```python
ip = self.headers.get("X-Real-IP") or self.client_address[0]
```

推理链,逐环可核验:

1. **web-1/web-2 的端口不发布到 compose 网络之外**(`docker-compose.yml`
   中 web 服务没有 `ports:` 条目)——外部不存在绕过 nginx 的直连路径;
2. **nginx 对 `X-Real-IP` 强制覆写**(`docker/nginx.conf`:
   `proxy_set_header X-Real-IP $remote_addr;`)——外部请求自带的
   伪造 `X-Real-IP` 头到达 web 前一定被 `$remote_addr`(TCP 对端地址)覆盖;
3. 因此 web 端读到的 `X-Real-IP` 可信,基于它的 (username, ip) 计数
   无法被外部请求通过伪造头分化;
4. 本地开发(无代理)没有 `X-Real-IP`,兜底用套接字对端地址
   (`self.client_address[0]`),同样可信。

**边界**:同 compose 网络内的其它容器(理论上)可以直连 `web-1:8001`
并自带伪造 `X-Real-IP`。它们属于部署自身的可信域;若未来把 web 端口
发布出去或向不可信网络开放 compose 网络,该推理链即失效,
须改为仅在 nginx 后监听或校验对端来源。

### 2.3 CSRF 现状(`web.py` `_same_origin`)

对所有 POST 接口做同源校验(`web.py` `_do_post_dispatch`):已登录分支
在 `resolve_user` 之后、能力检查与业务逻辑之前;登录接口
`/api/auth/login` 同样校验(防"把受害者登进攻击者账号"的登录 CSRF),
位置在空凭据校验之后、节流与口令验证之前。

- **取头**:`Origin` 优先,`Referer` 兜底;两者并存时只用 `Origin`
  (Referer 可能被 Referrer-Policy 裁剪,不可信);
- **判定**:来源的 netloc(主机:端口,小写化)必须与请求自身的
  `Host` 头一致。端口参与比较;**scheme 不参与比较**——TLS 终止部署下
  浏览器发 `https://host` 而内网请求无 scheme,仍判定同源。
  若未来同一服务以 http 与 https 两种 scheme 并存对外,需收紧为含
  scheme 比较,否则存在跨 scheme 放行面;
- **无头放行(重要权衡)**:`Origin` 与 `Referer` 都缺失时放行。依据:
  浏览器对跨站 POST 必定携带 `Origin`(Fetch/HTML 规范强制),缺失即
  非浏览器客户端(curl/测试脚本/服务间调用);这类客户端没有 Cookie
  自动附带语义,不在 CSRF 威胁模型内。若强制要求会破坏整个 API 与
  278 项测试。Cookie 的 `SameSite=Strict` 是第一层,本校验是纵深防御
  的第二层;
- **Host 的可信性**:受害者浏览器无法在跨站请求中伪造自身 Host 头;
  nginx 以 `$http_host` 原样透传(含端口,`docker/nginx.conf` 注释:
  用 `$host` 会剥掉端口,导致合法跨端口访问被误拒)。

不符合同源的请求得到 HTTP 403「跨站请求被拒绝」,先于任何业务逻辑。

## 3. TLS 终止方案(生产方向,当前演示为 http)

当前演示在 nginx :8080 明文 http。生产应在 nginx 终止 TLS,应用侧零改动
(比较 netloc 不含 scheme 即为此设计)。示例配置块
(加在 `/etc/nginx/conf.d/` 新 server 或改造 `docker/nginx.conf`):

```nginx
server {
    listen 443 ssl;
    server_name billguard.example.com;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;   # 挂载证书卷
    ssl_certificate_key /etc/nginx/certs/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:SSL:10m;

    location / {
        proxy_pass http://billguard;
        proxy_next_upstream error timeout http_502 http_503;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;   # 便于将来按 scheme 收紧
    }
}
# 可选:80 → 443 跳转
server {
    listen 80;
    return 301 https://$host$request_uri;
}
```

启用 HTTPS 后**必须**为 web 服务设置 `BILLGUARD_SECURE_COOKIES=1`
(compose environment 追加),使会话 Cookie 带 `Secure` 属性
(仅经 https 发送)。建议同时评估 HSTS(`Strict-Transport-Security`)
与安全响应头(见「已知边界」——当前未下发 CSP 等头)。

## 4. 密钥管理:现状与生产路径

### 4.1 现状(演示定位,如实)

| 密钥 | 现状 | 位置 |
| --- | --- | --- |
| 模型 API Key | 仓库根 `.env` 写 `API_KEY=...`(或 `OPENROUTER_API_KEY`),compose 读取后以 `OPENROUTER_API_KEY` 注入容器;`.env` 已 gitignore | `.env`(参照 `docker/.env.example`) |
| PostgreSQL 口令 | `billguard/billguard` 写死在 compose(镜像 `POSTGRES_PASSWORD` 与 DSN 两处) | `docker-compose.yml` |
| Redis | **无口令**(`redis://redis:6379/0`,未设 `requirepass`),依赖"仅 compose 网络内可达" | `docker-compose.yml` |
| MCP 服务(bill/work-item) | **无认证**——streamable-http 端点不校验任何凭据,依赖容器网络隔离 | `billguard/mcp_servers/` |
| Cookie/令牌 | 无独立签名密钥(会话是服务端随机令牌查表,不需要) | — |

### 4.2 生产路径(方向,未实现)

1. **Docker secrets / 文件挂载**:PG 口令、模型 Key 改为 secrets 文件,
   compose 以 `_FILE` 约定或入口脚本读入;`.env` 仅留非敏感变量;
2. **外部 Secret Manager**(Vault/KMS/云厂商):短期凭据 + 自动轮换,
   容器启动时拉取;适合模型 Key 这类高频轮换对象;
3. **env_file 权限兜底(最低限度)**:仍在用 `.env` 时,文件权限收紧到
   `600`、属主为运行 compose 的用户、绝不入镜像层;
4. Redis 启用 `requirepass` + DSN 带口令;PG 换强口令并从 compose
   环境变量注入(两处同步改:`POSTGRES_PASSWORD` 与 `BILLGUARD_PG_DSN`)。

## 5. 审计面(哪些操作留痕)

| 痕迹 | 内容 | 位置/表 |
| --- | --- | --- |
| 交易/类别/订阅变更 | `tx_audits` 行:action(`category`/`workflow`/`subscription`/`purge`)、operator、new_value(旧值→新值 JSON)、changed_at、owner 戳 | PG `tx_audits`;写入点分布在 `storage_pg.py` 各写方法 |
| 审批决策 | `approvals` 行:status 流转(pending→approved/rejected→executed/failed)、decided_by、decided_at、decision_note、execution_result/execution_error | PG `approvals`(`PGApprovalStore.decide`/`mark_execution`) |
| 工单域审批 | `wi_approvals`:status、decided_by、decided_at;`issues`:created_by、approval_id | PG `wi_approvals`/`issues` |
| Agent 运行全量 | traces:工具调用(参数+结果)、模型输出事件、token 用量与 cost 汇总、审批挂起/拒绝事件 | PG `traces`(`PGTraceStore`) |
| 会话内容 | 用户消息与助手回答原文、摘要 | PG `sessions` |
| HTTP 访问日志 | 每请求一行 `[web] <addr> - "<METHOD> <path> <proto>" <status> <bytes>`,含登录/管理接口 | 容器 stdout(`docker compose logs web-1` 等) |
| 登录失败/节流 | `login_failures_total`、`login_throttle_blocks_total` 计数器(进程内,实例重启清零) | `billguard/metrics.py`,经 `/api/metrics` 读 |

**如实说明(现状缺口)**:用户管理操作——创建用户、改角色、重置密码、
禁用/启用、删除用户——**不写任何业务审计表**,只有访问日志一行
(`POST /api/admin/users/...` + 状态码)与结果副作用。`admin_delete_user`
内部的 `purge_owner(username)` 不传 operator,因此也不留 `__purge__`
审计标记(对比:用户自助清空 `purge_my_data` 会留)。生产化应把
`users_manage` 级操作补进独立审计流。

## 6. Token 轮换建议

当前没有内置轮换机制,以下为运维侧做法与方向:

- **被动轮换(零成本,已具备)**:令牌 7 天 TTL + <6 天滑动,长期不活跃
  的会话自然过期;改密/删户即整批吊销。若怀疑令牌泄露,立即改该用户密码
  即可全端踢出;
- **主动轮换(生产方向,未实现)**:增加"签发时间戳入键值 + 定期
  `delete_by_user` 重签"、或把 TTL 缩短到小时级 + refresh 接口;
- **轮换频率建议**:普通用户会话 7 天可接受;管理员会话建议 1 天内,
  可通过对 admin 用户更频繁地重置密码临时达成(代价是体验)。

## 7. 管理员操作二次认证(设计方向,不实现)

`users_manage` 能力(建/删用户、改角色、重置密码、禁用)是本系统的
最高权限面:拿到管理员会话即可接管任意账号(重置密码后以其身份登录)。
设计方向是 **step-up 认证**:执行 `users_manage` 级写操作时,要求请求
携带近期(如 5 分钟内)二次验证凭证——独立 PIN/TOTP,或"重输入当前密码"
换发的短时提升令牌(Redis 键 `auth:stepup:{username}`,TTL 300s)。

**为什么现在不做**:演示系统管理员即部署者,单人使用,step-up 只增加
摩擦;实现需要新增前端交互与一条凭证链路,收益在多管理员/不可信终端
场景才成立。先把方向写清楚,生产多租户化时一并落地。

## 8. 已知边界(诚实清单)

1. **无头放行**:无 `Origin`/`Referer` 的请求绕过同源校验(见 §2.3 的
   论证——非浏览器客户端无 Cookie 附带语义;`SameSite=Strict` 兜底);
2. **compose 内置口令是 dev-only**:PG `billguard/billguard` 写死、Redis
   无口令、MCP 容器间无认证,全部依赖"compose 网络即信任域";
   端口面上仅 nginx 8080 对外,PG(127.0.0.1:5433)与 Redis(127.0.0.1:6380)
   仅宿主机回环可达;
3. **用户管理操作无业务审计行**(见 §5 如实说明);
4. **密码策略仅长度 8**;无防重用/弱口令库校验;
5. **登录节流按 (username, ip) 组合**:分布式撞库(大量 IP 对同一用户名)
   每个组合各有 5 次窗口,不聚合;对单账号的分布式暴力破解防御有限
   (生产方向:按用户名维度的第二级计数);
6. **无安全响应头**:未下发 `Content-Security-Policy`/`X-Content-Type-Options`/
   `X-Frame-Options` 等(静态页由 `web_static` 直出,生产应在 nginx 层统一加);
7. **TLS 缺省未启用**,`BILLGUARD_SECURE_COOKIES` 缺省关闭(演示 http);
8. **MCP 熔断降级话术可能被模型转述为"服务异常"**:非安全问题,
   语义边界见 `docs/concurrency-guarantees.md` §7。

## 9. 非目标 / 明确不做

- **不做 OAuth2/OIDC/SSO 接入**:`Authenticator` 是单一身份解析边界,
  预留了替换点(类注释明示"swap this class for SSO later"),但本分支
  不实现外部 IdP;
- **不做多因素登录(MFA/TOTP)**:含 §7 的 step-up 在内,只写设计方向;
- **不做请求级 WAF/防爬**:仅有输入长度上限与登录节流;
- **不做数据库列级加密(TDE/pgcrypto)**:note 等敏感列明文存储,
  生产以卷加密 + 备份加密替代(见 `docs/data-governance.md` §6);
- **不做零信任网络模型**:compose 内网即信任域,不引入 mTLS 服务网格。
