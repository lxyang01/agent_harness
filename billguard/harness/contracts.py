from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ArgumentConstraint:
    tool_roles: tuple[str, ...]
    path: str
    operator: str
    value: int
    source: str


@dataclass(frozen=True)
class OutputSection:
    name: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class RequestContract:
    argument_constraints: tuple[ArgumentConstraint, ...] = ()
    required_sections: tuple[OutputSection, ...] = ()

    def tool_violations(self, tool_name: str, arguments: dict[str, Any],
                        schema: dict[str, Any]) -> list[str]:
        if "limit" not in schema.get("properties", {}):
            return []
        role = _tool_role(tool_name)
        violations: list[str] = []
        for constraint in self.argument_constraints:
            if role not in constraint.tool_roles or constraint.path != "limit":
                continue
            actual = arguments.get("limit")
            if actual is None:
                violations.append(
                    f"参数 limit 必须显式提供，且不能超过 {constraint.value}"
                )
            elif not isinstance(actual, (int, float)) or isinstance(actual, bool):
                violations.append("参数 limit 必须是数字")
            elif constraint.operator == "lte" and actual > constraint.value:
                violations.append(
                    f"参数 limit={actual} 超过用户要求的最大值 {constraint.value}"
                )
        return violations

    def missing_sections(self, answer: str) -> list[str]:
        keys = _json_keys(answer)
        return [
            section.name for section in self.required_sections
            if not _has_section(answer, keys, section)
        ]

    def prompt_text(self) -> str:
        parts: list[str] = []
        if self.argument_constraints:
            rendered = []
            for item in self.argument_constraints:
                roles = "/".join(item.tool_roles)
                rendered.append(f"{roles}.{item.path} <= {item.value}（必须显式传参）")
            parts.append("动态参数契约：" + "；".join(rendered) + "。")
        if self.required_sections:
            parts.append(
                "最终输出结构契约：必须包含这些可识别章节："
                + "、".join(section.name for section in self.required_sections)
                + "。可使用 Markdown 标题或同名 JSON 字段。"
            )
        return "\n".join(parts)


_MAX_ROWS = re.compile(r"最多\s*(?:返回|读取|给出)?\s*(\d+)\s*条([^，。；,;]{0,10})")
_TOP_ITEMS = re.compile(r"(?:返回|给出|列出)?\s*前\s*(\d+)\s*项")
_REPORT_TERMS = ("周报", "月报", "管理层报告", "生成报告", "汇报")
# monthly-guard-report 的报告型触发词沿用 legacy 的强报告名词口径（不收录
# 裸“报告/总结”，避免误伤复合激活下的普通概览问法），仅补充账单域名词
# “守卫报告”，与路由 triggers 中的守卫报告保持一致。
_BILL_REPORT_TERMS = ("周报", "月报", "守卫报告", "汇报")

_SECTION_CATALOG = {
    "执行摘要": OutputSection("执行摘要", ("执行摘要", "摘要", "summary", "executive_summary")),
    "数据事实": OutputSection("数据事实", ("数据事实", "数据概览", "data_facts", "facts")),
    "异常问题": OutputSection("异常问题", ("异常问题", "异常变化", "anomalies")),
    "代表性样本": OutputSection("代表性样本", ("代表性样本", "样本观察", "samples", "evidence_samples")),
    "行动建议": OutputSection("行动建议", ("行动建议", "下一步建议", "建议的验证动作", "recommendations", "action_items")),
    "数据局限": OutputSection("数据局限", ("数据局限", "局限与风险", "风险和待确认事项", "limitations", "risks")),
}

# monthly-guard-report 固定四章（与 skills/monthly-guard-report/SKILL.md 一致）。
_BILL_SECTION_CATALOG = {
    "支出事实": OutputSection("支出事实", ("支出事实", "支出概览", "data_facts", "facts")),
    "异常清单": OutputSection("异常清单", ("异常清单", "异常问题", "异常变化", "anomalies")),
    "根因推测": OutputSection("根因推测", ("根因推测", "根因分析", "原因推测", "root_cause", "hypotheses")),
    "行动计划": OutputSection("行动计划", ("行动计划", "行动建议", "下一步行动", "action_plan", "action_items", "recommendations")),
}

