"""Platform trace capture and tenant-scoped reads use PostgreSQL directly."""

import asyncio
import json
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_platform.auth.dependencies import get_current_user
from agent_platform.core.agent import AgentLoop
from agent_platform.db.models import AgentTraceLog, User
from agent_platform.interface.api import app
from agent_platform.llm import LLMChunk, ToolCall
from agent_platform.observation import recorder as observations
from agent_platform.observation.client import get_langfuse_client
from agent_platform.tools.executor import ToolExecutor
from agent_platform.tools.registry import ToolRegistry


class RecordedProvider:
    def __init__(self, model, first_batch=()):
        self.model = model
        self.first_batch = first_batch

    async def stream(self, messages, tools=None):
        calls, self.first_batch = self.first_batch, ()
        if calls:
            for index, (name, args) in enumerate(calls):
                yield LLMChunk(
                    type="tool_call_start",
                    tool_call=ToolCall(id=f"call-{index}", name=name, arguments=args),
                    tool_call_index=index,
                )
                yield LLMChunk(type="tool_call_delta", tool_call_index=index, argument_delta=json.dumps(args))
        else:
            yield LLMChunk(type="text_delta", content="done")
        yield LLMChunk(type="done")
        observations.get_recorder().record_llm_call(model=self.model, provider="test", duration_ms=1)


@pytest.mark.postgres
async def test_execution_trace_records_concurrent_delegation_and_scopes_queries(
    client, db_session, engine, monkeypatch,
):
    tenant_id, other_tenant = uuid4(), uuid4()
    user_id, session_id = uuid4(), uuid4()
    parent_id, first_id, second_id = uuid4(), uuid4(), uuid4()
    executor = ToolExecutor(ToolRegistry(), sandbox_mgr=None)
    monkeypatch.setattr(executor, "tool_timeout", lambda name: 0.01 if name == "slow" else 5)
    recorder = observations.Recorder()
    monkeypatch.setattr(observations, "_recorder", recorder)
    monkeypatch.setattr(observations, "get_session_factory", lambda: async_sessionmaker(engine))
    assert get_langfuse_client() is None

    async def run(agent_id, model, batch):
        loop = AgentLoop(
            provider=RecordedProvider(model, batch), tool_executor=executor, execution_tenant_id=tenant_id,
        )
        loop.execution_agent_id = agent_id
        async for _ in loop.run("test", user_id, session_id, [], []):
            pass

    async def delegate(args, *_args, **_kwargs):
        child_id = UUID(args["agent"])
        await run(child_id, f"child-{child_id}", [("slow", {})] if child_id == first_id else [])
        return "done"

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(60)

    executor.register_direct_handler("delegate_task", delegate, permission_level="read")
    executor.register_direct_handler("slow", slow, permission_level="read")
    recorder.start()
    try:
        await run(parent_id, "parent", [
            ("delegate_task", {"agent": str(first_id)}),
            ("delegate_task", {"agent": str(second_id)}),
        ])
        async with asyncio.timeout(8):
            while (root := await db_session.scalar(select(AgentTraceLog).where(
                AgentTraceLog.agent_id == parent_id,
            ))) is None:
                await asyncio.sleep(0.05)
    finally:
        await recorder.shutdown()
    assert observations.get_obs_context() is None

    # Historical rows remain searchable. An inconsistent child relation cannot
    # pull another tenant's execution into the current scope.
    legacy_id, foreign_id = uuid4(), uuid4()
    db_session.add_all([
        AgentTraceLog(trace_id=legacy_id, tenant_id=tenant_id, user_id=user_id,
                      created_at=datetime.now(UTC) - timedelta(days=30)),
        AgentTraceLog(trace_id=foreign_id, root_trace_id=root.trace_id, tenant_id=other_tenant,
                      session_id=session_id, user_id=user_id),
    ])
    await db_session.commit()
    user = User(id=user_id, tenant_id=tenant_id, role="admin")
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: user)

    response = await client.get("/api/observability/traces", params={"roots_only": True, "session_id": session_id})
    assert response.status_code == 200
    assert [row["trace_id"] for row in response.json()["items"]] == [str(root.trace_id)]
    response = await client.get("/api/observability/traces", params={"trace_id": legacy_id})
    assert response.json()["total"] == 1

    path = f"/api/observability/traces/{root.trace_id}"
    response = await client.get(path, params={"include_children": True})
    assert response.status_code == 200
    detail = response.json()
    children = {row["agent_id"]: row for row in detail["child_traces"]}
    assert set(children) == {str(first_id), str(second_id)}
    delegated = [row for row in detail["tool_calls"] if row["tool_name"] == "delegate_task"]
    assert len(delegated) == 2
    assert {row["parent_tool_span_id"] for row in children.values()} == {row["span_id"] for row in delegated}
    assert all(row["iteration"] == 1 for row in delegated)
    assert [row["iteration"] for row in detail["llm_calls"] if row["model"] == "parent"] == [1, 2]
    slow_tool = next(row for row in detail["tool_calls"] if row["tool_name"] == "slow")
    assert slow_tool["trace_id"] == children[str(first_id)]["trace_id"]
    assert slow_tool["is_error"] and slow_tool["error_type"] == "timeout"

    selected_child = await client.get(
        f"/api/observability/traces/{slow_tool['trace_id']}", params={"include_children": True},
    )
    assert selected_child.json()["trace"]["trace_id"] == str(root.trace_id)
    assert "child_traces" not in (await client.get(path)).json()
    assert (await client.get(f"/api/observability/traces/{foreign_id}")).status_code == 404

    user.role = "user"
    assert (await client.get(path)).status_code == 403


@pytest.mark.parametrize("cancelled", [False, True])
async def test_execution_trace_finalizes_in_its_worker(monkeypatch, cancelled):
    recorder = observations.Recorder()
    monkeypatch.setattr(observations, "_recorder", recorder)

    class FailingProvider:
        model = "test"

        async def stream(self, messages, tools=None):
            if not cancelled:
                raise RuntimeError("model failed")
            yield LLMChunk(type="text_delta", content="partial")

    loop = AgentLoop(
        provider=FailingProvider(), tool_executor=ToolExecutor(ToolRegistry(), sandbox_mgr=None),
        execution_tenant_id=uuid4(),
    )
    loop.execution_agent_id = uuid4()
    async with aclosing(loop.run("test", uuid4(), uuid4(), [], [])) as events:
        if cancelled:
            await anext(events)
        else:
            with pytest.raises(RuntimeError, match="model failed"):
                await anext(events)

    kind, trace = recorder._queue.get_nowait()
    assert kind == "trace"
    assert trace["status"] == ("interrupted" if cancelled else "error")
    assert trace["iteration_count"] == 1
    assert observations.get_obs_context() is None
