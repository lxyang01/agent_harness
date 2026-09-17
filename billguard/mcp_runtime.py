from __future__ import annotations

import asyncio
import concurrent.futures
import json
import math
import re
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Coroutine, TypeVar

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Prompt, Resource, TextContent, Tool as MCPTool

from .tools import Tool, ToolRegistry
from .policy import ToolPolicy


_SERVER_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
T = TypeVar("T")
AuditHook = Callable[[str, dict[str, Any]], None]


class MCPError(RuntimeError):
    """Raised when an MCP transport, protocol, or remote tool operation fails."""


class MCPCircuitOpenError(MCPError):
    """熔断拒绝/重连进行中:服务暂不可用,调用被快速失败(不发起网络调用)。

    注册表 handler(_make_handler)捕获本类型并转为 {"error": ..., "degraded": true}
    结构化结果,使 Agent 循环存活并能让模型给出降级话术;其余 MCPError 保持
    原语义(经 ToolRegistry 包为 ToolError → tool_error 事件)。"""


# 连接类错误判定:异常链上出现这些类型(或报文特征)才触发重连/熔断;
# 工具级业务错误(result.isError → MCPError,无此类 cause/报文)不在此列。
# 实测依赖:http 服务器死亡时 session.call_tool 抛上游
# McpError("Timed out while waiting for response ..."),外层 _submit 超时的
# cause 是 TimeoutError——两者都是"服务不可达"的信号,计入连接类。
_CONNECTION_ERROR_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,
    EOFError,
    TimeoutError,                 # 外层 _submit 超时(3.11+ concurrent.futures 同源)
    httpx.TransportError,         # ConnectError / RemoteProtocolError / ReadTimeout ...
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
)
_CONNECTION_MESSAGE_HINTS = (
    "connection closed", "server disconnected", "peer closed",
    "connection refused", "connection reset", "connection aborted",
    "connect call failed", "closedresource", "brokenresource",
    "timed out while waiting for response",  # mcp.shared.exceptions.McpError 超时
)


@dataclass(frozen=True)
class MCPToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    policy: ToolPolicy


@dataclass(frozen=True)
class MCPServerSnapshot:
    name: str
    transport: str
    server_name: str
    server_version: str
    protocol_version: str
    tools: tuple[MCPToolInfo, ...]
    resources: tuple[str, ...]
    prompts: tuple[str, ...]


@dataclass
class _Connection:
    stack: AsyncExitStack
    session: ClientSession
    snapshot: MCPServerSnapshot


@dataclass
class _ServerSpec:
    """重连所需的全部连接参数(两种传输共用同一重连路径)。"""
    transport: str                       # "stdio" | "streamable-http"
    stdio_params: StdioServerParameters | None = None
    http_url: str = ""
    http_headers: dict[str, str] | None = None


class _CircuitState:
    """单服务器熔断状态机:closed → open(冷却)→ half_open(一次探测)→ closed。

    状态迁移只在持 _state_lock 的临界区内发生;重连期间的 I/O 与退避睡眠
    在锁外执行(recovering 标志让并发调用者快速失败而非阻塞等待)。
    """

    __slots__ = ("state", "opened_at", "half_open_at", "recovering")

    def __init__(self) -> None:
        self.state = "closed"      # closed | open | half_open
        self.opened_at = 0.0       # open 起始时刻(monotonic)
        self.half_open_at = 0.0    # 进入 half_open 的时刻(过期兜底用)
        self.recovering = False    # 有界重连或半开探测正在进行


def _make_handler(manager: "MCPClientManager", server: str, tool: str) -> Callable[..., Any]:
    """闭包工厂:handler 只接受 **arguments,没有任何具名参数——模型显式传
    _server/_tool 同名关键字无处绑定,无法重路由到其他服务器/工具(与
    web._make_owner_wrapper 同构)。

    熔断类失败(MCPCircuitOpenError)在此转为结构化降级结果而不是抛出:
    Agent 循环存活,模型可基于 {"error": ..., "degraded": true} 给出"服务
    暂不可用"的降级回答。降级结果的记账由引擎按风险分级处理:读/低写工具
    记为成功(保证运行能以降级话术收尾);高写工具由 HarnessEngine 记为
    未执行(审批恢复路径 mark_execution(False),审计不落"已执行");
    其余 MCPError 保持抛出(注册表包为 ToolError → 既有 tool_error 路径)。"""
    def handler(**arguments: Any) -> Any:
        try:
            return manager.call_tool(server, tool, arguments)
        except MCPCircuitOpenError as exc:
            return {"error": str(exc), "degraded": True}
    return handler


