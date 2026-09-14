# BillGuard

一个面向个人账单守卫场景的**可审计 Agent Harness**:框架无关的受控 Loop、Session、Context、Tool Registry 与 Trace,叠加兼容 Agent Skills 目录规范的 Skill Runtime、MCP 双端运行时和策略化审批。业务载体是 BillGuard —— 一个从账单与订阅 CSV 中发现异常扣费、涨价和重复收费,并输出可执行行动计划的守卫工作台。

**核心亮点**

- **受控 Harness**:模型只能调用注册过的受控工具,不执行任意 SQL/Shell;工具参数过 Schema 硬校验
- **Skill Runtime**:`$skill-name` 显式选择 + 业务触发词隐式路由,Skill 声明的工具白名单在模型可见面和执行面同时生效
- **MCP Host + Server**:既是 MCP Host(动态发现远程工具),也内置 Bill Data / Work Item 两个 MCP Server
- **三阶段审批协议**:`prepare → 人工审批 → commit`,高风险写操作以 Checkpoint 持久化暂停,批准后可跨进程恢复
- **登录与三角色**:强制 Cookie 会话认证,admin / approver / viewer 能力矩阵,审批与操作身份一律取服务端
- **三层评测**:确定性对抗评测 22/22、路由消融对照、真实模型端到端任务

## 快速开始

```powershell
cd D:\shixi\aicoding\feedback-agent-runtime
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .

# 首次启动前创建管理员(交互输两次密码,至少 8 位)
python -m billguard.users add admin --role admin
python -m billguard.web
```

打开 <http://127.0.0.1:8000> 登录(角色:admin=用户管理+全部业务,approver=业务写入+审批,viewer=只读+对话)。每个用户的数据相互隔离:各自登录后导入自己的账单副本,看板与守卫 Agent 只能看到本人数据。进入"账单导入"导入两份样例:

```text
sample_data/bills_demo.csv
sample_data/subscriptions_demo.csv
```

即可查看看板并向账单守卫 Agent 提问。默认离线 Mock 模式,不需要 API Key。

**演示建议**:样例数据内嵌两条故事线 —— 视频会员涨价(腾讯视频预期 ¥15/月,8 月实扣 ¥25)和云盘重复扣费(百度网盘同日 5 分钟内两笔 ¥18)。先问"最近有什么异常扣费"再问"生成本月守卫报告",能完整走通检测、下钻和报告链路。

切换 OpenRouter 真实模型(同一窗口设置环境变量后启动):

```powershell
$env:OPENROUTER_API_KEY="你的 Key"
python -m billguard.web --tool-source mcp --llm openai --base-url "https://openrouter.ai/api/v1" --model "openai/gpt-4o-mini" --llm-proxy "http://127.0.0.1:7897"
```

不要将 API Key 写入代码或提交到 Git。使用本地 MCP 工具可省略 `--tool-source mcp`。

## 架构

```text
账单 CSV + 订阅 CSV
 └─ Import + Validation + Deduplication
     └─ SQLite Bill Store
         ├─ 支出概览 / 交易明细 / 类别管理
         └─ Controlled Bill Tools
             └─ BillGuard 账单守卫助手
                 └─ HarnessEngine
                     ├─ Context / Session
                     ├─ Mock or OpenAI-compatible LLM
                     ├─ Tool schema validation
                     └─ JSONL Trace
```

LLM 不执行任意 SQL。它只能调用受控工具查询概览、环比比较、异常检测、脱敏样本和订阅比对,回答中的数据可以追溯到工具结果;每次模型调用、工具调用、错误和最终答案都写入结构化 JSONL Trace。原有 PlanningAgent 保留在 `billguard/agents/planning.py`,用于展示同一个 Harness 如何承载不同业务 Agent。

## 核心能力

### Skill Runtime

- 启动时只发现 `SKILL.md` 的名称和描述,激活时才加载完整正文
- 显式 `$skill-name` 与基于触发词的隐式路由;每次最多激活两个 Skill,记录内容哈希版本、匹配原因和分数
- Skill Policy 缩小本轮模型可见工具范围,工具执行时使用同一白名单;路由失败、内容损坏均安全失败
- `skill_activated` / `skill_error` 事件写入 Harness Trace

| Skill | 用途 |
| --- | --- |
| `bill-triage` | 支出概览、账单分流和初步证据收集 |
| `anomaly-investigation` | 环比比较、四类异常下钻和低基数排查 |
| `root-cause-analysis` | 区分交易事实、原因假设、反向证据和验证动作 |
| `monthly-guard-report` | 生成证据化的月度守卫报告和行动计划 |

路由与工具策略位于 `skills/routes.json`,运行 Web 时自动加载项目根目录的 `skills/`。

月度守卫报告有固定四章节完成契约 —— **支出事实 / 异常清单 / 根因推测 / 行动计划**;触发守卫报告任务时,Harness 会编译输出结构契约,回答缺任何章节都会被拦截并获得可执行的纠正反馈。