_ALL_SECTIONS_CATALOG = {**_SECTION_CATALOG, **_BILL_SECTION_CATALOG}
_BILL_REPORT_SECTIONS = ("支出事实", "异常清单", "根因推测", "行动计划")


def compile_request_contract(user_input: str,
                             active_skill_names: Iterable[str]) -> RequestContract:
    constraints: list[ArgumentConstraint] = []
    for match in _MAX_ROWS.finditer(user_input):
        maximum = int(match.group(1))
        tail = match.group(2)
        prefix = user_input[max(0, match.start() - 24):match.start()]
        roles: list[str] = []
        if "样本" in tail or "样本" in prefix[-8:]:
            roles.append("samples")
        if any(term in prefix for term in ("搜索", "检索", "查找")):
            roles.append("query")
        if not roles:
            roles.append("query")
        constraints.append(ArgumentConstraint(
            tuple(dict.fromkeys(roles)), "limit", "lte", maximum, match.group(0),
        ))
    for match in _TOP_ITEMS.finditer(user_input):
        constraints.append(ArgumentConstraint(
            ("anomalies",), "limit", "lte", int(match.group(1)), match.group(0),
        ))

    # When multiple phrases constrain the same role, the strictest bound wins.
    strictest: dict[tuple[tuple[str, ...], str], ArgumentConstraint] = {}
    for item in constraints:
        key = (item.tool_roles, item.path)
        if key not in strictest or item.value < strictest[key].value:
            strictest[key] = item

    skills = set(active_skill_names)
    sections: list[OutputSection] = []
    if "executive-report" in skills and any(term in user_input for term in _REPORT_TERMS):
        names = ["执行摘要", "数据事实", "行动建议", "数据局限"]
        if any(term in user_input for term in ("异常", "激增", "变化", "趋势")):
            names.insert(2, "异常问题")
        if any(term in user_input for term in ("样本", "案例", "代表性")):
            names.insert(-2, "代表性样本")
        sections = [_SECTION_CATALOG[name] for name in dict.fromkeys(names)]
    elif ("monthly-guard-report" in skills
          and any(term in user_input for term in _BILL_REPORT_TERMS)):
        sections = [_ALL_SECTIONS_CATALOG[name] for name in _BILL_REPORT_SECTIONS]

    return RequestContract(tuple(strictest.values()), tuple(sections))


def output_sections_missing(answer: str, required_names: Iterable[str]) -> list[str]:
    sections = tuple(_ALL_SECTIONS_CATALOG[name] for name in required_names)
    return RequestContract(required_sections=sections).missing_sections(answer)


def _tool_role(tool_name: str) -> str:
    # 别名组:每列覆盖 legacy feedback_*、本地 bill_* 与 MCP bill.* 三套命名;
    # legacy 名保留供对抗探针夹具(adv-006/007)使用。
    leaf = tool_name.rsplit(".", 1)[-1]
    if leaf in {"feedback_samples", "bill_samples", "get_samples"}:
        return "samples"
    if leaf in {"feedback_search", "bill_search", "query"}:
        return "query"
    if leaf in {"feedback_anomalies", "bill_anomalies", "detect_anomalies"}:
        return "anomalies"
    return leaf


def _json_keys(answer: str) -> set[str]:
    try:
        value = json.loads(answer)
    except (json.JSONDecodeError, TypeError):
        return set()
    keys: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                keys.add(str(key).casefold())
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return keys


def _has_section(answer: str, keys: set[str], section: OutputSection) -> bool:
    for label in section.labels:
        lowered = label.casefold()
        if lowered in keys:
            return True
        pattern = re.compile(
            rf"(?im)^\s*(?:#{{1,6}}\s*|[-*]\s*|\*\*)?{re.escape(label)}"
            rf"(?:\*\*)?\s*(?::|：|$)"
        )
        if pattern.search(answer):
            return True
    return False
