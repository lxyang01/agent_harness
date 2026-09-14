from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from billguard.agents import BillMockLLM, create_bill_agent
from billguard.bills import BillService

DEMO = ("tx_id,paid_at,merchant,category,amount,method,note\n"
        "TX-1,2026-08-05 21:00:00,腾讯视频,订阅,25.0,微信,月费\n"
        "TX-2,2026-08-06 09:15:00,百度网盘,订阅,18.0,支付宝,月费\n"
        "TX-3,2026-08-06 09:20:00,百度网盘,订阅,18.0,支付宝,重复\n")


class MockReportTests(unittest.TestCase):
    def test_report_question_completes_with_four_sections(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp) / "bills")
            service.import_bills("d.csv", DEMO, owner="alice")
            agent = create_bill_agent(BillMockLLM(), "s1", temp,
                                      service.for_user("alice"))
            response = agent.run("s1", "生成本月守卫报告")
            self.assertEqual("completed", response.status, response.answer)
            for section in ("支出事实", "异常清单", "根因推测", "行动计划"):
                self.assertIn(section, response.answer)


class MockActionTests(unittest.TestCase):
    def test_cancel_subscription_walks_prepare_commit_approval(self):
        from billguard.policy import ApprovalStore, PolicyGateway, ToolPolicy
        from billguard.tools import Tool, ToolRegistry
        from billguard.web import BillGuardApp
        from billguard.auth import User
        from billguard.work_items import WorkItemStore

        class _WorkItemsManager:
            def __init__(self, store):
                self.store = store

            def snapshots(self):
                return [SimpleNamespace(name="work-items", transport="t",
                                        server_name="t", server_version="t",
                                        protocol_version="t", tools=(),
                                        resources=(), prompts=())]

            def register_tools(self, registry, name):
                # 还原生产拓扑:bill 工具与 work-items 工具同时在册
                registry.register(Tool(
                    "bill_overview", "Overview",
                    {"type": "object", "properties": {}, "required": [],
                     "additionalProperties": False},
                    lambda **kwargs: {"count": 1, "total_amount": 25.0, "pending": 0,
                                      "by_category": [], "top_merchants": []},
                    policy=ToolPolicy("read", True, "demo")))
                registry.register(Tool(
                    "work-items.prepare_issue", "Prepare",
                    {"type": "object",
                     "properties": {"title": {"type": "string"},
                                    "description": {"type": "string"},
                                    "priority": {"type": "string"}},
                     "required": ["title", "description", "priority"],
                     "additionalProperties": False},
                    self.store.prepare_issue,
                    policy=ToolPolicy("read", False, "Prepares a cancellable request")))
                registry.register(Tool(
                    "work-items.commit_issue", "Commit",
                    {"type": "object",
                     "properties": {"approval_id": {"type": "string"}},
                     "required": ["approval_id"], "additionalProperties": False},
                    self.store.commit_issue,
                    policy=ToolPolicy("high_write", True, "Creates durable work item")))
                return registry.names()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_items = WorkItemStore(root / "wi")
            gateway = PolicyGateway(ApprovalStore(root / "policy"))
            app = BillGuardApp(root / "web", root / "docs", BillMockLLM(),
                               _WorkItemsManager(work_items), gateway, work_items)
            alice = User("alice", "user")
            app.import_bills(alice, {"filename": "d.csv", "csv_text": DEMO})

            paused = app.chat(alice, "s1", "帮我取消腾讯视频订阅")
            self.assertEqual("approval_pending", paused["status"], paused.get("answer"))
            decision = app.decide_approval(alice, "s1", {
                "approval_id": paused["approval"]["id"], "decision": "approve"})
            self.assertEqual("completed", decision["status"], decision.get("answer"))
            issues = work_items.list_issues()["items"]
            self.assertEqual(1, len(issues))
            self.assertEqual("alice", issues[0]["created_by"])

    def test_action_without_workitems_tool_explains_gracefully(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp) / "bills")
            agent = create_bill_agent(BillMockLLM(), "s1", temp,
                                      service.for_user("alice"))
            response = agent.run("s1", "帮我取消腾讯视频订阅")
            self.assertEqual("completed", response.status)
            self.assertIn("mcp", response.answer.lower())


if __name__ == "__main__":
    unittest.main()


