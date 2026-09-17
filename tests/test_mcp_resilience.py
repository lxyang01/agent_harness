"""MCP 韧性测试:有界重连(指数退避)+ 熔断 + 降级 + /health 就绪路由。

场景全部走 streamable-http 传输(生产分布式形态);重连/熔断代码路径对
stdio 与 http 共享(见 mcp_runtime._reconnect),此处只按需演练 http。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from billguard.harness import AgentSpec, HarnessEngine
from billguard.mcp_runtime import MCPClientManager, MCPCircuitOpenError, MCPError
from billguard.session import SessionStore
from billguard.tools import ToolRegistry
from tests.llm_doubles import ScriptedLLM
from tests.test_mcp import PROJECT_ROOT, mcp_subprocess_env

SERVER = "work-items"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_port(port: int, process: subprocess.Popen, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def spawn_work_item_server(root: Path, port: int) -> subprocess.Popen:
    """在指定端口拉起 work_item_server(streamable-http);绑定失败自动重试。"""
    env = mcp_subprocess_env()
    env.pop("BILLGUARD_PG_DSN", None)  # SQLite 分支:不依赖 compose PostgreSQL
    for _ in range(3):
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "billguard.mcp_servers.work_item_server",
             "--data-dir", str(root / "work-items"), "serve",
             "--transport", "streamable-http", "--host", "127.0.0.1",
             "--port", str(port)],
            cwd=PROJECT_ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if _wait_port(port, process):
            return process
        process.kill()
        process.wait(timeout=5)
        time.sleep(0.2)
    raise AssertionError(f"work_item_server did not open port {port}")


class ResilienceTestCase(unittest.TestCase):
    """公共夹具:真实 http 子进程服务器 + 可注入重试参数的管理器。"""

    # 生产缺省:重连 3 次(1s/2s/4s),熔断 60s;测试注入毫秒级退避。
    # 连接期用宽超时(系统代理抖动),调用期收紧到 1s:每个失败阶段(初始
    # 调用 + 3 次重连)至多一个超时窗口,整轮熔断触发 < 6s,可断言有界性。
    ATTEMPTS = 3
    BACKOFF = 0.01
    COOLDOWN = 60.0
    CONNECT_TIMEOUT = 10.0
    REQUEST_TIMEOUT = 1.0

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.port = _free_port()
        self.process = spawn_work_item_server(self.root, self.port)
        self.addCleanup(self._stop_server)
        self.audit: list[tuple[str, dict]] = []
        self.manager = MCPClientManager(
            request_timeout=self.CONNECT_TIMEOUT,
            reconnect_attempts=self.ATTEMPTS,
            reconnect_backoff_base=self.BACKOFF,
            circuit_cooldown=self.COOLDOWN,
            audit_hook=lambda event, data: self.audit.append((event, data)),
        )
        self.addCleanup(self.manager.close)
        # 建连(initialize+发现)给宽窗口——本机系统代理延迟偶发超 1s;
        # 连接成功后收紧调用窗口,让死服路径的每个失败阶段都快速有界
        self.manager.connect_streamable_http(SERVER, f"http://127.0.0.1:{self.port}/mcp")
        self.manager.request_timeout = self.REQUEST_TIMEOUT
        self.assertEqual(0, self.manager.call_tool(SERVER, "list_issues", {})["count"])

    def _stop_server(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)

    def _kill_server(self) -> None:
        self.process.kill()
        self.process.wait(timeout=5)

    def events(self) -> list[str]:
        return [event for event, _ in self.audit]


class CircuitBreakerTests(ResilienceTestCase):
    def test_connection_failure_trips_circuit_after_bounded_retries(self):
        """服务死亡:连接类错误触发 3 次指数退避重连,耗尽后进入 OPEN。"""
        self._kill_server()
        started = time.perf_counter()
        with self.assertRaises(MCPError) as ctx:
            self.manager.call_tool(SERVER, "list_issues", {})
        tripped_elapsed = time.perf_counter() - started
        # 有界性:初始失败 + 3 次重连各至多一个 request_timeout(1s)+ 退避
        # 0.07s,总耗时 < 6s(生产缺省上界 ≈ 4×20s+7s,同构的有界模式)
        self.assertLess(tripped_elapsed, 6.0)
        self.assertIn("暂时不可用", str(ctx.exception))
        self.assertIn("熔断", str(ctx.exception))
        self.assertEqual("open", self.manager.circuit_state(SERVER))
        self.assertEqual(self.ATTEMPTS, self.events().count("mcp_retry"))
        self.assertIn("mcp_circuit_open", self.events())

    def test_open_circuit_fails_fast_without_network(self):
        """OPEN 期间调用快速失败:不再重连、不发起网络调用,报文可读。"""
        self._kill_server()
        with self.assertRaises(MCPError):
            self.manager.call_tool(SERVER, "list_issues", {})
        retries_after_trip = self.events().count("mcp_retry")

        started = time.perf_counter()
        with self.assertRaises(MCPCircuitOpenError) as ctx:
            self.manager.call_tool(SERVER, "list_issues", {})
        fast_elapsed = time.perf_counter() - started
        self.assertLess(fast_elapsed, 0.5)  # 无退避、无网络:纯状态机拒绝
        self.assertIn("熔断中", str(ctx.exception))
        self.assertIn("剩余", str(ctx.exception))
        self.assertIn("请稍后重试", str(ctx.exception))
        self.assertEqual("open", self.manager.circuit_state(SERVER))
        # 快速失败不新增重连尝试(未发起网络调用)
        self.assertEqual(retries_after_trip, self.events().count("mcp_retry"))


class HalfOpenRecoveryTests(ResilienceTestCase):
    COOLDOWN = 0.2  # 冷却窗口缩短,测试探测路径

    def test_half_open_probe_success_closes_circuit(self):
        """冷却期满 → HALF-OPEN 放行一次探测;复活的服务使其 CLOSED。"""
        self._kill_server()
        with self.assertRaises(MCPError):
            self.manager.call_tool(SERVER, "list_issues", {})
        self.assertEqual("open", self.manager.circuit_state(SERVER))

        self.process = spawn_work_item_server(self.root, self.port)  # 同端口复活
        # 探测要做一次完整重连(initialize+发现):恢复连接期宽窗口
        self.manager.request_timeout = self.CONNECT_TIMEOUT
        time.sleep(self.COOLDOWN + 0.1)  # 越过冷却窗口

        result = self.manager.call_tool(SERVER, "list_issues", {})  # 探测调用
        self.assertEqual(0, result["count"])
        self.assertEqual("closed", self.manager.circuit_state(SERVER))
        self.assertIn("mcp_circuit_half_open", self.events())
        self.assertIn("mcp_reconnected", self.events())
        # 恢复后的常规调用:不再产生半开/熔断事件
        self.assertEqual(0, self.manager.call_tool(SERVER, "list_issues", {})["count"])
        self.assertEqual("closed", self.manager.circuit_state(SERVER))
        self.assertEqual(1, self.events().count("mcp_circuit_half_open"))

    def test_half_open_probe_failure_reopens_circuit(self):
        """探测仍失败 → 重新 OPEN,冷却窗口从头计。"""
        self._kill_server()
        with self.assertRaises(MCPError):
            self.manager.call_tool(SERVER, "list_issues", {})
        self.manager.request_timeout = self.CONNECT_TIMEOUT  # 探测=重连,给宽窗口
        time.sleep(self.COOLDOWN + 0.1)  # 冷却期满,但服务仍未复活

        with self.assertRaises(MCPError) as ctx:
            self.manager.call_tool(SERVER, "list_issues", {})
        self.assertIn("熔断", str(ctx.exception))
        self.assertEqual("open", self.manager.circuit_state(SERVER))
        # 立即再调:仍是 OPEN 快速失败(冷却重计,不再放探测)
        with self.assertRaises(MCPCircuitOpenError):
            self.manager.call_tool(SERVER, "list_issues", {})


class DegradationTests(ResilienceTestCase):
    def test_open_circuit_degrades_registry_call_to_structured_error(self):
        """熔断中:注册表形态(模型工具调用)返回降级结构,不抛异常。"""
        registry = ToolRegistry()
        registered = self.manager.register_tools(registry, SERVER)
        self._kill_server()
        with self.assertRaises(MCPError):
            self.manager.call_tool(SERVER, "list_issues", {})  # 驱动熔断 OPEN

        result = registry.execute(
            "work-items.list_issues", {}, allowed=registered)
        self.assertTrue(result["degraded"])
        self.assertIn("熔断", result["error"])
        # 非熔断类错误不降级:远端工具业务错误仍按原语义抛出(经注册表包为 ToolError)
        # (此处熔断已拦截,业务错误路径由 test_mcp.py 覆盖)

    def test_degraded_tool_call_keeps_agent_run_alive(self):
        """熔断中的工具调用不中断 Agent 循环:模型拿到降级载荷后正常收尾。"""
        registry = ToolRegistry()
        registered = self.manager.register_tools(registry, SERVER)
        self._kill_server()
        with self.assertRaises(MCPError):
            self.manager.call_tool(SERVER, "list_issues", {})  # 驱动熔断 OPEN

        engine = HarnessEngine(
            AgentSpec(
                name="韧性测试助手",
                instructions="测试 Agent",
                tool_names=registered,
                max_steps=4,
            ),
            ScriptedLLM([
                {"thought": "查工单", "tool_call": {
                    "name": "work-items.list_issues", "arguments": {}}},
                {"thought": "服务不可用,如实告知",
                 "final": "工单服务暂时不可用(熔断中),请稍后重试。"},
            ]),
            registry,
            SessionStore(self.root / "sessions"),
        )
        response = engine.run("resilience-session", "列出我的行动项")
        self.assertEqual("completed", response.status)
        self.assertIn("暂时不可用", response.answer)


class HealthRouteTests(unittest.TestCase):
    """/health 就绪路由:两个 MCP 服务器 streamable-http 形态都返回 200 {"ok": true}。"""

    def _get(self, port: int) -> tuple[int, dict]:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_bill_server_health_route(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            port = _free_port()
            env = mcp_subprocess_env()
            env.pop("BILLGUARD_PG_DSN", None)
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "billguard.mcp_servers.bill_server",
                 "--data-dir", str(root / "bills"), "--transport", "streamable-http",
                 "--host", "127.0.0.1", "--port", str(port)],
                cwd=PROJECT_ROOT, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: (process.kill(), process.wait(timeout=5)))
            self.assertTrue(_wait_port(port, process), "bill_server 未开放端口")
            status, body = self._get(port)
            self.assertEqual(200, status)
            self.assertEqual({"ok": True}, body)

    def test_work_item_server_health_route(self):
        with tempfile.TemporaryDirectory() as temp:
            port = _free_port()
            process = spawn_work_item_server(Path(temp), port)
            self.addCleanup(lambda: (process.kill(), process.wait(timeout=5)))
            status, body = self._get(port)
            self.assertEqual(200, status)
            self.assertEqual({"ok": True}, body)


if __name__ == "__main__":
    unittest.main()
