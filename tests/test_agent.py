from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from minimal_agent.agents import FeedbackMockLLM, PLANNING_AGENT_SPEC, create_planning_agent
from minimal_agent.auth import User
from minimal_agent.feedback import FeedbackFilters, FeedbackService
from minimal_agent.harness import AgentSpec, HarnessEngine
from minimal_agent.llm import MockLLM, OpenAICompatibleLLM
from minimal_agent.parser import DecisionParseError, parse_decision
from minimal_agent.session import SessionStore
from minimal_agent.tools import DocumentService, TaskService, ToolError, build_planning_registry
from minimal_agent.types import Message, Session
from minimal_agent.web import PlanningWebApp


class ScriptedLLM:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = iter(outputs)

    def complete(self, messages, tools):
        return next(self.outputs)


class EndlessLLM:
    def complete(self, messages, tools):
        return json.dumps({"thought": "继续", "tool_call": {"name": "search", "arguments": {"query": "harness"}}})


class PlanningAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        (self.docs / "requirements.md").write_text(
            "实现 Harness 循环、Session 隔离、工具注册和测试。", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def agent(self, session_id="project-a", llm=None):
        return create_planning_agent(llm or MockLLM(), session_id, self.root / "state", self.docs)

    def registry(self, session_id="project-a"):
        return build_planning_registry(session_id, self.docs, self.root / "state")

    def test_direct_answer_and_capability_description(self):
        result = self.agent().run("project-a", "你能做什么？")
        self.assertIn("项目文档", result.answer)
        self.assertIn("结构化项目任务", result.answer)
        self.assertEqual(1, result.steps)

    def test_planning_tools_are_cohesive_and_weather_removed(self):
        names = set(self.registry().names())
        self.assertTrue({"read_doc", "search", "calculator", "task_create", "task_complete"} <= names)
        self.assertNotIn("weather", names)
        self.assertNotIn("todo_add", names)

    def test_read_document_then_create_multiple_tasks(self):
        result = self.agent().run("project-a", "读取 requirements.md 并拆分创建任务")
        self.assertEqual(4, result.steps)
        self.assertIn("本轮实际执行结果", result.answer)
        self.assertIn("T001", result.answer)
        self.assertIn("数据文件", result.answer)
        tasks = self.registry().execute("task_list", {})["tasks"]
        self.assertEqual(2, len(tasks))
        self.assertEqual(["T001", "T002"], [task["id"] for task in tasks])

    def test_document_path_is_sandboxed(self):
        docs = DocumentService(self.docs)
        with self.assertRaises(ToolError):
            docs.read_doc("../secret.txt")
        with self.assertRaises(ToolError):
            docs.read_doc("script.py")

    def test_calculator_tool_loop(self):
        result = self.agent().run("project-a", "计算项目工时 (12+8)*3")
        self.assertEqual("计算结果是 60。", result.answer)
        self.assertEqual(2, result.steps)

    def test_structured_task_crud_and_stable_ids(self):
        registry = self.registry()
        first = registry.execute("task_create", {"title": "设计 Harness", "priority": "high", "estimate_hours": 4})["created"]
        second = registry.execute("task_create", {"title": "编写测试"})["created"]
        self.assertEqual(("T001", "T002"), (first["id"], second["id"]))
        updated = registry.execute("task_update", {"task_id": "T001", "status": "in_progress", "notes": "已开始"})["updated"]
        self.assertEqual("in_progress", updated["status"])
        completed = registry.execute("task_complete", {"task_id": "T001"})["updated"]
        self.assertEqual("completed", completed["status"])
        registry.execute("task_delete", {"task_id": "T002"})
        self.assertEqual(["T001"], [task["id"] for task in registry.execute("task_list", {})["tasks"]])

    def test_task_state_survives_restart_and_sessions_are_isolated(self):
        self.agent("project-a").run("project-a", "创建任务：整理需求")
        reopened = self.agent("project-a")
        self.assertIn("整理需求", reopened.run("project-a", "列出任务").answer)
        other = self.agent("project-b")
        self.assertNotIn("整理需求", other.run("project-b", "列出任务").answer)

    def test_task_schema_rejects_invalid_priority(self):
        with self.assertRaises(ToolError):
            self.registry().execute("task_create", {"title": "x", "priority": "urgent"})

    def test_agent_spec_restricts_tool_capabilities(self):
        spec = AgentSpec("restricted", "test", ("calculator",), max_steps=2)
        llm = ScriptedLLM([json.dumps({"thought": "try", "tool_call": {"name": "search", "arguments": {"query": "x"}}}),
                           json.dumps({"thought": "done", "final": "stopped"})])
        engine = HarnessEngine(spec, llm, self.registry(), SessionStore(self.root / "restricted"))
        result = engine.run("s", "test")
        self.assertEqual("stopped", result.answer)

    def test_harness_emits_structured_hook_events(self):
        events = []
        registry = self.registry()
        engine = HarnessEngine(PLANNING_AGENT_SPEC, MockLLM(), registry,
                               SessionStore(self.root / "hook-state"), hooks=[events.append])
        engine.run("project-a", "计算 2+2")
        event_types = [event.event_type for event in events]
        self.assertIn("model_decision", event_types)
        self.assertIn("tool_start", event_types)
        self.assertIn("tool_end", event_types)
        self.assertEqual("PlanningAgent（项目规划助手）", events[0].data["agent"])

    def test_max_steps_stops_endless_loop(self):
        spec = AgentSpec("loop-test", "test", ("search",), max_steps=2)
        engine = HarnessEngine(spec, EndlessLLM(), self.registry(), SessionStore(self.root / "loop"))
        result = engine.run("s", "go")
        self.assertIn("最大执行步数", result.answer)
        self.assertEqual(2, result.steps)

    def test_invalid_model_output_is_handled(self):
        result = self.agent(llm=ScriptedLLM(["not json"])).run("project-a", "hello")
        self.assertIn("模型步骤失败", result.answer)

    def test_context_compression_keeps_recent_messages(self):
        store = SessionStore(self.root / "compressed", max_messages=4, keep_recent=2)
        session = Session("s1", [Message("user", f"m{i}") for i in range(6)])
        store.save(session)
        loaded = store.load("s1")
        self.assertTrue(loaded.summary)
        self.assertEqual(["m4", "m5"], [message.content for message in loaded.messages])

    def test_trace_contains_harness_events(self):
        result = self.agent().run("project-a", "计算 2+2")
        trace_files = list((self.root / "state" / "traces").glob("*.jsonl"))
        self.assertEqual(1, len(trace_files))
        text = trace_files[0].read_text(encoding="utf-8")
        self.assertIn(result.trace_id, text)
        self.assertIn("model_start", text)
        self.assertIn("tool_end", text)

    def test_parser_and_openai_wire_adapter(self):
        decision = parse_decision('prefix ```json\n{"thought":"x","final":"ok"}\n``` suffix')
        self.assertEqual("ok", decision.final)
        with self.assertRaises(DecisionParseError):
            parse_decision('{"thought":"missing action"}')
        wire = OpenAICompatibleLLM._wire_messages([
            {"role": "assistant", "content": "call", "tool_call_id": "abc"},
            {"role": "tool", "content": '{"ok":true}', "name": "read_doc", "tool_call_id": "abc"},
        ])
        self.assertNotIn("tool_call_id", wire[0])
        self.assertEqual("user", wire[1]["role"])

    def test_openai_client_retries_transport_failure_and_records_usage(self):
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("TLS EOF", request=request)
            payload = json.loads(request.content)
            self.assertEqual(0, payload["temperature"])
            return httpx.Response(200, request=request, json={
                "model": "provider/model-v1",
                "usage": {"total_tokens": 12},
                "choices": [{"message": {"content": '{"thought":"done","final":"ok"}'}}],
            })

        llm = OpenAICompatibleLLM(
            "test/model", api_key="test-key", temperature=0, max_retries=1,
            transport=httpx.MockTransport(handler),
        )
        try:
            result = llm.complete([{"role": "user", "content": "hello"}], [])
        finally:
            llm.close()
        self.assertIn('"final":"ok"', result)
        self.assertEqual(2, attempts)
        self.assertEqual(12, llm.last_usage["total_tokens"])
        self.assertEqual("provider/model-v1", llm.last_model)

    def test_parser_accepts_common_multi_call_variants(self):
        plural = parse_decision(json.dumps({"thought": "拆任务", "tool_calls": [
            {"function": {"name": "task_create", "arguments": '{"title":"任务 A"}'}},
            {"function": {"name": "task_create", "arguments": {"title": "任务 B"}}},
        ]}, ensure_ascii=False))
        self.assertEqual("task_create", plural.tool_call.name)
        self.assertEqual("任务 A", plural.tool_call.arguments["title"])
        array = parse_decision(json.dumps([
            {"thought": "first", "tool_call": {"name": "task_create", "arguments": {"title": "A"}}},
            {"thought": "second", "tool_call": {"name": "task_create", "arguments": {"title": "B"}}},
        ]))
        self.assertEqual("A", array.tool_call.arguments["title"])

    def test_parser_accepts_structured_final_and_task_list_is_presented(self):
        decision = parse_decision(json.dumps({"thought": "done", "final": {"tasks": [{"id": "T001"}]}}))
        self.assertIn("T001", decision.final)
        llm = ScriptedLLM([
            json.dumps({"tool_call": {"name": "task_list", "arguments": {}}}),
            json.dumps({"final": {"tasks": []}}),
        ])
        self.registry().execute("task_create", {"title": "确认需求", "estimate_hours": 3})
        result = self.agent(llm=llm).run("project-a", "列出任务")
        self.assertIn("T001", result.answer)
        self.assertIn("确认需求", result.answer)
        self.assertIn("3h", result.answer)

    def test_parser_accepts_bare_structured_final_from_json_mode(self):
        decision = parse_decision(json.dumps({
            "数据事实": {"反馈量": 24},
            "下一步建议": ["优先分析支付问题"],
        }, ensure_ascii=False))
        self.assertIn("反馈量", decision.final)
        self.assertIn("优先分析支付问题", decision.final)
        array = parse_decision(json.dumps([{"问题": "支付失败"}, {"问题": "订单异常"}], ensure_ascii=False))
        self.assertIn("订单异常", array.final)

    def test_trace_records_raw_model_output(self):
        self.agent().run("project-a", "计算 2+2")
        trace_file = next((self.root / "state" / "traces").glob("*.jsonl"))
        events = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
        outputs = [event for event in events if event["event"] == "model_output"]
        self.assertTrue(outputs)
        self.assertIn("tool_call", outputs[0]["raw"])

    @staticmethod
    def feedback_csv() -> str:
        return """ticket_id,created_at,product_module,content,customer_tier,status
TK-001,2026-08-01 10:00:00,支付,微信支付失败但已经扣款,高级,待处理
TK-002,2026-08-02 11:00:00,账户,一直收不到登录验证码,普通,处理中
TK-003,2026-08-03 12:00:00,支付,退款三天还没有到账,企业,已完成
"""

    def test_feedback_import_deduplicate_query_and_tag_audit(self):
        service = FeedbackService(self.root / "feedback-state")
        first = service.import_csv("feedback.csv", self.feedback_csv())
        self.assertEqual(3, first["imported_rows"])
        second = service.import_csv("feedback.csv", self.feedback_csv())
        self.assertEqual(3, second["duplicate_rows"])

        overview = service.overview()
        self.assertEqual(3, overview["total"])
        self.assertIn("支付问题", [item["name"] for item in overview["top_tags"]])
        payment = service.query(FeedbackFilters(product_module="支付"))
        self.assertEqual(2, payment["total"])
        sample = service.samples(tag="支付问题", limit=5)
        self.assertTrue(sample["pii_masked"])
        self.assertGreater(service.query(FeedbackFilters(query="支付问题"))["total"], 0)
        self.assertGreater(service.query(FeedbackFilters(query="登录问题"))["total"], 0)
        empty = service.samples(query="完全不存在的问题", limit=5)
        self.assertEqual(0, empty["matched"])
        self.assertIn("retry_hint", empty)

        updated = service.update_tags("TK-001", ["支付故障", "高优先级"], "tester")
        self.assertEqual(["支付故障", "高优先级"], updated["new_tags"])
        exported = service.export_csv(FeedbackFilters(tag="支付故障"))
        self.assertIn("TK-001", exported)
        self.assertNotIn("TK-002", exported)

        anomalies = service.anomalies(days=1, dimension="tag")
        self.assertEqual("2026-08-03", anomalies["anchor_date"])
        self.assertTrue(any(item["name"] == "退款问题" and item["is_new"] for item in anomalies["items"]))
        report = service.save_report("analysis-a", "本周洞察", "支付问题需要重点关注")
        self.assertEqual(1, len(service.reports()))
        service.delete_report(report["id"])
        self.assertEqual([], service.reports())

        rule = service.save_tag_rule("到账异常", ["没有到账", "未到账"], True, operator="tester")
        rematched = service.rematch_tags()
        self.assertEqual(3, rematched["feedback_count"])
        self.assertGreater(service.query(FeedbackFilters(tag="到账异常"))["total"], 0)
        service.save_tag_rule("到账异常", ["未到账"], False, tag_id=rule["id"], operator="tester")
        self.assertFalse(next(tag for tag in service.tags() if tag["id"] == rule["id"])["enabled"])
        self.assertGreaterEqual(len(service.tag_rule_audits()), 2)

        workflow = service.update_workflow(["TK-001", "TK-002"], "tester", status="处理中",
                                           priority="high", assignee="产品团队", internal_notes="正在排查")
        self.assertEqual(2, workflow["count"])
        high_priority = service.query(FeedbackFilters(priority="high"))
        self.assertEqual(2, high_priority["total"])
        self.assertEqual("产品团队", high_priority["items"][0]["assignee"])
        self.assertTrue(service.feedback_audits("TK-001"))

    def test_web_app_snapshot_chat_import_and_session_delete(self):
        app = PlanningWebApp(self.root / "web-state", self.docs, FeedbackMockLLM())
        user = User("tester", "approver")
        empty = app.snapshot(user, "web-project")
        self.assertEqual(0, empty["overview"]["total"])
        self.assertEqual("web-project", empty["sessions"][0]["id"])

        imported = app.import_feedback({"filename": "feedback.csv", "csv_text": self.feedback_csv()})
        self.assertEqual(3, imported["result"]["imported_rows"])
        result = app.chat(user, "web-project", "总结客户反馈中的主要问题")
        self.assertIn("3 条客户反馈", result["answer"])
        self.assertTrue(result["evidence"])
        queried = app.feedback_query({"filters": {"product_module": "支付"}})
        self.assertEqual(2, queried["total"])
        anomaly_result = app.feedback_anomalies({"days": 1, "dimension": "tag"})
        self.assertTrue(anomaly_result["items"])
        saved = app.save_report(user, "web-project", {"title": "洞察", "content": result["answer"]})
        self.assertEqual(1, len(saved["reports"]))
        removed = app.delete_report({"report_id": saved["report"]["id"]})
        self.assertEqual([], removed["reports"])

        restored = app.snapshot(user, "web-project")
        self.assertTrue(restored["messages"])
        assistant_messages = [item for item in restored["messages"] if item["role"] == "assistant"]
        self.assertTrue(assistant_messages[-1]["evidence"])
        self.assertEqual(3, restored["overview"]["total"])
        self.assertGreater(restored["sessions"][0]["message_count"], 0)

        trace_path = self.root / "web-state" / "feedback_sessions" / "traces" / f"{SessionStore._key('web-project')}.jsonl"
        self.assertTrue(trace_path.exists())
        deleted = app.delete_session(user, "web-project")
        self.assertTrue(deleted["deleted"])
        self.assertNotIn("web-project", [item["id"] for item in deleted["sessions"]])
        self.assertFalse(trace_path.exists())
        self.assertEqual(3, app.feedback.overview()["total"])


if __name__ == "__main__":
    unittest.main()