class MultiTurnTests(unittest.TestCase):
    def test_report_after_anomaly_chat_in_same_session(self):
        with tempfile.TemporaryDirectory() as temp:
            service = BillService(Path(temp) / "bills")
            agent = create_bill_agent(BillMockLLM(), "s1", temp,
                                      service.for_user("alice"))
            service.import_bills("d.csv", DEMO, owner="alice")
            first = agent.run("s1", "有没有重复扣费")
            self.assertEqual("completed", first.status)
            second = agent.run("s1", "生成本月守卫报告")
            self.assertEqual("completed", second.status, second.answer)
            for section in ("支出事实", "异常清单", "根因推测", "行动计划"):
                self.assertIn(section, second.answer)


class ApprovalContextTests(unittest.TestCase):
    def test_commit_pause_carries_work_item_context(self):
        """commit_issue 暂停时,审批数据应回查工单补全标题/说明,供人做决定。"""
        from types import SimpleNamespace
        from billguard.auth import User
        from billguard.policy import ApprovalStore, PolicyGateway, ToolPolicy
        from billguard.tools import Tool, ToolRegistry
        from billguard.web import BillGuardApp
        from billguard.work_items import WorkItemStore

        class _Manager:
            def __init__(self, store):
                self.store = store

            def snapshots(self):
                return [SimpleNamespace(name="work-items", transport="t", server_name="t",
                                        server_version="t", protocol_version="t",
                                        tools=(), resources=(), prompts=())]

            def register_tools(self, registry, name):
                registry.register(Tool(
                    "bill_overview", "O", {"type": "object", "properties": {},
                                           "required": [], "additionalProperties": False},
                    lambda **kw: {"count": 1, "total_amount": 25.0, "pending": 0,
                                  "by_category": [], "top_merchants": []},
                    policy=ToolPolicy("read", False, "o")))
                registry.register(Tool(
                    "work-items.prepare_issue", "P",
                    {"type": "object",
                     "properties": {"title": {"type": "string"},
                                    "description": {"type": "string"},
                                    "priority": {"type": "string"}},
                     "required": ["title", "description", "priority"],
                     "additionalProperties": False},
                    self.store.prepare_issue, policy=ToolPolicy("read", False, "p")))
                registry.register(Tool(
                    "work-items.commit_issue", "C",
                    {"type": "object", "properties": {"approval_id": {"type": "string"}},
                     "required": ["approval_id"], "additionalProperties": False},
                    self.store.commit_issue, policy=ToolPolicy("high_write", True, "c")))
                return registry.names()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work_items = WorkItemStore(root / "wi")
            gateway = PolicyGateway(ApprovalStore(root / "policy"))
            app = BillGuardApp(root / "web", root / "docs", BillMockLLM(),
                               _Manager(work_items), gateway, work_items)
            alice = User("alice", "user")
            app.import_bills(alice, {"filename": "d.csv", "csv_text": DEMO})

            paused = app.chat(alice, "s1", "帮我取消腾讯视频订阅")

            self.assertEqual("approval_pending", paused["status"])
            approval = paused["approval"]
            self.assertEqual("取消腾讯视频订阅", approval.get("action_title"))
            self.assertIn("取消", approval.get("action_description", ""))
            self.assertEqual("高", approval.get("action_priority"))


class MockActionIntentTests(unittest.TestCase):
    def test_cancel_intent_extracts_target_subscription(self):
        llm = BillMockLLM()
        decision = json.loads(llm.complete(
            [{"role": "user", "content": "帮我取消Keep的订阅"}],
            [{"name": "work-items.prepare_issue"}, {"name": "work-items.commit_issue"}]))
        self.assertEqual("取消Keep订阅", decision["tool_call"]["arguments"]["title"])

        decision = json.loads(llm.complete(
            [{"role": "user", "content": "取消订阅网易云音乐"}],
            [{"name": "work-items.prepare_issue"}]))
        self.assertEqual("取消网易云音乐订阅", decision["tool_call"]["arguments"]["title"])

        decision = json.loads(llm.complete(
            [{"role": "user", "content": "把iCloud取消订阅"}],
            [{"name": "work-items.prepare_issue"}]))
        self.assertEqual("取消iCloud订阅", decision["tool_call"]["arguments"]["title"])

    def test_cancel_intent_falls_back_when_no_target(self):
        llm = BillMockLLM()
        decision = json.loads(llm.complete(
            [{"role": "user", "content": "帮我取消订阅"}],
            [{"name": "work-items.prepare_issue"}]))
        self.assertIn("订阅", decision["tool_call"]["arguments"]["title"])  # 兜底仍可建单