异常检测的四个维度使用确定性阈值,默认值与 `billguard/bills.py` 中的常量一致:spike = 本期支出 ≥ 上期×2 且 ≥¥100;duplicate = 同商户同金额间隔 ≤3 天;price_hike = 订阅最近实扣与预期差额 ≥ max(¥1, 预期×20%);outlier = 单笔 ≥¥200 且 ≥ 类别均值×5。阈值会随异常工具结果一并返回,便于核对每条命中的判定依据。

### MCP Runtime

| 组件 | Transport | 能力 |
| --- | --- | --- |
| Bill Data MCP | stdio / Streamable HTTP | 6 Tools、3 Resources、2 Prompts |
| Work Item MCP | Streamable HTTP / stdio | 查询工单、准备工单、审批后幂等提交 |

`MCPClientManager` 在后台事件循环中维护持久 `ClientSession`,对同步 Harness 提供超时受控的同步接口;远程工具以 `server.tool` 命名空间动态注册(如 `bill.detect_anomalies`、`work-items.commit_issue`)。工单创建采用三阶段协议:

```text
Agent: prepare_issue
  -> Human: approve/reject outside MCP tool channel
  -> Agent: commit_issue
```

`approve` 不出现在工具列表中;未经人工批准的 `commit_issue` 失败,批准后重复提交幂等返回同一工单。

### 策略网关与可恢复审批

动态 MCP 工具按服务端风险元数据映射为 `read` / `low_write` / `high_write` / `forbidden`。高风险写操作不直接执行:Harness 把 Session、Trace、工具参数、Skill 版本和白名单持久化为 Checkpoint 并返回 `approval_pending`;Web 审批中心可查看完整参数后批准或拒绝,批准后从暂停 step 继续执行。创建正式工单还会经过 Work Item 服务自身的业务审批,形成双重防线。

Skill 还会声明有序完成契约,Harness 从"最多 N 条"等用户原话编译动态参数契约,并为报告类任务编译输出结构契约 —— 漏调、乱序、参数越界或报告缺章节都会被拦截并获得可执行的纠正反馈。

### 多用户与角色

- 强制登录:HttpOnly + SameSite=Strict Cookie 会话,服务端只存 token 哈希,7 天滑动过期;用户库为空时拒绝启动并提示建号
- 三角色能力矩阵(服务端强制,前端仅隐藏 UI):viewer=只读+对话;approver=业务写入+审批;admin=全部+用户管理
- 审批人 `decided_by` 与操作人 `operator` 一律取服务端登录身份,请求体伪造无效
- 账单数据按用户隔离:每个用户的数据相互隔离,各自导入自己的账单副本;本地工具与 MCP 工具走同一条 owner 边界,模型伪造 `owner` 参数会被服务端身份覆盖
- 分析 Session 按用户归属隔离;存量无主 Session 仅 admin 可见

### 并发与限流

- 审批决定使用条件 UPDATE 乐观并发:并发 decide 恰好一个生效
- `chat` 与审批恢复共用 per-session 锁,消除会话文件丢失更新
- LLM 并发上限(`--max-concurrent-llm`,默认 4),超限返回 429"服务繁忙"
- 有界线程池与请求排队:`--max-threads`(默认 16)+ `--queue-capacity`(默认 32),容量满立即 503,不再无界开线程
- 单次运行总时间预算:`--run-timeout`(默认 120 秒),超限发出 `run_timeout` 事件并安全停止,与最大步数同款兜底

## 评测

### 对抗评测(确定性,零费用)

```powershell
python -m billguard.adversarial_eval
```

用恶意脚本模型直接驱动真实 Parser、Harness、Registry、Policy、Checkpoint、Session 与沙箱组件,覆盖:模型协议破坏、工具越权、Schema 注入、无限循环、参数/输出契约、提前结束、审批绕过/重放、Checkpoint 篡改、无证据数字、PII 泄露、资源预算、伪造审批身份、路径穿越、并发审批双提交、跨用户数据泄露。

基线演进:首轮 15/20 → 资源预算与脱敏门禁后 19/20 → 身份层后 20/20 → 并发加固后 21/21 → 阶段 3 数据隔离后 **22/22(100%)**,探针异常 0。完整报告见本地 `docs/adversarial_evaluation_report.md`(评测命令可随时再生成)。

### 路由与完成契约评测(确定性,零费用)

```powershell
python -m billguard.eval --variant compare
```

50 条固定中文用例,Baseline / Skills / Full Runtime 三组消融。当前实测:Skill 路由准确率 Baseline 20%(10/50)→ Skills 100%(50/50)→ Full 100%(50/50)。完成契约指标在该数据集上为 N/A —— 50 条用例只声明期望 Skill,未声明期望工具清单,评测器因此跳过该比率(完成契约本身由对抗探针 adv-006/007 与真实模型评测覆盖)。数字仅代表路由与完成契约回归,不代表 LLM 回答质量。

### 真实模型端到端评测

```powershell
$env:OPENROUTER_API_KEY="你的 Key"
python -m billguard.live_eval --limit 3 --proxy "http://127.0.0.1:7897" --confirm-live   # 冒烟
python -m billguard.live_eval --repeats 3 --proxy "http://127.0.0.1:7897" --confirm-live # 稳定性(15×3)
python -m billguard.live_eval --cases "7-10,12-15" --proxy "http://127.0.0.1:7897" --confirm-live  # 只回归指定用例
```

