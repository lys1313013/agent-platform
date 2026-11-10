"""Real SQL authorization and approval regressions; no external tool calls."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from agent_platform.core import chat, chat_runs
from agent_platform.core.agent import AgentLoop, AgentStep, ToolContext
from agent_platform.core.confirmation import confirmation_manager
from agent_platform.core.execution_scope import bind_execution_scope
from agent_platform.db.models import (
    Agent,
    ChatRun,
    ChatRunEvent,
    MCPServer,
    Message,
    Session,
    Tenant,
    TenantMembership,
    User,
    UserConfig,
)
from agent_platform.llm import ToolCall
from agent_platform.tools.executor import ToolExecutor
from agent_platform.tools.mcp.adapter import MCPToolInfo
from agent_platform.tools.registry import Tool, ToolRegistry


@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kwargs):
    return "JSON"


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (User, UserConfig, Tenant, TenantMembership, Agent, Session, Message, ChatRun, ChatRunEvent, MCPServer):
            await conn.run_sync(model.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    a, b, uid, aid, bid, sid, server = [uuid4() for _ in range(7)]
    async with factory() as db:
        user = User(id=uid, username="review", email="review@test.invalid", password_hash="unused", tenant_id=b)
        db.add_all([user, Tenant(id=a, name="A", slug="a"), Tenant(id=b, name="B", slug="b"),
                    TenantMembership(user_id=uid, tenant_id=a), TenantMembership(user_id=uid, tenant_id=b),
                    Agent(id=aid, name="A", created_by=uid, tenant_id=a),
                    Agent(id=bid, name="B", created_by=uid, tenant_id=b),
                    Session(id=sid, user_id=uid, agent_id=aid),
                    MCPServer(id=server, tenant_id=a, name="A", transport_type="sse", url="https://invalid.test")])
        await db.commit()
    monkeypatch.setattr(chat_runs, "get_session_factory", lambda: factory)
    monkeypatch.setattr("agent_platform.db.connection.get_session_factory", lambda: factory)
    yield NS(factory=factory, a=a, b=b, uid=uid, aid=aid, bid=bid, sid=sid, server=server, user=user)
    await chat_runs.shutdown()
    await engine.dispose()


async def test_request_cannot_override_session_agent(env):
    async with env.factory() as db:
        with pytest.raises(HTTPException) as error:
            await chat.load_chat_agent(db, env.bid, env.sid, env.user)
        assert error.value.status_code == 409
        await db.execute(delete(TenantMembership).where(TenantMembership.tenant_id == env.a))
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await chat.load_chat_agent(db, env.bid, env.sid, env.user)
        assert error.value.status_code == 403


@pytest.mark.parametrize("revocation", ["membership", "tenant", "user", "private"])
async def test_run_replay_rechecks_object_access_and_finalizes_after_revocation(env, revocation):
    async with env.factory() as db:
        run = await chat_runs.reserve(db, env.uid, env.sid, {"message": "test"})
        await db.commit()
        assert run.request["_execution_tenant_id"] == str(env.a)
        assert (await chat_runs.owned(db, env.uid, run.id)).id == run.id
    stream = chat_runs.subscribe(run.id, env.uid)
    assert '"type": "run"' in await anext(stream)
    async with env.factory() as db:
        if revocation == "membership":
            await db.execute(delete(TenantMembership).where(TenantMembership.tenant_id == env.a))
        elif revocation == "tenant":
            (await db.get(Tenant, env.a)).is_active = False
        elif revocation == "user":
            (await db.get(User, env.uid)).is_active = False
        else:
            agent = await db.get(Agent, env.aid)
            agent.visibility, agent.created_by = "private", uuid4()
        await db.commit()
    assert "access_revoked" in await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    async with env.factory() as db:
        with pytest.raises(HTTPException):
            await chat_runs.latest(db, env.uid, env.sid)
        with pytest.raises(HTTPException):
            await chat_runs.owned(db, env.uid, run.id)
    await chat_runs.finish(run.id, env.uid, "interrupted")
    async with env.factory() as db:
        assert (await db.get(ChatRun, run.id)).status == "interrupted"
        assert (await db.get(User, env.uid)).tenant_id == env.b


async def test_mcp_dispatch_rechecks_tenant_even_with_forged_allowlist(env):
    manager = NS(_tool_to_server={"a_read": env.server}, is_mcp_tool=lambda n: n == "a_read",
                 call_tool=AsyncMock(return_value="ok"),
                 list_all_tools=lambda: [("a_read", MCPToolInfo(name="read", description="A"))])
    executor = ToolExecutor(ToolRegistry(), None, mcp_manager=manager)
    assert not chat.filter_tools_by_agent(executor, None)[1]
    for tenant, allowed in [(env.b, False), (env.a, True)]:
        with bind_execution_scope(env.uid, tenant):
            result = await executor._execute_impl("a_read", {}, "call", str(env.uid), str(env.sid),
                                                  allowed_tools={"a_read"}, trust_level="auto_all")
        assert result.success is allowed
    assert manager.call_tool.await_count == 1
    async with env.factory() as db:
        await db.execute(delete(TenantMembership).where(TenantMembership.tenant_id == env.a))
        await db.commit()
    with bind_execution_scope(env.uid, env.a):
        result = await executor._execute_impl("a_read", {}, "call", str(env.uid), str(env.sid), trust_level="auto_all")
    assert not result.success
    assert manager.call_tool.await_count == 1


async def test_user_trust_setting_is_used(env):
    async with env.factory() as db:
        db.add(UserConfig(user_id=env.uid, trust_level="ask_always"))
        await db.commit()
        assert await chat.resolve_trust_level(db, env.uid) == "ask_always"


async def test_tool_catalog_does_not_disclose_another_tenants_mcp_schema(env):
    from agent_platform.interface.routes.tools import list_tools

    manager = NS(_tool_to_server={"a_read": env.server},
                 list_all_tools=lambda: [("a_read", MCPToolInfo(name="read", description="private schema"))])
    request = NS(app=NS(state=NS(tool_executor=ToolExecutor(ToolRegistry(), None), mcp_manager=manager)))
    async with env.factory() as db:
        assert await list_tools(request, env.user, db) == []


async def test_unbound_run_keeps_tenant_when_ui_selection_becomes_unusable(env):
    async with env.factory() as db:
        session = await db.get(Session, env.sid)
        session.agent_id = None
        await db.flush()
        run = await chat_runs.reserve(db, env.uid, env.sid, {"message": "test"}, tenant_id=env.a)
        (await db.get(Tenant, env.b)).is_active = False
        await db.commit()
        assert (await chat_runs.latest(db, env.uid, env.sid)).id == run.id
        assert await chat.load_chat_agent(db, None, env.sid, env.user, pinned_tenant_id=env.a) is None
    await chat_runs.finish(run.id, env.uid, "interrupted")
    async with env.factory() as db:
        resumed = await chat_runs.reserve(db, env.uid, env.sid, {}, run.id, tenant_id=env.a)
        assert resumed.request["_execution_tenant_id"] == str(env.a)


async def test_confirmation_uses_execution_tenant_after_ui_switch_and_rechecks_membership(env):
    from agent_platform.interface.routes.confirmations import get_pending_confirmations

    loop = AgentLoop(provider=Mock(), tool_executor=ToolExecutor(ToolRegistry(), None),
                     event_queue=asyncio.Queue(), execution_tenant_id=env.a)
    flow = loop._run_ask_user_flow(ToolCall("confirm", "AskUserQuestion", {
        "question": "Approve?", "mode": "approve", "context": {"execution_tenant_id": str(env.b)},
    }), ToolContext(env.uid, env.sid))
    try:
        await anext(flow)
        confirmation = confirmation_manager.get_pending(str(env.sid))[0]
        assert confirmation.execution_tenant_id == env.a
        async with env.factory() as db:
            (await db.get(Session, env.sid)).agent_id = None
            (await db.get(Tenant, env.b)).is_active = False
            await db.commit()
            assert (await get_pending_confirmations(env.sid, env.user, db))[0].id == confirmation.id
            await db.execute(delete(TenantMembership).where(TenantMembership.tenant_id == env.a))
            await db.commit()
            with pytest.raises(HTTPException) as error:
                await get_pending_confirmations(env.sid, env.user, db)
            assert error.value.status_code == 403
    finally:
        await flow.aclose()
    assert not confirmation_manager.get_pending(str(env.sid))


async def test_executor_requires_explicit_policy_for_unregistered_direct_handler():
    executor = ToolExecutor(ToolRegistry(), None)
    handler = AsyncMock(return_value="ok")
    executor.register_direct_handler("custom", handler)
    result = await executor._execute_impl("custom", {}, "call", str(uuid4()), str(uuid4()))
    assert not result.success
    handler.assert_not_called()


async def test_approval_timeout_does_not_dispatch_or_leave_pending(monkeypatch):
    executor = ToolExecutor(ToolRegistry(), None)
    queue = asyncio.Queue()
    loop = AgentLoop(Mock(), executor, trust_level="ask_always", event_queue=queue)
    ctx = ToolContext(uuid4(), uuid4(), trust_level="ask_always")
    create = confirmation_manager.create_confirmation

    def short_confirmation(**kwargs):
        result = create(**kwargs)
        result.timeout_seconds = 0.01
        return result

    monkeypatch.setattr(confirmation_manager, "create_confirmation", short_confirmation)
    step = AgentStep(1, tool_calls=[ToolCall("call", "custom", {})])
    async for _ in loop._execute_step_tools(step, ctx):
        pass
    assert not step.tool_results[0].success
    assert not confirmation_manager.get_pending(str(ctx.session_id))


@pytest.mark.parametrize("trust,permission,approval,executions", [
    ("ask_always", "read", "approved", 1), ("ask_always", "read", "rejected", 0),
    ("ask_dangerous", "dangerous", "modified", 0), ("ask_dangerous", "dangerous", "approved", 1),
    ("ask_dangerous", "read", None, 1), ("auto_all", "dangerous", None, 1),
    ("ask_always", "dangerous", None, 0),
])
async def test_approval_controls_dispatch(trust, permission, approval, executions):
    registry = ToolRegistry()
    registry.register(Tool("test_tool", "test", {}, False, permission_level=permission))
    executor = ToolExecutor(registry, None)
    executor._execute_direct = AsyncMock(return_value="ok")
    executor._prepare_output = AsyncMock()
    executor._record_tool_call = Mock()
    queue = asyncio.Queue() if approval else None
    loop = AgentLoop(Mock(), executor, trust_level=trust, event_queue=queue, allowed_tools={"test_tool"})
    ctx = ToolContext(uuid4(), uuid4(), trust_level=trust)
    step = AgentStep(1, tool_calls=[ToolCall("call", "test_tool", {})])
    async for event in loop._execute_step_tools(step, ctx):
        if getattr(event, "kind", None) == "confirmation_flow":
            request = await queue.get()
            assert request["tool_call_id"] == "call"
            assert not executor._execute_direct.called
            confirmation_manager.resolve_confirmation(request["confirmation_id"], {"status": approval})
    assert executor._execute_direct.await_count == executions
    assert not confirmation_manager.get_pending(str(ctx.session_id))


async def test_closing_approval_does_not_leave_pending_confirmation():
    executor = ToolExecutor(ToolRegistry(), None)
    loop = AgentLoop(Mock(), executor, trust_level="ask_always", event_queue=asyncio.Queue())
    ctx = ToolContext(uuid4(), uuid4(), trust_level="ask_always")
    flow = loop._execute_step_tools(AgentStep(1, tool_calls=[ToolCall("c", "test", {})]), ctx)
    await anext(flow)
    await flow.aclose()
    assert not confirmation_manager.get_pending(str(ctx.session_id))


def test_tool_approval_survives_message_persistence_and_does_not_shift_legacy_question():
    from agent_platform.core.turn_metadata import InteractiveTurnMetadata

    metadata = InteractiveTurnMetadata()
    calls = [{"id": "shell", "name": "run_shell", "arguments": {}},
             {"id": "ask", "name": "AskUserQuestion", "arguments": {}}]
    approval = {"type": "confirmation_required", "tool_call_id": "shell",
                "confirmation_id": "approval", "question": "Execute?", "mode": "approve"}
    metadata.track(approval)
    metadata.track(approval)  # replay must be idempotent
    metadata.track({"type": "confirmation_resolved", "confirmation_id": "approval", "status": "approved"})
    metadata.track({"type": "confirmation_required", "confirmation_id": "legacy", "question": "Choose?"})
    metadata.merge(calls)
    metadata.merge(calls)
    assert calls[0]["confirmation"]["resolved"]["status"] == "approved"
    assert calls[1]["confirmation"]["confirmation_id"] == "legacy"


async def test_remote_tool_is_denied_outside_its_owning_tenant():
    from agent_platform.tools.executor import SecurityError
    from agent_platform.tools.remote.manager import RemoteToolConfig

    user_id = uuid4()
    executor = ToolExecutor(ToolRegistry(), Mock())
    executor.remote_manager = Mock(get_config=Mock(return_value=RemoteToolConfig(
        id=uuid4(), name="crm_lookup", label="CRM", description="lookup",
        method="GET", url_template="https://example.test", parameters_schema={},
        headers=None, auth_type="none", auth_config=None, query_params=None,
        body_template=None, response_extract=None, timeout=10, is_active=True,
        tenant_id=uuid4(),
    )))

    with bind_execution_scope(user_id, uuid4()):
        with pytest.raises(SecurityError, match="not available in the execution tenant"):
            await executor._authorize_remote_tool("crm_lookup", str(user_id))
