from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from billguard.auth import User
from billguard.harness import AgentSpec, HarnessEngine
from billguard.policy import PolicyError, PolicyGateway, ToolPolicy
from billguard.skills import SkillRuntime
from billguard.storage_pg import (
    PGApprovalStore, PGBillService, PGEvidenceStore, PGSessionStore,
    PGTraceStore, PGWorkItemStore,
)
from billguard.tools import Tool, ToolRegistry
from billguard.web import BillGuardApp

from tests.conftest import StoreTestCase


class QueueLLM:
    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = list(outputs)

    def complete(self, messages, tools) -> str:
        if not self.outputs:
            raise AssertionError("unexpected model call")
        return json.dumps(self.outputs.pop(0), ensure_ascii=False)


def high_risk_registry(handler) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(
        name="danger.write",
        description="Persist a controlled write",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler=handler,
        policy=ToolPolicy("high_write", True, "Changes durable external state"),
    ))
    return registry


class PolicyHarnessTests(StoreTestCase):
    """HarnessEngine 会话/审批走 PG(PGSessionStore + PGApprovalStore;
    trace_writer 注入 PGTraceStore,否则引擎回退 TraceLogger(sessions.root))。"""

    def sessions(self) -> PGSessionStore:
        return PGSessionStore(self.pool)

    def traces(self) -> PGTraceStore:
        return PGTraceStore(self.pool)

    def test_completion_contract_blocks_early_final_until_required_write_is_proposed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill_root = root / "skills"
            skill_dir = skill_root / "work-item-action"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: work-item-action\ndescription: Create a controlled work item.\n---\n"
                "Call prepare first, then propose commit. Do not stop early.",
                encoding="utf-8",
            )
            (skill_root / "routes.json").write_text(json.dumps({
                "default_skill": "work-item-action",
                "routes": [{
                    "skill": "work-item-action",
                    "triggers": ["工单"],
                    "allowed_tools": ["prepare", "commit"],
                    "completion_rules": [{
                        "triggers": ["工单"],
                        "required_tools": ["prepare", "commit"],
                    }],
                }],
            }, ensure_ascii=False), encoding="utf-8")

            calls: list[str] = []
            registry = ToolRegistry()
            registry.register(Tool(
                "prepare", "Prepare", {"type": "object", "properties": {},
                "required": [], "additionalProperties": False},
                lambda: calls.append("prepare") or {"approval_id": "APR-1"},
                policy=ToolPolicy("low_write", False, "Creates a draft"),
            ))
            registry.register(Tool(
                "commit", "Commit", {"type": "object", "properties": {},
                "required": [], "additionalProperties": False},
                lambda: calls.append("commit") or {"issue_id": "ISS-1"},
                policy=ToolPolicy("high_write", True, "Creates a durable item"),
            ))
            llm = QueueLLM([
                {"thought": "enough", "final": "done too early"},
                {"thought": "prepare", "tool_call": {"name": "prepare", "arguments": {}}},
                {"thought": "enough", "final": "still too early"},
                {"thought": "commit", "tool_call": {"name": "commit", "arguments": {}}},
                {"thought": "done", "final": "issue created"},
            ])
            gateway = PolicyGateway(PGApprovalStore(self.pool))
            events = []
            engine = HarnessEngine(
                AgentSpec("completion-test", "Complete the request.", ("prepare", "commit"), 6),
                llm, registry, self.sessions(),
                skills=SkillRuntime(skill_root), policy_gateway=gateway, hooks=[events.append],
                trace_writer=self.traces(),
            )

            paused = engine.run("completion-session", "创建跟进工单")
            self.assertEqual("approval_pending", paused.status)
            self.assertEqual("commit", paused.approval["tool_name"])
            self.assertEqual(["prepare"], calls)
            self.assertEqual(2, sum(event.event_type == "completion_blocked" for event in events))

            gateway.store.decide(paused.approval["id"], True, "reviewer")
            completed = engine.resume(paused.approval["id"])
            self.assertEqual("completed", completed.status)
            self.assertEqual("issue created", completed.answer)
            self.assertEqual(["prepare", "commit"], calls)

    def test_high_risk_tool_pauses_and_resumes_after_process_reconstruction(self):
        calls: list[str] = []
        llm = QueueLLM([
            {"thought": "write", "tool_call": {"name": "danger.write", "arguments": {"value": "A"}}},
            {"thought": "done", "final": "write completed"},
        ])
        spec = AgentSpec("policy-test", "Use the tool.", ("danger.write",), max_steps=4)
        gateway = PolicyGateway(PGApprovalStore(self.pool))
        sessions = self.sessions()

        first_engine = HarnessEngine(
            spec, llm, high_risk_registry(lambda value: calls.append(value) or {"saved": value}),
            sessions, policy_gateway=gateway, trace_writer=self.traces(),
        )
        paused = first_engine.run("s-1", "save A")

        self.assertEqual("approval_pending", paused.status)
        self.assertEqual([], calls)
        self.assertEqual("pending", gateway.store.get(paused.approval["id"]).status)
        self.assertEqual(["user"], [message.role for message in sessions.load("s-1").messages])

        gateway.store.decide(paused.approval["id"], True, "tester", "verified")
        restarted_engine = HarnessEngine(
            spec, llm, high_risk_registry(lambda value: calls.append(value) or {"saved": value}),
            sessions, policy_gateway=PolicyGateway(PGApprovalStore(self.pool)),
            trace_writer=self.traces(),
        )
        completed = restarted_engine.resume(paused.approval["id"])

        self.assertEqual("completed", completed.status)
        self.assertEqual("write completed", completed.answer)
        self.assertEqual(["A"], calls)
        self.assertEqual("executed", gateway.store.get(paused.approval["id"]).status)
        self.assertEqual(1, sum(message.role == "user" for message in sessions.load("s-1").messages))

    def test_rejection_finishes_without_executing_tool(self):
        calls: list[str] = []
        gateway = PolicyGateway(PGApprovalStore(self.pool))
        engine = HarnessEngine(
            AgentSpec("policy-test", "Use the tool.", ("danger.write",)),
            QueueLLM([{"tool_call": {"name": "danger.write", "arguments": {"value": "B"}}}]),
            high_risk_registry(lambda value: calls.append(value)),
            self.sessions(), policy_gateway=gateway, trace_writer=self.traces(),
        )
        paused = engine.run("s-2", "save B")
        gateway.store.decide(paused.approval["id"], False, "reviewer", "not allowed")
        rejected = engine.finalize_rejection(paused.approval["id"])

        self.assertEqual("rejected", rejected.status)
        self.assertEqual([], calls)
        self.assertIn("danger.write", rejected.answer)
        with self.assertRaises(PolicyError):
            engine.resume(paused.approval["id"])


