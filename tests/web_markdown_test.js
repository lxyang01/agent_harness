const fs = require("fs");

const source = fs.readFileSync("minimal_agent/web_static/app.js", "utf8");
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  })[char]);
}
function formatNumber(value) { return String(value); }
function formatDate(value) { return String(value).replace("T", " "); }

eval(source.slice(
  source.indexOf("const FIELD_LABELS"),
  source.indexOf("function answerView"),
));

const rendered = markdownHtml([
  "## 标题", "", "**重点** 与 `code`", "", "- 第一项", "- 第二项", "",
  "<script>alert(1)</script>",
].join("\n"));

if (!rendered.includes("<strong>重点</strong>")) throw new Error("bold was not rendered");
if (!rendered.includes("<ul>")) throw new Error("list was not rendered");
if (!rendered.includes("<code>code</code>")) throw new Error("inline code was not rendered");
if (rendered.includes("<script>")) throw new Error("unsafe HTML was not escaped");

const structured = structuredHtml({
  "工单信息": {
    id: "ISS-0003",
    title: "功能建议反馈跟进",
    description: "这是一段需要占据整行显示的较长工单描述，用来验证信息层级和阅读宽度。",
    priority: "high",
    status: "open",
  },
  "功能建议样本": [{
    id: 23,
    ticket_id: "TK-20260823",
    created_at: "2026-08-04T11:35:00",
    product_module: "订单",
    content: "建议增加订单状态变化通知",
    priority: "medium",
  }],
});
if (!structured.includes("反馈编号")) throw new Error("field labels were not localized");
if (!structured.includes("优先级")) throw new Error("priority label was not localized");
if (!structured.includes(">中<")) throw new Error("priority value was not localized");
if (!structured.includes("is-wide is-primary")) throw new Error("long content did not receive wide layout");
if (structured.includes(">23<")) throw new Error("redundant internal id was not hidden");
if (!structured.includes("样本 01")) throw new Error("sample index was not made readable");

console.log("safe Markdown rendering passed");
console.log("structured answer readability passed");