没有 `--confirm-live` 时只显示计划、不发起 API 调用。Runner 为每次评测创建隔离数据快照和临时 MCP 服务;日期相对评测日平移;工单用例只运行到审批 Checkpoint。模型客户端对 TLS/连接/429/5xx 有界重试,连续 3 次基础设施错误自动熔断并单独统计。报告包含任务成功率、Skill 路由、工具选择与顺序、参数准确率、证据命中、数字 Groundedness、因果措辞安全、报告结构、审批违规率、重复稳定性(`repeats=1` 时显示 N/A)、P95 延迟和 Token 汇总,写入 `.sessions/evaluations/`,可在 Web"自动评测"面板查看。

## 工作台功能

- **支出概览**:总支出、笔数、待核查占比、近 31 天类别激增数与最大单笔,按类别/商户分布,每日支出趋势,订阅清单(周期/预期金额/最近扣款);支持全部时间/7/30/90 天与类别筛选
- **守卫 Agent**:总结支出结构、比较周期、识别异常、搜索交易、读取脱敏样本,区分数据事实与原因推测;回答附带可点击的"数据依据",跳转到对应筛选的交易明细
- **交易明细**:按交易编号/商户/备注搜索,多维筛选与分页,导出 CSV(导出含未脱敏备注,仅审批人/管理员可用,观察者隐藏导出按钮),人工修正单条类别,批量处理(≤200 条)核查状态并留审计
- **类别管理**:类别关键词规则增删改、启停、审计记录;重新匹配只替换规则生成的类别,人工类别保留
- **账单导入**:UTF-8 CSV 按交易编号去重、缺失字段标准化、自动归类,记录成功/重复/失败明细;支持账单与订阅两份文件
- **守卫报告**:Agent 回答保存为报告,支持查看、复制、导出 Markdown、A4 打印/PDF、删除
- **Session 与 Trace**:分析会话相互隔离,可新建/切换/删除;运行观测面板支持只读 Trace 回放,查看 Skill 激活、模型决策、耗时、Token 与审批暂停/恢复

## 进阶运维

### 远程 Work Item MCP(可选)

```powershell
# 窗口一:启动 Work Item MCP
python -m billguard.mcp_servers.work_item_server --data-dir ".sessions/work-items" serve --transport streamable-http --host 127.0.0.1 --port 8020
# 窗口二:Web 接入
python -m billguard.web --tool-source mcp --work-item-mcp-url "http://127.0.0.1:8020/mcp"
```

```powershell
python -m billguard.mcp_servers.work_item_server --data-dir ".sessions/work-items" pending
python -m billguard.mcp_servers.work_item_server --data-dir ".sessions/work-items" approve "APR-XXXXXXXXXX" --by "product-owner"
python -m billguard.mcp_servers.work_item_server --data-dir ".sessions/work-items" reject "APR-XXXXXXXXXX" --by "product-owner"
```

### 用户管理 CLI

```powershell
python -m billguard.users add <用户名> --role <admin|approver|viewer>   # 建号,--password-stdin 可从管道读密码
python -m billguard.users list
python -m billguard.users set-role <用户名> --role <角色>
python -m billguard.users reset-password <用户名>
python -m billguard.users disable <用户名> / enable <用户名>
```

保护约束:不能禁用自己,不能禁用/降级最后一个启用中的 admin。Web 内置等价的管理面板(admin 可见)。

## 数据与隐私

- 账单数据库 `.sessions/billguard/bills/`;分析 Session 与 Trace 在 `.sessions/billguard/sessions/`;审批 Checkpoint 在 `.sessions/billguard/policy/`;认证库 `.sessions/billguard/auth/`(密码 PBKDF2 哈希,会话只存 token 哈希);评测报告在 `.sessions/evaluations/`
- Agent 最多读取 20 条样本;发给模型的搜索结果和样本做基础手机号/邮箱脱敏;原始备注只在本机页面展示
- 真实账单数据不能离开内网时,使用本地模型或纯统计看板

**CSV 格式**:账单必要字段 `tx_id,paid_at,merchant,category,amount,method,note`;订阅必要字段 `name,merchant,cycle,expected_amount`。同时支持对应中文表头("交易编号""支付时间""商户""类别""金额""支付方式""备注";"订阅名称""周期""预期金额")。

```csv
tx_id,paid_at,merchant,category,amount,method,note
TX0001,2026-06-01 11:41:15,美团外卖,餐饮,21.28,微信支付,
```

## 当前边界

- 企业级 SSO(OIDC/LDAP)尚未接入(登录、三角色与按用户数据隔离已可用)
- 类别自动归类使用关键词规则,不是逐条调用大模型
- 基础脱敏不应替代企业级数据脱敏系统;PDF 依赖浏览器打印
- 设计文档保留在本地 `docs/` 目录,未随仓库分发

## 测试

```powershell
python -m unittest discover -s tests -v
```

当前 154 项(含并发专项、身份层、数据隔离、运行时加固与离线演示回归用例)。
