from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Coroutine, TypeVar

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


def _make_handler(manager: "MCPClientManager", server: str, tool: str) -> Callable[..., Any]:
    """闭包工厂:handler 只接受 **arguments,没有任何具名参数——模型显式传
    _server/_tool 同名关键字无处绑定,无法重路由到其他服务器/工具(与
    web._make_owner_wrapper 同构)。"""
    def handler(**arguments: Any) -> Any:
        return manager.call_tool(server, tool, arguments)
    return handler


class MCPClientManager:
    """Persistent MCP connections behind a synchronous, timeout-bounded facade."""

    def __init__(self, request_timeout: float = 20.0,
                 audit_hook: AuditHook | None = None) -> None:
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self.request_timeout = request_timeout
        self.audit_hook = audit_hook
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self._connections: dict[str, _Connection] = {}
        self._snapshots: dict[str, MCPServerSnapshot] = {}
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
        snapshot = self._submit(self._connect_stdio(name, params))
        self._snapshots[name] = snapshot
        return snapshot

    def connect_streamable_http(self, name: str, url: str,
                                headers: dict[str, str] | None = None) -> MCPServerSnapshot:
        self._validate_new_name(name)
        snapshot = self._submit(self._connect_http(name, url, headers or {}))
        self._snapshots[name] = snapshot
        return snapshot

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
            result = self._submit(self._call_tool(server_name, tool_name, arguments or {}))
        except Exception as exc:
            self._audit("mcp_tool_error", server=server_name, tool=tool_name, error=str(exc))
            raise
        self._audit("mcp_tool_end", server=server_name, tool=tool_name, result=result)
        return result

    def read_resource(self, server_name: str, uri: str) -> list[dict[str, Any]]:
        return self._submit(self._read_resource(server_name, uri))

    def get_prompt(self, server_name: str, name: str,
                   arguments: dict[str, str] | None = None) -> dict[str, Any]:
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
