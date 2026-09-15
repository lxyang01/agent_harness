"""MCP servers owned by the BillGuard runtime."""

from __future__ import annotations

from typing import Any


def apply_streamable_bind(server: Any, host: str, port: int) -> None:
    """把最终绑定地址写入 FastMCP 设置,并按该地址重算 DNS 重绑定防护。

    FastMCP 构造期按默认 host=127.0.0.1 自动启用“仅 localhost Host”防护;
    若事后仅改 settings.host 为非回环地址(集群 0.0.0.0)而不重算防护,
    来自服务名的合法请求(Host: bill-server:8010)会被 streamable-http
    端点以 421 Misdirected Request 拒绝。这里与 SDK 构造期的自动策略保持
    同一语义:非回环绑定不启用 Host/Origin 校验(Content-Type 校验始终
    保留);该形态下服务只监听内网/compose 网络端口,无浏览器直连面。
    """
    from mcp.server.transport_security import TransportSecuritySettings

    server.settings.host = host
    server.settings.port = port
    if host not in ("127.0.0.1", "localhost", "::1"):
        server.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )
