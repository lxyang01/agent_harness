# 并发保证与边界(distributed 分支)

集群形态下,一次恶意/失控的并发由四层防御依次兜住,再加 MCP 熔断的
并发正确性。本文逐层写清:**机制、不变式、崩溃/分区时发生什么、自愈
时间**,以及诚实的边界(哪里只有弱保证)。除标注"草图/未实现"的
段落外,全部为当前代码行为。

## 0. 总览

| 层 | 机制 | 超限表现 | 自愈时间 | 代码 |
| --- | --- | --- | --- | --- |
| 1. 实例线程池 | 每实例 16 线程 + 32 排队,`BoundedSemaphore(48)` 非阻塞准入 | HTTP 503(不经业务逻辑) | 即时(容量释放即恢复) | `web.py` `BoundedHTTPServer` |
| 2. LLM 并发限额 | Redis Lua check-and-incr,集群共享计数 | HTTP 429 | ≤120s(键 TTL) | `coordination.py` `RedisLLMLimiter` |
| 3. 会话锁 | Redis `SET NX PX`,TTL=run_timeout+60s | HTTP 423(立即拒绝,不排队) | ≤run_timeout+60s(默认 180s) | `coordination.py` `RedisSessionLock` |
| 4. 审批恰好一次 | PG 条件 UPDATE + rowcount 裁决 | 第二个 decide 得 `PolicyError`(HTTP 400) | 不需要(状态机终态) | `storage_pg.py` `decide` |

## 1. 第一层:线程池 503

- **机制**:`ThreadingHTTPServer` 的 `process_request` 先以
  `BoundedSemaphore(max_threads + queue_capacity)`(16+32=48)非阻塞
  取容量,取不到立即写 503 JSON 并断开;取到则提交线程池。请求完成
  (或异常)即释放容量。
- **不变式**:单实例"处理中+排队"≤48;全局容量=实例数×48。
- **崩溃/分区**:实例死亡,其 in-flight 请求丢失;nginx 的
  `proxy_next_upstream error timeout http_502 http_503` 对**尚未发出**
  的请求换实例重试——注意 nginx 默认不对"已发送给上游的非幂等请求"
  重试(未开 `non_idempotent`):kill web-1 演练里连接失败发生在发送前,
  所以 POST 也能零感知切换;而真实 503(容量满)的 POST 响应会原样
  送达客户端,GET 则可能被 nginx 自动重试到另一实例。
- **自愈**:即时。容器 `restart: unless-stopped` 自动拉回。
- **计数**:503 直接计 `http_requests_total`/`http_status_503`
  (不经 handler,`web.py` `_reject_busy` 内直埋)。

## 2. 第二层:LLM 并发限额 429

- **机制**:`llm:slots` 单键计数。Lua 原子执行:当前值 < limit 才
  `INCR` 并 `EXPIRE 120`,返回 1;否则返回 0 → `BusyError` → HTTP 429。
  释放走带地板的 `DECR`(不为负)。**计数是集群共享的**,两台实例用
  同一键互斥;限额值取各实例 `--max-concurrent-llm`(默认 4)——
  两实例配置不一致时,实际集群上限=较大者(各自与自己的 limit 比较),
  部署时应保持一致。
- **粒度(如实)**:槽位在 **web 层按请求**获取(`chat`/`decide_approval`
  进入临界区前一次 acquire,结束 release),不是按单次模型调用;
  harness 引擎内不再有第二级信号量。一次对话(内含多次模型调用)
  占一个槽,占用时长≈整个 run。
- **不变式**:集群同时进行的 chat/decide 运行数 ≤ limit(正常路径)。
- **崩溃**:持有者崩溃没有 release → 槽位泄漏,额度最多损失至
  键 TTL 到期;**自愈 ≤120s**(键消失后重新从 0 计数)。
- **TTL 边界多放 1-2 个的量化**:TTL 在**每次成功 acquire** 时刷新到
  120s(失败不刷新、release 不动 TTL),即键在"最后一次成功获取后
  120s"消失。合法运行可以活过这个时刻:run_timeout 默认 120s 与 TTL
  同量级,run 结束后的收尾(证据/trace 写入、响应组装)仍在持槽。
  于是出现"老持有者还在收尾、键已过期、新请求已重新计数"的窗口:
  瞬时集群并发最多 ≈ 2×limit,超发个数≈仍在收尾的老请求数(典型
  1-2 个,持续秒级)。演示定位可接受(README「设计决策」同此结论)。
- **陷阱(未成为现实)**:若未来把 run_timeout 调大到远超 120s 而
  TTL 硬编码不动,超发窗口随之变大(老请求合法存活更久)。TTL 的
  "120"写死在 `coordination.py` `RedisLLMLimiter.acquire` 的参数里,
  与 run_timeout 无联动——调大 run_timeout 时须同步评估这里。

## 3. 第三层:会话锁 423

