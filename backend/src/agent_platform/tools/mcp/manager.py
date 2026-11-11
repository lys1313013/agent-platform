"""MCP Manager — lifecycle management for multiple MCP servers."""

from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack
from uuid import UUID

import structlog

from agent_platform.tools.mcp.adapter import MCPServerConnection, MCPToolInfo, _join_task

logger = structlog.get_logger()

TOOLS_CACHE_TTL_SECONDS = 60
REFRESH_RETRY_SECONDS = 60
RECONNECT_MAX_DELAY_SECONDS = 60
RECONNECT_TIMEOUT_SECONDS = 15
RECONNECT_INITIAL_DELAY_SECONDS = 1
RECONNECT_POLL_SECONDS = 1


class MCPManager:
    """
    MCP Server connection manager.

    Responsibilities:
    - Manage connections to multiple MCP Servers (lazy + eager loading)
    - Provide unified tool discovery and invocation interface
    - Error isolation (one server crash doesn't affect others)
    - Connection status monitoring
    """

    def __init__(self) -> None:
        self._server_locks: dict[UUID, asyncio.Lock] = {}
        self._refresh_locks: dict[UUID, asyncio.Lock] = {}
        self._retry_after: dict[UUID, float] = {}
        self._connections: dict[UUID, MCPServerConnection] = {}
        self._tool_to_server: dict[str, UUID] = {}  # full_tool_name -> server_id
        # Keep desired servers even when the initial handshake fails.
        self._configs: dict[UUID, dict] = {}
        self._reconnect_after: dict[UUID, float] = {}
        self._reconnect_failures: dict[UUID, int] = {}
        self._reconnect_task: asyncio.Task | None = None
        self._closing = False

    def start(self) -> None:
        """Recover disconnected servers without waiting for another tool call."""
        if self._reconnect_task is None:
            self._closing = False
            self._reconnect_task = asyncio.create_task(
                self._reconnect_loop(), name="mcp-reconnect"
            )

    async def _reconnect_loop(self) -> None:
        tasks: list[asyncio.Task] = []
        try:
            while True:
                await asyncio.sleep(RECONNECT_POLL_SECONDS)
                pending = [
                    sid for sid in list(self._configs)
                    if (sid not in self._connections or not self._connections[sid].connected)
                    and time.monotonic() >= self._reconnect_after.get(sid, 0)
                ]
                # An unavailable integration must not prevent another from recovering.
                tasks = [asyncio.create_task(self._recover_server(sid)) for sid in pending]
                await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            # A normal supervisor stop is not cancellation of shutdown's caller.
            # Join partial handshakes before closing the remaining connections.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _recover_server(self, server_id: UUID) -> None:
        try:
            await self._ensure_connected(server_id)
        except Exception:
            # _ensure_connected records the failure and schedules the next attempt.
            pass

    def _schedule_reconnect(self, server_id: UUID) -> None:
        failures = self._reconnect_failures.get(server_id, 0) + 1
        self._reconnect_failures[server_id] = failures
        delay = min(
            RECONNECT_INITIAL_DELAY_SECONDS * 2 ** min(failures - 1, 6),
            RECONNECT_MAX_DELAY_SECONDS,
        )
        self._reconnect_after[server_id] = time.monotonic() + delay
        logger.warning(
            "mcp_reconnect_scheduled", server_id=str(server_id),
            attempt=failures, retry_seconds=delay,
        )

    async def _ensure_connected(self, server_id: UUID, *, force: bool = False) -> MCPServerConnection:
        lock = self._server_locks.setdefault(server_id, asyncio.Lock())
        async with lock:
            if self._closing:
                raise RuntimeError("MCP Manager is shutting down")
            conn = self._connections.get(server_id)
            if conn and conn.connected:
                return conn
            config = self._configs.get(server_id)
            if config is None:
                raise RuntimeError(f"MCP Server not connected: {server_id}")
            if not force and time.monotonic() < self._reconnect_after.get(server_id, 0):
                raise RuntimeError(f"MCP Server not connected: {server_id}; reconnect scheduled")
            try:
                async with asyncio.timeout(min(
                    float(config.get("timeout") or 60), RECONNECT_TIMEOUT_SECONDS
                )):
                    if conn is None:
                        await self._add_server(server_id, config)
                        conn = self._connections[server_id]
                    else:
                        await conn.connect()
                        self._sync_tool_mappings(server_id, conn)
            except Exception:
                self._schedule_reconnect(server_id)
                raise
            self._reconnect_after.pop(server_id, None)
            self._reconnect_failures.pop(server_id, None)
            self._retry_after.pop(server_id, None)
            logger.info("mcp_server_recovered", server_id=str(server_id), tools_count=len(conn.tools))
            return conn

    async def add_server(self, server_id: UUID, config: dict) -> list[MCPToolInfo]:
        """
        Add and connect an MCP Server.

        Args:
            server_id: Database ID of the MCP server
            config: Server configuration dict (transport_type, command, args, env, url, etc.)

        Returns:
            List of discovered tools

        Raises:
            Exception: If connection fails
        """
        lock = self._server_locks.setdefault(server_id, asyncio.Lock())
        async with lock:
            if self._closing:
                raise RuntimeError("MCP Manager is shutting down")
            self._configs[server_id] = dict(config)
            try:
                tools = await self._add_server(server_id, config)
            except BaseException:
                self._schedule_reconnect(server_id)
                raise
            self._reconnect_after.pop(server_id, None)
            self._reconnect_failures.pop(server_id, None)
            return tools

    async def _add_server(self, server_id: UUID, config: dict) -> list[MCPToolInfo]:
        # Serialize replacement so concurrent requests cannot orphan an owner.
        if server_id in self._connections:
            await self._remove_server(server_id)

        conn = MCPServerConnection(server_id, config)
        try:
            await conn.connect()
        except BaseException:
            # connect() can be cancelled after opening a transport but before
            # the connection is published in this manager's registry.
            await conn.disconnect()
            raise

        self._connections[server_id] = conn
        self._retry_after.pop(server_id, None)

        # Register tool mappings
        prefix = config.get("tool_prefix") or ""
        discovered_tools = []
        for tool in conn.tools:
            full_name = f"{prefix}{tool.name}" if prefix else tool.name
            if self._reserved_name(full_name, server_id):
                logger.warning(
                    "mcp_tool_name_conflict", server_id=str(server_id), tool=full_name,
                )
                continue
            self._tool_to_server[full_name] = server_id
            discovered_tools.append(tool)

        logger.info(
            "mcp_server_added",
            server_id=str(server_id),
            name=config.get("name"),
            tools_count=len(discovered_tools),
        )
        return discovered_tools

    def _reserved_name(self, full_name: str, server_id: UUID) -> bool:
        """Reject names owned by built-ins or by another server."""
        owner = self._tool_to_server.get(full_name)
        if owner is not None and owner != server_id:
            return True
        from agent_platform.tools.builtin import BUILTIN_TOOL_NAMES

        return full_name in BUILTIN_TOOL_NAMES

    async def remove_server(self, server_id: UUID) -> None:
        """Disconnect and remove an MCP Server."""
        lock = self._server_locks.setdefault(server_id, asyncio.Lock())
        async with lock:
            self._configs.pop(server_id, None)
            self._reconnect_after.pop(server_id, None)
            self._reconnect_failures.pop(server_id, None)
            self._retry_after.pop(server_id, None)
            await self._remove_server(server_id)

    async def _remove_server(self, server_id: UUID) -> None:
        conn = self._connections.pop(server_id, None)
        if conn:
            # Clean up tool mappings
            tools_to_remove = [
                name
                for name, sid in self._tool_to_server.items()
                if sid == server_id
            ]
            for name in tools_to_remove:
                del self._tool_to_server[name]

            await conn.disconnect()
            logger.info("mcp_server_removed", server_id=str(server_id))

    async def refresh_server(self, server_id: UUID, config: dict) -> list[MCPToolInfo]:
        """Refresh an MCP Server connection (reconnect and rediscover tools)."""
        return await self.add_server(server_id, config)

    def _sync_tool_mappings(self, server_id: UUID, conn: MCPServerConnection) -> None:
        if self._connections.get(server_id) is not conn:
            return
        mappings = {name: sid for name, sid in self._tool_to_server.items() if sid != server_id}
        prefix = conn.config.get("tool_prefix") or ""
        mappings.update({
            f"{prefix}{tool.name}": server_id for tool in conn.tools
            if not self._reserved_name(f"{prefix}{tool.name}", server_id)
        })
        self._tool_to_server = mappings

    async def refresh_tools(self, server_id: UUID, *, force: bool = True) -> list[MCPToolInfo]:
        """Refresh atomically; force bypasses the 60s TTL and failure backoff."""
        lock = self._refresh_locks.setdefault(server_id, asyncio.Lock())
        async with lock:
            conn = self._connections.get(server_id)
            if not conn:
                return (await self._ensure_connected(server_id, force=force)).tools
            if not conn.connected:
                return (await self._ensure_connected(server_id, force=force)).tools
            now = time.monotonic()
            if not force and (
                now - conn.tools_refreshed_at < TOOLS_CACHE_TTL_SECONDS
                or now < self._retry_after.get(server_id, 0)
            ):
                return conn.tools
            failed_session = conn.session
            try:
                async with asyncio.timeout(float(conn.config.get("timeout", 60))):
                    tools = await conn.refresh_tools()
            except Exception as exc:
                self._retry_after[server_id] = time.monotonic() + REFRESH_RETRY_SECONDS
                if isinstance(exc, TimeoutError) or conn._is_reconnectable(exc):
                    await conn.invalidate(failed_session)
                    if not conn.connected:
                        self._schedule_reconnect(server_id)
                raise
            self._sync_tool_mappings(server_id, conn)
            self._retry_after.pop(server_id, None)
            return tools

    async def ensure_tools_fresh(self, server_ids: set[str] | None = None) -> None:
        """Refresh selected servers on demand; preserve cached tools on failure."""
        async def refresh(server_id: UUID) -> None:
            try:
                await self.refresh_tools(server_id, force=False)
            except Exception as exc:
                logger.warning("mcp_tools_refresh_failed", server_id=str(server_id), error=str(exc))

        await asyncio.gather(*(
            refresh(server_id) for server_id in list(self._connections)
            if server_ids is None or str(server_id) in server_ids
        ))

    def get_tool_info(self, tool_name: str) -> MCPToolInfo | None:
        """Get tool info by full name (with prefix)."""
        server_id = self._tool_to_server.get(tool_name)
        if not server_id:
            return None

        conn = self._connections.get(server_id)
        if not conn:
            return None

        # Strip prefix to find the base tool name
        prefix = conn.config.get("tool_prefix") or ""
        base_name = tool_name[len(prefix):] if prefix and tool_name.startswith(prefix) else tool_name

        for tool in conn.tools:
            if tool.name == base_name:
                return tool
        return None

    def list_all_tools(self) -> list[tuple[str, MCPToolInfo]]:
        """
        List all MCP tools across all connected servers.

        Returns:
            List of (full_name, tool_info) tuples
        """
        result = []
        for _server_id, conn in self._connections.items():
            prefix = conn.config.get("tool_prefix") or ""
            for tool in conn.tools:
                full_name = f"{prefix}{tool.name}" if prefix else tool.name
                if self._tool_to_server.get(full_name) != _server_id:
                    continue
                result.append((full_name, tool))
        return result

    def is_mcp_tool(self, tool_name: str) -> bool:
        """Check if a tool name is an MCP tool."""
        return tool_name in self._tool_to_server

    def timeout_for(self, tool_name: str) -> float:
        """Dispatch ceiling for one tool, taken from its server's configured timeout."""
        server_id = self._tool_to_server.get(tool_name)
        conn = self._connections.get(server_id) if server_id else None
        return float(conn.config.get("timeout") or 60) if conn else 60.0

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """
        Call an MCP tool.

        Args:
            tool_name: Full tool name (with prefix if configured)
            arguments: Tool arguments

        Returns:
            Tool output as text
        """
        server_id = self._tool_to_server.get(tool_name)
        if not server_id:
            raise ValueError(f"Unknown MCP tool: {tool_name}")

        conn = self._connections.get(server_id)
        if not conn or not conn.connected:
            conn = await self._ensure_connected(server_id)

        # Strip prefix to get base tool name
        prefix = conn.config.get("tool_prefix") or ""
        base_name = (
            tool_name[len(prefix):]
            if prefix and tool_name.startswith(prefix)
            else tool_name
        )

        try:
            return await conn.call_tool(base_name, arguments)
        finally:
            # A tool call may have reconnected and rediscovered the upstream tools.
            self._sync_tool_mappings(server_id, conn)

    async def shutdown(self) -> None:
        """Close all MCP Server connections."""
        self._closing = True
        task, self._reconnect_task = self._reconnect_task, None
        self._configs.clear()
        self._reconnect_after.clear()
        self._reconnect_failures.clear()
        server_ids = list(self._connections.keys())
        async with AsyncExitStack() as cleanup:
            for server_id in reversed(server_ids):
                cleanup.push_async_callback(self.remove_server, server_id)
            if task is not None:
                async def stop_reconnect() -> None:
                    task.cancel()
                    # gather consumes cancellation even if the supervisor was
                    # stopped before its coroutine had a chance to start.
                    await asyncio.gather(task, return_exceptions=True)

                if await _join_task(asyncio.create_task(stop_reconnect())):
                    raise asyncio.CancelledError
        logger.info("mcp_manager_shutdown", servers_closed=len(server_ids))

    def get_status(self) -> dict[str, dict]:
        """
        Get status of all MCP Server connections.

        Returns:
            Dict mapping server_id to status info
        """
        return {
            str(server_id): {
                "name": conn.config.get("name", "unknown"),
                "transport": conn.config.get("transport_type"),
                "connected": conn.connected,
                "tools_count": len(conn.tools),
                "tool_prefix": conn.config.get("tool_prefix"),
            }
            for server_id, conn in self._connections.items()
        }

    def get_server_tools(self, server_id: UUID) -> list[MCPToolInfo] | None:
        """Get tools for a specific server."""
        conn = self._connections.get(server_id)
        if not conn:
            return None
        return conn.tools
