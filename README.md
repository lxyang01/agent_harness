# BillGuard · 可审计的账单守卫 Agent

让 LLM Agent 直接碰业务数据是不可信的:它会编数字、越权调工具、被账单备注里的提示注入带着跑,写操作更没人拦。BillGuard 是这个问题的一个完整工程答案 —— 一个**框架无关的可审计 Agent Harness**,以"分层防线 + 可验证"为设计原则,业务载体是个人账单守卫:从账单与订阅 CSV 中发现涨价、重复扣费和大额离群,输出带证据的行动计划。

每一层防线都可独立验证:**22 条对抗探针**(零费用、确定性)全部防御成功,155 项单元测试覆盖并发竞态、权限边界与数据隔离。

| 防线 | 一句话证据 |
| --- | --- |
| **受控工具面** | 模型只能调用注册过的工具,不执行任意 SQL/Shell;参数过 Schema 硬校验,越权参数被拦截 |
| **Skill 路由与契约** | 触发词/`$显式`路由到 4 个 Skill;白名单在模型可见面与执行面同时生效;报告缺章节、漏调工具、参数越界都会被拦截并给出纠正反馈 |
| **三阶段审批** | 高风险写操作以 Checkpoint 持久化暂停(`prepare → 人工审批 → commit`),批准后跨进程恢复;伪造审批人被服务端身份覆盖 |
| **登录与角色** | HttpOnly Cookie 会话 + admin/user 两角色能力矩阵;`decided_by`/`operator` 一律取服务端身份 |
| **按用户数据隔离** | 本地与 MCP 工具走同一条 owner 边界;模型伪造 `owner` 参数会被注入层覆盖(对抗探针实锤验证) |
| **并发与资源加固** | 审批乐观并发恰好一次生效;有界线程池 + 排队 503;LLM 并发上限 429;单次运行总时间预算安全停止 |

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

打开 <http://127.0.0.1:8000> 登录。角色:admin=用户管理+全部业务,user=业务写入+审批。**每个用户的数据相互隔离** —— 各自导入自己的账单副本,看板与守卫 Agent 只能看到本人数据。默认离线 Mock 模式,不需要 API Key。

### 三分钟演示剧本(Mock 模式)

1. 登录后进入"账单导入",依次导入两份样例:`sample_data/bills_demo.csv` 和 `sample_data/subscriptions_demo.csv`(内嵌两条故事线:腾讯视频预期 ¥15/月、8 月实扣 ¥25;百度网盘同日 5 分钟内两笔 ¥18)
2. 问守卫 Agent:**"最近有什么异常扣费"** → 检出腾讯视频涨价
3. 问:**"有没有重复扣费"** → 检出百度网盘两笔
4. 问:**"生成本月守卫报告"** → 四章节报告(支出事实/异常清单/根因推测/行动计划),缺章节会被契约拦截
5. 问:**"帮我取消腾讯视频订阅"** → 本地模式提示需接工单服务;按下方"进阶运维"接入 Work Item MCP 后,同一句话走完整三阶段审批

### 切换真实模型

```powershell
$env:OPENROUTER_API_KEY="你的 Key"
python -m billguard.web --tool-source mcp --llm openai --base-url "https://openrouter.ai/api/v1" --model "openai/gpt-4o-mini" --llm-proxy "http://127.0.0.1:7897"
```

不要将 API Key 写入代码或提交到 Git。使用本地 MCP 工具可省略 `--tool-source mcp`。

## 架构

```text
账单 CSV + 订阅 CSV
 └─ Import + Validation + Deduplication
     └─ SQLite Bill Store(按 owner 隔离)
         ├─ 支出概览 / 交易明细 / 类别管理
         └─ Controlled Bill Tools(本地 + MCP 同一 owner 边界)
             └─ BillGuard 账单守卫助手
                 └─ HarnessEngine
                     ├─ Skill Runtime(路由 + 工具白名单 + 完成契约)
                     ├─ Policy Gateway(高风险写 → Checkpoint 审批)
                     ├─ Context / Session(按用户归属)
                     ├─ Mock or OpenAI-compatible LLM
                     └─ JSONL Trace(全量留痕,可回放)
```