- **机制**:`SET lock:session:{sha256(session_id)} <uuid> NX PX <ttl>`;
  抢占失败立即 `LockedError` → HTTP 423,不排队(跨实例等待队列的
  生命周期/公平性复杂度与收益不成比例,README「为什么 423 而不是排队」)。
  释放走 Lua:`GET == 持有者 uuid` 才 `DEL`——**只会释放自己持有的锁**,
  不会误删后继持有者。
- **TTL 公式**:`web.py` `_session_guard`:
  `ttl_ms = int((run_timeout or 120) * 1000 + 60_000)`,默认 180s。
- **覆盖面**:chat / decide_approval / delete_session 的**整个临界区**
  (含全部 trace/evidence 写入与响应组装),防丢失更新(会话行是整行
  upsert,无锁并发互相覆盖)。snapshot/list 等只读路径不加锁。
- **崩溃**:持锁实例死亡无 release → 锁存活至 TTL 到期,期间该会话
  所有 chat/decide 得 423;**自愈 ≤ run_timeout+60s**。
- **获取顺序**:先取 LLM 槽(非阻塞,429),再取会话锁(非阻塞,423);
  全部立即失败、无阻塞等待 → 无死锁面。抢锁失败时槽位在 finally 释放。

### 3.1 锁租约讨论:为何当前无需续租

锁 TTL 由**同一个 run_timeout 推导**(TTL = run_timeout + 60s),
而 harness 引擎在**每个步骤循环头部**检查 `time.perf_counter() -
started_at > run_timeout`,超预算即安全停止(`engine.py` `_loop`,
产出「已达到最大执行时间…」并以 status=failed 收尾)。由此得到不变式:

```
持锁临界区时长 ≤ run_timeout(引擎预算)+ 最后一步尾部 + 收尾写入
             < run_timeout + 60s = TTL     (正常情况)
```

即"运行不可能(显著)超过自己的锁"——租约不会在持有者仍在合法工作时
到期,续租机制(P EXPIRE 心跳)因此不必要。**诚实的例外**:预算检查
只发生在步骤间,超预算瞬间的"最后一步"会跑完:一次模型调用最多
3 次尝试×60s 超时+退避(llm.py `max_retries=2`),或一次 MCP 调用带
重连退避(20s×3 次 + 1s/2s/4s)。病理尾部(端点挂起+全量重试)可超
过 +60s 缓冲,此时锁先于最后写入到期,另一实例可入,会话行最后一次
upsert 获胜(丢失更新面仅限该会话自身消息)。概率低、影响面小,
演示可接受;生产可把缓冲从 60s 调大或按下节心跳。

**陷阱**:若未来把 TTL 改为独立配置(与 run_timeout 解耦)且允许
配置小于 run_timeout+尾部,上述不变式即破坏——锁会在合法运行中途
过期,双实例同会话并发成为常态而非异常。改配置时必须保持
`TTL ≥ run_timeout + 最长单步尾部 + 收尾余量`。

### 3.2 可选心跳方案草图(不实现)

持有者每 30s 执行 Lua:`if GET(key)==my_uuid then PEXPIRE(key, 60s)`;
返回 0 说明锁已易主,持有者立即停止后续写入(放弃剩余步骤,以降级
话术收尾)。要点:心跳线程与业务线程共享"是否继续"标志;心跳失败
处理即事实上的租约丢失语义。复杂度(独立心跳线程/停写传播/与
run_timeout 的组合)超过当前问题规模,故不实现。

### 3.3 网络分区下的双主窗口 = TTL

实例 A 持锁后被网络分区(与 Redis 断连但仍在运行):Redis 端锁在
TTL 到期后消失,实例 B 可获取同一会话的锁 → **双主窗口最长 = TTL**
(默认 180s)。当前没有 fencing token(锁值是随机 uuid,不是单调序),
存储端(PG upsert)不校验写入者是否仍是锁持有者,后写覆盖先写。
**缓解方向(讨论,未实现)**:锁值改为单调递增的 fencing token,
会话行携带最后接受的 token,upsert 带 `WHERE lock_token < %s`——
PG 行版本机制天然承载该校验。当前实际风险低:双主要求同会话请求
恰好分散到两实例且前者被隔离而非死亡,且最坏后果是该会话消息
丢失更新;真正的高危写(审批/工单)由第四层的条件 UPDATE 兜底,
不依赖会话锁。

### 3.4 Redis 整体不可用

会话解析(`resolve_user`)先于一切业务逻辑访问 Redis:Redis 不可达时
所有需登录的 API 返回 500(「请求失败:…」);静态页与 `/api/health`
(无鉴权、不碰 Redis)仍可服务。这是**集中式协调器的可用性代价**,
当前不做本地缓存降级(缓存令牌会削弱"改密即全端失效"的吊销语义)。

## 4. 第四层:审批恰好一次

- **机制(SQL 一字未改地继承单进程版,仅 `?`→`%s`)**:

  ```sql
  UPDATE approvals SET status=%s, decided_at=%s, decided_by=%s, decision_note=%s
  WHERE id=%s AND status='pending'
  ```

  `cursor.rowcount == 0` 即抛 `PolicyError`(「approval is already
  decided」)。PG 行锁把并发 decide 串行化:**恰好一个事务的 rowcount=1**。
  工单域 `wi_approvals.decide` 是同款条件 UPDATE。
