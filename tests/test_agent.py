from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from billguard.agents import PLANNING_AGENT_SPEC, create_planning_agent
from tests.llm_doubles import FinalLLM, PlanningScriptLLM
from billguard.llm import OpenAICompatibleLLM
from billguard.auth import User
from billguard.bills import BillFilters
from billguard.harness import AgentSpec, HarnessEngine
from tests.llm_doubles import PlanningScriptLLM
from billguard.parser import DecisionParseError, parse_decision
from billguard.storage_pg import (
    PGBillService, PGEvidenceStore, PGSessionStore, PGTraceStore,
)
from billguard.tools import DocumentService, ToolError, build_planning_registry
from billguard.types import Message, Session
from billguard.web import BillGuardApp

from tests.conftest import StoreTestCase, seed_default_categories


class ScriptedLLM:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = iter(outputs)

    def complete(self, messages, tools):
        return next(self.outputs)


class EndlessLLM:
    def complete(self, messages, tools):
        return json.dumps({"thought": "继续", "tool_call": {"name": "search", "arguments": {"query": "harness"}}})


class PlanningAgentTests(StoreTestCase):
    """Planning 工具链(TaskService/DocumentService)为文件型服务(F5 范围内
    无 PG 对应物),保持文件机制;直接构造存储的用例已切 PG:
    SessionStore→PGSessionStore(引擎须同时注入 trace_writer=PGTraceStore)。"""

    def setUp(self) -> None:
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        (self.docs / "requirements.md").write_text(
            "实现 Harness 循环、Session 隔离、工具注册和测试。", encoding="utf-8"
        )
        self.addCleanup(self.temp.cleanup)

    def agent(self, session_id="project-a", llm=None):
        return create_planning_agent(llm or PlanningScriptLLM(), session_id, self.root / "state", self.docs)

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
        engine = HarnessEngine(spec, llm, self.registry(), PGSessionStore(self.pool),
                               trace_writer=PGTraceStore(self.pool))
        result = engine.run("s", "test")
        self.assertEqual("stopped", result.answer)

    def test_harness_emits_structured_hook_events(self):
        events = []
        registry = self.registry()
        engine = HarnessEngine(PLANNING_AGENT_SPEC, PlanningScriptLLM(), registry,
                               PGSessionStore(self.pool), hooks=[events.append],
                               trace_writer=PGTraceStore(self.pool))
        engine.run("project-a", "计算 2+2")
        event_types = [event.event_type for event in events]
        self.assertIn("model_decision", event_types)
        self.assertIn("tool_start", event_types)
        self.assertIn("tool_end", event_types)
        self.assertEqual("PlanningAgent（项目规划助手）", events[0].data["agent"])

    def test_max_steps_stops_endless_loop(self):
        spec = AgentSpec("loop-test", "test", ("search",), max_steps=2)
        engine = HarnessEngine(spec, EndlessLLM(), self.registry(), PGSessionStore(self.pool),
                               trace_writer=PGTraceStore(self.pool))
        result = engine.run("s", "go")
        self.assertIn("最大执行步数", result.answer)
        self.assertEqual(2, result.steps)

    def test_invalid_model_output_is_handled(self):
        result = self.agent(llm=ScriptedLLM(["not json"])).run("project-a", "hello")
        self.assertIn("模型步骤失败", result.answer)

    def test_engine_compression_keeps_recent_messages(self):
        # 压缩已上移至引擎层(按 AgentSpec 阈值),存储层只持久化
        from billguard.harness.engine import HarnessEngine
        session = Session("s1", [Message("user", f"m{i}") for i in range(6)])
        compressed = HarnessEngine.compress_history(session, keep_recent=2)
        self.assertEqual(["m4", "m5"], [message.content for message in compressed.messages])
        self.assertIn("m0", compressed.summary)
        store = PGSessionStore(self.pool)
        store.save(compressed)
        loaded = store.load("s1")
        self.assertEqual(loaded.messages, compressed.messages)  # save 不再隐藏改写
        self.assertEqual(compressed.summary, loaded.summary)

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
    def bills_csv() -> str:
        return """tx_id,paid_at,merchant,category,amount,method,note
TX-001,2026-09-01 08:30:00,美团外卖,餐饮,32.5,支付宝,午餐
TX-002,2026-09-02 09:00:00,滴滴出行,交通,25.0,微信支付,打车到公司
TX-003,2026-09-03 12:00:00,Apple Store,购物,899.0,信用卡,购买显示器
TX-004,2026-09-04 12:05:00,Apple Store,购物,899.0,信用卡,疑似重复扣款 订单号 SO-88990011
"""

    def test_bill_import_deduplicate_query_and_category_audit(self):
        # PG 版不在空库播种全局默认类别(spec 决策);owner=None 的根句柄导入
        # 依赖夹具对齐 SQLite 版“空库播种”契约,按类别名的断言语义保持不变
        seed_default_categories(self.pool)
        service = PGBillService(self.pool)
        first = service.import_bills("bills.csv", self.bills_csv())
        self.assertEqual(4, first["imported_rows"])
        second = service.import_bills("bills.csv", self.bills_csv())
        self.assertEqual(4, second["duplicate_rows"])
        self.assertEqual(2, len(service.imports()))

        overview = service.overview()
        self.assertEqual(4, overview["count"])
        self.assertEqual(1855.5, overview["total_amount"])
        self.assertEqual("Apple Store", overview["max_tx"]["merchant"])
        dining = service.query(BillFilters(category="餐饮"))
        self.assertEqual(1, dining["total"])
        sample = service.samples(category="餐饮", limit=5)
        self.assertTrue(sample["pii_masked"])
        self.assertGreater(service.query(BillFilters(query="显示器"))["total"], 0)
        empty = service.samples(query="完全不存在的商户", limit=5)
        self.assertEqual(0, empty["matched"])
        self.assertIn("retry_hint", empty)

        recategorized = service.update_transaction_category("TX-002", "订阅", "tester")
        self.assertEqual("交通", recategorized["old_category"])
        self.assertEqual("订阅", recategorized["new_category"])
        exported = service.export_csv(BillFilters(category="订阅"))
        self.assertIn("TX-002", exported)
        self.assertNotIn("TX-001", exported)

        duplicates = service.anomalies(days=31, dimension="duplicate")
        self.assertTrue(any(item["name"].startswith("Apple Store") for item in duplicates["items"]))
        report = service.save_report("analysis-a", "本周守卫", "购物类支出需要重点关注")
        self.assertEqual(1, len(service.reports()))
        service.delete_report(report["id"])
        self.assertEqual([], service.reports())

        rule = service.save_category("数码", ["显示器", "显卡"], True, operator="tester")
        rematched = service.rematch_categories(operator="tester")
        self.assertEqual(4, rematched["transactions"])
        self.assertGreater(service.query(BillFilters(category="数码"))["total"], 0)
        service.save_category("数码", ["显示器"], False, category_id=rule["id"], operator="tester")
        self.assertFalse(next(item for item in service.categories() if item["id"] == rule["id"])["enabled"])
        self.assertGreaterEqual(len(service.recent_audits()), 2)

        workflow = service.update_workflow(["TX-001", "TX-002"], "tester", status="待核查", note="批量核查")
        self.assertEqual(2, workflow["count"])
        pending = service.query(BillFilters(status="待核查"))
        self.assertEqual(2, pending["total"])
        audits = service.transaction_audits("TX-001")
        self.assertEqual("tester", audits[-1]["operator"])
        self.assertIn("批量核查", audits[-1]["new_value"])

    def test_web_app_snapshot_chat_import_and_session_delete(self):
        app = BillGuardApp(
            self.root / "web-state", self.docs, FinalLLM(),
            bills=PGBillService(self.pool),
            session_store=PGSessionStore(self.pool),
            evidence_store=PGEvidenceStore(self.pool),
            trace_store=PGTraceStore(self.pool),
            redis_client=self.redis,
        )
        user = User("tester", "user")
        empty = app.snapshot(user, "web-project")
        self.assertEqual(0, empty["overview"]["count"])
        self.assertEqual("web-project", empty["sessions"][0]["id"])
        self.assertTrue(empty["categories"])  # 空库也播种默认类别
        self.assertIn("items", empty["bills"])

        imported = app.import_bills(user, {"filename": "bills.csv", "csv_text": self.bills_csv()})
        self.assertEqual(4, imported["result"]["imported_rows"])
        from tests.llm_doubles import ScriptedLLM
        scripted_app = BillGuardApp(
            self.root / "web-state", self.docs, ScriptedLLM([
                {"thought": "先查总览", "tool_call": {"name": "bill_overview", "arguments": {}}},
                {"thought": "done", "final": "当前共 4 笔支出,其中 Apple Store 2 笔。"},
            ]),
            bills=PGBillService(self.pool),
            session_store=PGSessionStore(self.pool),
            evidence_store=PGEvidenceStore(self.pool),
            trace_store=PGTraceStore(self.pool),
            redis_client=self.redis,
        )
        result = scripted_app.chat(user, "web-project", "总结一下当前的支出情况")
        self.assertIn("4 笔支出", result["answer"])
        self.assertTrue(result["evidence"])
        queried = app.bill_query(user, {"filters": {"merchant": "Apple Store"}})
        self.assertEqual(2, queried["total"])
        anomaly_result = app.bill_anomalies(user, {"days": 31, "dimension": "duplicate"})
        self.assertTrue(anomaly_result["items"])
        saved = app.save_report(user, "web-project", {"title": "守卫报告", "content": result["answer"]})
        self.assertEqual(1, len(saved["reports"]))
        removed = app.delete_report(user, {"report_id": saved["report"]["id"]})
        self.assertEqual([], removed["reports"])

        restored = app.snapshot(user, "web-project")
        self.assertTrue(restored["messages"])
        assistant_messages = [item for item in restored["messages"] if item["role"] == "assistant"]
        self.assertTrue(assistant_messages[-1]["evidence"])
        self.assertEqual(4, restored["overview"]["count"])
        self.assertGreater(restored["sessions"][0]["message_count"], 0)

        # 原断言检查本地 trace JSONL 文件存在/删除(文件机制);PG 等价可观察量:
        # PGTraceStore 的 run 列表在删除会话前后由非空变空
        traces = PGTraceStore(self.pool)
        self.assertEqual(1, len(traces.list_runs("web-project")))
        deleted = app.delete_session(user, "web-project")
        self.assertTrue(deleted["deleted"])
        self.assertNotIn("web-project", [item["id"] for item in deleted["sessions"]])
        self.assertEqual([], traces.list_runs("web-project"))
        self.assertEqual(4, app.bills.overview()["count"])


if __name__ == "__main__":
    unittest.main()