LLM 不执行任意 SQL。它只能调用受控工具查询概览、环比、异常、脱敏样本和订阅比对,回答中的每个数字都可追溯到工具结果;模型调用、工具调用、错误与最终答案全量写入 JSONL Trace。`billguard/agents/planning.py` 保留了一个 PlanningAgent,展示同一 Harness 换业务载体的成本接近于零。

## 核心能力

### Skill Runtime

- 启动时只发现 `SKILL.md` 的名称和描述,激活时才加载完整正文
- `$skill-name` 显式选择 + 业务触发词隐式路由;每次最多激活两个 Skill,记录内容哈希、匹配原因和分数
- Skill 白名单同时作用于模型可见工具与执行校验;路由失败、内容损坏均安全失败;`skill_activated`/`skill_error` 写入 Trace

| Skill | 用途 |
| --- | --- |
| `bill-triage` | 支出概览、账单分流和初步证据收集 |
| `anomaly-investigation` | 环比比较、四类异常下钻和低基数排查 |
| `root-cause-analysis` | 区分交易事实、原因假设、反向证据和验证动作 |
| `monthly-guard-report` | 证据化的月度守卫报告与行动计划 |

路由与工具策略位于 `skills/routes.json`。月度守卫报告有固定四章节完成契约(**支出事实 / 异常清单 / 根因推测 / 行动计划**),缺任何章节会被拦截并获得可执行的纠正反馈。

**四类异常的确定性阈值**(与 `billguard/bills.py` 常量一致,随结果一并返回供核对):

| 维度 | 判定 |
| --- | --- |
| spike | 本期支出 ≥ 上期×2 且 ≥¥100 |
| duplicate | 同商户同金额,间隔 ≤3 天 |
| price_hike | 订阅最近实扣与预期差额 ≥ max(¥1, 预期×20%) |
| outlier | 单笔 ≥¥200 且 ≥ 类别均值×5 |

### MCP Runtime

既是 MCP Host,也内置两个 MCP Server:

| 组件 | Transport | 能力 |
| --- | --- | --- |
| Bill Data MCP | stdio / Streamable HTTP | 6 Tools、3 Resources、2 Prompts |
| Work Item MCP | Streamable HTTP / stdio | 查询工单、准备工单、审批后幂等提交 |

`MCPClientManager` 在后台事件循环维护持久 `ClientSession`,对同步 Harness 提供超时受控接口;远程工具以 `server.tool` 命名空间动态注册(如 `bill.detect_anomalies`、`work-items.commit_issue`)。共享的 MCP 子进程无法认证,因此 owner 身份在**工具边界服务端注入**:web 层包装每个 `bill.*` 工具强制覆盖 `owner` 并从模型可见 Schema 中移除 —— 伪造无效。

工单创建采用三阶段协议:

```text
Agent: prepare_issue
  -> Human: approve/reject outside MCP tool channel
  -> Agent: commit_issue
```

`approve` 不出现在工具列表;未经人工批准的 `commit_issue` 失败,批准后重复提交幂等返回同一工单。

### 策略网关与可恢复审批

动态 MCP 工具按服务端风险元数据映射为 `read`/`low_write`/`high_write`/`forbidden`。高风险写不直接执行:Harness 把 Session、Trace、工具参数、Skill 版本和白名单持久化为 Checkpoint 并返回 `approval_pending`;Web 审批中心查看完整参数后批准/拒绝,批准后从暂停 step 跨进程恢复。创建正式工单还要过 Work Item 服务自身的业务审批 —— 双重防线。

Harness 还从用户原话编译**动态契约**:"最多 8 条"变成参数上限,报告类任务变成章节结构;漏调、乱序、越界、缺章节全部拦截。

### 多用户与角色

- 强制登录:HttpOnly + SameSite=Strict Cookie,服务端只存 token 哈希,7 天滑动过期;空用户库拒绝启动
- 能力矩阵服务端强制:user=业务写入+审批+导出;admin=全部+用户管理
- `decided_by`/`operator` 一律取服务端登录身份,请求体伪造无效
- 账单数据按用户隔离,各自导入自己的副本;存量无主数据仅 admin 可见;分析 Session 同样按用户归属