class FakeWorkItemManager:
    def __init__(self, store) -> None:
        self.store = store

    def snapshots(self):
        return [SimpleNamespace(
            name="work-items", transport="test", server_name="Work Item MCP",
            server_version="test", protocol_version="test", tools=[], resources=(), prompts=(),
        )]

    def register_tools(self, registry: ToolRegistry, server_name: str):
        registry.register(Tool(
            "work-items.commit_issue", "Commit approved issue",
            {
                "type": "object",
                "properties": {"approval_id": {"type": "string"}},
                "required": ["approval_id"],
                "additionalProperties": False,
            },
            self.store.commit_issue,
            policy=ToolPolicy("high_write", True, "Creates a durable external work item"),
        ))
        return registry.names()


class WebApprovalTests(StoreTestCase):
    def test_web_decision_approves_domain_gate_then_resumes_harness(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_items = PGWorkItemStore(self.pool)
            remote = work_items.prepare_issue("Fix checkout", "Investigate repeated failures", "high")
            llm = QueueLLM([
                {"thought": "commit", "tool_call": {
                    "name": "work-items.commit_issue",
                    "arguments": {"approval_id": remote["approval_id"]},
                }},
                {"thought": "done", "final": "Issue created safely"},
            ])
            gateway = PolicyGateway(PGApprovalStore(self.pool))
            approver = User("alice", "user")
            app = BillGuardApp(
                root / "web", root / "docs", llm,
                FakeWorkItemManager(work_items), gateway, work_items,
                bills=PGBillService(self.pool),
                session_store=PGSessionStore(self.pool),
                evidence_store=PGEvidenceStore(self.pool),
                trace_store=PGTraceStore(self.pool),
                redis_client=self.redis,
            )

            paused = app.chat(approver, "approval-session", "$monthly-guard-report create issue")
            self.assertEqual("approval_pending", paused["status"])
            self.assertEqual("pending", work_items.approval(remote["approval_id"])["status"])

            result = app.decide_approval(approver, "approval-session", {
                "approval_id": paused["approval"]["id"],
                "decision": "approve",
            })

            self.assertEqual("completed", result["status"])
            self.assertEqual("executed", result["approval"]["status"])
            self.assertEqual("alice", result["approval"]["decided_by"])
            issues = work_items.list_issues()["items"]
            self.assertEqual(1, len(issues))
            self.assertEqual("alice", issues[0]["created_by"])


if __name__ == "__main__":
    unittest.main()
