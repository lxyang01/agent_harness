# Feedback Agent Runtime

一个面向客户反馈洞察场景的**可审计 Agent Harness**:框架无关的受控 Loop、Session、Context、Tool Registry 与 Trace,叠加兼容 Agent Skills 目录规范的 Skill Runtime、MCP 双端运行时和策略化审批。业务载体是 Feedback Lens —— 一个从客服 CSV 中发现高频问题、趋势变化和典型案例的洞察工作台。

**核心亮点**

- **受控 Harness**:模型只能调用注册过的受控工具,不执行任意 SQL/Shell;工具参数过 Schema 硬校验
- **Skill Runtime**:`$skill-name` 显式选择 + 业务触发词隐式路由,Skill 声明的工具白名单在模型可见面和执行面同时生效
- **MCP Host + Server**:既是 MCP Host(动态发现远程工具),也内置 Feedback Data / Work Item 两个 MCP Server
- **三阶段审批协议**:`prepare → 人工审批 → commit`,高风险写操作以 Checkpoint 持久化暂停,批准后可跨进程恢复
- **登录与三角色**:强制 Cookie 会话认证,admin / approver / viewer 能力矩阵,审批与操作身份一律取服务端
- **三层评测**:确定性对抗评测 21/21、路由消融对照、真实模型端到端任务

## 快速开始

```powershell
cd D:\shixi\aicoding\feedback-agent-runtime
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .

# 首次启动前创建管理员(交互输两次密码,至少 8 位)
python -m minimal_agent.users add admin --role admin
python -m minimal_agent.web
```

打开 <http://127.0.0.1:8000> 登录(角色:admin=用户管理+全部业务,approver=业务写入+审批,viewer=只读+对话),进入"导入记录"导入:

```text
sample_data/bills_demo.csv
sample_data/subscriptions_demo.csv
```

即可查看看板并向账单守卫 Agent 提问。默认离线 Mock 模式,不需要 API Key。

切换 OpenRouter 真实模型(同一窗口设置环境变量后启动):

```powershell
$env:OPENROUTER_API_KEY="你的 Key"
python -m minimal_agent.web --tool-source mcp --llm openai --base-url "https://openrouter.ai/api/v1" --model "openai/gpt-4o-mini" --llm-proxy "http://127.0.0.1:7897"
```

不要将 API Key 写入代码或提交到 Git。使用本地 MCP 工具可省略 `--tool-source mcp`。

## 架构

```text
CSV
 └─ Import + Validation + Deduplication
     └─ SQLite Feedback Store
         ├─ Dashboard / Filters / Export
         └─ Controlled Feedback Tools
             └─ FeedbackInsightAgent
                 └─ HarnessEngine
                     ├─ Context / Session
                     ├─ Mock or OpenAI-compatible LLM
                     ├─ Tool schema validation
                     └─ JSONL Trace
```

LLM 不执行任意 SQL。它只能调用受控工具查询概览、周期对比、反馈搜索和脱敏样本,回答中的数据可以追溯到工具结果;每次模型调用、工具调用、错误和最终答案都写入结构化 JSONL Trace。原有 PlanningAgent 保留在 `minimal_agent/agents/planning.py`,用于展示同一个 Harness 如何承载不同业务 Agent。

## 核心能力

### Skill Runtime

- 启动时只发现 `SKILL.md` 的名称和描述,激活时才加载完整正文
- 显式 `$skill-name` 与基于触发词的隐式路由;每次最多激活两个 Skill,记录内容哈希版本、匹配原因和分数
- Skill Policy 缩小本轮模型可见工具范围,工具执行时使用同一白名单;路由失败、内容损坏均安全失败
- `skill_activated` / `skill_error` 事件写入 Harness Trace

| Skill | 用途 |
| --- | --- |
| `feedback-triage` | 反馈概览、分流和初步证据收集 |
| `anomaly-investigation` | 周期对比、异常下钻和低基数排查 |
| `root-cause-analysis` | 区分事实、原因假设、反向证据和验证动作 |
| `executive-report` | 生成证据化的管理层报告和行动计划 |

路由与工具策略位于 `skills/routes.json`,运行 Web 时自动加载项目根目录的 `skills/`。

### MCP Runtime

| 组件 | Transport | 能力 |
| --- | --- | --- |
| Feedback Data MCP | stdio / Streamable HTTP | 6 Tools、3 Resources、2 Prompts |
| Work Item MCP | Streamable HTTP / stdio | 查询工单、准备工单、审批后幂等提交 |

`MCPClientManager` 在后台事件循环中维护持久 `ClientSession`,对同步 Harness 提供超时受控的同步接口;远程工具以 `server.tool` 命名空间动态注册(如 `feedback.aggregate`、`work-items.commit_issue`)。工单创建采用三阶段协议:

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
- 分析 Session 按用户归属隔离;存量无主 Session 仅 admin 可见

### 并发与限流

- 审批决定使用条件 UPDATE 乐观并发:并发 decide 恰好一个生效
- `chat` 与审批恢复共用 per-session 锁,消除会话文件丢失更新
- LLM 并发上限(`--max-concurrent-llm`,默认 4),超限返回 429"服务繁忙"

## 评测

### 对抗评测(确定性,零费用)

```powershell
python -m minimal_agent.adversarial_eval
```

用恶意脚本模型直接驱动真实 Parser、Harness、Registry、Policy、Checkpoint、Session 与沙箱组件,覆盖:模型协议破坏、工具越权、Schema 注入、无限循环、参数/输出契约、提前结束、审批绕过/重放、Checkpoint 篡改、无证据数字、PII 泄露、资源预算、伪造审批身份、路径穿越、并发审批双提交。

