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

from agent_platform.tools.mcp.adapter import MCPServerConnection
from agent_platform.tools.mcp.manager import MCPManager

# Real TCP/SDK handshakes need headroom on shared CI runners with coverage.
# The lifecycle test checks ownership/cancellation, not network latency; keep
# the short cancellation deadline below separate from this I/O budget.
_REAL_IO_TIMEOUT = 15


@pytest.fixture(params=["sse", "streamable-http"])
def sdk(request, monkeypatch):
    state = SimpleNamespace(
        entered=[], exited=[], sessions=[], initialize_gate=None,
        close_gate=None, closing=asyncio.Event(), fail=asyncio.Event(),
        available=True, failure=RuntimeError("transport crashed"),
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
        if not state.available:
            raise httpx.ConnectError("server is stopped")

        async def fail():
            await state.fail.wait()
            raise state.failure

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
            return SimpleNamespace(tools=[SimpleNamespace(
                name="remote_echo", description="echo", inputSchema={"type": "object"},
            )], nextCursor=None)

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


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest_asyncio.fixture
async def recovery_manager(monkeypatch):
    monkeypatch.setattr("agent_platform.tools.mcp.manager.RECONNECT_POLL_SECONDS", 0.005)
    monkeypatch.setattr("agent_platform.tools.mcp.manager.RECONNECT_INITIAL_DELAY_SECONDS", 0.005)
    monkeypatch.setattr("agent_platform.tools.mcp.manager.RECONNECT_MAX_DELAY_SECONDS", 0.02)
    manager = MCPManager()
    try:
        yield manager
    finally:
        await manager.shutdown()


async def test_startup_outage_recovers_without_tool_call(sdk, recovery_manager):
    manager = recovery_manager
    sid = uuid4()
    sdk.available = False
    with pytest.raises(httpx.ConnectError):
        await manager.add_server(sid, sdk.config)
    manager.start()
    await eventually(lambda: manager._reconnect_failures.get(sid, 0) >= 7)
    sdk.available = True
    await eventually(lambda: sid in manager._connections and manager._connections[sid].connected)
    assert manager.is_mcp_tool("remote_echo")
    assert await manager.call_tool("remote_echo", {}) == "ok"
    assert sid not in manager._reconnect_failures


async def test_background_502_outage_recovers_without_tool_call(sdk, recovery_manager):
    manager = recovery_manager
    sid = uuid4()
    await manager.add_server(sid, sdk.config)
    conn = manager._connections[sid]
    owner = conn._owner_task
    sdk.failure = httpx.HTTPStatusError(
        "502 Bad Gateway", request=httpx.Request("POST", "http://test"),
        response=httpx.Response(502),
    )
    sdk.available = False
    sdk.fail.set()
    await asyncio.wait_for(asyncio.shield(owner), timeout=1)
    manager.start()
    await eventually(lambda: manager._reconnect_failures.get(sid, 0) >= 2)
    assert manager.is_mcp_tool("remote_echo")  # Preserve cached routing during the outage.
    sdk.fail.clear()
    sdk.available = True
    await eventually(lambda: conn.connected)
    assert len(sdk.sessions) == 2
    assert await manager.call_tool("remote_echo", {}) == "ok"


async def test_disconnected_concurrent_calls_share_recovery(sdk, recovery_manager):
    manager = recovery_manager
    sid = uuid4()
    await manager.add_server(sid, sdk.config)
    conn = manager._connections[sid]
    owner = conn._owner_task
    sdk.fail.set()
    await asyncio.wait_for(asyncio.shield(owner), timeout=1)
    sdk.fail.clear()
    results = await asyncio.gather(*(manager.call_tool("remote_echo", {}) for _ in range(8)))
    assert results == ["ok"] * 8
    assert len(sdk.sessions) == 2


async def test_manual_refresh_recovers_despite_backoff_and_fresh_cache(sdk, recovery_manager):
    manager = recovery_manager
    sid = uuid4()
    await manager.add_server(sid, sdk.config)
    conn = manager._connections[sid]
    owner = conn._owner_task
    sdk.fail.set()
    await asyncio.wait_for(asyncio.shield(owner), timeout=1)
    sdk.fail.clear()
    manager._reconnect_after[sid] = asyncio.get_running_loop().time() + 3600
    tools = await manager.refresh_tools(sid)
    assert conn.connected
    assert [tool.name for tool in tools] == ["remote_echo"]
    assert sid not in manager._reconnect_after


async def test_removed_server_is_not_recreated_by_background_retry(sdk, recovery_manager):
    manager = recovery_manager
    sid = uuid4()
    sdk.available = False
    with pytest.raises(httpx.ConnectError):
        await manager.add_server(sid, sdk.config)
    manager.start()
    await eventually(lambda: manager._reconnect_failures.get(sid, 0) >= 2)
    await manager.remove_server(sid)
    sdk.available = True
    await asyncio.sleep(0.04)
    assert sid not in manager._configs
    assert sid not in manager._connections
    assert not sdk.sessions


async def test_shutdown_cancels_pending_background_handshake(sdk, recovery_manager):
    manager = recovery_manager
    sid = uuid4()
    await manager.add_server(sid, sdk.config)
    owner = manager._connections[sid]._owner_task
    sdk.fail.set()
    await asyncio.wait_for(asyncio.shield(owner), timeout=1)
    sdk.fail.clear()
    sdk.initialize_gate = asyncio.Event()
    manager.start()
    await eventually(lambda: len(sdk.sessions) == 2)
    await asyncio.wait_for(manager.shutdown(), timeout=1)
    assert not manager._configs
    assert not manager._connections
    assert manager._reconnect_task is None
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("mcp-connection-")]


