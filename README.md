# Feedback Agent Runtime

这是一个面向客户反馈洞察场景的可审计 Agent Harness。项目保留框架无关的受控 Loop、Session、Context、Tool Registry 和 Trace，并开始加入兼容 Agent Skills 目录规范的渐进式 Skill Runtime。

完整演进方案见本地 `docs/agent_runtime_technical_roadmap.md`（设计文档保留在本地，未随仓库分发）。

## Skill Runtime（Milestone 1）

当前实现包含：

- 启动时只发现 `SKILL.md` 的名称和描述，激活时才加载完整正文。
- 支持 `$skill-name` 显式选择和基于业务触发词的隐式路由。
- 每次最多激活两个 Skill，并记录内容哈希版本、匹配原因和分数。
- Skill Policy 会缩小本轮模型可见工具范围，工具执行时使用同一白名单。
- `skill_activated` 和 `skill_error` 事件写入 Harness Trace。
- Skill 内容损坏、路由指向未知 Skill、显式调用未知 Skill 时安全失败。

已提供四个客户反馈 Skill：

| Skill | 用途 |
| --- | --- |
| `feedback-triage` | 反馈概览、分流和初步证据收集 |
| `anomaly-investigation` | 周期对比、异常下钻和低基数排查 |
| `root-cause-analysis` | 区分事实、原因假设、反向证据和验证动作 |
| `executive-report` | 生成证据化的管理层报告和行动计划 |

Skill 路由和工具策略位于 `skills/routes.json`，Skill 正文位于各自的 `SKILL.md`。运行 Web 服务时，客户反馈 Agent 会自动加载项目根目录下的 `skills/`。

## MCP Runtime（Milestone 2）

项目现在既是 MCP Host，也包含两个独立 MCP Server：

| 组件 | Transport | 能力 |
| --- | --- | --- |
| Feedback Data MCP | stdio / Streamable HTTP | 6 Tools、3 Resources、2 Prompts |
| Work Item MCP | Streamable HTTP / stdio | 查询工单、准备工单、审批后幂等提交 |

`MCPClientManager` 在后台事件循环中维护持久 `ClientSession`，对同步 Harness 提供超时受控的同步接口。连接时完成初始化和 Capability Discovery，并把远程工具以 `server.tool` 命名空间动态注册到现有 `ToolRegistry`，例如：

- `feedback.aggregate`
- `feedback.detect_anomalies`
- `work-items.prepare_issue`
- `work-items.commit_issue`

工单创建采用三阶段协议：

```text
Agent: prepare_issue
  -> Human: approve/reject outside MCP tool channel
  -> Agent: commit_issue
```

`approve` 不会出现在 MCP 工具列表中。未经人工批准的 `commit_issue` 会失败；批准后的重复提交会幂等返回同一工单。

详细设计见本地 `docs/mcp_runtime_design.md`。

## Policy Gateway 与可恢复审批（Milestone 3）

动态 MCP 工具会根据服务端风险元数据和 ToolAnnotations 映射为 `read`、`low_write`、`high_write` 或 `forbidden`。高风险写操作不会直接执行：Harness 会把 Session、Trace、工具参数、Skill 版本和本轮工具白名单持久化为 Checkpoint，并返回 `approval_pending`。

Web 左侧的“审批中心”可查看完整参数并批准或拒绝。批准后即使 Harness 已重新构造，也会从暂停 step 继续；拒绝时工具不会执行。创建正式工单还会经过 Work Item 服务自身的业务审批，形成双重防线。

对于“创建工单”“检索后读取样本”等明确动作，Skill 还会声明有序完成契约。Harness 还会从“最多 N 条”等用户原话编译动态参数契约，并为周报、月报和管理层报告编译输出结构契约。模型漏调、乱序、参数越界或报告缺章节时都会被拦截并获得可执行的纠正反馈。

实现与状态机说明见本地 `docs/policy_checkpoint_design.md`。

## 自动评测与运行观测（Milestone 4）

项目包含 50 条固定中文路由/完成契约用例，并支持 Baseline、Skills、Full Runtime 三组确定性消融对照：

```powershell
python -m minimal_agent.eval --variant compare
```

报告写入 `.sessions/evaluations/`。当前固定集实测为 Baseline 20%、Skills 80%、Full 100%；这些数字只用于路由和完成契约回归，不代表 LLM 回答质量或 Groundedness。

项目还包含 15 条真实端到端任务，覆盖查询、异常分析、根因分析、报告和审批安全。先用 3 条任务做一次低成本冒烟：