- **不变式**:一个审批有且只有一个终态(approved/rejected),之后
  `mark_execution` 只允许从 approved 迁移;`commit_issue` 用
  `SELECT … FOR UPDATE` + `status='consumed' AND issue_id` 判定实现
  幂等重放(`idempotent_replay: true`),并发 commit 只建一单。
- **崩溃窗口(如实)**:decide 落库后、`mark_execution` 前实例崩溃 →
  审批停留 `approved`、工具未执行,**没有自动重放/对账任务**——审批卡
  会如实显示中间态,需人工重新触发或放弃。同样,`decided` 到执行之间
  的双门(工单域还有第二把审批锁)保证伪造检查点也无法直接建单。
- **分区**:该层只依赖 PG,与 Redis 分区无关;PG 主备切换超出当前
  部署形态(单实例)。

## 5. 幂等现状

- **同会话请求**:被会话锁串行化(请求级互斥)——并发同会话 chat
  一成一败(423),不会交错执行;这**不是**请求级幂等:客户端对同一
  请求重试(先 423 后重发)会真的再跑一次 run。
- **无请求幂等键**:没有 `Idempotency-Key` 类机制;网络层的重试面
  上,nginx 对已发送的非幂等 POST 不重试(§1),客户端自行重试则由
  会话锁+审批状态机把重复损害限制在"多跑一次读多写少的 run"。
- **方案草图(不实现)**:请求头带 `Idempotency-Key`,web 层
  `SETNX idemp:{hash} "" EX 600` 抢占,抢到者执行并把响应摘要写回
  键值,后来者直接返回摘要。需要处理响应体大小(存 hash+需一致性
  校验)与键清理,当前流量形态下收益有限。

## 6. Redis 键丢失时的并发正确性

Redis 重建(容器重建/宿主重启,当前无持久化卷)瞬间清空全部键:
锁全消(同会话双主窗口=0 时刻打开,见 §3.3)、LLM 额度回满、
登录态全失效(全员重登)、节流清零。**审批层不受影响**(PG 独立)。
逐键丢失可接受性论证见 `docs/operations.md` §4。

## 7. MCP 熔断的并发正确性(`billguard/mcp_runtime.py`)

- **状态机**:closed → open(重连 3 次全败,冷却 60s)→ half_open
  (冷却期满的第一个调用,单探测)→ closed(探测成功);参数:
  `reconnect_attempts=3`、退避 `base×2^n` = **1s/2s/4s**、
  `circuit_cooldown=60s`、`half_open_expiry=30s`。
- **并发正确性**:
  1. 状态迁移(closed↔open↔half_open、recovering 标志)全部在
     `_state_lock`(RLock)临界区内;重连 I/O 与退避睡眠在锁外——
     不会持锁做网络调用;
  2. **单恢复者**:`recovering` 标志保证同一时刻只有一个线程执行
     重连/半开探测,并发调用者在 `_admit_call` 得 `MCPCircuitOpenError`
     快速失败(不发网络调用);
  3. **半开过期兜底**:探测线程异常中断(BaseException 越过 finally)
     会留下 `half_open + recovering=False` 的卡死态;`_admit_call` 对
     `time.monotonic() - half_open_at > 30s` 的过期半开态重新放行一次
     探测(带 `expired=True` 审计事件),避免永久快速失败。
- **错误分类**:`_is_connection_error` 只沿显式因果链
  (`__cause__`+异常组展开)判定连接类(TimeoutError/TransportError/
  报文特征等);**HTTP 5xx/4xx 不是连接死亡**——不重连、不熔断,
  原样上抛。仅上游 408/429(明确的"请重试"信号)按连接类处理。
  `__context__` 隐式链不参与判定(曾把无关 500 误判成连接类触发
  多余重连,故收紧)。
- **降级语义(与审计的联动)**:熔断拒绝被 `_make_handler` 转为
  `{"error": …, "degraded": true}` 结构化结果,Agent 循环存活:
  - 读/低写工具:记为成功,运行以降级话术收尾;
  - **高写工具:按失败记账**——引擎 emit `tool_error` 并在审批恢复
    路径 `mark_execution(False)`,**审计不落"已执行"**(降级期间
    没有任何业务动作发生,审批卡如实显示"执行失败")。

## 8. 非目标 / 明确不做

- **不做分布式等待队列/公平调度**:423 立即拒绝,客户端显式重试;
- **不做锁续租心跳**(§3.2 草图止于讨论);
- **不做 fencing token**(§3.3 方向讨论,未实现,风险与缓解已写明);
- **不做请求级幂等键**(§5 草图止于讨论);
- **不做 LLM 限额的令牌桶/排队**:并发槽是硬门槛,超限 429,
  不平滑、不排队;
- **不做 PG 侧多主/主备**:单 PG 实例,分区语义不适用于存储层。