基线演进:首轮 15/20 → 资源预算与脱敏门禁后 19/20 → 身份层后 20/20 → 并发加固后 **21/21(100%)**,探针异常 0。完整报告见本地 `docs/adversarial_evaluation_report.md`(评测命令可随时再生成)。

### 路由与完成契约评测(确定性,零费用)

```powershell
python -m minimal_agent.eval --variant compare
```

50 条固定中文用例,Baseline / Skills / Full Runtime 三组消融(当前 20% / 80% / 100%);数字仅代表路由与完成契约回归,不代表 LLM 回答质量。

### 真实模型端到端评测

```powershell
$env:OPENROUTER_API_KEY="你的 Key"
python -m minimal_agent.live_eval --limit 3 --proxy "http://127.0.0.1:7897" --confirm-live   # 冒烟
python -m minimal_agent.live_eval --repeats 3 --proxy "http://127.0.0.1:7897" --confirm-live # 稳定性(15×3)
python -m minimal_agent.live_eval --cases "7-10,12-15" --proxy "http://127.0.0.1:7897" --confirm-live  # 只回归指定用例
```

没有 `--confirm-live` 时只显示计划、不发起 API 调用。Runner 为每次评测创建隔离数据快照和临时 MCP 服务;日期相对评测日平移;工单用例只运行到审批 Checkpoint。模型客户端对 TLS/连接/429/5xx 有界重试,连续 3 次基础设施错误自动熔断并单独统计。报告包含任务成功率、Skill 路由、工具选择与顺序、参数准确率、证据命中、数字 Groundedness、因果措辞安全、报告结构、审批违规率、重复稳定性(`repeats=1` 时显示 N/A)、P95 延迟和 Token 汇总,写入 `.sessions/evaluations/`,可在 Web"自动评测"面板查看。

## 工作台功能

- **数据概览**:总量/待处理、高频问题标签、按天趋势、模块与客户等级分布、相邻周期异常增长对比,支持 7/30/90 天与模块筛选
- **洞察 Agent**:总结主要问题、比较周期、识别异常、搜索反馈、读取脱敏样本,区分数据事实与原因推测;回答附带可点击的"数据依据",跳转到对应筛选的反馈明细
- **反馈明细**:按工单/内容搜索,多维筛选与分页,导出 CSV,人工修正单条标签,批量处理(≤200 条)状态/优先级/负责人并留审计
- **标签管理**:关键词规则增删改、启停、审计记录;重新匹配只替换规则生成的标签,人工标签保留
- **数据导入**:UTF-8 CSV 按工单 ID 去重、缺失字段标准化、自动打标,记录成功/重复/失败明细;提供模板下载
- **洞察报告**:Agent 回答保存为报告,支持查看、复制、导出 Markdown、A4 打印/PDF、删除
- **Session 与 Trace**:分析会话相互隔离,可新建/切换/删除;运行观测面板支持只读 Trace 回放,查看 Skill 激活、模型决策、耗时、Token 与审批暂停/恢复

## 进阶运维

### 远程 Work Item MCP(可选)

```powershell
# 窗口一:启动 Work Item MCP
python -m minimal_agent.mcp_servers.work_item_server --data-dir ".sessions/work-items" serve --transport streamable-http --host 127.0.0.1 --port 8020
# 窗口二:Web 接入
python -m minimal_agent.web --tool-source mcp --work-item-mcp-url "http://127.0.0.1:8020/mcp"
```

```powershell
python -m minimal_agent.mcp_servers.work_item_server --data-dir ".sessions/work-items" pending
python -m minimal_agent.mcp_servers.work_item_server --data-dir ".sessions/work-items" approve "APR-XXXXXXXXXX" --by "product-owner"
python -m minimal_agent.mcp_servers.work_item_server --data-dir ".sessions/work-items" reject "APR-XXXXXXXXXX" --by "product-owner"
```

### 用户管理 CLI

```powershell
python -m minimal_agent.users add <用户名> --role <admin|approver|viewer>   # 建号,--password-stdin 可从管道读密码
python -m minimal_agent.users list
python -m minimal_agent.users set-role <用户名> --role <角色>
python -m minimal_agent.users reset-password <用户名>
python -m minimal_agent.users disable <用户名> / enable <用户名>
```

保护约束:不能禁用自己,不能禁用/降级最后一个启用中的 admin。Web 内置等价的管理面板(admin 可见)。

## 数据与隐私

- 反馈数据库 `.sessions/feedback/feedback.db`;分析 Session 与 Trace 在 `.sessions/feedback_sessions/`;认证库 `.sessions/auth/`(密码 PBKDF2 哈希,会话只存 token 哈希)
- Agent 最多读取 20 条样本;发给模型的搜索结果和样本做基础手机号/邮箱/订单号脱敏;原始反馈只在本机页面展示
- 真实客户数据不能离开内网时,使用本地模型或纯统计看板

**CSV 格式**:必要字段 `ticket_id,created_at,content`;完整字段增加 `product_module,customer_tier,status`;同时支持对应中文表头("工单 ID""创建时间"等)。

```csv
ticket_id,created_at,product_module,content,customer_tier,status
TK-001,2026-08-04 10:30:00,支付,微信支付失败,高级,待处理
```

## 当前边界

- 企业级 SSO(OIDC/LDAP)与租户数据隔离尚未接入(登录与最简三角色已可用)
- 请求排队/线程池尚未接入,当前为 429 拒绝式限流
- 标签自动分类使用关键词规则,不是逐条调用大模型
- 基础脱敏不应替代企业级数据脱敏系统;PDF 依赖浏览器打印
- 设计文档保留在本地 `docs/` 目录,未随仓库分发

## 测试

```powershell
python -m unittest discover -s tests -v
```

当前 108 项(含并发专项与身份层用例)。