```powershell
$env:OPENROUTER_API_KEY="你的 Key"
python -m minimal_agent.live_eval --limit 3 --proxy "http://127.0.0.1:7897" --confirm-live
```

正式稳定性评测对每条任务重复 3 次（15 × 3，共 45 次 Agent Run，可能产生多次模型调用和费用）：

```powershell
python -m minimal_agent.live_eval --repeats 3 --proxy "http://127.0.0.1:7897" --confirm-live
```

修复失败用例后可以只回归指定 Case，避免重复支付已通过任务的费用：

```powershell
python -m minimal_agent.live_eval --cases "7-10,12-15" --proxy "http://127.0.0.1:7897" --confirm-live
```

没有 `--confirm-live` 时命令只显示计划，不发起 API 调用。Live Runner 会为每次评测创建隔离的数据快照和临时 MCP 服务；日期相对评测日平移，工单用例只运行到审批 Checkpoint，不批准、也不创建正式工单。完成契约支持本地工具和 MCP 工具别名组，明确要求样本、根因、周报或工单时会阻止模型提前结束并校验工具顺序。模型客户端对 TLS、连接、429 和 5xx 故障执行有界重试；连续 3 次基础设施错误时评测自动熔断。基础设施失败会单独统计，不会伪装成 Agent 质量 0 分。Live Benchmark v3 检查非空证据、因果措辞安全和报告结构完整性；动态参数契约在工具执行前阻止缺失或越界的 `limit`。报告固定写入项目内 `.sessions/evaluations/`，包含任务成功率、Skill 路由、工具选择、工具顺序、参数准确率、证据命中率、数字 Groundedness、因果措辞安全、报告结构、审批违规率、重复稳定性、P95 延迟和 Token 汇总；`repeats=1` 时重复稳定性显示为 `N/A`，避免把单次成功率误称为稳定性。

Web 的“运行观测”和“自动评测”可以查看 Skill 激活、模型原始决策、模型/工具耗时、Token、审批暂停、Checkpoint 恢复、只读 Trace 回放，以及 Routing/Live 两类评测报告。详细设计见本地 `docs/evaluation_observability.md`。

### 安装项目依赖

```powershell
cd D:\shixi\aicoding\feedback-agent-runtime
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

### 使用 Feedback MCP 启动 Web

Web 进程会自动以 stdio 启动 Feedback MCP Server：

```powershell
python -m minimal_agent.web --tool-source mcp
```

OpenRouter 加 MCP：

```powershell
python -m minimal_agent.web --tool-source mcp --llm openai --base-url "https://openrouter.ai/api/v1" --model "openai/gpt-4o-mini" --llm-proxy "http://127.0.0.1:7897"
```

### 接入远程 Work Item MCP

窗口一：

```powershell
python -m minimal_agent.mcp_servers.work_item_server --data-dir ".sessions/work-items" serve --transport streamable-http --host 127.0.0.1 --port 8020
```

窗口二：

```powershell
python -m minimal_agent.web --tool-source mcp --work-item-mcp-url "http://127.0.0.1:8020/mcp"
```

查看待审批项：

```powershell
python -m minimal_agent.mcp_servers.work_item_server --data-dir ".sessions/work-items" pending
```

批准或拒绝：

```powershell
python -m minimal_agent.mcp_servers.work_item_server --data-dir ".sessions/work-items" approve "APR-XXXXXXXXXX" --by "product-owner"
python -m minimal_agent.mcp_servers.work_item_server --data-dir ".sessions/work-items" reject "APR-XXXXXXXXXX" --by "product-owner"
```

## 原项目说明

一个不依赖 Agent 框架的轻量客户反馈洞察 Agent。它将确定性的 SQLite 统计与 LLM 多步工具调用结合起来，帮助产品和运营团队从客服 CSV 中发现高频问题、趋势变化和典型案例。

## 功能概览

Feedback Lens 支持导入 UTF-8 CSV 并按工单 ID 自动去重，通过关键词规则为单条反馈分配多个标签；用户可以按时间、产品模块、客户等级、处理状态、标签和关键词筛选数据，查看反馈趋势、模块分布、客户等级分布及高频问题，还能自动比较相邻周期以识别异常增长的问题标签。系统既支持通过 Agent 查询统计结果、比较周期和读取脱敏样本，也允许人工修正标签、维护标签规则并重新匹配历史反馈，同时可设置反馈状态、优先级、负责人和内部备注并执行批量处理。Agent 的回答会附带可点击的数据证据，并安全渲染 Markdown 标题、加粗、列表、引用和代码块；分析结果可以保存为洞察报告，支持查看、删除、复制、导出 Markdown，以及 A4 打印或生成 PDF。此外，系统还支持导出当前筛选结果，并持久化分析 Session、对话历史和结构化 Trace，便于后续复盘与审计。

## 功能说明

Feedback Lens 的典型使用流程如下：

```text
导入客服 CSV
  → 校验字段并按工单 ID 去重
  → 使用关键词规则自动分配标签
  → 在数据概览中发现高频问题和异常增长
  → 通过洞察 Agent 查询统计与脱敏样本
  → 跳转到结论对应的原始反馈
  → 设置优先级、负责人和处理状态
  → 保存并导出洞察报告
