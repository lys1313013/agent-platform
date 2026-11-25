"""The published API consumes the current AgentLoop event contract."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import anyio
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from agent_platform.core import chat as core_chat
from agent_platform.core.agent import AgentStep
from agent_platform.core.execution_scope import execution_tenant_id
from agent_platform.db.models import Message
from agent_platform.db.models import Session as ChatSession
from agent_platform.interface.routes import agent_api


@compiles(JSONB, "sqlite")
def sqlite_jsonb(_type, _compiler, **_kwargs):
    return "JSON"


@pytest_asyncio.fixture
async def external_chat(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ChatSession.__table__.create)
        await conn.run_sync(Message.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    agent = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), skills=[], children=[], system_prompt="base")
    session_id = uuid4()
    async with factory() as db:
        db.add(ChatSession(id=session_id, user_id=user.id, agent_id=agent.id, source="api", context_summary="previous decision"))
        db.add(Message(id=uuid4(), session_id=session_id, user_id=user.id, role="user", content="repeat"))
        await db.commit()
    monkeypatch.setattr("agent_platform.db.connection.get_session_factory", lambda: factory)
    monkeypatch.setattr(agent_api, "_load_agent_with_relations", AsyncMock(return_value=agent))
    monkeypatch.setattr(agent_api, "_get_active_version", AsyncMock(return_value=SimpleNamespace(config_snapshot={})))
    monkeypatch.setattr(agent_api, "_filter_tools_for_agent", Mock(return_value=([], [])))
    monkeypatch.setattr(core_chat, "build_system_prompt", Mock(return_value="base"))

    async def memory(*_args, **_kwargs):
        assert execution_tenant_id(user.id) == agent.tenant_id
        return {"l1_memories": [], "l2_memories": [], "l3_memories": [], "daily_memories": []}

    monkeypatch.setattr(core_chat.MemoryService, "get_memories_for_prompt", memory)
    monkeypatch.setattr(core_chat, "get_memory_top_k", AsyncMock(return_value=5))
    monkeypatch.setattr(core_chat, "resolve_model", AsyncMock(return_value=SimpleNamespace(
        id=uuid4(), is_multimodal=False, provider=SimpleNamespace(provider_type="openai"))))
    # Transport persistence is isolated from object auth (covered by test_execution_security).
    monkeypatch.setattr(agent_api, "owned_session", AsyncMock())
    from agent_platform.storage import chat_attachments
    monkeypatch.setattr(chat_attachments, "ObjectStorage", Mock())
    monkeypatch.setattr(core_chat.SkillService, "get_skills_for_prompt", AsyncMock(return_value=[]))
    monkeypatch.setattr("agent_platform.workspaces.service.WorkspaceService.get_or_create_default", AsyncMock(return_value=SimpleNamespace(id=uuid4(), slug="default")))
    # Only the profile query uses a table absent from this disposable schema.
    real_execute = factory.class_.execute

    async def execute(db, statement, *args, **kwargs):
        if "user_profiles" in str(statement):
            return Mock(scalar_one_or_none=Mock(return_value=None))
        return await real_execute(db, statement, *args, **kwargs)

    monkeypatch.setattr(factory.class_, "execute", execute)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(tool_executor=SimpleNamespace(mcp_manager=None))))
    yield SimpleNamespace(factory=factory, user=user, agent=agent, session_id=session_id, request=request)
    await engine.dispose()


@pytest.mark.parametrize("outcome", ["success", "plain", "final_only", "final_changed", "error", "disconnect", "cancel", "cancel_scope"])
async def test_stream_persists_current_events(external_chat, monkeypatch, outcome):
    fixture = external_chat
    observed = []
    closed = asyncio.Event()
    waiting = asyncio.Event()

    async def run(**kwargs):
        try:
            observed.extend(kwargs["conversation_history"])
            yield "reasoning_delta:checking"
            yield "reasoning:checking"
            if outcome not in {"plain", "final_only", "final_changed"}:
                yield 'tool_call:one:read_file:{"path":"a"}'
                yield 'tool_call:two:read_file:{"path":"b"}'
                yield 'tool_result:two:read_file:ok:"B"'
                yield 'tool_result:one:read_file:ok:"A"'
            if outcome != "final_only":
                yield "text_delta:answer"
            if outcome == "error":
                raise RuntimeError("provider unavailable")
            if outcome in {"disconnect", "cancel", "cancel_scope"}:
                waiting.set()
                await asyncio.Event().wait()
            yield AgentStep(step=2, done=True, final_output="iteration limit" if outcome == "final_changed" else "answer")
        finally:
            closed.set()

    monkeypatch.setattr(core_chat, "build_agent_loop", AsyncMock(return_value=SimpleNamespace(provider=Mock(), run=run)))
    async with fixture.factory() as db:
        response = await agent_api.sse_chat(fixture.session_id, agent_api.SseChatRequest(message="repeat"), fixture.request, fixture.user, db)
    frames = []
    iterator = response.body_iterator
    async for frame in iterator:
        frames.append(frame)
        if outcome in {"disconnect", "cancel", "cancel_scope"} and "event: text_delta" in frame:
            if outcome == "disconnect":
                await asyncio.create_task(iterator.aclose())
            elif outcome == "cancel_scope":
                async def consume_rest():
                    async for remaining in iterator:
                        frames.append(remaining)
                async with anyio.create_task_group() as group:
                    group.start_soon(consume_rest)
                    await asyncio.wait_for(waiting.wait(), timeout=2)
                    group.cancel_scope.cancel()
            else:
                pending = asyncio.create_task(anext(iterator))
                await asyncio.wait_for(waiting.wait(), timeout=2)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            break
    assert closed.is_set()
    assert execution_tenant_id(fixture.user.id) is None
    assert any("previous decision" in str(m.content) for m in observed)
    assert sum(m.role == "user" and m.content == "repeat" for m in observed) == 1
    wire = "".join(frames)
    assert 'event: text_delta\ndata: {"text": "answer"}' in wire
    assert wire.count("event: tool_call\n") == (0 if outcome in {"plain", "final_only", "final_changed"} else 2)
    assert wire.count("event: tool_result\n") == (0 if outcome in {"plain", "final_only", "final_changed"} else 2)
    assert ("event: error" in wire) is (outcome == "error")
    assert ("event: done" in wire) is (outcome in {"success", "plain", "final_only", "final_changed"})
    async with fixture.factory() as db:
        rows = (await db.scalars(select(Message).where(Message.session_id == fixture.session_id).order_by(Message.created_at))).all()
    assert len(rows) == 3  # Old user turn, new user turn, one assistant row.
    assistant = next(m for m in rows if m.role == "assistant")
    assert assistant.content == ("iteration limit" if outcome == "final_changed" else "answer")
    if outcome in {"plain", "final_only", "final_changed"}:
        assert assistant.tool_calls is None
    else:
        assert [call["result"]["preview"] for call in assistant.tool_calls] == ["A", "B"]
    assert assistant.reasoning[0]["content"] == "checking"
    if outcome in {"success", "plain", "final_only", "final_changed"}:
        done = next(frame for frame in frames if frame.startswith("event: done"))
        payload = json.loads(done.split("data: ")[1])
        assert payload["message_id"] == str(assistant.id)
        assert payload["content"] == assistant.content


async def test_execute_rejects_session_bound_to_another_agent(external_chat):
    fixture = external_chat
    # The existing session belongs to another agent, although it has the same owner.
    async with fixture.factory() as db:
        session = await db.get(ChatSession, fixture.session_id)
        session.agent_id = uuid4()
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await agent_api._execute_agent_inner(
                fixture.agent.id, agent_api.ExecuteRequest(user_input="do not mix tenants", session_rid=str(fixture.session_id)),
                fixture.request, fixture.user, db,
            )
    assert error.value.status_code == 404


@pytest.mark.parametrize("transport", ["execute", "stream"])
@pytest.mark.parametrize("saved_prompt", ["missing", "", None, "published prompt"])
async def test_published_prompt_does_not_fall_back_when_explicitly_empty(
    external_chat, monkeypatch, transport, saved_prompt,
):
    fixture = external_chat
    snapshot = {} if saved_prompt == "missing" else {"system_prompt": saved_prompt}
    monkeypatch.setattr(agent_api, "_get_active_version", AsyncMock(
        return_value=SimpleNamespace(config_snapshot=snapshot),
    ))

    async def run(**_kwargs):
        yield AgentStep(step=1, done=True, final_output="answer")

    monkeypatch.setattr(core_chat, "build_agent_loop", AsyncMock(
        return_value=SimpleNamespace(provider=Mock(), run=run),
    ))
    async with fixture.factory() as db:
        if transport == "execute":
            await agent_api._execute_agent_inner(
                fixture.agent.id,
                agent_api.ExecuteRequest(user_input="repeat", session_rid=str(fixture.session_id)),
                fixture.request, fixture.user, db,
            )
        else:
            response = await agent_api.sse_chat(
                fixture.session_id, agent_api.SseChatRequest(message="repeat"),
                fixture.request, fixture.user, db,
            )
            async for _frame in response.body_iterator:
                pass
    expected = fixture.agent.system_prompt if saved_prompt == "missing" else saved_prompt
    assert core_chat.build_system_prompt.call_args.kwargs["agent_prompt"] == expected


@pytest.mark.parametrize("transport", ["execute", "stream"])
async def test_published_bindings_survive_draft_changes(external_chat, monkeypatch, transport):
    from agent_platform.db.models import Agent

    f = external_chat
    old_skill, draft_skill, old_child, draft_child, private_child = [uuid4() for _ in range(5)]
    snapshot = {"skill_ids": [str(old_skill)], "child_ids": [str(old_child), str(private_child)]}
    monkeypatch.setattr(agent_api, "_get_active_version", AsyncMock(return_value=SimpleNamespace(config_snapshot=snapshot)))
    f.agent.skills = [SimpleNamespace(id=draft_skill)]
    f.agent.children = [SimpleNamespace(id=draft_child)]
    async with f.factory.kw["bind"].begin() as connection:
        await connection.run_sync(Agent.__table__.create)
    async with f.factory() as db:
        db.add_all([Agent(id=cid, name="child", tenant_id=f.agent.tenant_id, created_by=owner, visibility=scope)
                    for cid, owner, scope in ((old_child, f.user.id, "tenant"), (draft_child, f.user.id, "tenant"),
                                              (private_child, uuid4(), "private"))])
        await db.commit()
    async def run(**kwargs):
        yield AgentStep(step=1, done=True, final_output="answer")

    build = AsyncMock(return_value=SimpleNamespace(provider=Mock(), run=run))
    monkeypatch.setattr(core_chat, "build_agent_loop", build)
    async with f.factory() as db:
        if transport == "execute":
            await agent_api.execute_agent(f.agent.id, agent_api.ExecuteRequest(
                user_input="repeat", session_rid=str(f.session_id)), f.request, f.user, db)
            await db.commit()
        else:
            stream = await agent_api.sse_chat(f.session_id, agent_api.SseChatRequest(message="repeat"), f.request, f.user, db)
            assert any("event: done" in frame for frame in [frame async for frame in stream.body_iterator])
    assert core_chat.SkillService.get_skills_for_prompt.await_args.kwargs["bound_skill_ids"] == (old_skill,)
    assert [child.id for child in core_chat.build_system_prompt.call_args.kwargs["child_agents"]] == [old_child]
    assert build.await_args.kwargs["delegation"].published_bindings.child_ids == (old_child, private_child)


async def test_feedback_changes_preserve_existing_usage(external_chat):
    f = external_chat
    async with f.factory() as db:
        row = await db.scalar(select(Message).where(Message.session_id == f.session_id))
        row.token_usage = {"input_tokens": 12}
        await db.commit()
        message_id = row.id
    for choice in ("up", "down"):
        async with f.factory() as db:
            await agent_api.submit_message_feedback(f.session_id, message_id, {"feedback": choice}, f.user, db)
            await db.commit()
        async with f.factory() as db:
            assert (await db.get(Message, message_id)).token_usage == {"input_tokens": 12, "feedback": choice}


def test_variable_payload_requires_finite_bounded_json():
    from pydantic import ValidationError

    for value in ({"size": "大" * 11000}, {"amount": float("nan")}, []):
        with pytest.raises(ValidationError):
            agent_api.ExecuteRequest(user_input="hello", variables=value)


async def test_delegate_uses_snapshot_membership_and_rechecks_live_access(monkeypatch):
    from agent_platform.delegation import handler

    parent, frozen, draft, owner, tenant = [uuid4() for _ in range(5)]
    visible = SimpleNamespace(id=frozen)
    load = AsyncMock(return_value=visible)
    actor = SimpleNamespace(id=owner)
    verify = AsyncMock(return_value=actor)
    monkeypatch.setattr(core_chat, "load_agent", load)
    monkeypatch.setattr(handler, "require_execution_user", verify)
    db = AsyncMock()
    assert await handler._load_child_agent(db, draft, parent, owner, tenant, published_child_ids=(frozen,)) is None
    load.assert_not_awaited()
    assert await handler._load_child_agent(db, frozen, parent, owner, tenant, published_child_ids=(frozen,)) is visible
    load.assert_awaited_once_with(db, frozen, user=actor, tenant_id=tenant)
    verify.assert_awaited_once_with(db, owner, tenant)
    load.return_value = None
    assert await handler._load_child_agent(db, frozen, parent, owner, tenant, published_child_ids=(frozen,)) is None
    db.execute.assert_not_awaited()

async def test_execute_variables_only_reach_the_current_model_call(external_chat, monkeypatch):
    f = external_chat
    calls = []

    async def run(**kwargs):
        calls.append(kwargs)
        yield AgentStep(step=1, done=True, final_output="answer")

    monkeypatch.setattr(core_chat, "build_agent_loop", AsyncMock(return_value=SimpleNamespace(provider=Mock(), run=run)))
    for question, variables in (("first", {"invoice": "invoice-only-this-turn"}), ("second", None)):
        async with f.factory() as db:
            await agent_api.execute_agent(f.agent.id, agent_api.ExecuteRequest(
                user_input=question, variables=variables, session_rid=str(f.session_id)), f.request, f.user, db)
            await db.commit()

    first, second = calls
    assert first["user_input"] == "first" and second["user_input"] == "second"
    assert '"invoice": "invoice-only-this-turn"' in first["request_context"]
    assert second["request_context"] is None
    for call in calls:
        assert not any("invoice-only-this-turn" in str(m.content) for m in call["conversation_history"])
    assert any(m.content == "first" for m in second["conversation_history"])
    assert [call.args[2] for call in core_chat.SkillService.get_skills_for_prompt.await_args_list] == ["first", "second"]
    async with f.factory() as db:
        history = (await db.scalars(select(Message.content).where(Message.session_id == f.session_id))).all()
        assert "first" in history and "second" in history
        assert not any("invoice-only-this-turn" in content for content in history)
        assert (await db.get(ChatSession, f.session_id)).context_summary == "previous decision"


async def test_request_context_consumes_history_budget_without_entering_summary(monkeypatch):
    from agent_platform.core import context
    from agent_platform.llm import LLMMessage

    budget = context.ContextBudget(total_window=500, reserve_output=50, compress_threshold=0.8)
    monkeypatch.setattr(context.ContextBudget, "from_settings", lambda: budget)
    history = [LLMMessage(role="user", content="previous-" + str(i) + "x" * 90) for i in range(10)]
    summarize = AsyncMock(return_value="summary")
    monkeypatch.setattr(context, "generate_summary", summarize)
    params = {"system_prompt": "s" * 350, "history": history, "user_input": "question", "provider": Mock()}
    ordinary, _ = await context.prepare_context(**params)
    variables = "turn-only-budget-" + "y" * 450
    constrained, _ = await context.prepare_context(**params, request_context=variables)
    assert len(constrained) < len(ordinary)
    assert all(variables not in str(m.content) for m in constrained)
    assert variables not in str(summarize.await_args_list)
    # Exercise the summary fallback when light truncation cannot meet the budget.
    monkeypatch.setattr(context, "truncate_history_messages", lambda messages, _budget: messages)
    summarize.reset_mock()
    _, generated = await context.prepare_context(**params, request_context=variables)
    assert generated
    summarize.assert_awaited_once()
    assert variables not in str(summarize.await_args_list)
