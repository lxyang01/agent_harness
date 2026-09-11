const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const WORKFLOW_STATUSES = ["正常", "待核查", "核查中", "已确认", "已忽略"];
const state = {
  session: localStorage.getItem("billguard-session") || "billguard",
  user: null, userCaps: new Set(),
  sessions: [], messages: [], overview: {}, anomalies: {items:[]}, bills: { items: [], total: 0, page: 1, page_size: 30 },
  categories: [], audits: [], imports: [], subscriptions: [], reports: [], approvals: [], mcpServers: [], runs: [], runDetail: null, evaluations: [], filters: {}, selected: new Set(), busy: false,
};
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
const formatNumber = (value) => new Intl.NumberFormat("zh-CN").format(Number(value || 0));
const formatAmount = (value) => `¥${new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2}).format(Number(value || 0))}`;
const formatDate = (value) => { if (!value) return "—"; const date = new Date(value); return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}); };

async function api(path, body = {}) {
  const response = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({session_id:state.session, ...body})});
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) { showLogin(); throw new Error(data.error || "请先登录"); }
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}
const ROLE_CAPS = {viewer:[], approver:["report_write","bills_write","approval_decide"], admin:["report_write","bills_write","approval_decide","users_manage"]};
const ROLE_LABELS = {admin:"管理员", approver:"审批人", viewer:"观察者"};
const hasCap = (capability) => state.userCaps.has(capability);
function showLogin() { $("#login-overlay").hidden = false; $("#login-username").focus(); }
function hideLogin() { $("#login-overlay").hidden = true; }
function applyUser(user) {
  state.user = user; state.userCaps = new Set(ROLE_CAPS[user.role] || []);
  $("#user-badge").hidden = false; $("#logout").hidden = false;
  $("#user-name").textContent = user.username;
  $("#user-role").textContent = ROLE_LABELS[user.role] || user.role;
  $("#admin-nav").hidden = !hasCap("users_manage");
  document.body.classList.toggle("viewer-mode", user.role === "viewer");
}
async function initAuth() {
  try { const response = await fetch("/api/auth/me"); if (response.ok) { applyUser(await response.json()); await loadSession(state.session, true); return; } } catch (error) {}
  showLogin();
}
async function loadUsers() {
  if (!hasCap("users_manage")) return;
  try { const response = await fetch("/api/admin/users"); const data = await response.json().catch(() => ({}));
    if (response.status === 401) { showLogin(); return; }
    if (!response.ok) throw new Error(data.error || "加载用户失败");
    $("#admin-rows").innerHTML = (data.users || []).map(user => `<tr><td>${escapeHtml(user.username)}</td><td>${ROLE_LABELS[user.role] || escapeHtml(user.role)}</td><td>${user.disabled ? "已禁用" : "启用"}</td><td>${escapeHtml(formatDate(user.created_at))}</td><td><div class="row-actions"><button data-user-action="role" data-username="${escapeHtml(user.username)}" data-role="${escapeHtml(user.role)}">改角色</button><button data-user-action="password" data-username="${escapeHtml(user.username)}">重置密码</button><button data-user-action="toggle" data-username="${escapeHtml(user.username)}" data-disabled="${user.disabled ? "true" : "false"}">${user.disabled ? "启用" : "禁用"}</button></div></td></tr>`).join("") || '<tr><td colspan="5" class="table-empty">暂无用户</td></tr>';
  } catch (error) { toast(error.message); }
}
function toast(message) { const element = $("#toast"); element.textContent = message; element.classList.add("show"); setTimeout(() => element.classList.remove("show"), 2400); }
function showPanel(name) {
  $$(".panel,.nav-item").forEach((element) => element.classList.remove("active"));
  $(`#${name}-panel`).classList.add("active");
  $(`.nav-item[data-panel="${name}"]`)?.classList.add("active");
  $("#page-title").textContent = {overview:"支出概览", insight:"守卫 Agent", bills:"交易明细", categories:"类别管理", reports:"守卫报告", imports:"账单导入", approvals:"审批中心", runs:"运行观测", evaluations:"自动评测", admin:"用户管理"}[name];
  $(".sidebar").classList.remove("open");
  if (name === "admin") loadUsers();
}
function renderSessions() {
  $("#active-session-name").textContent = state.session;
  $("#session-list").innerHTML = state.sessions.length ? state.sessions.map((session) => `
    <div class="session-item ${session.id === state.session ? "active" : ""}">
      <button class="session-select" data-session="${escapeHtml(session.id)}"><span class="session-dot"></span><span class="session-copy"><strong>${escapeHtml(session.id)}</strong></span></button>
      <button class="session-delete" data-delete-session="${escapeHtml(session.id)}" title="删除 Session">×</button>
    </div>`).join("") : '<div class="session-empty">暂无分析会话</div>';
}
function renderApprovals() {
  const items = state.approvals || [];
  const pending = items.filter(item => item.status === "pending");
  $("#approval-count").textContent = pending.length;
  $("#approval-pending-count").textContent = pending.length;
  $("#mcp-server-count").textContent = state.mcpServers.length;
  $("#mcp-server-list").innerHTML = state.mcpServers.length ? state.mcpServers.map(server => `<span><i></i>${escapeHtml(server.name)} · ${escapeHtml(server.transport)} · ${server.tools?.length || 0} tools</span>`).join("") : '<span class="muted">当前使用本地工具，未连接 MCP 服务</span>';
  $("#approval-list").innerHTML = items.length ? items.map(item => {
    const statusLabel = {pending:"等待审批",approved:"已批准，待执行",rejected:"已拒绝",executed:"执行成功",failed:"执行失败"}[item.status] || item.status;
    const riskLabel = {high_write:"高风险写操作",low_write:"低风险写操作",read:"只读",forbidden:"禁止"}[item.risk_level] || item.risk_level;
    return `<article class="approval-card ${item.status}">
      <div class="approval-card-head"><div><span class="risk-pill ${item.risk_level}">${escapeHtml(riskLabel)}</span><h3>${escapeHtml(item.tool_name)}</h3></div><span class="approval-status">${escapeHtml(statusLabel)}</span></div>
      <p>${escapeHtml(item.reason || "此操作需要人工确认")}</p>
      <dl><div><dt>审批 ID</dt><dd>${escapeHtml(item.id)}</dd></div><div><dt>请求时间</dt><dd>${escapeHtml(formatDate(item.requested_at))}</dd></div></dl>
      <details><summary>查看工具参数</summary><pre>${escapeHtml(JSON.stringify(item.arguments || {}, null, 2))}</pre></details>
      ${item.execution_error ? `<div class="approval-error">${escapeHtml(item.execution_error)}</div>` : ""}
      ${item.status === "pending" ? `<div class="approval-actions"><button class="reject" data-approval-decision="reject" data-approval-id="${escapeHtml(item.id)}">拒绝</button><button class="primary" data-approval-decision="approve" data-approval-id="${escapeHtml(item.id)}">批准并继续</button></div>` : `<div class="approval-audit">${item.decided_by ? `由 ${escapeHtml(item.decided_by)} 处理` : ""}${item.decided_at ? ` · ${escapeHtml(formatDate(item.decided_at))}` : ""}</div>`}
    </article>`;
  }).join("") : '<div class="approval-empty"><strong>暂无审批记录</strong><span>Agent 请求执行高风险 MCP 工具时，会在这里暂停并等待你的决定。</span></div>';
}
function renderRuns() {
  const runs = state.runs || [];
  const completed = runs.filter(run => run.status === "completed").length;
  const average = runs.length ? Math.round(runs.reduce((sum, run) => sum + Number(run.active_time_ms || 0), 0) / runs.length) : 0;
  const tokens = runs.reduce((sum, run) => sum + Number(run.token_usage?.total_tokens || 0), 0);
  $("#run-count").textContent = runs.length;
  $("#run-kpi-total").textContent = runs.length;
  $("#run-kpi-latency").textContent = `${formatNumber(average)} ms`;
  $("#run-kpi-tokens").textContent = formatNumber(tokens);
  $("#run-kpi-success").textContent = `${runs.length ? Math.round(completed * 100 / runs.length) : 0}%`;
  $("#run-list").innerHTML = runs.length ? runs.map(run => `<button class="run-list-item ${state.runDetail?.summary?.trace_id===run.trace_id?'active':''}" data-trace-id="${escapeHtml(run.trace_id)}"><span class="run-state ${escapeHtml(run.status)}"></span><div><strong>${escapeHtml(run.skills.join(" + ") || "未激活 Skill")}</strong><small>${escapeHtml(formatDate(run.started_at))} · ${run.steps} steps · ${formatNumber(Math.round(run.active_time_ms || 0))} ms</small></div><code>${escapeHtml(run.trace_id.slice(0,8))}</code></button>`).join("") : '<div class="run-empty"><strong>暂无运行记录</strong><span>向 Agent 提问后，这里会出现可回放 Trace。</span></div>';
}
function eventPresentation(event) {
  const map = {
    run_start:["开始运行","run"], skill_activated:[`激活 Skill · ${event.skill||""}`,"skill"],
    model_start:[`模型调用 · Step ${event.step}`,"model"], model_output:[`模型响应 · ${formatNumber(Math.round(event.latency_ms||0))} ms`,"model"],
    model_decision:[event.tool?`决定调用 · ${event.tool}`:"生成最终回答","decision"],
    tool_start:[`开始工具 · ${event.tool||""}`,"tool"], tool_end:[`工具完成 · ${event.tool||""} · ${formatNumber(Math.round(event.latency_ms||0))} ms`,"tool"],
    tool_error:[`工具失败 · ${event.tool||""}`,"error"], completion_blocked:["拦截过早结束 · 完成契约未满足","warning"],
    argument_blocked:[`拦截工具参数 · ${event.tool||""}`,"warning"], output_contract_blocked:["拦截不完整报告 · 输出契约未满足","warning"],
    approval_pending:[`等待人工审批 · ${event.tool||""}`,"approval"], run_resume:["审批通过 · 恢复 Checkpoint","approval"],
    approval_rejected:["审批已拒绝","error"], run_end:[`运行结束 · ${event.status||"completed"}`,"run"],
    run_error:["运行失败","error"], max_steps:["达到最大步骤限制","error"]
  };
  return map[event.event] || [event.event,"default"];
}
function renderRunDetail() {
  const detail = state.runDetail; const box = $("#run-detail");
  if (!detail) { box.innerHTML='<div class="run-empty"><strong>选择一次运行</strong><span>这里会按时间顺序回放完整 Harness Trace。</span></div>'; return; }
  const run=detail.summary; const usage=run.token_usage||{};
  box.innerHTML=`<div class="run-detail-head"><div><span>TRACE REPLAY</span><h3>${escapeHtml(run.trace_id)}</h3></div><span class="run-status ${escapeHtml(run.status)}">${escapeHtml(run.status)}</span></div><div class="run-facts"><div><span>活跃耗时</span><strong>${formatNumber(Math.round(run.active_time_ms||0))} ms</strong></div><div><span>模型调用</span><strong>${run.model_calls}</strong></div><div><span>工具调用</span><strong>${run.tool_calls}</strong></div><div><span>Token</span><strong>${formatNumber(usage.total_tokens||0)}</strong></div></div><div class="run-tools">${(run.tools||[]).map(tool=>`<span>${escapeHtml(tool)}</span>`).join("")}</div><div class="timeline">${detail.events.map(event=>{const [label,type]=eventPresentation(event);const payload={...event};delete payload.timestamp;delete payload.trace_id;delete payload.session_id;delete payload.agent;return `<article class="timeline-event ${type}"><i></i><div><div class="timeline-head"><strong>${escapeHtml(label)}</strong><time>${escapeHtml(formatDate(event.timestamp))}</time></div>${Object.keys(payload).length?`<details><summary>查看事件数据</summary><pre>${escapeHtml(JSON.stringify(payload,null,2))}</pre></details>`:""}</div></article>`;}).join("")}</div>`;
}
async function loadRunDetail(traceId){try{state.runDetail=await api("/api/runs/detail",{trace_id:traceId});renderRuns();renderRunDetail();}catch(error){toast(error.message);}}
function renderEvaluations(){
  const reports=state.evaluations||[];$("#evaluation-count").textContent=reports.length;
  $("#evaluation-list").innerHTML=reports.length?reports.map((report,index)=>{
    const isLive=report.evaluation_type==="live_llm";const metrics=report.metrics||{};
    const scoreText=value=>value===null||value===undefined?"—":`${Math.round(Number(value)*100)}%`;
    const scoreBar=(value,inverse=false)=>value===null||value===undefined?0:Math.round(Number(inverse?1-Number(value):value)*100);
    const cards=isLive?[
      ["任务成功",metrics.task_success_rate,`${metrics.scoreable_runs||0}/${metrics.runs||0} 可评分`],
      ["基础设施失败",metrics.infrastructure_failure_rate,"越低越好",true],
      ["工具选择",metrics.tool_selection_accuracy,"Trace"],
      ["参数准确",metrics.argument_accuracy,"Schema"],["证据命中",metrics.evidence_retrieval_accuracy,"非空结果"],
      ["数字有据",metrics.numeric_groundedness,"Grounded"],["因果措辞",metrics.causal_claim_safety_accuracy,"安全边界"],
      ["报告结构",metrics.report_structure_accuracy,"章节契约"],
      ["重复稳定",metrics.repeat_stability,`${report.repeats||1} 次`],["审批违规",metrics.approval_violation_rate,"越低越好",true],
    ].map(([label,value,note,inverse])=>`<div class="eval-variant ${inverse&&Number(value)>0?'unsafe':'live'}"><span>${label}</span><strong>${scoreText(value)}</strong><small>${escapeHtml(note)}</small><i style="--score:${scoreBar(value,inverse)}%"></i></div>`).join(""):(report.variants||[]).map(item=>{const m=item.metrics||{};return `<div class="eval-variant ${item.variant}"><span>${escapeHtml(item.variant)}</span><strong>${Math.round(Number(m.overall_accuracy||0)*100)}%</strong><small>路由 ${Math.round(Number(m.skill_routing_accuracy||0)*100)}% · 契约 ${m.completion_contract_accuracy==null?"N/A":Math.round(Number(m.completion_contract_accuracy)*100)+"%"}</small><i style="--score:${Math.round(Number(m.overall_accuracy||0)*100)}%"></i></div>`;}).join("");
    return `<article class="eval-report"><div class="eval-head"><div><span>${isLive?'LIVE LLM E2E':'ROUTING BENCHMARK'}</span><h3>${escapeHtml(report.filename)}</h3><small>${escapeHtml(formatDate(report.evaluated_at))} · ${report.dataset_size} cases${isLive?` · ${escapeHtml(report.model||'unknown model')} · T=${escapeHtml(report.temperature??'—')}`:''}</small></div>${index===0?'<b>最新</b>':''}</div><div class="eval-variants ${isLive?'live-metrics':''}">${cards}</div><p>${escapeHtml(report.scope_note||"")}</p></article>`;
  }).join(""):'<div class="eval-empty"><strong>还没有评测报告</strong><span>在项目目录执行评测命令，报告会保存到 .sessions/evaluations。</span></div>';
}
const FIELD_LABELS={
  tx_id:"交易编号",paid_at:"支付时间",merchant:"商户",category:"类别",amount:"金额",
  method:"支付方式",status:"状态",note:"备注",evidence:"证据",name:"名称",count:"笔数",
  current_count:"本期",previous_count:"上期",change_percent:"变化率",thresholds:"阈值",
  current_period:"本周期",previous_period:"上周期",by_category:"类别分布",top_merchants:"高频商户",
  max_tx:"最大单笔",trend:"趋势",options:"筛选项",samples:"样本",items:"明细",detail:"详情",
  matched:"命中",subscriptions:"订阅",expected_amount:"预期金额",cycle:"周期",
  last_paid_at:"最近扣款",active:"生效中",from:"起",to:"止",total:"合计",total_amount:"总金额",
  pending:"待核查",avg_daily:"日均",days:"天数",dimension:"维度",retry_hint:"重试提示",
};
const WIDE_FIELDS=new Set(["note","detail","retry_hint","evidence"]);
function friendlyKey(key){return FIELD_LABELS[key]||String(key).replaceAll("_"," ");}
function factClass(key,value){
  const wide=WIDE_FIELDS.has(key)||(typeof value==="string"&&value.length>48);
  const primary=["merchant","name","detail"].includes(key);
  return `insight-fact${wide?" is-wide":""}${primary?" is-primary":""}`;
}
function scalarText(value,key=""){
  if(value===null||value===undefined)return "—";
  if(typeof value==="boolean")return value?"是":"否";
  if(typeof value==="number")return /百分比|变化率|增长率|占比/.test(key)?`${value}%`:(/金额|合计|预期/.test(key)?formatAmount(value):formatNumber(value));
  if(["paid_at","created_at","updated_at","last_paid_at","changed_at"].includes(key))return formatDate(value);
  return String(value);
}
function structuredHtml(value,level=0){
  if(Array.isArray(value)){
    if(!value.length)return '<div class="insight-empty">暂无数据</div>';
    if(value.every(item=>item===null||typeof item!=="object"))return `<ol class="insight-list">${value.map(item=>`<li>${escapeHtml(scalarText(item))}</li>`).join("")}</ol>`;
    return `<div class="insight-object-list">${value.map((item,index)=>`<article class="insight-object-card"><span class="object-index">样本 ${String(index+1).padStart(2,"0")}</span>${structuredHtml(item,level+1)}</article>`).join("")}</div>`;
  }
  if(value&&typeof value==="object"){
    const hasTxId=Object.prototype.hasOwnProperty.call(value,"tx_id");
    const entries=Object.entries(value).filter(([key])=>!(hasTxId&&key==="id"));
    const simple=entries.filter(([,item])=>item===null||typeof item!=="object");const complex=entries.filter(([,item])=>item!==null&&typeof item==="object");
    const facts=simple.length?`<div class="insight-facts">${simple.map(([key,item])=>`<div class="${factClass(key,item)}"><span>${escapeHtml(friendlyKey(key))}</span><strong>${escapeHtml(scalarText(item,key))}</strong></div>`).join("")}</div>`:"";
    const sections=complex.map(([key,item])=>`<section class="${level===0?'insight-section':'insight-subsection'}"><${level===0?'h3':'h4'}>${escapeHtml(friendlyKey(key))}</${level===0?'h3':'h4'}>${structuredHtml(item,level+1)}</section>`).join("");
    return facts+sections;
  }
  return `<span>${escapeHtml(scalarText(value))}</span>`;
}
function inlineMarkdown(value){
  const codeTokens=[];
  let text=String(value??"").replace(/`([^`\n]+)`/g,(_,code)=>{
    const token=`${codeTokens.length}`;
    codeTokens.push(`<code>${escapeHtml(code)}</code>`);
    return token;
  });
  text=escapeHtml(text)
    .replace(/\*\*([^*\n]+)\*\*/g,"<strong>$1</strong>")
    .replace(/__([^_\n]+)__/g,"<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g,"$1<em>$2</em>");
  codeTokens.forEach((html,index)=>{text=text.replace(`${index}`,html);});
  return text;
}
function markdownHtml(value){
  const lines=String(value??"").replace(/\r\n?/g,"\n").split("\n");
  const output=[];let paragraph=[];let listType="";let listItems=[];let quote=[];
  const flushParagraph=()=>{if(paragraph.length){output.push(`<p>${paragraph.map(inlineMarkdown).join("<br>")}</p>`);paragraph=[];}};
  const flushList=()=>{if(listItems.length){output.push(`<${listType}>${listItems.map(item=>`<li>${inlineMarkdown(item)}</li>`).join("")}</${listType}>`);listItems=[];listType="";}};
  const flushQuote=()=>{if(quote.length){output.push(`<blockquote>${quote.map(inlineMarkdown).join("<br>")}</blockquote>`);quote=[];}};
  for(let index=0;index<lines.length;index+=1){
    const line=lines[index];
    if(/^\s*```/.test(line)){
      flushParagraph();flushList();flushQuote();
      const code=[];index+=1;
      while(index<lines.length&&!/^\s*```/.test(lines[index])){code.push(lines[index]);index+=1;}
      output.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }
    const heading=line.match(/^\s*(#{1,4})\s+(.+)$/);
    const unordered=line.match(/^\s*[-+*]\s+(.+)$/);
    const ordered=line.match(/^\s*\d+[.)]\s+(.+)$/);
    const quoted=line.match(/^\s*>\s?(.*)$/);
    if(!line.trim()){flushParagraph();flushList();flushQuote();continue;}
    if(heading){flushParagraph();flushList();flushQuote();const level=Math.min(heading[1].length+1,5);output.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);continue;}
    if(unordered||ordered){flushParagraph();flushQuote();const nextType=unordered?"ul":"ol";if(listType&&listType!==nextType)flushList();listType=nextType;listItems.push((unordered||ordered)[1]);continue;}
    if(quoted){flushParagraph();flushList();quote.push(quoted[1]);continue;}
    flushList();flushQuote();paragraph.push(line);
  }
  flushParagraph();flushList();flushQuote();
  return output.join("");
}
function answerView(content){
  const text=String(content??"").trim();
  if((text.startsWith("{")&&text.endsWith("}"))||(text.startsWith("[")&&text.endsWith("]"))){
    try{return{html:structuredHtml(JSON.parse(text)),structured:true};}catch(error){/* regular text fallback */}
  }
  return{html:markdownHtml(content),structured:false,markdown:true};
}
function renderMessages() {
  const box = $("#messages");
  box.innerHTML = state.messages.map((message,index) => {const view=message.role==="assistant"?answerView(message.content):{html:escapeHtml(message.content),structured:false};const evidence=message.evidence||[];return `<div class="message ${message.role} ${view.structured?'has-structured':''}"><div><div class="bubble ${view.structured?'structured-answer':''}">${view.html}</div>${message.role === "assistant" ? `<div class="meta"><span>BillGuard 账单守卫</span><button class="save-report" data-save-index="${index}">保存为报告</button></div>${evidence.length?`<div class="evidence-strip"><span>数据依据</span>${evidence.map(item=>`<button data-evidence="${encodeURIComponent(JSON.stringify(item.filters||{}))}" title="${escapeHtml(item.description||'查看相关交易')}">${escapeHtml(item.label)}</button>`).join("")}</div>`:""}` : ""}</div></div>`;}).join("");
  box.classList.toggle("has-messages", state.messages.length > 0);
  $("#empty-state").style.display = state.messages.length ? "none" : "";
}
function renderTrend(trend) {
  const box = $("#trend-chart");
  if (!trend?.length) { box.innerHTML = '<div class="empty-chart">导入账单后，这里会显示每日支出金额</div>'; return; }
  const width = 700, height = 210, left = 34, right = 12, top = 12, bottom = 28;
  const max = Math.max(...trend.map((item) => item.amount), 1);
  const x = (index) => left + index * (width-left-right) / Math.max(trend.length-1, 1);
  const y = (amount) => top + (max-amount) * (height-top-bottom) / max;
  const points = trend.map((item,index) => `${x(index)},${y(item.amount)}`).join(" ");
  const area = `M ${x(0)} ${height-bottom} L ${points.replaceAll(",", " ")} L ${x(trend.length-1)} ${height-bottom} Z`;
  const grid = [0,.25,.5,.75,1].map((ratio) => `<line class="grid" x1="${left}" y1="${top+(height-top-bottom)*ratio}" x2="${width-right}" y2="${top+(height-top-bottom)*ratio}"/>`).join("");
  const labelIndexes = [...new Set([0, Math.floor((trend.length-1)/2), trend.length-1])];
  box.innerHTML = `<svg class="trend-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"><defs><linearGradient id="trendFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#60a5fa" stop-opacity=".28"/><stop offset="1" stop-color="#60a5fa" stop-opacity=".02"/></linearGradient></defs>${grid}<path class="area" d="${area}"/><polyline class="line" points="${points}"/>${trend.map((item,index)=>`<circle class="dot" cx="${x(index)}" cy="${y(item.amount)}" r="3"><title>${item.date}: ${formatAmount(item.amount)}</title></circle>`).join("")}${labelIndexes.map(index=>`<text x="${x(index)}" y="${height-7}" text-anchor="${index===0?'start':index===trend.length-1?'end':'middle'}">${escapeHtml(trend[index].date.slice(5))}</text>`).join("")}<text x="3" y="${top+3}">${Math.round(max)}</text><text x="18" y="${height-bottom+3}">0</text></svg>`;
}
function renderAmountBars(selector, items) {
  const box = $(selector); const max = Math.max(...(items || []).map((item) => item.amount), 1);
  box.innerHTML = items?.length ? items.slice(0,8).map((item) => `<div class="bar-row"><span title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span><div class="bar-track"><div class="bar-fill" style="width:${Math.max(4,item.amount/max*100)}%"></div></div><strong>${formatAmount(item.amount)}</strong></div>`).join("") : '<div class="empty-chart">暂无数据</div>';
}
function fillSelect(selector, values, emptyLabel) {
  const select = $(selector); const current = select.value;
  select.innerHTML = `<option value="">${emptyLabel}</option>` + (values || []).map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
  if ([...select.options].some((option) => option.value === current)) select.value = current;
}
function renderOverview() {
  const data = state.overview || {}; const count = data.count || 0; const maxTx = data.max_tx;
  $("#kpi-total").textContent = formatAmount(data.total_amount); $("#kpi-pending").textContent = formatNumber(data.pending);
  $("#kpi-pending-rate").textContent = `占比 ${count ? Math.round(data.pending/count*100) : 0}%`;
  $("#kpi-anomalies").textContent = formatNumber((state.anomalies?.items || []).length);
  $("#kpi-max-tx").textContent = maxTx ? formatAmount(maxTx.amount) : "暂无";
  $("#kpi-max-tx-note").textContent = maxTx ? `${maxTx.merchant} · ${formatDate(maxTx.paid_at)}` : "等待数据";
  $("#bills-count").textContent = formatNumber(count);
  renderTrend(data.trend); renderAmountBars("#category-chart", data.by_category);
  $("#merchant-list").innerHTML = data.top_merchants?.length ? data.top_merchants.map((item,index) => `<button class="topic-item" data-merchant="${escapeHtml(item.name)}"><span class="topic-rank">${String(index+1).padStart(2,"0")}</span><strong>${escapeHtml(item.name)}</strong><small>${formatAmount(item.amount)} · ${formatNumber(item.count)} 笔</small></button>`).join("") : '<div class="empty-chart">暂无商户数据</div>';
  renderSubscriptions();
  const options = data.options || {}; fillSelect("#overview-category", options.categories, "全部类别"); fillSelect("#filter-category", options.categories, "全部类别"); fillSelect("#filter-method", options.methods, "全部方式");
}
function renderSubscriptions() {
  const items = state.subscriptions || [];
  $("#subscription-list").innerHTML = items.length ? items.map((sub) => `<div class="bar-row"><span title="${escapeHtml(sub.name)}">${escapeHtml(sub.name)}<small>${escapeHtml(sub.cycle)} · ${escapeHtml(sub.merchant)} · 最近扣款 ${escapeHtml(formatDate(sub.last_paid_at))}</small></span><div class="bar-track"><div class="bar-fill" style="width:${sub.active?100:0}%"></div></div><strong>${formatAmount(sub.expected_amount)}</strong></div>`).join("") : '<div class="empty-chart">还没有订阅记录</div>';
}
function renderAnomalies() {
  const data=state.anomalies||{items:[]};const period=data.current_period;
  $("#anomaly-period").textContent=period?`${period.from} 至 ${period.to}，与上一周期对比`:"与上一周期对比";
  $("#anomaly-list").innerHTML=data.items?.length?data.items.slice(0,6).map(item=>`<article class="anomaly-item"><strong>${escapeHtml(item.name)}</strong><span>类别激增</span><small>${escapeHtml(item.detail||"")}</small></article>`).join(""):'<div class="empty-chart">暂无可识别的异常</div>';
}
function renderBills() {
  const data = state.bills; $("#bills-total").textContent = `共 ${formatNumber(data.total)} 笔`; $("#page-number").textContent = data.page; $("#prev-page").disabled = data.page <= 1; $("#next-page").disabled = data.page * data.page_size >= data.total;
  $("#bills-rows").innerHTML = data.items?.length ? data.items.map((item) => `<tr><td><input class="row-select" type="checkbox" data-select-tx="${escapeHtml(item.tx_id)}" ${state.selected.has(item.tx_id)?'checked':''}></td><td>${escapeHtml(formatDate(item.paid_at))}</td><td><span class="ticket-id">${escapeHtml(item.merchant)}</span><div class="tx-note">${escapeHtml(item.note||"")}</div></td><td><span class="tag-pill">${escapeHtml(item.category)}</span></td><td><strong>${formatAmount(item.amount)}</strong></td><td>${escapeHtml(item.method)}</td><td><span class="status-pill ${item.status==="正常"?"":"warn"}">${escapeHtml(item.status)}</span></td><td><div class="row-actions"><button data-workflow-tx="${escapeHtml(item.tx_id)}">核查</button><button class="category-edit" data-edit-tx="${escapeHtml(item.tx_id)}" data-current-category="${escapeHtml(item.category)}">类别</button></div></td></tr>`).join("") : '<tr><td colspan="8" class="table-empty">没有符合条件的交易</td></tr>';
  renderSelection();
}
function auditChanges(item){let changes={};try{changes=JSON.parse(item.new_value)||{};}catch(error){/* 非审计负载时按原样展示 */}return changes;}
function rematchSummary(changes){const target=(state.categories||[]).find(category=>category.id===changes.category_id);return target?`重新匹配 → ${target.name}`:"重新匹配";}
function renderCategories() {
  $("#category-grid").innerHTML = state.categories.length ? state.categories.map((category) => `<article class="tag-card ${category.enabled?'':'disabled'}"><div class="tag-card-head"><div><h3>${escapeHtml(category.name)}</h3><span class="rule-state">${category.enabled?'已启用':'已停用'}</span></div><strong>${formatNumber(category.count)}</strong></div><p>${category.keywords.length ? category.keywords.map(word=>`<span class="keyword">${escapeHtml(word)}</span>`).join("") : "暂无自动匹配关键词"}</p><button class="edit-rule" data-edit-rule="${category.id}">编辑规则</button></article>`).join("") : '<div class="empty-chart">暂无类别</div>';
  const actionLabels={category:"类别变更",workflow:"核查状态",subscription:"订阅设置"};
  $("#category-audit-list").innerHTML=state.audits.length?state.audits.slice(0,20).map(item=>{const changes=auditChanges(item);const label=changes.source==="rematch"?rematchSummary(changes):(actionLabels[item.action]||item.action);return `<div class="audit-item"><span class="audit-dot"></span><div><strong>${escapeHtml(label)}${item.tx_id?` · ${escapeHtml(item.tx_id)}`:""}</strong><small>${escapeHtml(item.operator)} · ${escapeHtml(formatDate(item.changed_at))}</small></div></div>`;}).join(""):'<div class="audit-empty">暂无审计记录</div>';
}
function renderImports() {
  $("#import-rows").innerHTML = state.imports.length ? state.imports.map((job) => `<tr><td>${escapeHtml(job.filename)}</td><td>${escapeHtml(formatDate(job.imported_at))}</td><td>${formatNumber(job.total_rows)}</td><td>${formatNumber(job.imported_rows)}</td><td>${formatNumber(job.duplicate_rows)}</td><td>${formatNumber(job.failed_rows)}</td><td><span class="import-status ${escapeHtml(job.status)}">${{completed:"成功",partial:"部分成功",failed:"失败"}[job.status]||escapeHtml(job.status)}</span></td></tr>`).join("") : '<tr><td colspan="7" class="table-empty">尚未导入账单文件</td></tr>';
}
function renderReports(){
  $("#report-count").textContent=state.reports.length;
  $("#report-list").innerHTML=state.reports.length?state.reports.map(report=>{const view=answerView(report.content);return `<article class="report-card"><div class="report-head"><div><h3>${escapeHtml(report.title)}</h3><small>${escapeHtml(formatDate(report.created_at))} · ${escapeHtml(report.session_id)}</small></div><div class="report-actions"><button data-report-action="copy" data-report-id="${report.id}" title="复制">复制</button><button data-report-action="markdown" data-report-id="${report.id}" title="导出 Markdown">MD</button><button data-report-action="print" data-report-id="${report.id}" title="打印或保存 PDF">PDF</button><button class="delete" data-delete-report="${report.id}" title="删除报告">×</button></div></div><div class="report-content ${view.structured?'structured-answer':''}">${view.html}</div></article>`;}).join(""):'<div class="report-empty">还没有守卫报告。<br>在 Agent 回答下方点击“保存为报告”。</div>';
}
async function loadSession(sessionId = state.session, quiet = false) {
  state.session = String(sessionId || "").trim() || "billguard"; localStorage.setItem("billguard-session", state.session);
  try { const data = await api("/api/snapshot"); state.sessions=data.sessions||[]; state.messages=data.messages||[]; state.overview=data.overview||{}; state.anomalies=data.anomalies||{items:[]}; state.bills=data.bills||state.bills; state.categories=data.categories||[]; state.audits=data.audits||[]; state.imports=data.imports||[]; state.subscriptions=data.subscriptions||[]; state.reports=data.reports||[]; state.approvals=data.approvals||[]; state.mcpServers=data.mcp_servers||[]; state.runs=data.runs||[]; state.runDetail=null; state.evaluations=data.evaluations||[]; renderSessions(); renderMessages(); renderOverview(); renderAnomalies(); renderBills(); renderCategories(); renderImports(); renderReports(); renderApprovals(); renderRuns(); renderRunDetail(); renderEvaluations(); if(!quiet) toast(`已切换到 ${state.session}`); } catch(error) { toast(error.message); }
}
function createSession() { const date=new Date(); const suggested=`billguard-${date.getFullYear()}${String(date.getMonth()+1).padStart(2,"0")}${String(date.getDate()).padStart(2,"0")}`; const name=prompt("输入分析 Session 名称",suggested); if(name?.trim()){showPanel("insight");loadSession(name.trim());} }
async function deleteSession(id) { if(!confirm(`确定删除分析 Session “${id}”吗？\n\n对话和运行记录将永久删除，账单数据库不会受影响。`))return; try{const data=await api("/api/session/delete",{session_id:id});state.sessions=data.sessions||[];if(id===state.session)await loadSession(state.sessions[0]?.id||"billguard",true);else renderSessions();toast(`已删除 ${id}`);}catch(error){toast(error.message);} }
async function send(text) {
  const message=String(text||$("#message").value).trim();if(!message||state.busy)return;showPanel("insight");state.messages.push({role:"user",content:message});renderMessages();$("#messages").insertAdjacentHTML("beforeend",'<div class="message assistant loading"><div><div class="bubble">正在查询账单数据…</div><div class="meta">BillGuard 账单守卫</div></div></div>');$("#message").value="";state.busy=true;$("#send").disabled=true;window.scrollTo({top:document.body.scrollHeight,behavior:"smooth"});
  try{const data=await api("/api/chat",{message});$(".loading")?.remove();state.messages.push({role:"assistant",content:data.answer,evidence:data.evidence||[]});state.sessions=data.sessions||state.sessions;state.overview=data.overview||state.overview;state.approvals=data.approvals||state.approvals;state.mcpServers=data.mcp_servers||state.mcpServers;state.runs=data.runs||state.runs;renderMessages();renderSessions();renderOverview();renderApprovals();renderRuns();if(data.status==="approval_pending"){showPanel("approvals");toast("Agent 已暂停，请检查并审批高风险操作");}else{toast(`分析完成 · ${data.steps} 步`);}}catch(error){$(".loading .bubble").textContent=`分析失败：${error.message}`;}finally{state.busy=false;$("#send").disabled=false;$("#message").focus();}
}
async function saveMessageAsReport(index){const message=state.messages[index];if(!message||message.role!=="assistant")return;const defaultTitle=`账单守卫报告 · ${new Date().toLocaleDateString("zh-CN")}`;const title=prompt("报告标题",defaultTitle);if(!title?.trim())return;try{const data=await api("/api/reports/save",{title:title.trim(),content:message.content});state.reports=data.reports||[];renderReports();toast("已保存为守卫报告");}catch(error){toast(error.message);}}
function currentFilters(){return{query:$("#filter-query").value.trim(),category:$("#filter-category").value,merchant:$("#filter-merchant").value.trim(),method:$("#filter-method").value,status:$("#filter-status").value};}
async function loadBills(page=1,override=null){state.filters=override||currentFilters();try{state.bills=await api("/api/bills/query",{filters:state.filters,page,page_size:30});renderBills();}catch(error){toast(error.message);}}
async function loadOverview(){const days=Number($("#overview-period").value);const filters={category:$("#overview-category").value};if(days){const date=new Date();date.setDate(date.getDate()-days+1);filters.date_from=date.toISOString().slice(0,10);}try{const results=await Promise.all([api("/api/bills/overview",{filters}),api("/api/bills/anomalies",{days:days||31,dimension:"spike",limit:8})]);state.overview=results[0];state.anomalies=results[1];renderOverview();renderAnomalies();}catch(error){toast(error.message);}}
async function importFile(file){if(!file)return;if(file.size>50*1024*1024){toast("CSV 不能超过 50MB");return;}const progress=$("#import-progress");progress.classList.add("show");progress.textContent=`正在导入 ${file.name}…`;try{const text=await file.text();const subsFile=$("#subs-csv-file").files[0];const body={filename:file.name,csv_text:text};if(subsFile){body.subscriptions_filename=subsFile.name;body.subscriptions_csv_text=await subsFile.text();}const data=await api("/api/bills/import",body);state.overview=data.overview;state.imports=data.imports;const subsNote=data.subscriptions_result?`，订阅成功 ${data.subscriptions_result.imported_rows}`:"";progress.textContent=`导入完成：成功 ${data.result.imported_rows}，重复 ${data.result.duplicate_rows}，失败 ${data.result.failed_rows}${subsNote}`;await loadSession(state.session,true);toast("CSV 导入完成");}catch(error){progress.textContent=`导入失败：${error.message}`;toast(error.message);}$("#csv-file").value="";$("#subs-csv-file").value="";}
function downloadText(filename,text,type="text/csv;charset=utf-8"){const url=URL.createObjectURL(new Blob([text],{type}));const link=document.createElement("a");link.href=url;link.download=filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function renderSelection(){const count=state.selected.size;$("#selected-count").textContent=count;$("#bulk-bar").classList.toggle("show",count>0);const visible=state.bills.items||[];$("#select-all").checked=visible.length>0&&visible.every(item=>state.selected.has(item.tx_id));$("#select-all").indeterminate=visible.some(item=>state.selected.has(item.tx_id))&&!$("#select-all").checked;}
function showModal(name){const modal=$(`#${name}-modal`);modal.classList.add("open");modal.setAttribute("aria-hidden","false");}
function closeModal(name){const modal=$(`#${name}-modal`);modal.classList.remove("open");modal.setAttribute("aria-hidden","true");}
let workflowTx="";
async function openWorkflow(txId){const item=(state.bills.items||[]).find(row=>row.tx_id===txId);if(!item)return;workflowTx=txId;$("#workflow-tx").textContent=txId;$("#workflow-status").value=item.status||"正常";$("#workflow-note").value="";$("#workflow-audits").innerHTML='<div class="audit-empty">正在读取核查记录…</div>';showModal("workflow");try{const data=await api("/api/bills/audits",{tx_id:txId});renderWorkflowAudits(data.audits||[]);}catch(error){$("#workflow-audits").innerHTML=`<div class="audit-empty">${escapeHtml(error.message)}</div>`;}}
function renderWorkflowAudits(audits){$("#workflow-audits").innerHTML=audits.length?audits.map(item=>{const changes=auditChanges(item);let summary="";if(Object.prototype.hasOwnProperty.call(changes,"old_status")){summary=`状态：${changes.old_status||"—"} → ${changes.status||"—"}${changes.note?` · 备注：${changes.note}`:""}`;}else if(Object.prototype.hasOwnProperty.call(changes,"new_category")){summary=`类别：${changes.old_category||"—"} → ${changes.new_category}`;}else if(changes.source==="rematch"){summary=rematchSummary(changes);}else{summary=item.action;}return `<div class="audit-item"><span class="audit-dot"></span><div><strong>${escapeHtml(summary)}</strong><small>${escapeHtml(item.operator)} · ${escapeHtml(formatDate(item.changed_at))}</small></div></div>`;}).join(""):'<div class="audit-empty">暂无核查记录</div>';}
function openCategoryRule(category=null){$("#category-rule-id").value=category?.id||"";$("#category-rule-name").value=category?.name||"";$("#category-rule-keywords").value=(category?.keywords||[]).join("，");$("#category-rule-enabled").checked=category?.enabled!==false;$("#category-modal-title").textContent=category?"编辑类别规则":"新建类别";$("#delete-category-rule").style.visibility=category?"visible":"hidden";showModal("category");}
function applyEvidence(filters){showPanel("bills");$("#filter-query").value=filters.query||"";$("#filter-category").value=filters.category||"";$("#filter-merchant").value=filters.merchant||"";$("#filter-method").value=filters.method||"";$("#filter-status").value=filters.status||"";loadBills(1,filters);toast("已打开这条结论对应的数据");}
function markdownValue(value,level=2){if(Array.isArray(value))return value.map((item,index)=>item&&typeof item==="object"?`${"#".repeat(Math.min(level,6))} ${index+1}\n\n${markdownValue(item,level+1)}`:`- ${scalarText(item)}`).join("\n\n");if(value&&typeof value==="object")return Object.entries(value).map(([key,item])=>item&&typeof item==="object"?`${"#".repeat(Math.min(level,6))} ${key}\n\n${markdownValue(item,level+1)}`:`- **${key}：** ${scalarText(item,key)}`).join("\n\n");return String(value??"");}
function reportMarkdown(report){let content=report.content;try{content=markdownValue(JSON.parse(report.content),2);}catch(error){}return `# ${report.title}\n\n- 分析 Session：${report.session_id}\n- 创建时间：${formatDate(report.created_at)}\n\n${content}\n`;}
function printReport(report){const view=answerView(report.content);const popup=window.open("","_blank");if(!popup){toast("浏览器阻止了打印窗口");return;}popup.opener=null;popup.document.write(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>${escapeHtml(report.title)}</title><style>@page{size:A4;margin:18mm}*{box-sizing:border-box}body{margin:0;color:#182235;font:12px/1.65 Arial,"Microsoft YaHei",sans-serif}header{padding-bottom:16px;border-bottom:2px solid #2563eb}h1{margin:0;font-size:24px}header p{margin:5px 0 0;color:#66758b}.content{padding-top:18px;white-space:pre-wrap}.insight-section{margin:0 0 18px}.insight-section h3{padding:7px 10px;border-left:3px solid #2563eb;background:#eaf2ff;color:#1d4ed8;font-size:14px}.insight-subsection h4{margin-bottom:8px}.insight-facts,.insight-object-list{display:grid;grid-template-columns:1fr 1fr;gap:7px}.insight-fact,.insight-object-card,.insight-list li{padding:8px;border:1px solid #dfe7f1;border-radius:5px;background:#f8faff}.insight-fact span,.insight-fact strong{display:block}.insight-fact span,.object-index{color:#66758b;font-size:9px}.insight-list{padding:0;list-style:none}.insight-list li{margin-bottom:6px}.object-index{display:block;margin-bottom:6px}footer{position:fixed;bottom:0;color:#8895a8;font-size:9px}@media print{button{display:none}}</style></head><body><header><h1>${escapeHtml(report.title)}</h1><p>BillGuard · ${escapeHtml(formatDate(report.created_at))} · ${escapeHtml(report.session_id)}</p></header><main class="content ${view.structured?'structured-answer':''}">${view.html}</main><footer>由 BillGuard 账单守卫助手生成</footer><script>window.onload=()=>setTimeout(()=>window.print(),200)<\/script></body></html>`);popup.document.close();}

async function decideApproval(id, decision) {
  const verb = decision === "approve" ? "批准并执行" : "拒绝";
  if (!confirm(`确定${verb}这个高风险操作吗？`)) return;
  $$("[data-approval-decision]").forEach(button => button.disabled = true);
  try {
    const data = await api("/api/approvals/decide", {approval_id:id, decision});
    state.approvals = data.approvals || [];
    state.sessions = data.sessions || state.sessions;
    state.overview = data.overview || state.overview;
    state.runs = data.runs || state.runs;
    if (data.answer) state.messages.push({role:"assistant", content:data.answer, evidence:data.evidence||[]});
    renderApprovals(); renderMessages(); renderSessions(); renderOverview(); renderRuns();
    if (decision === "approve") showPanel("insight");
    toast(decision === "approve" ? "审批通过，Agent 已从暂停点继续" : "已拒绝，工具没有执行");
  } catch (error) {
    toast(error.message);
    await loadSession(state.session, true);
  }
}

$$('.nav-item').forEach(item=>item.addEventListener("click",()=>showPanel(item.dataset.panel)));$$('[data-panel-target]').forEach(item=>item.addEventListener("click",()=>showPanel(item.dataset.panelTarget)));$$('[data-prompt]').forEach(item=>item.addEventListener("click",()=>send(item.dataset.prompt)));
$("#refresh-approvals").onclick=()=>loadSession(state.session,true);$("#approval-list").onclick=(event)=>{const button=event.target.closest("[data-approval-decision]");if(button)decideApproval(button.dataset.approvalId,button.dataset.approvalDecision);};
$("#refresh-runs").onclick=async()=>{try{const data=await api("/api/runs/list");state.runs=data.runs||[];renderRuns();toast("运行记录已刷新");}catch(error){toast(error.message);}};$("#run-list").onclick=(event)=>{const button=event.target.closest("[data-trace-id]");if(button)loadRunDetail(button.dataset.traceId);};
$("#refresh-evaluations").onclick=async()=>{try{const data=await api("/api/evaluations/list");state.evaluations=data.evaluations||[];renderEvaluations();toast("评测报告已刷新");}catch(error){toast(error.message);}};
$("#mobile-menu").onclick=()=>$(".sidebar").classList.toggle("open");$("#new-session").onclick=createSession;$("#refresh-sessions").onclick=()=>loadSession(state.session,true);$("#session-list").onclick=(event)=>{const remove=event.target.closest("[data-delete-session]");if(remove){deleteSession(remove.dataset.deleteSession);return;}const select=event.target.closest("[data-session]");if(select)loadSession(select.dataset.session);};
$("#composer").onsubmit=(event)=>{event.preventDefault();send();};$("#message").onkeydown=(event)=>{if(event.key==="Enter"&&!event.shiftKey){event.preventDefault();send();}};$("#message").oninput=(event)=>{event.target.style.height="auto";event.target.style.height=`${Math.min(event.target.scrollHeight,150)}px`;};
$("#messages").onclick=(event)=>{const evidence=event.target.closest("[data-evidence]");if(evidence){try{applyEvidence(JSON.parse(decodeURIComponent(evidence.dataset.evidence)));}catch(error){toast("无法打开数据依据");}return;}const button=event.target.closest("[data-save-index]");if(button)saveMessageAsReport(Number(button.dataset.saveIndex));};
$("#overview-period").onchange=loadOverview;$("#overview-category").onchange=loadOverview;$("#ask-topics").onclick=()=>send("分析当前高频商户的支出构成，说明数据事实并给出控费建议");$("#ask-anomalies").onclick=()=>send("分析当前的支出异常，区分数据事实和原因推测，并给出下一步建议");$("#merchant-list").onclick=(event)=>{const merchant=event.target.closest("[data-merchant]");if(merchant){showPanel("bills");$("#filter-merchant").value=merchant.dataset.merchant;loadBills(1);}};
$("#apply-filters").onclick=()=>loadBills(1);$("#filter-query").onkeydown=(event)=>{if(event.key==="Enter")loadBills(1);};$("#prev-page").onclick=()=>loadBills(state.bills.page-1);$("#next-page").onclick=()=>loadBills(state.bills.page+1);
$("#bills-rows").onclick=async(event)=>{const select=event.target.closest("[data-select-tx]");if(select){select.checked?state.selected.add(select.dataset.selectTx):state.selected.delete(select.dataset.selectTx);renderSelection();return;}const workflow=event.target.closest("[data-workflow-tx]");if(workflow){openWorkflow(workflow.dataset.workflowTx);return;}const button=event.target.closest("[data-edit-tx]");if(!button)return;const value=prompt(`修改 ${button.dataset.editTx} 的类别`,button.dataset.currentCategory);if(value===null)return;const category=value.trim();if(!category){toast("类别不能为空");return;}try{const data=await api("/api/bills/categories",{tx_id:button.dataset.editTx,category});state.categories=data.categories;await loadBills(state.bills.page);state.overview=await api("/api/bills/overview");renderOverview();renderCategories();toast("类别已更新并记录审计");}catch(error){toast(error.message);}};
$("#select-all").onchange=(event)=>{(state.bills.items||[]).forEach(item=>event.target.checked?state.selected.add(item.tx_id):state.selected.delete(item.tx_id));renderBills();};
$("#apply-bulk").onclick=async()=>{const updates={};if($("#bulk-status").value)updates.status=$("#bulk-status").value;if($("#bulk-note").value.trim())updates.note=$("#bulk-note").value.trim();if(!updates.status){toast("请选择要更新的状态(备注可选)");return;}try{const result=await api("/api/bills/workflow",{tx_ids:[...state.selected],updates});state.selected.clear();await loadBills(state.bills.page);toast(`已更新 ${result.count} 笔交易`);}catch(error){toast(error.message);}};
$("#save-workflow").onclick=async()=>{try{await api("/api/bills/workflow",{tx_ids:[workflowTx],updates:{status:$("#workflow-status").value,note:$("#workflow-note").value}});closeModal("workflow");await loadBills(state.bills.page);state.overview=await api("/api/bills/overview");renderOverview();toast("核查信息已更新");}catch(error){toast(error.message);}};
$$('[data-close-modal]').forEach(button=>button.onclick=()=>closeModal(button.dataset.closeModal));
$("#new-category-rule").onclick=()=>openCategoryRule();$("#category-grid").onclick=(event)=>{const button=event.target.closest("[data-edit-rule]");if(button)openCategoryRule(state.categories.find(category=>category.id===Number(button.dataset.editRule)));};
$("#save-category-rule").onclick=async()=>{const id=$("#category-rule-id").value;const keywords=$("#category-rule-keywords").value.split(/[,，\n]/).map(item=>item.trim()).filter(Boolean);try{const data=await api("/api/category-rules/save",{category_id:id?Number(id):null,name:$("#category-rule-name").value,keywords,enabled:$("#category-rule-enabled").checked});state.categories=data.categories;state.audits=data.audits;renderCategories();closeModal("category");toast("类别规则已保存，点击重新归类后应用到历史交易");}catch(error){toast(error.message);}};
$("#delete-category-rule").onclick=async()=>{const id=Number($("#category-rule-id").value);if(!id||!confirm("删除类别后相关交易会被置为未分类，确定继续吗？"))return;try{const data=await api("/api/category-rules/delete",{category_id:id});state.categories=data.categories;state.audits=data.audits;renderCategories();closeModal("category");toast("类别已删除");}catch(error){toast(error.message);}};
$("#rematch-categories").onclick=async()=>{if(!confirm("将使用当前启用的规则重新归类全部历史交易。确定继续吗？"))return;try{const data=await api("/api/category-rules/rematch");state.categories=data.categories;state.overview=data.overview;renderCategories();renderOverview();await loadBills(state.bills.page);toast(`已重新归类 ${data.result.changed} 笔交易`);}catch(error){toast(error.message);}};
$("#report-list").onclick=async(event)=>{const deleteButton=event.target.closest("[data-delete-report]");if(deleteButton){if(!confirm("确定删除这份守卫报告吗？"))return;try{const data=await api("/api/reports/delete",{report_id:Number(deleteButton.dataset.deleteReport)});state.reports=data.reports||[];renderReports();toast("报告已删除");}catch(error){toast(error.message);}return;}const action=event.target.closest("[data-report-action]");if(!action)return;const report=state.reports.find(item=>item.id===Number(action.dataset.reportId));if(!report)return;if(action.dataset.reportAction==="copy"){try{await navigator.clipboard.writeText(reportMarkdown(report));toast("报告已复制");}catch(error){toast("复制失败，请使用 Markdown 导出");}}else if(action.dataset.reportAction==="markdown"){downloadText(`${report.title}.md`,reportMarkdown(report),"text/markdown;charset=utf-8");}else if(action.dataset.reportAction==="print"){printReport(report);}};
$("#export-bills").onclick=async()=>{try{const data=await api("/api/bills/export",{filters:currentFilters()});downloadText(data.filename,data.csv_text);toast("导出已开始");}catch(error){toast(error.message);}};
$("#csv-file").onchange=(event)=>importFile(event.target.files[0]);const drop=$("#drop-zone");["dragenter","dragover"].forEach(name=>drop.addEventListener(name,event=>{event.preventDefault();drop.classList.add("dragging");}));["dragleave","drop"].forEach(name=>drop.addEventListener(name,event=>{event.preventDefault();drop.classList.remove("dragging");}));drop.addEventListener("drop",event=>importFile(event.dataTransfer.files[0]));
$("#download-template").onclick=()=>downloadText("bills-template.csv","﻿tx_id,paid_at,merchant,category,amount,method,note\nTX-001,2026-09-01 08:30:00,美团外卖,餐饮,32.5,支付宝,午餐\n");
$("#download-subs-template").onclick=()=>downloadText("subscriptions-template.csv","﻿name,merchant,cycle,expected_amount\n视频会员,某视频平台,月,25.0\n");
$("#login-form").onsubmit = async (event) => {
  event.preventDefault();
  const error = $("#login-error"); error.hidden = true;
  try {
    const response = await fetch("/api/auth/login", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({username:$("#login-username").value.trim(), password:$("#login-password").value})});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { error.textContent = data.error || "登录失败"; error.hidden = false; return; }
    applyUser(data.user); hideLogin(); $("#login-password").value = "";
    await loadSession(state.session, true);
  } catch (err) { error.textContent = "网络错误,请重试"; error.hidden = false; }
};
$("#logout").onclick = async () => { try { await fetch("/api/auth/logout", {method:"POST"}); } catch (error) {} state.user = null; state.userCaps = new Set(); $("#user-badge").hidden = true; $("#logout").hidden = true; document.body.classList.remove("viewer-mode"); showPanel("overview"); $("#admin-rows").innerHTML = ""; showLogin(); };
$("#admin-create").onclick = async () => {
  const username = prompt("新用户用户名(2-32 位小写字母/数字,可用 - _)"); if (!username?.trim()) return;
  const password = prompt(`为 ${username.trim()} 设置密码(至少 8 位)`); if (!password) return;
  const role = prompt("角色:admin / approver / viewer", "viewer"); if (!["admin","approver","viewer"].includes(role || "")) { toast("角色无效"); return; }
  try { await api("/api/admin/users", {username: username.trim(), password, role}); toast("用户已创建"); await loadUsers(); } catch (error) { toast(error.message); }
};
$("#admin-rows").onclick = async (event) => {
  const button = event.target.closest("[data-user-action]"); if (!button) return;
  const username = button.dataset.username;
  try {
    if (button.dataset.userAction === "role") {
      const role = prompt(`将 ${username} 的角色改为(admin/approver/viewer)`, button.dataset.role);
      if (!["admin","approver","viewer"].includes(role || "")) return;
      await api("/api/admin/users/role", {username, role}); toast("角色已更新");
    } else if (button.dataset.userAction === "password") {
      const password = prompt(`为 ${username} 设置新密码(至少 8 位)`); if (!password) return;
      await api("/api/admin/users/password", {username, password}); toast("密码已重置");
    } else {
      const disable = button.dataset.disabled !== "true";
      if (disable && !confirm(`确定禁用 ${username} 吗?禁用后其会话立即失效。`)) return;
      await api("/api/admin/users/disable", {username, disabled: disable}); toast(disable ? "已禁用" : "已启用");
    }
    await loadUsers();
  } catch (error) { toast(error.message); }
};
initAuth();
