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

from billguard.agents import BillMockLLM, create_mcp_bill_agent
from billguard.bills import BillService
from billguard.mcp_runtime import MCPClientManager, MCPError
from billguard.tools import ToolRegistry
from billguard.work_items import WorkItemStore


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


class BillServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bills_dir = self.root / "bills"
        service = BillService(self.bills_dir)
        service.import_bills("bills.csv", """tx_id,paid_at,merchant,category,amount,method,note
BG-001,2026-09-01 08:30:00,饿了么,餐饮,35.5,支付宝,午餐外卖
BG-002,2026-09-02 12:10:00,滴滴出行,交通,26.0,微信,打车到公司 联系电话 13812345678
BG-003,2026-09-03 19:45:00,美团,餐饮,88.0,支付宝,家庭晚餐
BG-004,2026-09-05 20:00:00,爱奇艺,订阅,35.0,支付宝,视频会员自动续费
""")
        service.import_subscriptions("subscriptions.csv", """name,merchant,cycle,expected_amount
视频会员,爱奇艺,月,25.0
""")
        self.audit: list[tuple[str, dict]] = []
        self.manager = MCPClientManager(
            request_timeout=15,
            audit_hook=lambda event, data: self.audit.append((event, data)),
        )
        self.snapshot = self.manager.connect_stdio(
            "bill",
            sys.executable,
            ["-u", "-m", "billguard.mcp_servers.bill_server",
             "--data-dir", str(self.bills_dir)],
            cwd=PROJECT_ROOT,
            env=mcp_subprocess_env(),
        )

    def tearDown(self) -> None:
        self.manager.close()
        self.temp.cleanup()

    def test_capability_discovery_tools_resources_and_prompts(self):
        self.assertEqual("stdio", self.snapshot.transport)
        self.assertEqual("BillGuard Data MCP", self.snapshot.server_name)
        self.assertEqual({
            "aggregate", "query", "compare_periods", "detect_anomalies",
            "get_samples", "update_status",
        }, {tool.name for tool in self.snapshot.tools})
        self.assertEqual({
            "bill://schema", "bill://categories", "bill://metric-definitions",
        }, set(self.snapshot.resources))
        self.assertEqual({
            "investigate-bill-anomaly", "monthly-guard-report",
        }, set(self.snapshot.prompts))

        schema = self.manager.read_resource("bill", "bill://schema")
        self.assertIn("tx_id", str(schema))
        categories = self.manager.read_resource("bill", "bill://categories")
        self.assertIn("餐饮", str(categories))
        prompt = self.manager.get_prompt(
            "bill", "investigate-bill-anomaly", {"days": "7", "dimension": "price_hike"},
        )
        self.assertIn("最近 7 天", str(prompt))
        self.assertIn("price_hike", str(prompt))

    def test_detect_anomalies_dimension_enum_and_price_hike_flag(self):
        tool = next(item for item in self.snapshot.tools if item.name == "detect_anomalies")
        self.assertEqual(
            ["spike", "duplicate", "price_hike", "outlier"],
            tool.input_schema["properties"]["dimension"]["enum"],
        )
        update = next(item for item in self.snapshot.tools if item.name == "update_status")
        self.assertEqual("high_write", update.policy.risk_level)
        self.assertTrue(update.policy.requires_approval)

        result = self.manager.call_tool("bill", "detect_anomalies", {
            "days": 7, "dimension": "price_hike", "limit": 10,
        })
        self.assertEqual("price_hike", result["dimension"])
        names = {item["name"] for item in result["items"]}
        self.assertIn("视频会员", names)
        flagged = next(item for item in result["items"] if item["name"] == "视频会员")
        self.assertEqual(25.0, flagged["evidence"]["expected_amount"])
        self.assertEqual(35.0, flagged["evidence"]["actual_amount"])
        self.assertIn("上涨", flagged["detail"])

    def test_query_and_get_samples_return_masked_notes(self):
        queried = self.manager.call_tool("bill", "query", {"query": "打车", "limit": 10})
        self.assertEqual(1, queried["total"])
        self.assertTrue(queried["pii_masked"])
        self.assertIn("[手机号]", queried["items"][0]["note"])
        self.assertNotIn("13812345678", queried["items"][0]["note"])

        sampled = self.manager.call_tool("bill", "get_samples", {
            "merchant": "滴滴出行", "limit": 5,
        })
        self.assertEqual(1, sampled["matched"])
        self.assertTrue(sampled["pii_masked"])
        self.assertIn("[手机号]", sampled["samples"][0]["note"])

    def test_aggregate_and_registry_adapter_return_structured_data(self):
        overview = self.manager.call_tool("bill", "aggregate", {})
        self.assertEqual(4, overview["count"])
        self.assertEqual(184.5, overview["total_amount"])
        self.assertEqual(0, overview["pending"])

        registry = ToolRegistry()
        registered = self.manager.register_tools(registry, "bill")
        self.assertIn("bill.aggregate", registered)
        queried = registry.execute(
            "bill.query", {"merchant": "美团", "limit": 10}, allowed=registered,
        )
        self.assertEqual(1, queried["total"])
        self.assertTrue(queried["pii_masked"])
        self.assertTrue(any(event == "mcp_tool_end" for event, _ in self.audit))

    def test_harness_runs_with_dynamically_discovered_mcp_tools(self):
        agent = create_mcp_bill_agent(
            BillMockLLM(), "mcp-session", self.manager,
            self.root / "sessions", PROJECT_ROOT / "skills",
        )
        result = agent.run("mcp-session", "最近有没有订阅涨价异常")
        self.assertEqual("completed", result.status)
        self.assertEqual(("anomaly-investigation",), result.active_skills)
        self.assertIn("视频会员", result.answer)
        trace = next((self.root / "sessions" / "traces").glob("*.jsonl")).read_text(encoding="utf-8")
        self.assertIn("bill.detect_anomalies", trace)

    def test_update_status_maps_to_workflow_with_status_enum(self):
        result = self.manager.call_tool("bill", "update_status", {
            "tx_ids": ["BG-001"], "status": "待核查",
            "operator": "mcp-test", "note": "金额需要核对",
        })
        self.assertEqual(["BG-001"], result["updated_tx_ids"])
        self.assertEqual(1, result["count"])
        self.assertEqual("待核查", result["changes"]["status"])

        audited = BillService(self.bills_dir).transaction_audits("BG-001")
        self.assertEqual("workflow", audited[-1]["action"])
        self.assertEqual("mcp-test", audited[-1]["operator"])

        with self.assertRaises(MCPError):
            self.manager.call_tool("bill", "update_status", {
                "tx_ids": ["BG-001"], "status": "无效状态", "operator": "mcp-test",
            })

    def test_unknown_remote_tool_and_closed_manager_fail_safely(self):
        with self.assertRaises(MCPError):
            self.manager.call_tool("bill", "not_advertised", {})
        self.manager.close()
        with self.assertRaises(MCPError):
            self.manager.call_tool("bill", "aggregate", {})


