"""Early tool-result compression must not rewrite history when context is small."""

import uuid
from unittest.mock import AsyncMock, MagicMock, Mock

from agent_platform.core import agent as agent_module
from agent_platform.core.agent import AgentLoop
from agent_platform.llm import LLMChunk, ToolCall
from agent_platform.tools.executor import ToolResult


def _tool_call_chunk(n: int) -> LLMChunk:
    return LLMChunk(
        type="tool_call_start",
        tool_call=ToolCall(id=f"call-{n}", name="lookup", arguments={}),
    )


def _done() -> LLMChunk:
    return LLMChunk(
        type="done",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )


async def test_over_budget_context_still_compacts(monkeypatch):
    monkeypatch.setattr(agent_module, "_resolve_tenant_id", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_module, "_resolve_agent_id", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_module, "get_recorder", Mock(return_value=Mock()))
    hooks = Mock()
    monkeypatch.setattr(agent_module, "get_hook_manager", Mock(return_value=hooks))

    class _TinyBudget:
        usable = 100
        trigger_at = 1

        @classmethod
        def from_settings(cls):
            return cls()

    monkeypatch.setattr(agent_module.ContextBudget, "from_settings", classmethod(lambda cls: _TinyBudget()))
    compress = Mock(side_effect=lambda messages, step: messages)
    monkeypatch.setattr(agent_module, "compress_early_tool_results", compress)

    calls = {"n": 0}

    def stream(messages, tools=None):
        calls["n"] += 1
        n = calls["n"]

        async def gen():
            if n <= 4:
                yield _tool_call_chunk(n)
            else:
                yield LLMChunk(type="text_delta", content="完成")
            yield _done()

        return gen()

    provider = MagicMock()
    provider.model = "test-model"
    provider.stream = stream
    executor = MagicMock()
    executor.execute = AsyncMock(return_value=ToolResult(
        tool_call_id="call-x", name="lookup", arguments={}, output="查询结果", success=True,
    ))

    loop = AgentLoop(
        execution_tenant_id=uuid.uuid4(),
        provider=provider,
        tool_executor=executor,
        system_prompt="test",
        max_iterations=5,
        trust_level="auto_all",
    )
    events = [
        event
        async for event in loop.run(
            user_input="开始",
            user_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            conversation_history=[],
            tools=[],
        )
    ]

    assert events[-1].done is True
    fired = [call.args[0] for call in hooks.fire_nowait.call_args_list]
    assert "PreCompact" in fired and "PostCompact" in fired
    assert compress.call_count == 2  # steps 4 and 5, both over the tiny budget


async def test_small_context_never_triggers_compaction(monkeypatch):
    monkeypatch.setattr(agent_module, "_resolve_tenant_id", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_module, "_resolve_agent_id", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_module, "get_recorder", Mock(return_value=Mock()))
    hooks = Mock()
    monkeypatch.setattr(agent_module, "get_hook_manager", Mock(return_value=hooks))

    calls = {"n": 0}

    def stream(messages, tools=None):
        calls["n"] += 1
        n = calls["n"]

        async def gen():
            if n <= 5:
                yield _tool_call_chunk(n)
            else:
                yield LLMChunk(type="text_delta", content="完成")
            yield _done()

        return gen()

    provider = MagicMock()
    provider.model = "test-model"
    provider.stream = stream
    executor = MagicMock()
    executor.execute = AsyncMock(return_value=ToolResult(
        tool_call_id="call-x", name="lookup", arguments={}, output="查询结果", success=True,
    ))

    loop = AgentLoop(
        execution_tenant_id=uuid.uuid4(),
        provider=provider,
        tool_executor=executor,
        system_prompt="test",
        max_iterations=6,
        trust_level="auto_all",
    )
    events = [
        event
        async for event in loop.run(
            user_input="开始",
            user_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            conversation_history=[],
            tools=[],
        )
    ]

    assert events[-1].done is True
    fired = [call.args[0] for call in hooks.fire_nowait.call_args_list]
    assert "PreCompact" not in fired and "PostCompact" not in fired
