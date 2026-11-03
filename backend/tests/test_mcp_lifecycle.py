"""MCP SDK scopes must stay in their owner task across cancellation/reconnect."""

import asyncio
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import anyio
import httpx
import pytest
import pytest_asyncio
import uvicorn

from aio_agent_platform.tools.mcp.adapter import MCPServerConnection
from aio_agent_platform.tools.mcp.manager import MCPManager


@pytest.fixture(params=["sse", "streamable-http"])
def sdk(request, monkeypatch):
    state = SimpleNamespace(
        entered=[], exited=[], sessions=[], initialize_gate=None,
        close_gate=None, closing=asyncio.Event(), fail=asyncio.Event(),
    )

    @asynccontextmanager
    async def scope(name):
        owner = asyncio.current_task()
        state.entered.append((name, owner))
        try:
            async with anyio.create_task_group():
                yield
        finally:
            assert asyncio.current_task() is owner, "SDK scope exited in another task"
            state.exited.append((name, owner))

    @asynccontextmanager
    async def transport(**kwargs):
        async def fail():
            await state.fail.wait()
            raise RuntimeError("transport crashed")

        async with scope("transport"), anyio.create_task_group() as group:
            group.start_soon(fail)
            try:
                yield (None, None, lambda: None)
            finally:
                group.cancel_scope.cancel()

    class Session:
        async def __aenter__(self):
            self.cm = scope("session")
            await self.cm.__aenter__()
            state.sessions.append(self)
            return self

        async def __aexit__(self, *exc):
            state.closing.set()
            if state.close_gate:
                await state.close_gate.wait()
            return await self.cm.__aexit__(*exc)

        async def initialize(self):
            if state.initialize_gate:
                await state.initialize_gate.wait()

        async def list_tools(self, **kwargs):
            return SimpleNamespace(tools=[], nextCursor=None)

        async def call_tool(self, *args):
            return SimpleNamespace(isError=False, content=[SimpleNamespace(text="ok")])

    monkeypatch.setattr("mcp.ClientSession", lambda *args: Session())
    monkeypatch.setattr("mcp.client.sse.sse_client", transport)
    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", transport)
    state.config = {"transport_type": request.param, "url": "http://test", "timeout": 1}
    return state


def assert_closed(sdk):
    assert sdk.exited == list(reversed(sdk.entered))
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("mcp-connection-")]


async def test_cross_task_connect_refresh_and_shutdown(sdk):
    manager = MCPManager()
    sid = uuid4()
    # Startup wait_for creates a short-lived task; shutdown runs elsewhere.
    await asyncio.wait_for(manager.add_server(sid, sdk.config), timeout=1)
    conn = manager._connections[sid]
    owner = conn._owner_task
    assert owner is not asyncio.current_task()
    await asyncio.wait_for(manager.refresh_tools(sid), timeout=1)
    await asyncio.wait_for(manager.shutdown(), timeout=1)
    assert owner.done()
    assert not conn.connected
    assert conn.session is None
    assert_closed(sdk)


async def test_startup_cancel_closes_partial_session(sdk):
    sdk.initialize_gate = asyncio.Event()
    conn = MCPServerConnection(uuid4(), sdk.config)
    task = asyncio.create_task(conn.connect())
    while not sdk.sessions:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert conn._owner_task is None
    assert not conn.connected
    assert_closed(sdk)


async def test_connect_cancelled_by_anyio_scope_joins_owner(sdk):
    sdk.initialize_gate = asyncio.Event()
    conn = MCPServerConnection(uuid4(), sdk.config)
    with anyio.move_on_after(0.01) as cancel_scope:
        await conn.connect()
    assert cancel_scope.cancel_called
    assert conn._owner_task is None
    assert_closed(sdk)


async def test_shutdown_cancel_waits_for_cleanup_then_propagates(sdk):
    conn = MCPServerConnection(uuid4(), sdk.config)
    await conn.connect()
    sdk.close_gate = asyncio.Event()
    task = asyncio.create_task(conn.disconnect())
    await sdk.closing.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    sdk.close_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert conn._owner_task is None
    assert_closed(sdk)


async def test_cancelled_disconnect_waiting_for_lifecycle_lock_still_closes(sdk):
    conn = MCPServerConnection(uuid4(), sdk.config)
    await conn.connect()
    async with conn._lifecycle_lock:
        task = asyncio.create_task(conn.disconnect())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert_closed(sdk)


