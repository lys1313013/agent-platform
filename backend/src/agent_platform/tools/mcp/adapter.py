"""MCP Server adapter — wraps the MCP Python SDK client."""

from __future__ import annotations

import asyncio
import importlib.util
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

import anyio
import httpx
import structlog

if TYPE_CHECKING:
    from mcp import ClientSession

logger = structlog.get_logger()

_MCP_PACKAGE_NAME = "mcp"
_MCP_INSTALL_HINT = (
    "MCP 客户端库未安装。请运行: pip install mcp>=1.0.0\n"
    "或安装项目完整依赖: pip install agent-platform[all]"
)


async def _join_task(task: asyncio.Task) -> bool:
    """Finish cleanup despite caller cancellation; report deferred cancellation."""
    cancelled = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
    if not task.cancelled():  # An owner can be cancelled before its first step.
        task.result()
    return cancelled


def _ensure_mcp_package() -> None:
    """Fail with an actionable hint; dependencies are installed at build time."""
    if importlib.util.find_spec(_MCP_PACKAGE_NAME) is None:
        logger.error("mcp_package_missing", hint=_MCP_INSTALL_HINT)
        raise ImportError(_MCP_INSTALL_HINT)


@dataclass
class MCPToolInfo:
    """MCP tool information (discovered from MCP Server)."""

    name: str
    description: str
    input_schema: dict = field(default_factory=dict)  # JSON Schema

    def to_openai_tool(self, prefix: str = "") -> dict:
        """Convert to OpenAI function-calling format."""
        full_name = f"{prefix}{self.name}" if prefix else self.name
        return {
            "type": "function",
            "function": {
                "name": full_name,
                "description": self.description,
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


class MCPServerConnection:
    """
    Encapsulates a single MCP Server connection.

    Manages the lifecycle of the transport (SSE / streamable-http) and session,
    provides tool discovery and invocation.
    """

    def __init__(self, server_id: UUID, config: dict):
        self.server_id = server_id
        self.config = config
        self.session: ClientSession | None = None
        self.tools: list[MCPToolInfo] = []
        self.tools_refreshed_at = 0.0
        self._owner_task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._owner_error: BaseException | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self.session is not None

    async def connect(self) -> None:
        """Establish connection and discover tools."""
        async with self._lifecycle_lock:
            if self.connected:
                return
            self._closed = False
            await self._start_owner()

    async def _start_owner(self) -> None:
        await self._cleanup()
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._owner_error = None
        self._owner_task = asyncio.create_task(
            self._run_connection(), name=f"mcp-connection-{self.server_id}"
        )
        try:
            await self._ready.wait()
            if not self.connected:
                error = self._owner_error
                if isinstance(error, Exception):
                    raise error
                raise RuntimeError("MCP connection stopped during startup") from error
        except BaseException:
            await self._cleanup()
            raise

    async def _run_connection(self) -> None:
        """Own every SDK cancel scope from entry to exit in this single task.

        SDK transports and ClientSession contain AnyIO task groups. Their
        contexts must never straddle the startup/request/shutdown tasks.
        Callers may use the session concurrently, but cannot close its scopes.
        """
        try:
            _ensure_mcp_package()
            from mcp import ClientSession

            async with AsyncExitStack() as stack:
                streams = await stack.enter_async_context(self._transport())
                self.session = await stack.enter_async_context(ClientSession(*streams[:2]))
                await self.session.initialize()
                await self.refresh_tools()
                self._connected = True
                self._ready.set()
                logger.info(
                    "mcp_server_connected", server_id=str(self.server_id),
                    name=self.config.get("name"), transport=self.config["transport_type"],
                )
                await self._stop.wait()
        except BaseException as error:
            self._owner_error = error
            # All SDK scopes have exited here, so cancellation cannot escape
            # this task into whichever request originally called connect().
            if not isinstance(error, asyncio.CancelledError):
                causes = [error]
                for cause in causes:
                    if isinstance(cause, BaseExceptionGroup):
                        causes.extend(cause.exceptions)
                # Remote downtime is recoverable. Keep actionable metadata
                # without dumping SDK locals, headers or request contents.
                logger.warning(
                    "mcp_connection_stopped", server_id=str(self.server_id),
                    name=self.config.get("name"),
                    error_types=sorted({type(cause).__name__ for cause in causes}),
                    http_status_codes=sorted({
                        cause.response.status_code for cause in causes
                        if isinstance(cause, httpx.HTTPStatusError)
                    }),
                )
        finally:
            self._connected = False
            self.session = None
            self._ready.set()

    def _transport(self):
        """Create a transport context; only the owner task may enter it."""
        url = self.config["url"]
        headers = self.config.get("headers") or None
        timeout = float(self.config.get("timeout", 60))
        if self.config["transport_type"] == "sse":
            from mcp.client.sse import sse_client

            return sse_client(
                url=url, headers=headers, timeout=timeout, sse_read_timeout=timeout
            )
        if self.config["transport_type"] == "streamable-http":
            from mcp.client.streamable_http import streamablehttp_client

            return streamablehttp_client(url=url, headers=headers, timeout=timeout)
        raise ValueError(f"Unknown transport type: {self.config['transport_type']}")

    async def disconnect(self) -> None:
        """Close the connection and clean up resources."""
        self._closed = True
        # Cancellation while waiting for a reconnect's lock must not orphan
        # the owner after the manager has already removed this connection.
        task = asyncio.create_task(self._disconnect(), name=f"mcp-close-{self.server_id}")
        if await _join_task(task):
            raise asyncio.CancelledError

    async def _disconnect(self) -> None:
        async with self._lifecycle_lock:
            try:
                await self._cleanup()
            finally:
                self.tools = []
        logger.info(
            "mcp_server_disconnected",
            server_id=str(self.server_id),
            name=self.config.get("name"),
        )

    async def _cleanup(self) -> None:
        """Signal the owner and join it without transferring context ownership."""
        task = self._owner_task
        if task is None:
            return
        self._stop.set()
        if not self._ready.is_set() and not task.done():
            task.cancel()  # Abort an incomplete initialize/list_tools request.
        # AnyIO callers may already be in a cancelled scope. Shield that scope
        # and shield the owner from direct asyncio cancellation while joining.
        cancelled = await _join_task(task)
        self._owner_task = None
        self._connected = False
        self.session = None
        if cancelled:
            raise asyncio.CancelledError

    async def invalidate(self, failed_session) -> None:
        """Retire a failed session without disabling future recovery.

        SSE can keep its receive stream open after its POST writer fails.
        A timeout must therefore retire that half-open session explicitly.
        """
        async with self._lifecycle_lock:
            if not self._closed and self.session is failed_session:
                await self._cleanup()

    async def refresh_tools(self) -> list[MCPToolInfo]:
        """Refresh the list of tools from the MCP Server."""
        if not self.session:
            raise RuntimeError("Not connected")

        # Build the complete snapshot before publishing it; failures retain the cache.
        tools = []
        cursor = None
        while True:
            result = await self.session.list_tools(cursor=cursor)
            tools.extend(
                MCPToolInfo(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema if hasattr(tool, "inputSchema") else {},
                )
                for tool in result.tools
            )
            cursor = getattr(result, "nextCursor", None)
            if not cursor:
                break
        self.tools = tools
        self.tools_refreshed_at = time.monotonic()

        logger.info(
            "mcp_tools_discovered",
            server_id=str(self.server_id),
            name=self.config.get("name"),
            tools_count=len(self.tools),
            tool_names=[t.name for t in self.tools],
        )
        return self.tools

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """
        Call a tool on the MCP Server and return the result as text.

        Args:
            tool_name: The tool name (without prefix)
            arguments: Tool arguments

        Returns:
            Text output from the tool

        If the server has reclaimed the session — streamable-http idle
        timeouts recycle sessions, so a stale session fails with "Session
        terminated" — transparently re-establish the connection and retry
        once before giving up. Without this, a single expired session
        fails every subsequent call until someone manually refreshes the
        server.
        """
        if not self.connected:
            if self._closed:
                raise RuntimeError("MCP connection was disconnected")
            await self.connect()

        failed_session = self.session
        try:
            return await self._call_tool_once(tool_name, arguments)
        except Exception as e:
            if not self._is_reconnectable(e):
                logger.error(
                    "mcp_tool_call_failed",
                    server_id=str(self.server_id),
                    name=self.config.get("name"),
                    tool_name=tool_name,
                    error=str(e),
                )
                raise
            logger.warning(
                "mcp_session_stale_reconnecting",
                server_id=str(self.server_id),
                name=self.config.get("name"),
                tool_name=tool_name,
                error=str(e),
            )
            try:
                await self._reconnect(failed_session)
            except Exception:
                logger.exception(
                    "mcp_reconnect_failed",
                    server_id=str(self.server_id),
                    name=self.config.get("name"),
                    tool_name=tool_name,
                )
                raise
            if isinstance(e, (httpx.HTTPStatusError, BaseExceptionGroup)):
                # A gateway can fail after a write was processed. Restore the
                # connection, but do not automatically replay an ambiguous call.
                raise
            # 重建成功，重试一次；若仍失败则原样上抛并记录。
            try:
                return await self._call_tool_once(tool_name, arguments)
            except Exception as e2:
                logger.error(
                    "mcp_tool_call_failed",
                    server_id=str(self.server_id),
                    name=self.config.get("name"),
                    tool_name=tool_name,
                    error=str(e2),
                )
                raise

    async def _call_tool_once(self, tool_name: str, arguments: dict) -> str:
        """Single tool invocation on the current session (no retry logic)."""
        result = await self.session.call_tool(tool_name, arguments)

        if result.isError:
            # Extract error text
            error_parts = []
            if result.content:
                for item in result.content:
                    if hasattr(item, "text"):
                        error_parts.append(item.text)
            error_text = "\n".join(error_parts) if error_parts else "Unknown MCP error"
            raise RuntimeError(f"MCP tool error: {error_text}")

        # Extract text content from result
        if result.content:
            text_parts = []
            for item in result.content:
                if hasattr(item, "text"):
                    text_parts.append(item.text)
            return "\n".join(text_parts) if text_parts else "(no output)"

        return "(no output)"

    def _is_reconnectable(self, exc: Exception) -> bool:
        """Whether an exception is worth recovering from by reconnecting.

        Recoverable cases include:
        - The server reclaimed our session. For streamable-http the server
          answers a stale session with HTTP 404, which the MCP SDK turns into
          ``McpError`` (code 32600, "Session terminated"). ``mcp`` is an
          optional dependency, so match by name instead of importing it.
        - A transport-level network failure (connection dropped, connect
          timeout) — rebuilding the session typically restores service.
        - Temporary HTTP failures, closed streams and SDK exception groups.
        """
        if type(exc).__name__ == "McpError":
            code = getattr(getattr(exc, "error", None), "code", None)
            if code == 32600 or "session terminated" in str(exc).lower():
                return True
        if isinstance(exc, httpx.RequestError):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in {408, 429} or exc.response.status_code >= 500
        if isinstance(exc, BaseExceptionGroup):
            return any(
                isinstance(error, Exception) and self._is_reconnectable(error)
                for error in exc.exceptions
            )
        if isinstance(exc, (anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError)):
            return True
        return False

    async def _reconnect(self, failed_session=None) -> None:
        """Tear down the stale connection and establish a fresh session."""
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("MCP connection was disconnected")
            if failed_session is not None and self.connected and self.session is not failed_session:
                return  # A concurrent call already replaced the stale session.
            await self._start_owner()