```

### 数据概览

数据概览用于快速了解当前反馈数据的整体情况，包括：

- 反馈总量、待处理数量和待处理占比
- 当前最高频的问题标签
- 按天统计的反馈趋势
- 产品模块分布和客户等级分布
- 高频问题标签排行
- 最近周期与上一等长周期的异常增长对比
- 7 天、30 天、90 天及产品模块筛选

周期分析以数据库中最新一条反馈的日期为基准，因此导入历史数据后仍能得到有效的周期对比结果。

### 洞察 Agent

FeedbackInsightAgent 通过受控工具读取真实统计结果，不直接执行任意 SQL。它可以：

- 总结当前主要问题和模块分布
- 比较最近周期与上一周期的变化
- 识别增长较快或新出现的问题标签
- 搜索满足条件的反馈
- 读取最多 20 条经过基础脱敏的代表性反馈
- 区分数据事实、原因推测和下一步建议

可以尝试以下问题：

```text
最近 7 天最值得关注的问题是什么？
分析当前异常增长的问题，并区分数据事实和原因推测。
支付问题主要集中在哪些方面？
比较最近 7 天和上一周期的反馈变化。
查看一些典型的原始反馈样本。
```

模型返回的普通文本和结构化 JSON 都会转换成适合阅读的页面布局。数字显示为指标块，异常项显示为卡片，原因和建议显示为编号列表。

### 数据证据

Agent 每次调用统计或反馈查询工具后，会在回答下方生成“数据依据”入口，例如问题标签、统计周期或工单 ID。

点击证据后，页面会跳转到“反馈明细”，并自动应用对应的标签、时间范围或工单筛选。证据随分析 Session 持久化，刷新页面后仍然有效。

### 反馈明细

反馈明细页面支持：

- 按工单 ID 或反馈内容搜索
- 按产品模块、客户等级、处理状态、优先级和标签筛选
- 分页查看原始反馈
- 导出当前筛选结果为 CSV
- 人工修正单条反馈的标签
- 勾选多条反馈进行批量处理

每条反馈包含工单 ID、创建时间、产品模块、反馈内容、客户等级、状态、优先级、负责人和标签。

### 反馈处理工作流

点击反馈右侧的“处理”按钮，可以维护：

- 处理状态：待处理、处理中、已完成
- 优先级：紧急、高、中、低
- 负责人或负责团队
- 内部处理备注

每次修改都会记录操作者、修改时间和字段变化。批量处理支持一次更新最多 200 条反馈的状态、优先级和负责人。

### 标签管理

系统通过关键词规则为反馈自动分配一个或多个标签。标签管理页面支持：

- 新建标签和关键词规则
- 修改标签名称与关键词
- 启用或停用自动匹配
- 删除标签
- 查看规则修改记录
- 使用当前规则重新匹配全部历史反馈

重新匹配只会替换规则生成的标签，运营人员手工添加的标签会被保留。

### 数据导入

导入页面支持点击选择或拖放 UTF-8 CSV 文件。导入过程会：

1. 检查必要字段和时间格式。
2. 按 `ticket_id` 去重。
3. 标准化缺失的模块、客户等级和状态。
4. 使用启用的关键词规则自动打标签。
5. 记录总行数、成功数、重复数、失败数和失败原因。

重复导入同一个文件不会产生重复反馈。页面提供 CSV 模板下载和最近导入记录。

### 洞察报告

Agent 回答可以保存为洞察报告。报告支持：

- 在系统中持久化查看
- 保留原有结构化展示
- 复制为格式化文本
- 导出为 Markdown 文件
- 打开 A4 打印版式并通过浏览器保存为 PDF
- 删除不再需要的报告

打印版包含报告标题、分析时间、分析 Session、正文分区和页脚。

### 分析 Session 与 Trace

- 不同分析 Session 的对话上下文相互隔离。
- 左侧可以新建、切换和删除分析 Session。
- 删除 Session 会清理对应对话、数据证据和 Trace，但不会删除共享的反馈数据库。
- 每次模型调用、工具调用、工具结果、错误和最终答案都会写入结构化 JSONL Trace。

### 当前边界

- 已接入强制登录与最简三角色（admin/approver/viewer）；审批人身份 `decided_by` 与操作人 `operator` 均取服务端登录身份。企业级 SSO（OIDC/LDAP）与租户数据隔离尚未接入。
- 标签自动分类主要使用关键词规则，不是逐条调用大模型。
- 基础脱敏覆盖手机号、邮箱和常见订单号，不应替代企业级数据脱敏系统。
- PDF 通过浏览器打印功能生成，浏览器阻止弹窗时需要允许本地页面打开弹窗。

## 快速启动

离线 Mock 模式不需要 API Key。首次启动前先创建管理员账号，再启动 Web：

```powershell
cd D:\shixi\aicoding\feedback-agent-runtime
python -m minimal_agent.users add admin --role admin
# 交互输入两次密码(至少 8 位)
python -m minimal_agent.web
```

首次访问 <http://127.0.0.1:8000> 需登录；角色说明：admin=用户管理+全部业务，approver=业务写入+审批，viewer=只读+对话。登录后进入“导入记录”，选择：

```text
sample_data/customer_feedback_demo.csv
```

导入后即可查看看板并向洞察 Agent 提问。

## OpenRouter

在同一个 PowerShell 窗口中执行：

```powershell
$env:OPENROUTER_API_KEY="你的 Key"
python -m minimal_agent.web --llm openai --base-url "https://openrouter.ai/api/v1" --model "openai/gpt-4o-mini" --llm-proxy "http://127.0.0.1:7897"
```

不要将 API Key 写入代码或提交到 Git。

## 对抗评测与失败案例报告

项目提供一套不调用外部模型、不会产生 API 费用的确定性对抗评测。它使用恶意脚本模型直接驱动真实 Parser、Harness、Tool Registry、Policy、Checkpoint、Session 和沙箱组件：

```powershell
python -m minimal_agent.adversarial_eval
```

评测覆盖模型协议破坏、工具越权、Schema 参数注入、无限循环、动态参数契约、报告结构契约、提前结束、高风险审批绕过、审批重放、Checkpoint 篡改、无证据数字、PII 泄露、资源预算、伪造审批身份和路径穿越。

首轮基线成功防御 15/20（75%）；接入资源预算、数字门禁与 PII 脱敏后达到 19/20；阶段 1 身份层落地（登录 + 最简角色 + 服务端审批身份）后，当前固定基线成功防御 20/20（100%），探针异常为 0。完整结论、复现证据和修复建议见本地 `docs/adversarial_evaluation_report.md`（运行 `python -m minimal_agent.adversarial_eval` 可随时再生成）。

## CSV 格式

必要字段：

```csv
ticket_id,created_at,content
TK-001,2026-08-04 10:30:00,微信支付失败
```

完整字段：

```csv
ticket_id,created_at,product_module,content,customer_tier,status
TK-001,2026-08-04 10:30:00,支付,微信支付失败,高级,待处理
```

同时支持对应的中文表头，例如“工单 ID”“创建时间”“产品模块”“反馈内容”“客户等级”和“处理状态”。

## 数据与隐私

- 反馈数据库：`.sessions/feedback/feedback.db`
- 分析 Session：`.sessions/feedback_sessions/<session-hash>.json`
- 运行 Trace：`.sessions/feedback_sessions/traces/<session-hash>.jsonl`
- Agent 最多读取 20 条样本
- 发送给模型的搜索结果和样本会进行基础手机号、邮箱和订单号脱敏
- 看板中的原始反馈只在本机页面展示

真实客户数据如果不能离开内网，请使用本地模型或继续使用不调用外部模型的统计看板。

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

LLM 不执行任意 SQL。它只能调用受控工具查询概览、周期对比、反馈搜索和脱敏样本，回答中的数据可以追溯到工具结果。

## 测试

```powershell
python -m unittest discover -s tests -v
```

原有 PlanningAgent 代码仍保留在 `minimal_agent/agents/planning.py`，用于展示同一个 Harness 如何承载不同业务 Agent；Web 工作台现在默认运行 FeedbackInsightAgent。