class MCPClientManager:
    """Persistent MCP connections behind a synchronous, timeout-bounded facade."""

    def __init__(self, request_timeout: float = 20.0,
                 audit_hook: AuditHook | None = None,
                 reconnect_attempts: int = 3,
                 reconnect_backoff_base: float = 1.0,
                 circuit_cooldown: float = 60.0,
                 half_open_expiry: float = 30.0) -> None:
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if reconnect_attempts < 1:
            raise ValueError("reconnect_attempts must be >= 1")
        if reconnect_backoff_base < 0:
            raise ValueError("reconnect_backoff_base must be non-negative")
        if circuit_cooldown <= 0:
            raise ValueError("circuit_cooldown must be positive")
        if half_open_expiry <= 0:
            raise ValueError("half_open_expiry must be positive")
        self.request_timeout = request_timeout
        self.audit_hook = audit_hook
        # 韧性参数(生产缺省:重连 3 次,退避 1s/2s/4s;熔断冷却 60s;
        # 半开探测异常中断后的过期兜底 30s——防止 recovering=False 的
        # half_open 永久快速失败)
        self.reconnect_attempts = reconnect_attempts
        self.reconnect_backoff_base = reconnect_backoff_base
        self.circuit_cooldown = circuit_cooldown
        self.half_open_expiry = half_open_expiry
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self._connections: dict[str, _Connection] = {}
        self._snapshots: dict[str, MCPServerSnapshot] = {}
        self._specs: dict[str, _ServerSpec] = {}
        self._circuits: dict[str, _CircuitState] = {}
        # 调用来自 HTTP 工作线程:状态机迁移全程持锁;I/O/退避在锁外
        self._state_lock = threading.RLock()
        self._thread = threading.Thread(target=self._run_loop, name="mcp-client-loop", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise MCPError("MCP event loop did not start")

    def connect_stdio(self, name: str, command: str, args: list[str] | None = None,
                      cwd: str | Path | None = None,
                      env: dict[str, str] | None = None) -> MCPServerSnapshot:
        self._validate_new_name(name)
        params = StdioServerParameters(
            command=command,
            args=list(args or []),
            cwd=Path(cwd) if cwd is not None else None,
            env=env,
            encoding="utf-8",
            encoding_error_handler="strict",
        )
        with self._state_lock:
            self._specs[name] = _ServerSpec("stdio", stdio_params=params)
            self._circuits[name] = _CircuitState()
        snapshot = self._submit(self._connect_stdio(name, params))
        self._snapshots[name] = snapshot
        return snapshot

    def connect_streamable_http(self, name: str, url: str,
                                headers: dict[str, str] | None = None) -> MCPServerSnapshot:
        self._validate_new_name(name)
        with self._state_lock:
            self._specs[name] = _ServerSpec("streamable-http", http_url=url,
                                            http_headers=dict(headers or {}))
            self._circuits[name] = _CircuitState()
        snapshot = self._submit(self._connect_http(name, url, headers or {}))
        self._snapshots[name] = snapshot
        return snapshot

    def circuit_state(self, server_name: str) -> str:
        """只读观测:closed / open / half_open(未知服务器视为 closed)。"""
        with self._state_lock:
            circuit = self._circuits.get(server_name)
            return circuit.state if circuit is not None else "closed"

    def snapshots(self) -> list[MCPServerSnapshot]:
        return [self._snapshots[name] for name in sorted(self._snapshots)]

    def register_tools(self, registry: ToolRegistry, server_name: str) -> tuple[str, ...]:
        snapshot = self._snapshot(server_name)
        registered: list[str] = []
        for remote in snapshot.tools:
            local_name = f"{server_name}.{remote.name}"
            registry.register(Tool(
                local_name,
                f"[MCP:{server_name}] {remote.description}".strip(),
                remote.input_schema,
                _make_handler(self, server_name, remote.name),
                policy=remote.policy,
            ))
            registered.append(local_name)
        return tuple(registered)

    def call_tool(self, server_name: str, tool_name: str,
                  arguments: dict[str, Any] | None = None) -> Any:
        self._audit("mcp_tool_start", server=server_name, tool=tool_name,
                    arguments=arguments or {})
        try:
            result = self._guarded_call(server_name, tool_name, arguments or {})
        except Exception as exc:
            self._audit("mcp_tool_error", server=server_name, tool=tool_name, error=str(exc))
            raise
        self._audit("mcp_tool_end", server=server_name, tool=tool_name, result=result)
        return result

    # ------------------------------------------------------------------
    # 韧性:有界重连(指数退避)+ 熔断(CLOSED/OPEN/HALF_OPEN)+ 降级入口
    # ------------------------------------------------------------------

    def _guarded_call(self, server_name: str, tool_name: str,
                      arguments: dict[str, Any]) -> Any:
        """单次调用的韧性外壳:先过熔断闸门,失败按错误类别走重连或原样抛出。"""
        mode = self._admit_call(server_name)   # closed | half_open;拒绝时抛 MCPCircuitOpenError
        if mode == "half_open" and server_name not in self._connections:
            # 重连耗尽时连接已被拆除:半开探测 = 一次"重连 + 调用"
            return self._recover_and_retry(
                server_name, tool_name, arguments, mode,
                MCPError(f"MCP connection to {server_name} was dropped"))
        try:
            result = self._submit(self._call_tool(server_name, tool_name, arguments))
        except MCPError as exc:
            if not self._is_connection_error(exc):
                if mode == "half_open":
                    # 半开探测中服务可达但本次调用失败(如参数错误):连接已
                    # 证明恢复,关闭熔断后按原语义抛出
                    self._mark_recovered(server_name)
                raise
            return self._recover_and_retry(server_name, tool_name, arguments, mode, exc)
        if mode == "half_open":
            # 探测成功(无需重连,连接仍存活):熔断关闭
            self._mark_recovered(server_name)
        return result

    def _admit_call(self, server: str) -> str:
        """熔断闸门(线程安全):返回 closed/half_open,拒绝时抛 MCPCircuitOpenError。"""
        with self._state_lock:
            circuit = self._circuits.get(server)
            if circuit is None:
                return "closed"
            if circuit.recovering:
                raise MCPCircuitOpenError(f"MCP 服务 {server} 正在重连,请稍后重试")
            if circuit.state == "open":
                remaining = circuit.opened_at + self.circuit_cooldown - time.monotonic()
                if remaining > 0:
                    raise MCPCircuitOpenError(
                        f"MCP 服务 {server} 暂时不可用(熔断中,剩余 {math.ceil(remaining)} 秒),"
                        "请稍后重试")
                circuit.state = "half_open"
                circuit.half_open_at = time.monotonic()
                circuit.recovering = True  # 半开只放一次探测,其余调用快速失败
                self._audit("mcp_circuit_half_open", server=server)
                return "half_open"
            if circuit.state == "half_open":
                if (not circuit.recovering
                        and time.monotonic() - circuit.half_open_at > self.half_open_expiry):
                    # 探测线程异常中断(BaseException 越过 finally 回流)留下的
                    # 过期半开状态:重新放行一次探测,避免永久快速失败
                    circuit.half_open_at = time.monotonic()
                    circuit.recovering = True
                    self._audit("mcp_circuit_half_open", server=server, expired=True)
                    return "half_open"
                raise MCPCircuitOpenError(f"MCP 服务 {server} 正在探测恢复,请稍后重试")
            return "closed"

    def _recover_and_retry(self, server: str, tool_name: str,
                           arguments: dict[str, Any], mode: str,
                           failure: MCPError) -> Any:
        """连接类失败后的有界恢复。

        closed:退避 base*2^n(n=0..attempts-1,生产 1s/2s/4s)逐次"重连+重试调用",
        全部失败 → OPEN;half_open:立即单次探测(冷却已提供等待),失败 → 重新 OPEN。
        恢复期间并发调用经 _admit_call 的 recovering 标志快速失败。
        """
        attempts = self.reconnect_attempts if mode == "closed" else 1
        with self._state_lock:
            circuit = self._circuits.setdefault(server, _CircuitState())
            if circuit.recovering:
                # 半开探测标志由 _admit_call 预置;closed 路径在此认领恢复权
                if mode != "half_open":
                    raise MCPCircuitOpenError(f"MCP 服务 {server} 正在重连,请稍后重试")
            circuit.recovering = True
        last_error: MCPError = failure
        try:
            for attempt in range(1, attempts + 1):
                delay = (self.reconnect_backoff_base * (2 ** (attempt - 1))
                         if mode == "closed" else 0.0)
                if delay > 0:
                    time.sleep(delay)
                self._audit("mcp_retry", server=server, attempt=attempt,
                            delay=round(delay, 3), error=str(last_error))
                try:
                    self._submit(self._reconnect(server))
                except MCPError as reconnect_exc:
                    # 重连这一步失败(任何错误类别:拒连/超时/代理 5xx/握手失败)
                    # = 会话未恢复,只算一次尝试,绝不视为"服务已恢复"
                    last_error = reconnect_exc
                    continue
                try:
                    result = self._submit(self._call_tool(server, tool_name, arguments))
                except MCPError as retry_exc:
                    last_error = retry_exc
                    if not self._is_connection_error(retry_exc):
                        # 重连成功但调用本身失败(如工具参数错误):服务已
                        # 恢复,关闭熔断后按原语义抛出
                        self._mark_recovered(server)
                        raise
                    continue
                self._mark_recovered(server)
                self._audit("mcp_reconnected", server=server, attempt=attempt)
                return result
            self._trip(server, last_error)
            raise MCPCircuitOpenError(
                f"MCP 服务 {server} 暂时不可用(重连 {attempts} 次失败,已熔断,"
                f"约 {math.ceil(self.circuit_cooldown)} 秒后自动重试),请稍后重试"
            ) from last_error
        finally:
            with self._state_lock:
                circuit.recovering = False

    def _trip(self, server: str, error: MCPError) -> None:
        """进入 OPEN:cooldown 秒内所有调用快速失败,不发起网络调用。"""
        with self._state_lock:
            circuit = self._circuits.setdefault(server, _CircuitState())
            circuit.state = "open"
            circuit.opened_at = time.monotonic()
            circuit.recovering = False
        self._audit("mcp_circuit_open", server=server,
                    cooldown=self.circuit_cooldown, error=str(error))

    def _mark_recovered(self, server: str) -> None:
        """恢复 → CLOSED(半开探测成功或重连成功后)。"""
        with self._state_lock:
            circuit = self._circuits.setdefault(server, _CircuitState())
            circuit.state = "closed"
            circuit.opened_at = 0.0
            circuit.recovering = False
        self._audit("mcp_circuit_closed", server=server)

    @classmethod
    def _is_connection_error(cls, exc: BaseException) -> bool:
        """沿异常链判定连接类失败(见 _CONNECTION_ERROR_TYPES)。

        只走显式因果链(__cause__ 与异常组子异常)。__context__ 是隐式继承
        (在处理 A 的 except 块里抛出的无关异常 B 会带上 A 作上下文),
        参与判定会把无关错误误分类为连接类——实测"no reconnect spec"曾因
        上下文挂着先前的 500 而被误判、触发多余重连。"""
        seen: set[int] = set()
        stack: list[BaseException] = [exc]
        while stack:
            current = stack.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            if isinstance(current, BaseExceptionGroup):
                # anyio 任务组的聚合异常:展开后逐个判定
                stack.extend(current.exceptions)
                continue
            if isinstance(current, _CONNECTION_ERROR_TYPES):
                return True
            if isinstance(current, httpx.HTTPStatusError):
                # 仅 408/429(上游自己的"请重试"信号)按可重试连接类处理;
                # 5xx/4xx 是服务端/请求错误而非连接死亡:不重连、不熔断,原样上抛
                if current.response.status_code in (408, 429):
                    return True
            text = str(current).lower()
            if any(hint in text for hint in _CONNECTION_MESSAGE_HINTS):
                return True
            if current.__cause__ is not None and current.__cause__ is not current:
                stack.append(current.__cause__)
        return False

    async def _reconnect(self, name: str) -> MCPServerSnapshot:
        """断开已损坏的连接并按原始参数重建(stdio 与 http 共用本路径)。"""
        spec = self._specs.get(name)
        if spec is None:
            raise MCPError(f"MCP server has no reconnect spec: {name}")
        old = self._connections.pop(name, None)
        if old is not None:
            try:
                await old.stack.aclose()
            except Exception:
                pass  # 关闭已损坏的连接不得阻断重连
            self._audit("mcp_disconnected", server=name)
        if spec.transport == "stdio":
            snapshot = await self._connect_stdio(name, spec.stdio_params)
        else:
            snapshot = await self._connect_http(name, spec.http_url, spec.http_headers or {})
        # 工具目录可能变化:重连成功即刷新快照(web 层按请求重注册,自动跟进)
        self._snapshots[name] = snapshot
        return snapshot

    def read_resource(self, server_name: str, uri: str) -> list[dict[str, Any]]:
        """读远端资源。注意:本方法与 get_prompt 属启动期能力发现路径,调用
        频度低,刻意不经过熔断/重连闸门(call_tool 才有);连接恢复后随新
        连接自然可用。"""
        return self._submit(self._read_resource(server_name, uri))

    def get_prompt(self, server_name: str, name: str,
                   arguments: dict[str, str] | None = None) -> dict[str, Any]:
        """取远端 Prompt 模板(启动期能力发现路径,不经过熔断闸门)。"""
        return self._submit(self._get_prompt(server_name, name, arguments or {}))

    def close(self) -> None:
        if self._closed:
            return
        for name in list(self._snapshots):
            try:
                self._submit(self._disconnect(name))
            except MCPError:
                continue
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def __enter__(self) -> MCPClientManager:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def _submit(self, coroutine: Coroutine[Any, Any, T]) -> T:
        if self._closed:
            coroutine.close()
            raise MCPError("MCP client manager is closed")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=self.request_timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise MCPError(f"MCP request timed out after {self.request_timeout:g}s") from exc
        except Exception as exc:
            if isinstance(exc, MCPError):
                raise
            raise MCPError(str(exc)) from exc

    async def _connect_stdio(self, name: str,
                             params: StdioServerParameters) -> MCPServerSnapshot:
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            initialize = await session.initialize()
            snapshot = await self._discover(name, "stdio", session, initialize)
            self._connections[name] = _Connection(stack, session, snapshot)
            self._audit("mcp_connected", server=name, transport="stdio",
                        protocol_version=snapshot.protocol_version)
            return snapshot
        except BaseException:
            await stack.aclose()
            raise

    async def _connect_http(self, name: str, url: str,
                            headers: dict[str, str]) -> MCPServerSnapshot:
        stack = AsyncExitStack()
        try:
            client = await stack.enter_async_context(httpx.AsyncClient(headers=headers, follow_redirects=True))
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(url, http_client=client)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            initialize = await session.initialize()
            snapshot = await self._discover(name, "streamable-http", session, initialize)
            self._connections[name] = _Connection(stack, session, snapshot)
            self._audit("mcp_connected", server=name, transport="streamable-http",
                        protocol_version=snapshot.protocol_version)
            return snapshot
        except BaseException:
            await stack.aclose()
            raise

    async def _discover(self, name: str, transport: str, session: ClientSession,
                        initialize: Any) -> MCPServerSnapshot:
        tools = await self._list_all(session.list_tools, "tools")
        resources = await self._list_all(session.list_resources, "resources")
        prompts = await self._list_all(session.list_prompts, "prompts")
        server_info = initialize.serverInfo
        return MCPServerSnapshot(
            name=name,
            transport=transport,
            server_name=server_info.name,
            server_version=server_info.version,
            protocol_version=initialize.protocolVersion,
            tools=tuple(self._tool_info(tool) for tool in tools),
            resources=tuple(str(resource.uri) for resource in resources if isinstance(resource, Resource)),
            prompts=tuple(prompt.name for prompt in prompts if isinstance(prompt, Prompt)),
        )

    @staticmethod
    async def _list_all(method: Callable[..., Any], field: str) -> list[Any]:
        values: list[Any] = []
        cursor: str | None = None
        while True:
            page = await method(cursor=cursor)
            values.extend(getattr(page, field))
            cursor = page.nextCursor
            if not cursor:
                return values

    async def _call_tool(self, server_name: str, tool_name: str,
                         arguments: dict[str, Any]) -> Any:
        connection = self._connection(server_name)
        known = {tool.name for tool in connection.snapshot.tools}
        if tool_name not in known:
            raise MCPError(f"MCP tool is not advertised by {server_name}: {tool_name}")
        result = await connection.session.call_tool(
            tool_name,
            arguments=arguments,
            read_timeout_seconds=timedelta(seconds=self.request_timeout),
        )
        if result.isError:
            raise MCPError(self._content_text(result.content) or f"MCP tool failed: {tool_name}")
        if result.structuredContent is not None:
            return result.structuredContent
        text = self._content_text(result.content)
        if not text:
            return {"content": [item.model_dump(mode="json", by_alias=True) for item in result.content]}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    async def _read_resource(self, server_name: str, uri: str) -> list[dict[str, Any]]:
        connection = self._connection(server_name)
        result = await connection.session.read_resource(uri)
        return [item.model_dump(mode="json", by_alias=True) for item in result.contents]

    async def _get_prompt(self, server_name: str, name: str,
                          arguments: dict[str, str]) -> dict[str, Any]:
        connection = self._connection(server_name)
        result = await connection.session.get_prompt(name, arguments=arguments)
        return result.model_dump(mode="json", by_alias=True)

    async def _disconnect(self, name: str) -> None:
        connection = self._connections.pop(name, None)
        self._snapshots.pop(name, None)
        with self._state_lock:
            self._specs.pop(name, None)
            self._circuits.pop(name, None)
        if connection is not None:
            await connection.stack.aclose()
            self._audit("mcp_disconnected", server=name)

    def _validate_new_name(self, name: str) -> None:
        if not _SERVER_NAME.fullmatch(name):
            raise MCPError(f"invalid MCP server name: {name}")
        if name in self._snapshots:
            raise MCPError(f"MCP server is already connected: {name}")

    def _snapshot(self, name: str) -> MCPServerSnapshot:
        try:
            return self._snapshots[name]
        except KeyError as exc:
            raise MCPError(f"MCP server is not connected: {name}") from exc

    def _connection(self, name: str) -> _Connection:
        try:
            return self._connections[name]
        except KeyError as exc:
            raise MCPError(f"MCP server is not connected: {name}") from exc

    @staticmethod
    def _tool_info(tool: MCPTool) -> MCPToolInfo:
        annotations = tool.annotations
        meta = dict(tool.meta or {})
        risk = str(meta.get("risk_level", ""))
        if risk not in {"read", "low_write", "high_write", "forbidden"}:
            if annotations is not None and annotations.readOnlyHint is True:
                risk = "read"
            elif annotations is not None and annotations.destructiveHint is True:
                risk = "high_write"
            else:
                # Unknown remote writes are treated as high risk by default.
                risk = "high_write"
        requires_approval = bool(meta.get("requires_approval", risk == "high_write"))
        reason = str(meta.get("policy_reason") or (
            "Remote tool is read-only" if risk == "read" else "Remote tool changes business state"
        ))
        return MCPToolInfo(
            tool.name, tool.description or "", dict(tool.inputSchema),
            ToolPolicy(risk, requires_approval, reason),
        )

    @staticmethod
    def _content_text(content: list[Any]) -> str:
        return "\n".join(item.text for item in content if isinstance(item, TextContent)).strip()

    def _audit(self, event: str, **data: Any) -> None:
        if self.audit_hook is None:
            return
        try:
            self.audit_hook(event, data)
        except Exception:
            # Observability must not break an MCP call.
            return