LEGACY_CSV = """tx_id,paid_at,merchant,category,amount,method,note
LEG-1,2026-09-01 09:00:00,水费中心,居住,40.0,微信,存量账单
LEG-2,2026-09-02 10:00:00,电费中心,居住,60.0,微信,存量账单
"""

ALICE_CSV = """tx_id,paid_at,merchant,category,amount,method,note
AL-1,2026-09-01 12:30:00,美团,餐饮,35.5,支付宝,午餐
AL-2,2026-09-03 20:00:00,爱奇艺,订阅,35.0,支付宝,视频会员
"""


class BillServerOwnerScopeTests(unittest.TestCase):
    """bill_server 的 owner 边界:空 owner 仅见存量 NULL 行,具名 owner 仅见本人行。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bills_dir = self.root / "bills"
        service = BillService(self.bills_dir)
        service.import_bills("legacy.csv", LEGACY_CSV)  # owner=None → 存量 NULL 行
        service.for_user("alice")  # 首访播种 alice 的默认类别
        service.import_bills("alice.csv", ALICE_CSV, owner="alice")
        self.manager = MCPClientManager(request_timeout=15)
        self.snapshot = self.manager.connect_stdio(
            "bill", sys.executable,
            ["-u", "-m", "billguard.mcp_servers.bill_server",
             "--data-dir", str(self.bills_dir)],
            cwd=PROJECT_ROOT, env=mcp_subprocess_env(),
        )

    def tearDown(self) -> None:
        self.manager.close()
        self.temp.cleanup()

    def test_default_and_empty_owner_see_only_legacy_null_rows(self):
        # 服务器广播的 schema 含 owner 参数(供 Host 注入,web 层再对模型隐藏)
        tool = next(item for item in self.snapshot.tools if item.name == "aggregate")
        self.assertIn("owner", tool.input_schema["properties"])
        self.assertNotIn("owner", tool.input_schema.get("required", []))
        # 不传 owner(默认形态)与显式空串同义:仅存量 NULL 视图
        for arguments in ({}, {"owner": ""}):
            with self.subTest(arguments=arguments):
                result = self.manager.call_tool("bill", "aggregate", arguments)
                self.assertEqual(2, result["count"])
                self.assertEqual(100.0, result["total_amount"])
                self.assertEqual({"水费中心", "电费中心"},
                                 {item["name"] for item in result["top_merchants"]})

    def test_named_owner_sees_only_own_rows(self):
        result = self.manager.call_tool("bill", "aggregate", {"owner": "alice"})
        self.assertEqual(2, result["count"])
        self.assertEqual(70.5, result["total_amount"])
        self.assertEqual({"美团", "爱奇艺"},
                         {item["name"] for item in result["top_merchants"]})
        # 具名 owner 的写入也按本人边界:存量行不在 alice 视野,更新 0 行不报错
        updated = self.manager.call_tool("bill", "update_status", {
            "tx_ids": ["LEG-1"], "status": "待核查", "operator": "alice",
            "owner": "alice",
        })
        self.assertEqual(0, updated["count"])
        # 空 owner 视图可更新存量行(存量数据的看护者语义)
        legacy_update = self.manager.call_tool("bill", "update_status", {
            "tx_ids": ["LEG-1"], "status": "待核查", "operator": "legacy-keeper",
        })
        self.assertEqual(1, legacy_update["count"])
        self.assertEqual(["LEG-1"], legacy_update["updated_tx_ids"])


class WorkItemMCPIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work_item_dir = self.root / "work-items"
        self.manager = MCPClientManager(request_timeout=15)
        self.snapshot = self.manager.connect_stdio(
            "work-items",
            sys.executable,
            ["-u", "-m", "billguard.mcp_servers.work_item_server",
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
            [sys.executable, "-u", "-m", "billguard.mcp_servers.work_item_server",
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
