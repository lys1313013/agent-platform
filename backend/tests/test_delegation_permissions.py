"""Delegation never widens the invoking agent's actual tool grant."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent_platform.delegation.handler import (
    _build_child_tools,
    _build_dynamic_agent,
    handle_delegate_task,
)
from agent_platform.tools.executor import ToolExecutor
from agent_platform.tools.registry import Tool, ToolRegistry


def executor():
    registry = ToolRegistry()
    for name in ("read_file", "write_file", "run_shell", "delegate_task"):
        registry.register(Tool(name, name, {}, requires_sandbox=False))
    return ToolExecutor(registry, SimpleNamespace())


@pytest.mark.parametrize("grant", [None, set(), {"read_file"}, {"read_file", "delegate_task"}])
@pytest.mark.parametrize("configured", [False, True])
def test_children_only_narrow_parent_permissions(grant, configured):
    child = _build_dynamic_agent(uuid4(), "specialist", "", uuid4())
    if configured:
        child.enabled_tools = ["read_file", "write_file", "run_shell"]
    tools, schemas = _build_child_tools(executor(), child, 1, 3, parent_allowed_tools=grant)
    names = {tool.name for tool in tools}
    assert names == (grant or set())
    assert {schema["function"]["name"] for schema in schemas} == names
    nested, _ = _build_child_tools(executor(), child, 2, 3, parent_allowed_tools=names)
    assert {tool.name for tool in nested} == names


@pytest.mark.parametrize("grant", [None, set(), {"read_file"}])
async def test_missing_or_empty_delegation_authorization_rejected_before_db(grant):
    result = await handle_delegate_task({"task": "write", "role_name": "writer"},
        str(uuid4()), str(uuid4()), delegation=SimpleNamespace(), tool_executor=executor(), allowed_tools=grant)
    assert "explicit parent tool permission grant" in result


@pytest.mark.parametrize("registered", [True, False])
async def test_executor_preserves_parent_grant_for_direct_handlers(registered):
    ex = executor()
    name = "delegate_task" if registered else "custom_direct"
    handler = AsyncMock(return_value="ok")
    ex.register_direct_handler(name, handler, permission_level="read")
    grant = {name, "read_file"}
    result = await ex.execute(name, {}, "call", str(uuid4()), str(uuid4()), allowed_tools=grant)
    assert result.success
    assert handler.await_args.kwargs["allowed_tools"] == grant


@pytest.mark.parametrize("outcome, expected", [
    ("completed", "completed"), ("timeout", "timeout"), ("cancel", "failed"),
    ("max_iterations", "failed"),
])
async def test_running_delegation_releases_its_connection_and_records_termination(
    monkeypatch, tmp_path, outcome, expected,
):
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from agent_platform.core import chat
    from agent_platform.core.agent import AgentStep, DelegationContext
    from agent_platform.db.models import Delegation
    from agent_platform.delegation import handler

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'delegations.db'}",
                                 pool_size=1, max_overflow=0, pool_timeout=0.15)
    monkeypatch.setattr(engine.dialect.type_compiler_instance, "visit_JSONB",
                        lambda _type, **_kwargs: "JSON", raising=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Delegation.__table__.create)
    monkeypatch.setattr(handler, "get_session_factory", lambda: factory)
    monkeypatch.setattr(handler, "require_execution_user", AsyncMock())
    monkeypatch.setattr(handler, "_build_child_system_prompt", AsyncMock(return_value="test"))
    monkeypatch.setattr(handler, "_build_child_provider", AsyncMock(return_value=object()))
    monkeypatch.setattr(chat, "resolve_trust_level", AsyncMock(return_value="auto_all"))

    class Child:
        def __init__(self, **kwargs):
            self.stop_reason = "max_iterations" if outcome == "max_iterations" else None

        async def run(self, **kwargs):
            async with factory() as reader:
                row = await reader.scalar(select(Delegation))
                assert row.status == "running"
            if outcome == "cancel":
                raise asyncio.CancelledError()
            if outcome == "timeout":
                raise TimeoutError()
            yield AgentStep(step=1, done=True, final_output="ok")

    monkeypatch.setattr(handler, "AgentLoop", Child)
    queue = asyncio.Queue()
    delegation = DelegationContext(parent_agent_id=uuid4(), tenant_id=uuid4(), delegation_depth=0, max_depth=1,
                                   event_queue=queue,
                                   workspace_id=uuid4(), workspace_slug="test")
    call = handle_delegate_task(
        {"task": "test", "role_name": "child"}, str(uuid4()), str(uuid4()),
        tool_executor=executor(), delegation=delegation, allowed_tools={"delegate_task"},
    )
    try:
        if outcome == "completed":
            assert await call == "ok"
        else:
            error = {"timeout": TimeoutError, "cancel": asyncio.CancelledError, "max_iterations": RuntimeError}
            with pytest.raises(error[outcome]):
                await call
        async with factory() as reader:
            row = await reader.scalar(select(Delegation))
            assert row.status == expected and row.completed_at and row.duration_ms is not None
        emitted = [queue.get_nowait() for _ in range(queue.qsize())]
        assert emitted[-1]["status"] == expected
    finally:
        await engine.dispose()