### 并发与限流

- 审批决定条件 UPDATE 乐观并发:并发 decide 恰好一个生效
- `chat` 与审批恢复共用 per-session 锁,消除会话文件丢失更新
- 有界线程池 + 排队(满载 503)、LLM 并发上限(429)、单次运行总时间预算(安全停止)

## 评测

### 对抗评测(确定性,零费用)

```powershell
python -m billguard.adversarial_eval
```

用恶意脚本模型直接驱动真实 Parser、Harness、Registry、Policy、Checkpoint、Session 与沙箱组件。覆盖:模型协议破坏、工具越权、Schema 注入、无限循环、参数/输出契约、提前结束、审批绕过/重放、Checkpoint 篡改、无证据数字、PII 泄露、资源预算、伪造审批身份、路径穿越、并发审批双提交、跨用户数据泄露。

基线演进(每一步都对应一次真实的工程修复):

| 轮次 | 结果 |
| --- | --- |
| 首轮 | 15/20 |
| + 资源预算与脱敏门禁 | 19/20 |
| + 身份层(登录/角色/服务端身份) | 20/20 |
| + 并发加固(乐观并发/会话锁) | 21/21 |
| + 数据隔离(owner 边界 + MCP 注入) | **22/22(100%)** |

完整报告在本地 `docs/adversarial_evaluation_report.md`(命令可随时再生成)。

### 路由评测(确定性,零费用)

```powershell
python -m billguard.eval --variant compare
```

50 条固定中文用例,三组消融:Baseline 20%(10/50)→ Skills 100%(50/50)→ Full 100%(50/50)。完成契约指标在该数据集为 N/A(用例只声明期望 Skill;契约本身由对抗探针与真实模型评测覆盖)。

### 真实模型端到端评测

```powershell
$env:OPENROUTER_API_KEY="你的 Key"
python -m billguard.live_eval --limit 3 --proxy "http://127.0.0.1:7897" --confirm-live   # 冒烟
python -m billguard.live_eval --repeats 3 --proxy "http://127.0.0.1:7897" --confirm-live # 稳定性(15×3)
python -m billguard.live_eval --cases "7-10,12-15" --proxy "http://127.0.0.1:7897" --confirm-live  # 只回归指定用例
```

没有 `--confirm-live` 时只显示计划、不发起 API 调用。Runner 每次评测创建隔离数据快照与临时 MCP 服务;日期相对评测日平移;工单用例只运行到审批 Checkpoint。模型客户端对 TLS/连接/429/5xx 有界重试,连续 3 次基础设施错误自动熔断并单独统计。报告含任务成功率、Skill 路由、工具选择与顺序、参数准确率、证据命中、数字 Groundedness、因果措辞安全、报告结构、审批违规率、重复稳定性、P95 延迟与 Token 汇总,写入 `.sessions/evaluations/`,可在 Web"自动评测"面板查看。

## 工作台功能

- **支出概览**:总支出/笔数/待核查/最大单笔,类别与商户分布,每日趋势,订阅清单(周期/预期/最近扣款)*—— 试试:"这个月花了多少钱"*
- **守卫 Agent**:总结结构、比较周期、识别异常、搜交易、读脱敏样本,区分数据事实与推测;回答附带可点击的"数据依据"直达交易明细 *—— 试试:"支付类支出最近有什么变化"*
- **交易明细**:多维筛选与分页,导出 CSV(含未脱敏备注,登录用户可用),人工修正类别,批量核查(≤200)留审计
- **类别管理**:关键词规则增删改/启停/审计;重匹配只替换规则类别,人工类别保留
- **账单导入**:UTF-8 CSV 按交易编号去重、自动归类,记录成功/重复/失败明细
- **守卫报告**:保存/复制/导出 Markdown/A4 打印
- **Session 与 Trace**:会话按用户隔离;运行观测面板只读回放 Trace —— Skill 激活、模型决策、耗时、Token、审批暂停/恢复

## 项目结构