async def test_cancelled_manager_shutdown_still_releases_connections(sdk, recovery_manager):
    manager = recovery_manager
    await manager.add_server(uuid4(), sdk.config)
    manager.start()
    sdk.close_gate = asyncio.Event()
    task = asyncio.create_task(manager.shutdown())
    await sdk.closing.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    sdk.close_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert not manager._connections
    assert manager._reconnect_task is None
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("mcp-connection-")]


async def test_manager_can_stop_before_recovery_task_starts():
    manager = MCPManager()
    manager.start()
    await manager.shutdown()
    assert manager._reconnect_task is None


@pytest.mark.parametrize("grouped", [False, True])
async def test_502_recovers_connection_without_replaying_uncertain_write(sdk, grouped):
    conn = MCPServerConnection(uuid4(), sdk.config)
    await conn.connect()
    first_session = conn.session
    calls = 0
    error = httpx.HTTPStatusError(
        "502 Bad Gateway", request=httpx.Request("POST", "http://test"),
        response=httpx.Response(502),
    )
    failure = ExceptionGroup("transport failure", [error]) if grouped else error

    async def uncertain_write(*args):
        nonlocal calls
        calls += 1
        raise failure

    first_session.call_tool = uncertain_write
    try:
        with pytest.raises(type(failure)):
            await conn.call_tool("save", {})
        assert calls == 1
        assert conn.connected and conn.session is not first_session
        assert await conn.call_tool("remote_echo", {}) == "ok"
    finally:
        await conn.disconnect()


@pytest_asyncio.fixture(params=["sse", "streamable-http"])
async def real_server(request, monkeypatch):
    from mcp.server.fastmcp import FastMCP
    from sse_starlette import sse as sse_module

    # Each fixture starts a new server/loop. SSE-Starlette keeps shutdown state
    # in thread globals; an earlier server's graceful exit must not close this one.
    monkeypatch.setattr(sse_module.AppStatus, "should_exit", False)
    monkeypatch.setattr(
        sse_module._thread_state, "shutdown_state", sse_module._ShutdownState(), raising=False,
    )

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
    upstream = app
    availability = SimpleNamespace(online=True)

    async def app(scope, receive, send):
        if scope["type"] == "http" and not availability.online:
            from starlette.responses import PlainTextResponse

            await PlainTextResponse("upstream stopped", status_code=502)(scope, receive, send)
        else:
            await upstream(scope, receive, send)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(_REAL_IO_TIMEOUT):
                while not server.started:
                    await asyncio.sleep(0.01)
            path = "/sse" if request.param == "sse" else "/mcp"
            yield {
                "transport_type": request.param, "url": f"http://127.0.0.1:{port}{path}",
                "timeout": _REAL_IO_TIMEOUT,
                "_availability": availability,
            }, entered
        finally:
            release.set()
            server.should_exit = True
            await asyncio.wait_for(task, timeout=_REAL_IO_TIMEOUT)


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
        await asyncio.wait_for(manager.add_server(sid, config), timeout=_REAL_IO_TIMEOUT)
        conn = manager._connections[sid]
        owner = conn._owner_task
        assert await manager.call_tool("echo", {"text": "first"}) == "first"
        call = asyncio.create_task(manager.call_tool("echo", {"text": "blocked", "block": True}))
        await asyncio.wait_for(entered.wait(), timeout=_REAL_IO_TIMEOUT)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call, timeout=1)
        assert await manager.call_tool("echo", {"text": "after cancel"}) == "after cancel"

        async def stale(*args):
            raise httpx.ConnectError("stale session")

        conn.session.call_tool = stale
        assert await asyncio.wait_for(
            manager.call_tool("echo", {"text": "reconnected"}), _REAL_IO_TIMEOUT
        ) == "reconnected"
        assert owner.done()
        assert conn._owner_task is not owner
    finally:
        await asyncio.wait_for(manager.shutdown(), timeout=_REAL_IO_TIMEOUT)
    # There must be no cancellation callback rescheduling itself after closure.
    await asyncio.sleep(0.02)
    settled = deliveries
    await asyncio.sleep(0.02)
    assert deliveries == settled
    assert deliveries < 500
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("mcp-connection-")]


async def test_real_sdk_gateway_502_then_background_recovery(real_server, recovery_manager):
    from mcp.shared.exceptions import McpError

    config, _ = real_server
    manager = recovery_manager
    sid = uuid4()
    await asyncio.wait_for(manager.add_server(sid, config), timeout=_REAL_IO_TIMEOUT)
    assert await manager.call_tool("echo", {"text": "before outage"}) == "before outage"
    config["_availability"].online = False
    # Bound the intentionally unanswered SSE request without shortening the
    # real SDK handshake budget used by the rest of this fixture.
    manager._connections[sid].config["timeout"] = 0.25
    try:
        with pytest.raises((httpx.HTTPError, ExceptionGroup, McpError, TimeoutError)):
            await asyncio.wait_for(manager.refresh_tools(sid), timeout=_REAL_IO_TIMEOUT + 1)
        await eventually(lambda: not manager._connections[sid].connected)
        manager._connections[sid].config["timeout"] = _REAL_IO_TIMEOUT
        manager.start()
        await eventually(lambda: manager._reconnect_failures.get(sid, 0) >= 2)
    finally:
        config["_availability"].online = True
    await eventually(lambda: manager._connections[sid].connected)
    assert await manager.call_tool("echo", {"text": "after recovery"}) == "after recovery"
