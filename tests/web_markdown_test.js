const fs = require("fs");

const source = fs.readFileSync("billguard/web_static/app.js", "utf8");
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  })[char]);
}
function formatNumber(value) { return String(value); }
function formatAmount(value) { return String(value); }
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
  "交易概况": {
    id: 3,
    tx_id: "TX-20260908",
    paid_at: "2026-09-08T12:00:00",
    merchant: "京东商城",
    amount: 899,
    status: "待核查",
  },
  "脱敏样本": [{
    id: 23,
    tx_id: "TX-20260909",
    paid_at: "2026-09-09T08:30:00",
    merchant: "美团外卖",
    category: "餐饮",
    note: "这是一段需要占据整行显示的较长交易备注，用来验证信息层级和阅读宽度。",
    status: "正常",
  }],
});
if (!structured.includes("交易编号")) throw new Error("field labels were not localized");
if (!structured.includes("支付时间")) throw new Error("paid_at label was not localized");
if (!structured.includes("待核查")) throw new Error("workflow status value was not preserved");
if (!structured.includes("is-wide")) throw new Error("long note did not receive wide layout");
if (!structured.includes("is-primary")) throw new Error("merchant did not receive primary layout");
if (structured.includes(">23<")) throw new Error("redundant internal id was not hidden");
if (!structured.includes("样本 01")) throw new Error("sample index was not made readable");

console.log("safe Markdown rendering passed");
console.log("structured answer readability passed");
