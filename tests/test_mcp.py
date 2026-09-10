from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import mcp

from minimal_agent.agents import FeedbackMockLLM, create_mcp_feedback_agent
from minimal_agent.feedback import FeedbackService
from minimal_agent.mcp_runtime import MCPClientManager, MCPError
from minimal_agent.tools import ToolRegistry
from minimal_agent.work_items import WorkItemStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def mcp_subprocess_env() -> dict[str, str]:
    dependency_root = str(Path(mcp.__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(PROJECT_ROOT), dependency_root,
        str(Path(dependency_root) / "win32"),
        str(Path(dependency_root) / "win32" / "lib"),
        env.get("PYTHONPATH", ""),
    ]))
    return env


class MCPIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.feedback_dir = self.root / "feedback"
        service = FeedbackService(self.feedback_dir)
        service.import_csv("feedback.csv", """ticket_id,created_at,product_module,content,customer_tier,status
MCP-001,2026-08-01 10:00:00,支付,支付失败但已经扣款,高级,待处理
MCP-002,2026-08-02 11:00:00,账户,一直收不到登录验证码,普通,处理中
""")
        self.audit: list[tuple[str, dict]] = []
        self.manager = MCPClientManager(
            request_timeout=15,
            audit_hook=lambda event, data: self.audit.append((event, data)),
        )
        self.snapshot = self.manager.connect_stdio(
            "feedback",
            sys.executable,
            ["-u", "-m", "minimal_agent.mcp_servers.feedback_server",
             "--data-dir", str(self.feedback_dir)],
            cwd=PROJECT_ROOT,
            env=mcp_subprocess_env(),
        )

    def tearDown(self) -> None:
        self.manager.close()
        self.temp.cleanup()

    def test_capability_discovery_tools_resources_and_prompts(self):
        self.assertEqual("stdio", self.snapshot.transport)
        self.assertEqual("Feedback Data MCP", self.snapshot.server_name)
        self.assertEqual({
            "aggregate", "query", "compare_periods", "detect_anomalies",
            "get_samples", "update_status",
        }, {tool.name for tool in self.snapshot.tools})
        self.assertIn("feedback://schema", self.snapshot.resources)
        self.assertIn("investigate-feedback-spike", self.snapshot.prompts)

        schema = self.manager.read_resource("feedback", "feedback://schema")
        self.assertIn("ticket_id", str(schema))
        prompt = self.manager.get_prompt(
            "feedback", "investigate-feedback-spike", {"days": "7", "dimension": "tag"},
        )
        self.assertIn("最近 7 天", str(prompt))

    def test_tool_call_and_registry_adapter_return_structured_data(self):
        overview = self.manager.call_tool("feedback", "aggregate", {})
        self.assertEqual(2, overview["total"])

        registry = ToolRegistry()
        registered = self.manager.register_tools(registry, "feedback")
        self.assertIn("feedback.aggregate", registered)
        queried = registry.execute(
            "feedback.query", {"product_module": "支付", "limit": 10}, allowed=registered,
        )
        self.assertEqual(1, queried["total"])
        self.assertTrue(queried["pii_masked"])
        self.assertTrue(any(event == "mcp_tool_end" for event, _ in self.audit))

    def test_harness_runs_with_dynamically_discovered_mcp_tools(self):
        agent = create_mcp_feedback_agent(
            FeedbackMockLLM(), "mcp-session", self.manager,
            self.root / "sessions", PROJECT_ROOT / "skills",
        )
        result = agent.run("mcp-session", "总结客户反馈中的主要问题")
        self.assertEqual(("feedback-triage",), result.active_skills)
        self.assertIn("2 条客户反馈", result.answer)
        trace = next((self.root / "sessions" / "traces").glob("*.jsonl")).read_text(encoding="utf-8")
        self.assertIn("feedback.aggregate", trace)

    def test_unknown_remote_tool_and_closed_manager_fail_safely(self):
        with self.assertRaises(MCPError):
            self.manager.call_tool("feedback", "not_advertised", {})
        self.manager.close()
        with self.assertRaises(MCPError):
            self.manager.call_tool("feedback", "aggregate", {})


class WorkItemMCPIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work_item_dir = self.root / "work-items"
        self.manager = MCPClientManager(request_timeout=15)
        self.snapshot = self.manager.connect_stdio(
            "work-items",
            sys.executable,
            ["-u", "-m", "minimal_agent.mcp_servers.work_item_server",
             "--data-dir", str(self.work_item_dir), "serve", "--transport", "stdio"],
            cwd=PROJECT_ROOT,
            env=mcp_subprocess_env(),
        )

    def tearDown(self) -> None:
        self.manager.close()
        self.temp.cleanup()

    def test_human_approval_is_outside_model_tool_channel(self):
        names = {tool.name for tool in self.snapshot.tools}
        self.assertEqual({"list_issues", "get_issue", "prepare_issue", "commit_issue"}, names)
        self.assertNotIn("approve", names)
        self.assertIn("work-items://schema", self.snapshot.resources)

        prepared = self.manager.call_tool("work-items", "prepare_issue", {
            "title": "排查支付扣款失败",
            "description": "根据反馈样本确认支付失败但已扣款。",
            "priority": "high",
            "evidence_refs": ["MCP-001"],
        })
        approval_id = prepared["approval_id"]
        self.assertEqual("pending", prepared["status"])
        with self.assertRaises(MCPError):
            self.manager.call_tool("work-items", "commit_issue", {"approval_id": approval_id})

        # This operation represents the web user's approval and is intentionally not an MCP tool.
        WorkItemStore(self.work_item_dir).decide(approval_id, True, "product-owner")
        created = self.manager.call_tool(
            "work-items", "commit_issue", {"approval_id": approval_id},
        )
        self.assertEqual("ISS-0001", created["created"]["id"])
        self.assertEqual("product-owner", created["created"]["created_by"])

        replay = self.manager.call_tool(
            "work-items", "commit_issue", {"approval_id": approval_id},
        )
        self.assertTrue(replay["idempotent_replay"])
        listed = self.manager.call_tool("work-items", "list_issues", {})
        self.assertEqual(1, listed["count"])


class StreamableHTTPMCPIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "minimal_agent.mcp_servers.work_item_server",
             "--data-dir", str(self.root / "work-items"), "serve",
             "--transport", "streamable-http", "--host", "127.0.0.1",
             "--port", str(self.port)],
            cwd=PROJECT_ROOT,
            env=mcp_subprocess_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail("Streamable HTTP MCP server exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            self.fail("Streamable HTTP MCP server did not open its port")
        self.manager = MCPClientManager(request_timeout=15)

    def tearDown(self) -> None:
        if hasattr(self, "manager"):
            self.manager.close()
        if hasattr(self, "process") and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.temp.cleanup()

    def test_remote_streamable_http_discovery_and_call(self):
        snapshot = self.manager.connect_streamable_http(
            "work-items-http", f"http://127.0.0.1:{self.port}/mcp",
        )
        self.assertEqual("streamable-http", snapshot.transport)
        self.assertIn("prepare_issue", {tool.name for tool in snapshot.tools})
        result = self.manager.call_tool("work-items-http", "list_issues", {})
        self.assertEqual(0, result["count"])


if __name__ == "__main__":
    unittest.main()
