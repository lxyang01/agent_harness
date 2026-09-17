from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable

from .context import ContextBuilder
from .contracts import RequestContract, compile_request_contract
from .events import RunEvent
from .spec import AgentSpec
from ..llm import LLM
from ..guardrails import (
    GuardrailError,
    redact_pii,
    unsupported_numeric_claims,
    validate_model_output,
    validate_user_input,
)
from ..parser import parse_decision
from ..policy import ApprovalRequest, PolicyError, PolicyGateway
from ..session import SessionStore
from ..skills import SkillActivation, SkillError, SkillRuntime
from ..tools import ToolError, ToolRegistry
from ..trace import TraceLogger
from ..types import Message, Session


@dataclass
class AgentResponse:
    answer: str
    steps: int
    trace_id: str
    active_skills: tuple[str, ...] = ()
    status: str = "completed"
    approval: dict[str, Any] | None = None


@dataclass
class _ToolExecution:
    payload: str
    succeeded: bool
    error: str = ""


EventHook = Callable[[RunEvent], None]


class HarnessEngine:
    """Controlled Agent loop with optional Skill routing and durable approval checkpoints."""

    def __init__(self, spec: AgentSpec, llm: LLM, tools: ToolRegistry,
                 sessions: SessionStore, context_builder: ContextBuilder | None = None,
                 hooks: list[EventHook] | None = None,
                 skills: SkillRuntime | None = None,
                 policy_gateway: PolicyGateway | None = None,
                 trace_writer: Any = None) -> None:
        missing = set(spec.tool_names) - set(tools.names())
        if missing:
            raise ValueError(f"AgentSpec references unregistered tools: {', '.join(sorted(missing))}")
        self.spec = spec
        self.llm = llm
        self.tools = tools
        self.sessions = sessions
        self.context_builder = context_builder or ContextBuilder()
        self.skills = skills
        self.policy_gateway = policy_gateway
        self.hooks = list(hooks or [])
        # 分布式模式注入 trace_writer(如 PGTraceStore.append_event)时,Trace 走
        # 注入存储;缺省(None)保持单进程 TraceLogger(sessions.root) 行为不变。
        self.trace_writer = trace_writer
        self.trace_logger = None if trace_writer is not None else TraceLogger(sessions.root)
        self.hooks.append(self._trace_hook)

    @staticmethod
    def compress_history(session: Session, keep_recent: int,
                         max_pairs: int = 20) -> Session:
        """确定性压缩:较早轮次进 summary(用户问题→助手结论),工具消息丢弃。

        返回新 Session 对象,不就地修改入参;近 keep_recent 条原文保留。"""
        messages = session.messages
        if len(messages) <= keep_recent:
            return session
        older, recent = messages[:-keep_recent], messages[-keep_recent:]
        pairs: list[str] = []
        pending_question: str | None = None
        for message in older:
            if message.role == "user" and not message.tool_call_id:
                if pending_question:  # 连续提问未获回答:逐条留痕
                    pairs.append(f"- 问:{pending_question[:120]} 答:(该轮无最终回答)")
                pending_question = message.content.strip()
            elif (message.role == "assistant" and not message.tool_call_id
                  and pending_question):
                answer = message.content.strip()
                if answer.startswith("{"):
                    try:
                        import json as _json
                        answer = str(_json.loads(answer).get("final", answer))
                    except (ValueError, AttributeError):
                        pass
                pairs.append(f"- 问:{pending_question[:120]} 答:{answer[:200]}")
                pending_question = None
        if pending_question:  # 压缩窗口内未获回答的问题也留痕
            pairs.append(f"- 问:{pending_question[:120]} 答:(该轮无最终回答)")
        segments = [line for line in session.summary.splitlines() if line.startswith("- 问:")]
        segments.extend(pairs)
        summary = chr(10).join(segments[-max_pairs:])
        return replace(session, summary=summary, messages=recent)

    def run(self, session_id: str, user_input: str) -> AgentResponse:
        user_input = user_input.strip()
        if not user_input:
            raise ValueError("user input cannot be empty")
        validate_user_input(user_input)
        session = self.sessions.load(session_id)
        if (self.spec.summary_threshold
                and len(session.messages) > self.spec.summary_threshold):
            before = len(session.messages)
            session = self.compress_history(session, self.spec.summary_keep_recent)
            self.sessions.save(session)
            trace_id_probe = uuid.uuid4().hex
            self._emit("history_compressed", trace_id_probe, session_id,
                       before=before, after=len(session.messages))
        session.messages.append(Message("user", user_input))
        trace_id = uuid.uuid4().hex
        self._emit("run_start", trace_id, session_id, input=user_input, agent=self.spec.name)
        try:
            active_skills, allowed_tools = self._activate(user_input, trace_id, session_id)
        except SkillError as exc:
            answer = f"Skill 路由或加载失败：{exc}"
            self._emit("skill_error", trace_id, session_id, error=str(exc))
            return self._finish(session, answer, 0, trace_id, session_id, [], status="failed")

        request_contract = compile_request_contract(user_input, active_skills)
        working = self.context_builder.build(
            self.spec, session.summary, session.messages, active_skills, request_contract,
        )
        return self._loop(
            session=session,
            working=working,
            trace_id=trace_id,
            session_id=session_id,
            user_input=user_input,
            active_skills=active_skills,
            allowed_tools=allowed_tools,
            start_step=1,
            execution_summaries=[],
            artifact_paths=[],
            completed_tools=[],
            request_contract=request_contract,
            full_payloads=[],
        )

    def resume(self, approval_id: str) -> AgentResponse:
        if self.policy_gateway is None:
            raise PolicyError("this Agent has no Policy Gateway")
        approval = self.policy_gateway.store.get(approval_id)
        if approval.status != "approved":
            raise PolicyError(f"approval must be approved before resume: {approval.status}")
        checkpoint = approval.checkpoint
        if checkpoint.get("schema_version") != 2:
            raise PolicyError("approval checkpoint version is no longer supported; rerun the request")
        if checkpoint.get("session_id") != approval.session_id:
            raise PolicyError("checkpoint session does not match approval")
        session = self.sessions.load(approval.session_id)
        active_skills, allowed_tools = self._activate(
            str(checkpoint["user_input"]), approval.trace_id, approval.session_id,
            emit_events=False,
        )
        expected_versions = checkpoint.get("skill_versions", {})
        actual_versions = {skill.name: skill.version for skill in active_skills}
        if actual_versions != expected_versions:
            raise PolicyError("active Skill versions changed while the run was awaiting approval")
        if tuple(allowed_tools) != tuple(checkpoint.get("allowed_tools", [])):
            raise PolicyError("tool policy changed while the run was awaiting approval")
        if approval.tool_name not in allowed_tools:
            raise PolicyError(f"approved tool is no longer allowed: {approval.tool_name}")

        request_contract = compile_request_contract(
            str(checkpoint["user_input"]), active_skills)
        working = self.context_builder.build(
            self.spec, session.summary, session.messages, active_skills, request_contract,
        )
        summaries = list(checkpoint.get("execution_summaries", []))
        artifacts = list(checkpoint.get("artifact_paths", []))
        completed_tools = list(checkpoint.get("completed_tools", []))
        self._emit(
            "run_resume", approval.trace_id, approval.session_id, approval.step,
            approval_id=approval.id, decided_by=approval.decided_by,
        )
        execution = self._execute_tool(
            session, working, approval.trace_id, approval.session_id,
            approval.step, approval.tool_name, approval.arguments,
            str(checkpoint["call_id"]), allowed_tools, summaries, artifacts,
        )
        self.policy_gateway.store.mark_execution(
            approval.id, execution.succeeded, execution.error,
        )
        if execution.succeeded:
            completed_tools.append(approval.tool_name)
        return self._loop(
            session=session,
            working=working,
            trace_id=approval.trace_id,
            session_id=approval.session_id,
            user_input=str(checkpoint["user_input"]),
            full_payloads=list(checkpoint.get("full_tool_payloads", [])),
            active_skills=active_skills,
            allowed_tools=allowed_tools,
            start_step=approval.step + 1,
            execution_summaries=summaries,
            artifact_paths=artifacts,
            completed_tools=completed_tools,
            request_contract=request_contract,
        )

    def finalize_rejection(self, approval_id: str) -> AgentResponse:
        if self.policy_gateway is None:
            raise PolicyError("this Agent has no Policy Gateway")
        approval = self.policy_gateway.store.get(approval_id)
        if approval.status != "rejected":
            raise PolicyError(f"approval must be rejected before finalization: {approval.status}")
        session = self.sessions.load(approval.session_id)
        answer = (
            f"已拒绝高风险操作 `{approval.tool_name}`，Agent 未执行该工具。"
            + (f"\n\n审批说明：{approval.decision_note}" if approval.decision_note else "")
        )
        self._emit(
            "approval_rejected", approval.trace_id, approval.session_id, approval.step,
            approval_id=approval.id, tool=approval.tool_name,
            decided_by=approval.decided_by, note=approval.decision_note,
        )
        return self._finish(
            session, answer, approval.step, approval.trace_id, approval.session_id,
            [], status="rejected",
        )

    def _loop(self, session: Session, working: list[Message], trace_id: str,
              session_id: str, user_input: str,
              active_skills: list[SkillActivation], allowed_tools: tuple[str, ...],
              start_step: int, execution_summaries: list[str],
              artifact_paths: list[str], completed_tools: list[str],
              request_contract: RequestContract,
              full_payloads: list[str] | None = None) -> AgentResponse:
        full_payloads = [] if full_payloads is None else full_payloads
        started_at = time.perf_counter()
        for step in range(start_step, self.spec.max_steps + 1):
            if (self.spec.run_timeout is not None
                    and time.perf_counter() - started_at > self.spec.run_timeout):
                answer = (f"已达到最大执行时间（{self.spec.run_timeout:g} 秒），"
                          "任务被 Harness 安全停止。")
                self._emit("run_timeout", trace_id, session_id, self.spec.run_timeout)
                return self._finish(
                    session, self._decorate(answer, execution_summaries, artifact_paths),
                    step - 1, trace_id, session_id, active_skills, status="failed",
                )
            try:
                self._emit("model_start", trace_id, session_id, step)
                model_started = time.perf_counter()
                raw = self.llm.complete(
                    [message.as_dict() for message in working],
                    self.tools.schemas(allowed_tools),
                )
                try:
                    validate_model_output(raw)
                except GuardrailError:
                    self._emit(
                        "model_output_blocked", trace_id, session_id, step,
                        output_chars=len(raw), reason="length_limit",
                    )
                    raise
                self._emit(
                    "model_output", trace_id, session_id, step, raw=raw[:20_000],
                    latency_ms=round((time.perf_counter() - model_started) * 1000, 2),
                    usage=getattr(self.llm, "last_usage", {}),
                    model=getattr(self.llm, "last_model", ""),
                )
                decision = parse_decision(raw)
                self._emit(
                    "model_decision", trace_id, session_id, step,
                    thought=decision.thought,
                    tool=decision.tool_call.name if decision.tool_call else None,
                    final=decision.final is not None,
                )
            except Exception as exc:
                answer = f"Agent 执行模型步骤失败：{exc}"
                self._emit("run_error", trace_id, session_id, step, error=str(exc))
                return self._finish(
                    session, self._decorate(answer, execution_summaries, artifact_paths),
                    step, trace_id, session_id, active_skills, status="failed",
                )

            if decision.final is not None:
                missing_tools = self._missing_required_tools(active_skills, completed_tools)
                if missing_tools:
                    self._emit(
                        "completion_blocked", trace_id, session_id, step,
                        missing_tools=missing_tools, proposed_final=decision.final[:4_000],
                    )
                    working.append(Message(
                        "system",
                        "本轮用户请求尚未完成，不能返回 final。"
                        f"必须先成功调用这些工具：{', '.join(missing_tools)}。"
                        "请继续执行缺失工具；不得声称尚未执行的操作已经完成。",
                    ))
                    continue
                missing_sections = request_contract.missing_sections(decision.final)
                if missing_sections:
                    self._emit(
                        "output_contract_blocked", trace_id, session_id, step,
                        missing_sections=missing_sections,
                        proposed_final=decision.final[:4_000],
                    )
                    working.append(Message(
                        "system",
                        "最终回答结构尚未满足本轮报告契约，不能返回 final。"
                        f"请保留已有事实并补充这些章节：{', '.join(missing_sections)}。"
                        "不得为填充章节而编造数据；缺少证据时明确写出局限。",
                    ))
                    continue
                safe_final, redactions = redact_pii(decision.final)
                if redactions:
                    self._emit(
                        "output_redacted", trace_id, session_id, step,
                        redactions=redactions,
                    )
                evidence_values = full_payloads + [
                    message.content for message in working
                    if message.role == "assistant" and message.tool_call_id
                ]
                unsupported_numbers = unsupported_numeric_claims(
                    safe_final, evidence_values,
                )
                if unsupported_numbers:
                    self._emit(
                        "grounding_blocked", trace_id, session_id, step,
                        unsupported_numbers=unsupported_numbers,
                        proposed_final=safe_final[:4_000],
                    )
                    rendered = "、".join(f"{value:g}" for value in unsupported_numbers[:12])
                    working.append(Message(
                        "system",
                        "最终回答包含无法在已执行工具参数或结果中找到依据的数字："
                        + rendered
                        + "。请删除无证据数字，或继续调用合适工具取得证据后再回答。"
                        "用户输入本身不能作为数据事实证据。",
                    ))
                    continue
                answer = self._decorate(safe_final, execution_summaries, artifact_paths)
                return self._finish(
                    session, answer, step, trace_id, session_id, active_skills,
                )

            call = decision.tool_call
            assert call is not None
            call_id = uuid.uuid4().hex[:12]
            try:
                tool_schema = self.tools.get(call.name).parameters
            except ToolError as exc:
                execution = self._append_tool_error(
                    session, working, trace_id, session_id, step,
                    call.name, call.arguments, call_id, str(exc),
                )
                if not execution.succeeded:
                    continue
                tool_schema = {}
            argument_violations = request_contract.tool_violations(
                call.name, call.arguments, tool_schema,
            )
            if argument_violations:
                self._emit(
                    "argument_blocked", trace_id, session_id, step,
                    tool=call.name, arguments=call.arguments,
                    violations=argument_violations,
                )
                working.append(Message(
                    "system",
                    f"工具 `{call.name}` 的参数违反用户原话编译出的动态契约："
                    + "；".join(argument_violations)
                    + "。该工具尚未执行。请修正参数后重新调用。",
                ))
                continue
            if self.policy_gateway is not None:
                try:
                    action = self.policy_gateway.enforce(
                        self.tools.get(call.name).policy, call.name,
                    )
                except (PolicyError, ToolError) as exc:
                    execution = self._append_tool_error(
                        session, working, trace_id, session_id, step,
                        call.name, call.arguments, call_id, str(exc),
                    )
                    if not execution.succeeded:
                        continue
                if action == "approval_required":
                    return self._pause_for_approval(
                        session, trace_id, session_id, user_input, step,
                        call.name, call.arguments, call_id, active_skills,
                        allowed_tools, execution_summaries, artifact_paths, completed_tools,
                        full_payloads=full_payloads,
                    )

            execution = self._execute_tool(
                session, working, trace_id, session_id, step,
                call.name, call.arguments, call_id, allowed_tools,
                execution_summaries, artifact_paths, full_payloads,
            )
            if execution.succeeded:
                completed_tools.append(call.name)

        answer = f"已达到最大执行步数（{self.spec.max_steps}），任务被 Harness 安全停止。"
        self._emit("max_steps", trace_id, session_id, self.spec.max_steps)
        return self._finish(
            session, self._decorate(answer, execution_summaries, artifact_paths),
            self.spec.max_steps, trace_id, session_id, active_skills, status="failed",
        )

    def _pause_for_approval(self, session: Session, trace_id: str, session_id: str,
                            user_input: str, step: int, tool_name: str,
                            arguments: dict[str, Any], call_id: str,
                            active_skills: list[SkillActivation],
                            allowed_tools: tuple[str, ...],
                            execution_summaries: list[str],
                            artifact_paths: list[str],
                            completed_tools: list[str],
                            full_payloads: list[str] | None = None) -> AgentResponse:
        assert self.policy_gateway is not None
        policy = self.tools.get(tool_name).policy
        checkpoint = {
            "schema_version": 2,
            "full_tool_payloads": list(full_payloads or []),
            "session_id": session_id,
            "trace_id": trace_id,
            "user_input": user_input,
            "step": step,
            "call_id": call_id,
            "skill_versions": {skill.name: skill.version for skill in active_skills},
            "allowed_tools": list(allowed_tools),
            "execution_summaries": execution_summaries,
            "artifact_paths": artifact_paths,
            "completed_tools": completed_tools,
        }
        approval = self.policy_gateway.store.request(
            session_id, trace_id, step, tool_name, arguments, policy, checkpoint,
        )
        self.sessions.save(session)
        self._emit(
            "approval_pending", trace_id, session_id, step,
            approval_id=approval.id, tool=tool_name, arguments=arguments,
            risk_level=policy.risk_level, reason=policy.reason,
        )
        answer = (
            f"操作 `{tool_name}` 需要人工审批，Agent 已保存 Checkpoint 并暂停。"
            "审批通过后会从当前步骤继续，不会重复前面的查询。"
        )
        return AgentResponse(
            answer, step, trace_id,
            tuple(skill.name for skill in active_skills),
            status="approval_pending",
            approval=approval.as_dict(),
        )

    def _execute_tool(self, session: Session, working: list[Message], trace_id: str,
                      session_id: str, step: int, tool_name: str,
                      arguments: dict[str, Any], call_id: str,
                      allowed_tools: tuple[str, ...], summaries: list[str],
                      artifact_paths: list[str],
                      full_payloads: list[str] | None = None) -> _ToolExecution:
        self._emit(
            "tool_start", trace_id, session_id, step,
            tool=tool_name, arguments=arguments,
        )
        tool_started = time.perf_counter()
        try:
            result: Any = self.tools.execute(tool_name, arguments, allowed=allowed_tools)
            payload = json.dumps(result, ensure_ascii=False, default=str)
            summary = self.tools.format_result(tool_name, result)
            if summary:
                summaries.append(summary)
            if isinstance(result, dict) and result.get("storage_path"):
                artifact_paths.append(str(result["storage_path"]))
            self._emit(
                "tool_end", trace_id, session_id, step,
                tool=tool_name, result=result,
                latency_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            )
            if (isinstance(result, dict) and result.get("degraded") is True
                    and "error" in result
                    and self.tools.get(tool_name).policy.risk_level == "high_write"):
                # 降级的高写工具 = 实际未执行(熔断/重连中没有任何业务动作发生)。
                # 按失败记账:审批恢复路径 mark_execution(False) 让审批卡如实
                # 显示"执行失败"而非"已执行";completed_tools 不追加。降级载荷
                # 仍进上下文,模型可给出"服务暂不可用"的降级回答。
                # (degraded 标记是 mcp_runtime._make_handler 的降级契约;本地
                # 工具不产生该形态的结果。)
                error = str(result["error"])
                self._emit("tool_error", trace_id, session_id, step,
                           tool=tool_name, error=error)
                execution = _ToolExecution(payload, False, error)
            else:
                execution = _ToolExecution(payload, True)
                if full_payloads is not None:
                    full_payloads.append(payload)  # 门禁用完整值,截断只影响模型可见面
        except ToolError as exc:
            payload = json.dumps({"error": str(exc)}, ensure_ascii=False)
            self._emit(
                "tool_error", trace_id, session_id, step,
                tool=tool_name, error=str(exc),
                latency_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            )
            execution = _ToolExecution(payload, False, str(exc))
        self._append_tool_messages(
            session, working, tool_name, arguments, call_id,
            self._contextual_payload(payload, trace_id, step),
        )
        return execution

    def _contextual_payload(self, payload: str, trace_id: str, step: int) -> str:
        """模型可见面截断;完整结果已在 tool_end Trace 与门禁证据中。"""
        limit = self.spec.tool_result_context_limit
        if not limit or len(payload) <= limit:
            return payload
        notice = (chr(10) + f"…[工具结果过长,已截断至 {limit} 字符;完整结果存证于 Trace 第 {step} 步"
                  + f"({trace_id[:8]}),可依据以上摘要作答]")
        return payload[:limit] + notice

    def _append_tool_error(self, session: Session, working: list[Message],
                           trace_id: str, session_id: str, step: int,
                           tool_name: str, arguments: dict[str, Any],
                           call_id: str, error: str) -> _ToolExecution:
        payload = json.dumps({"error": error}, ensure_ascii=False)
        self._emit(
            "tool_error", trace_id, session_id, step,
            tool=tool_name, error=error,
        )
        self._append_tool_messages(
            session, working, tool_name, arguments, call_id, payload,
        )
        return _ToolExecution(payload, False, error)

    @staticmethod
    def _append_tool_messages(session: Session, working: list[Message],
                              tool_name: str, arguments: dict[str, Any],
                              call_id: str, payload: str) -> None:
        assistant_call = Message(
            "assistant",
            json.dumps({"tool_call": {"name": tool_name, "arguments": arguments}}, ensure_ascii=False),
            tool_call_id=call_id,
        )
        tool_result = Message("tool", payload, name=tool_name, tool_call_id=call_id)
        working.extend([assistant_call, tool_result])
        session.messages.extend([assistant_call, tool_result])

    def _activate(self, user_input: str, trace_id: str, session_id: str,
                  emit_events: bool = True) -> tuple[list[SkillActivation], tuple[str, ...]]:
        active_skills: list[SkillActivation] = []
        allowed_tools = self.spec.tool_names
        if self.skills is not None:
            active_skills = self.skills.activate(user_input)
            allowed_tools = self.skills.allowed_tools(active_skills, self.spec.tool_names)
            resolved_skills: list[SkillActivation] = []
            available = set(allowed_tools)
            for activation in active_skills:
                plan = activation.required_tool_plan or tuple(
                    [(tool,) for tool in activation.required_tools]
                    + list(activation.required_tool_groups)
                )
                resolved_plan: list[str] = []
                for alternatives in plan:
                    selected = next((tool for tool in alternatives if tool in available), None)
                    if selected is None:
                        # 组内工具对当前 Agent 全部不可用(如本地模式未接入工单服务):
                        # 跳过该组,允许降级完成,而不是让整个请求硬失败。
                        continue
                    resolved_plan.append(selected)
                resolved_skills.append(replace(
                    activation,
                    required_tools=tuple(dict.fromkeys(resolved_plan)),
                    required_tool_plan=tuple((tool,) for tool in resolved_plan),
                ))
            active_skills = resolved_skills
            required_tools = {
                tool for activation in active_skills for tool in activation.required_tools
            }
            unavailable = sorted(required_tools - set(allowed_tools))
            if unavailable:
                raise SkillError(
                    "request requires unavailable tools: " + ", ".join(unavailable)
                )
            if emit_events:
                for activation in active_skills:
                    self._emit(
                        "skill_activated", trace_id, session_id,
                        skill=activation.name, version=activation.version,
                        reason=activation.reason, score=activation.score,
                        allowed_tools=list(activation.allowed_tools),
                        required_tools=list(activation.required_tools),
                    )
        return active_skills, allowed_tools

    @staticmethod
    def _missing_required_tools(active_skills: list[SkillActivation],
                                completed_tools: list[str]) -> list[str]:
        missing: list[str] = []
        for activation in active_skills:
            cursor = 0
            required = activation.required_tools
            for completed in completed_tools:
                if cursor < len(required) and completed == required[cursor]:
                    cursor += 1
            missing.extend(required[cursor:])
        return list(dict.fromkeys(missing))

    @staticmethod
    def _decorate(answer: str, summaries: list[str], artifact_paths: list[str]) -> str:
        parts = [answer.strip()]
        if summaries:
            parts.append("本轮实际执行结果：\n" + "\n".join(f"- {summary}" for summary in summaries))
        unique_paths = list(dict.fromkeys(artifact_paths))
        if unique_paths:
            parts.append("数据文件：\n" + "\n".join(f"- {path}" for path in unique_paths))
        return "\n\n".join(parts)

    def _finish(self, session: Session, answer: str, steps: int, trace_id: str,
                session_id: str, active_skills: list[SkillActivation] | None = None,
                status: str = "completed") -> AgentResponse:
        answer, redactions = redact_pii(answer)
        if redactions:
            self._emit(
                "output_redacted", trace_id, session_id, steps,
                redactions=redactions,
            )
        session.messages.append(Message("assistant", answer))
        self.sessions.save(session)
        self._emit("run_end", trace_id, session_id, steps, answer=answer, status=status)
        return AgentResponse(
            answer, steps, trace_id,
            tuple(skill.name for skill in active_skills or []), status=status,
        )

    def _emit(self, event_type: str, trace_id: str, session_id: str,
              step: int = 0, **data: Any) -> None:
        event = RunEvent(event_type, trace_id, session_id, step, data)
        for hook in self.hooks:
            try:
                hook(event)
            except Exception:
                # Observability must never break the Agent loop.
                continue

    def _trace_hook(self, event: RunEvent) -> None:
        if self.trace_writer is not None:
            # 与 TraceLogger.log 同构的记录结构(timestamp/event/trace_id/step/agent
            # + 事件数据);写入注入的追踪存储(如 PGTraceStore),且只会发生在
            # web 层会话锁内的 run/resume/finalize 期间(单写者,规避首写竞态)。
            record = {"timestamp": datetime.now(timezone.utc).isoformat(),
                      "event": event.event_type,
                      "trace_id": event.trace_id,
                      "step": event.step,
                      "agent": self.spec.name,
                      **event.data}
            self.trace_writer.append_event(
                event.session_id, event.trace_id, self.spec.name, record)
            return
        key = self.sessions._key(event.session_id)
        self.trace_logger.log(
            key, event.event_type, trace_id=event.trace_id,
            step=event.step, agent=self.spec.name, **event.data,
        )