async def test_transport_failure_does_not_cancel_connecting_task(sdk):
    conn = MCPServerConnection(uuid4(), sdk.config)
    await conn.connect()
    owner = conn._owner_task
    sdk.fail.set()
    await asyncio.wait_for(asyncio.shield(owner), timeout=1)
    # AnyIO cancellation remains confined to the owner task and unwinds there.
    await anyio.sleep(0)
    assert not conn.connected
    assert conn.session is None
    assert "transport crashed" in repr(conn._owner_error)
    await conn.disconnect()
    assert_closed(sdk)


async def test_concurrent_stale_calls_share_one_reconnect(sdk):
    conn = MCPServerConnection(uuid4(), sdk.config)
    await conn.connect()
    first_owner = conn._owner_task
    stale_session = conn.session
    calls = 0
    all_called = asyncio.Event()

    async def stale(*args):
        nonlocal calls
        calls += 1
        if calls == 8:
            all_called.set()
        await all_called.wait()
        raise httpx.ConnectError("connection lost")

    stale_session.call_tool = stale
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(conn.call_tool("test", {}) for _ in range(8))), timeout=1
        )
        assert results == ["ok"] * 8
        assert len(sdk.sessions) == 2
        assert first_owner.done()
        assert conn._owner_task is not first_owner
    finally:
        await conn.disconnect()
    # Each generation closes its session before its transport.
    assert [name for name, _ in sdk.exited] == ["session", "transport"] * 2
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("mcp-connection-")]


async def test_concurrent_server_replacement_does_not_orphan_connection(sdk):
    manager = MCPManager()
    sid = uuid4()
    try:
        await asyncio.wait_for(
            asyncio.gather(*(manager.add_server(sid, sdk.config) for _ in range(4))),
            timeout=1,
        )
        assert len(sdk.sessions) == 4
        owners = [t for t in asyncio.all_tasks() if t.get_name().startswith("mcp-connection-")]
        assert owners == [manager._connections[sid]._owner_task]
    finally:
        await manager.shutdown()
    assert [name for name, _ in sdk.exited] == ["session", "transport"] * 4


@pytest_asyncio.fixture(params=["sse", "streamable-http"])
async def real_server(request):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("LifecycleTest")
    entered = asyncio.Event()
    release = asyncio.Event()

    @mcp.tool()
    async def echo(text: str, block: bool = False) -> str:
        if block:
            entered.set()
            await release.wait()
        return text

    app = mcp.sse_app() if request.param == "sse" else mcp.streamable_http_app()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(3):
                while not server.started:
                    await asyncio.sleep(0.01)
            path = "/sse" if request.param == "sse" else "/mcp"
            yield {
                "transport_type": request.param, "url": f"http://127.0.0.1:{port}{path}",
                "timeout": 2,
            }, entered
        finally:
            release.set()
            server.should_exit = True
            await asyncio.wait_for(task, timeout=5)


async def test_real_sdk_cross_task_cancel_reconnect_and_shutdown(real_server, monkeypatch):
    from anyio._backends._asyncio import CancelScope

    deliveries = 0
    original = CancelScope._deliver_cancellation

    def deliver(*args, **kwargs):
        nonlocal deliveries
        deliveries += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(CancelScope, "_deliver_cancellation", deliver)
    config, entered = real_server
    manager = MCPManager()
    sid = uuid4()
    try:
        await asyncio.wait_for(manager.add_server(sid, config), timeout=3)
        conn = manager._connections[sid]
        owner = conn._owner_task
        assert await manager.call_tool("echo", {"text": "first"}) == "first"
        call = asyncio.create_task(manager.call_tool("echo", {"text": "blocked", "block": True}))
        await asyncio.wait_for(entered.wait(), timeout=1)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call, timeout=1)
        assert await manager.call_tool("echo", {"text": "after cancel"}) == "after cancel"

        async def stale(*args):
            raise httpx.ConnectError("stale session")

        conn.session.call_tool = stale
        assert await asyncio.wait_for(manager.call_tool("echo", {"text": "reconnected"}), 3) == "reconnected"
        assert owner.done()
        assert conn._owner_task is not owner
    finally:
        await asyncio.wait_for(manager.shutdown(), timeout=3)
    # There must be no cancellation callback rescheduling itself after closure.
    await asyncio.sleep(0.02)
    settled = deliveries
    await asyncio.sleep(0.02)
    assert deliveries == settled
    assert deliveries < 500
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("mcp-connection-")]
