from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EXPLICIT_SKILL = re.compile(r"\$([a-z0-9]+(?:-[a-z0-9]+)*)")


class SkillError(ValueError):
    """Raised when skill discovery, routing, or activation is invalid."""


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    path: Path


@dataclass(frozen=True)
class SkillCompletionRule:
    triggers: tuple[str, ...]
    required_tools: tuple[str, ...]
    required_tool_groups: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class SkillRoute:
    skill_name: str
    triggers: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    completion_rules: tuple[SkillCompletionRule, ...] = ()
    priority: int = 0


@dataclass(frozen=True)
class SkillActivation:
    name: str
    description: str
    instructions: str
    version: str
    reason: str
    score: int
    allowed_tools: tuple[str, ...]
    required_tools: tuple[str, ...] = ()
    required_tool_groups: tuple[tuple[str, ...], ...] = ()
    required_tool_plan: tuple[tuple[str, ...], ...] = ()


class SkillRuntime:
    """Discovers skill metadata and loads full instructions only on activation."""

    def __init__(self, root: str | Path, max_active: int = 2,
                 max_skill_bytes: int = 256_000) -> None:
        self.root = Path(root)
        self.max_active = max_active
        self.max_skill_bytes = max_skill_bytes
        if max_active < 1:
            raise SkillError("max_active must be positive")
        self._metadata: dict[str, SkillMetadata] = {}
        self._routes: dict[str, SkillRoute] = {}
        self._default_skill = ""
        self.refresh()

    def refresh(self) -> None:
        if not self.root.is_dir():
            raise SkillError(f"skill root does not exist: {self.root}")
        discovered: dict[str, SkillMetadata] = {}
        for path in sorted(self.root.glob("*/SKILL.md")):
            fields = self._read_frontmatter(path)
            name = fields.get("name", "")
            description = fields.get("description", "")
            self._validate_metadata(name, description, path)
            if name in discovered:
                raise SkillError(f"duplicate skill name: {name}")
            discovered[name] = SkillMetadata(name, description, path)
        if not discovered:
            raise SkillError(f"no skills found under: {self.root}")
        self._metadata = discovered
        self._load_routes()

    def catalog(self) -> list[SkillMetadata]:
        return list(self._metadata.values())

    def activate(self, user_input: str) -> list[SkillActivation]:
        text = user_input.strip()
        if not text:
            raise SkillError("cannot route an empty request")
        explicit = _EXPLICIT_SKILL.findall(text.lower())
        unknown = sorted(set(explicit) - set(self._metadata))
        if unknown:
            raise SkillError(f"unknown explicitly requested skill: {', '.join(unknown)}")

        candidates: dict[str, tuple[int, str]] = {}
        for name in explicit:
            candidates[name] = (10_000, f"explicit:${name}")
        lowered = text.casefold()
        for route in self._routes.values():
            matched = [trigger for trigger in route.triggers if trigger.casefold() in lowered]
            if not matched:
                continue
            longest = max(matched, key=len)
            score = 100 + route.priority + len(longest)
            previous = candidates.get(route.skill_name)
            if previous is None or score > previous[0]:
                candidates[route.skill_name] = (score, f"trigger:{longest}")

        if not candidates and self._default_skill:
            candidates[self._default_skill] = (1, "default")

        ordered = sorted(candidates.items(), key=lambda item: (-item[1][0], item[0]))[:self.max_active]
        return [self._load_activation(name, score, reason, lowered)
                for name, (score, reason) in ordered]

    @staticmethod
    def allowed_tools(activations: list[SkillActivation], fallback: tuple[str, ...]) -> tuple[str, ...]:
        if not activations:
            return fallback
        allowed = {tool for activation in activations for tool in activation.allowed_tools}
        constrained = tuple(tool for tool in fallback if tool in allowed)
        if not constrained:
            names = ", ".join(activation.name for activation in activations)
            raise SkillError(f"activated skills expose no tools allowed by AgentSpec: {names}")
        return constrained

    def _load_routes(self) -> None:
        path = self.root / "routes.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise SkillError(f"cannot load skill routes: {exc}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("routes"), list):
            raise SkillError("skills/routes.json must contain a routes array")
        default_skill = value.get("default_skill", "")
        if default_skill and default_skill not in self._metadata:
            raise SkillError(f"default skill is not installed: {default_skill}")
        routes: dict[str, SkillRoute] = {}
        for item in value["routes"]:
            if not isinstance(item, dict):
                raise SkillError("each skill route must be an object")
            name = item.get("skill", "")
            triggers = item.get("triggers", [])
            allowed_tools = item.get("allowed_tools", [])
            completion_rules = item.get("completion_rules", [])
            priority = item.get("priority", 0)
            if name not in self._metadata:
                raise SkillError(f"route references an uninstalled skill: {name}")
            if name in routes:
                raise SkillError(f"duplicate route for skill: {name}")
            if not isinstance(triggers, list) or not all(isinstance(value, str) and value for value in triggers):
                raise SkillError(f"invalid triggers for skill: {name}")
            if not isinstance(allowed_tools, list) or not all(
                isinstance(value, str) and value for value in allowed_tools
            ):
                raise SkillError(f"invalid allowed_tools for skill: {name}")
            if not isinstance(completion_rules, list):
                raise SkillError(f"invalid completion_rules for skill: {name}")
            if not isinstance(priority, int) or not -100 <= priority <= 100:
                raise SkillError(f"invalid route priority for skill: {name}")
            parsed_rules: list[SkillCompletionRule] = []
            for rule in completion_rules:
                if not isinstance(rule, dict):
                    raise SkillError(f"completion rule must be an object: {name}")
                rule_triggers = rule.get("triggers", [])
                required_tools = rule.get("required_tools", [])
                required_tool_groups = rule.get("required_tool_groups", [])
                if not isinstance(rule_triggers, list) or not all(
                    isinstance(value, str) and value for value in rule_triggers
                ):
                    raise SkillError(f"invalid completion rule triggers for skill: {name}")
                if not isinstance(required_tools, list) or not all(
                    isinstance(value, str) and value for value in required_tools
                ):
                    raise SkillError(f"invalid required_tools for skill: {name}")
                if not isinstance(required_tool_groups, list) or not all(
                    isinstance(group, list) and len(group) >= 2
                    and all(isinstance(value, str) and value for value in group)
                    for group in required_tool_groups
                ):
                    raise SkillError(f"invalid required_tool_groups for skill: {name}")
                unknown_required = set(required_tools) - set(allowed_tools)
                unknown_group_tools = {
                    tool for group in required_tool_groups for tool in group
                } - set(allowed_tools)
                if unknown_required:
                    raise SkillError(
                        f"completion rule requires tools outside allowed_tools for {name}: "
                        + ", ".join(sorted(unknown_required))
                    )
                if unknown_group_tools:
                    raise SkillError(
                        f"completion rule groups reference tools outside allowed_tools for {name}: "
                        + ", ".join(sorted(unknown_group_tools))
                    )
                parsed_rules.append(SkillCompletionRule(
                    tuple(rule_triggers), tuple(dict.fromkeys(required_tools)),
                    tuple(tuple(dict.fromkeys(group)) for group in required_tool_groups),
                ))
            routes[name] = SkillRoute(
                name, tuple(triggers), tuple(dict.fromkeys(allowed_tools)),
                tuple(parsed_rules), priority,
            )
        missing = set(self._metadata) - set(routes)
        if missing:
            raise SkillError(f"skills missing routing policy: {', '.join(sorted(missing))}")
        self._default_skill = str(default_skill)
        self._routes = routes

    def _load_activation(self, name: str, score: int, reason: str,
                         lowered_input: str) -> SkillActivation:
        metadata = self._metadata[name]
        try:
            size = metadata.path.stat().st_size
            if size > self.max_skill_bytes:
                raise SkillError(f"skill exceeds size limit: {name}")
            raw = metadata.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillError(f"cannot read skill {name}: {exc}") from exc
        _, body = self._split_document(raw, metadata.path)
        instructions = body.strip()
        if not instructions:
            raise SkillError(f"skill body is empty: {name}")
        if len(instructions.splitlines()) > 500:
            raise SkillError(f"skill body exceeds 500 lines: {name}")
        version = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        route = self._routes[name]
        matched_rules = [
            rule for rule in route.completion_rules
            if any(trigger.casefold() in lowered_input for trigger in rule.triggers)
        ]
        required_tools = tuple(dict.fromkeys(
            tool
            for rule in matched_rules
            for tool in rule.required_tools
        ))
        required_tool_groups = tuple(dict.fromkeys(
            group
            for rule in matched_rules
            for group in rule.required_tool_groups
        ))
        required_tool_plan = tuple(dict.fromkeys(
            step
            for rule in matched_rules
            for step in (
                *((tool,) for tool in rule.required_tools),
                *rule.required_tool_groups,
            )
        ))
        return SkillActivation(
            name=name,
            description=metadata.description,
            instructions=instructions,
            version=version,
            reason=reason,
            score=score,
            allowed_tools=route.allowed_tools,
            required_tools=required_tools,
            required_tool_groups=required_tool_groups,
            required_tool_plan=required_tool_plan,
        )

    @classmethod
    def _read_frontmatter(cls, path: Path) -> dict[str, str]:
        try:
            lines: list[str] = []
            with path.open("r", encoding="utf-8") as handle:
                if handle.readline().strip() != "---":
                    raise SkillError(f"SKILL.md must start with YAML frontmatter: {path}")
                for line in handle:
                    if line.strip() == "---":
                        break
                    lines.append(line)
                else:
                    raise SkillError(f"unterminated YAML frontmatter: {path}")
        except OSError as exc:
            raise SkillError(f"cannot read skill metadata {path}: {exc}") from exc
        return cls._parse_scalar_yaml(lines, path)

    @staticmethod
    def _parse_scalar_yaml(lines: list[str], path: Path) -> dict[str, str]:
        fields: dict[str, str] = {}
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                raise SkillError(f"unsupported frontmatter line in {path}: {line}")
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            fields[key] = value
        return fields

    @staticmethod
    def _split_document(raw: str, path: Path) -> tuple[str, str]:
        lines = raw.splitlines()
        if not lines or lines[0].strip() != "---":
            raise SkillError(f"SKILL.md must start with YAML frontmatter: {path}")
        try:
            end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
        except StopIteration as exc:
            raise SkillError(f"unterminated YAML frontmatter: {path}") from exc
        return "\n".join(lines[1:end]), "\n".join(lines[end + 1:])

    @staticmethod
    def _validate_metadata(name: str, description: str, path: Path) -> None:
        if not _SKILL_NAME.fullmatch(name) or len(name) > 64:
            raise SkillError(f"invalid skill name in {path}: {name}")
        if path.parent.name != name:
            raise SkillError(f"skill directory must match name '{name}': {path.parent}")
        if not description or len(description) > 1024:
            raise SkillError(f"invalid skill description for {name}")
