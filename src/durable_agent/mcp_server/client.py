"""Client helpers used by activities and tests.

Two kinds of session connect to the same server:

* the **model-facing** session (no credentials), whose tool list is filtered to ``MODEL_TOOLS``;
* the **orchestrator** session, which carries ``Authorization: Bearer <token>`` so that
  ``run_sql`` accepts it.

Both go over Streamable HTTP. Tests can also pass an ``MCPServer`` instance to ``Client`` for
an in-memory connection (no headers, so ``run_sql`` always refuses there, which is itself a
useful test).
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult


class ToolClient:
    """Thin wrapper: structured results in, exceptions out, no protocol noise in callers."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def __aenter__(self) -> ToolClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc, tb)

    @property
    def raw(self) -> Client:
        return self._client

    async def tool_names(self) -> list[str]:
        tools = await self._client.list_tools()
        return [t.name for t in tools.tools]

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call a tool and return its structured content; raise ``ToolCallError`` on is_error."""
        result = await self._client.call_tool(name, arguments or {})
        if result.is_error:
            raise ToolCallError(name, _text(result))
        return dict(result.structured_content or {})


class ToolCallError(RuntimeError):
    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.message = message


def _text(result: CallToolResult) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content).strip()


def tool_client(url: str, *, token: str | None = None, timeout: float = 60.0) -> ToolClient:
    """Connect over Streamable HTTP; attach the capability token as a bearer header if given."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http = httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(timeout, read=timeout))
    return ToolClient(Client(streamable_http_client(url, http_client=http)))