```text
billguard/
├─ harness/            # Agent 内核:engine(受控循环/预算/安全停止)、spec、contracts(动态契约)、context
├─ agents/             # 业务 Agent:bills.py(工具面+Mock)、planning.py(换载体示范)
├─ skills.py           # Skill 发现/路由/白名单/完成规则
├─ policy.py           # 风险分级 + Checkpoint 审批(条件 UPDATE 乐观并发)
├─ bills.py            # 账单/类别/订阅存储 + 四类异常检测 + for_user 隔离视图
├─ auth.py             # 用户/会话/能力矩阵(PBKDF2 + token 哈希)
├─ web.py              # HTTP 层:鉴权门/能力路由/owner 注入/有界线程池
├─ mcp_runtime.py      # MCP Host(闭包包装,具名参数注入免疫)
├─ mcp_servers/        # bill_server / work_item_server
├─ adversarial_evaluation.py  # 22 条对抗探针
├─ evaluation.py / live_evaluation.py  # 路由消融 / 真实模型 E2E
└─ web_static/         # 原生 JS 前端
skills/                # 4 个 SKILL.md + routes.json(运行时加载)
tests/                 # 155 项:并发竞态/隔离/契约/演示回归
evals/                 # 50 路由用例 + 15 live 用例
sample_data/           # 带剧本的合成账单(生成器在 scripts/)
```

## 运行参数速查

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--llm mock\|openai` | mock | 离线 Mock 或 OpenAI 兼容 API |
| `--tool-source local\|mcp` | local | 本地工具或 MCP 动态发现 |
| `--work-item-mcp-url` | 空 | 接入远程 Work Item MCP(解锁三阶段审批演示) |
| `--max-concurrent-llm` | 4 | 模型并发上限,超限 429 |
| `--max-threads` / `--queue-capacity` | 16 / 32 | HTTP 线程池与排队容量,满载 503 |
| `--run-timeout` | 120s | 单次 Agent 运行总预算,超限安全停止 |
| `--data-dir` | .sessions | 数据根目录(billguard/ 子树按用户隔离) |
| `--llm-proxy` / `--base-url` / `--model` | — | 模型接入三件套 |

## 进阶运维

### 远程 Work Item MCP(解锁三阶段审批演示)

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
python -m billguard.users add <用户名> --role <admin|user>   # 建号,--password-stdin 可从管道读密码
python -m billguard.users list
python -m billguard.users set-role <用户名> --role <角色>
python -m billguard.users reset-password <用户名>
python -m billguard.users disable <用户名> / enable <用户名>
```

保护约束:不能禁用自己,不能禁用/降级最后一个启用中的 admin。Web 内置等价管理面板(admin 可见)。

## 数据与隐私

- 账单库 `.sessions/billguard/bills/`;Session 与 Trace `.../sessions/`;审批 Checkpoint `.../policy/`;认证库 `.../auth/`(PBKDF2 密码哈希,会话只存 token 哈希);评测报告 `.sessions/evaluations/`
- Agent 最多读取 20 条样本;发给模型的搜索结果与样本做基础手机号/邮箱脱敏;原始备注只在本机页面展示
- 真实账单数据不能离开内网时,使用本地模型或纯统计看板

**CSV 格式**:账单必要字段 `tx_id,paid_at,merchant,category,amount,method,note`;订阅必要字段 `name,merchant,cycle,expected_amount`;支持对应中文表头。

```csv
tx_id,paid_at,merchant,category,amount,method,note
TX0001,2026-06-01 11:41:15,美团外卖,餐饮,21.28,微信支付,
```

## 当前边界

- 企业级 SSO(OIDC/LDAP)尚未接入(单点身份解析锚点已预留);类别归类为关键词规则;脱敏为规则级,不替代企业级 DLP;PDF 依赖浏览器打印
- 设计文档保留在本地 `docs/` 目录,未随仓库分发

## 测试

```powershell
python -m unittest discover -s tests -v
```

当前 **155 项**:并发竞态专项、身份与隔离、运行时加固、离线演示回归(报告四章节、三阶段审批、多轮会话)。
