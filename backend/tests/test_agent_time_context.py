"""The model gets a fresh clock at the end of every request."""

from copy import deepcopy
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from agent_platform.core import agent as agent_module
from agent_platform.core import prompt as prompt_module
from agent_platform.llm import LLMChunk, LLMMessage, LLMStreamError, ToolCall
from agent_platform.llm.client import AnthropicProvider
from agent_platform.tools.executor import ToolResult


@pytest.mark.parametrize("user_input", [
    "现在几点？",
    [{"type": "text", "text": "今天分析这张图"},
     {"type": "image_url", "image_url": {"url": "data:image/png;base64,example"}}],
    None,
])
async def test_clock_refreshes_after_tools_retries_and_new_turns(monkeypatch, user_input):
    monkeypatch.setattr(agent_module, "_resolve_tenant_id", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_module, "_resolve_agent_id", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_module, "get_recorder", Mock(return_value=Mock()))
    monkeypatch.setattr(agent_module, "get_hook_manager", Mock(return_value=Mock()))
    monkeypatch.setattr(agent_module.asyncio, "sleep", AsyncMock())
    times = [f"2026-09-20 12:0{i}:00+08:00 (Asia/Shanghai)" for i in range(4)]
    monkeypatch.setattr(prompt_module, "_format_current_datetime", Mock(side_effect=times))
    requests = []

    async def stream(messages, tools=None):
        requests.append(deepcopy(messages))
        if len(requests) == 1:
            yield LLMChunk(
                type="tool_call_start",
                tool_call=ToolCall(id="call-1", name="lookup", arguments={}),
            )
        elif len(requests) == 2:
            raise LLMStreamError("retry before any output")
        else:
            yield LLMChunk(type="text_delta", content="完成")
        yield LLMChunk(type="done")

    executor = Mock()
    executor.execute = AsyncMock(return_value=ToolResult(
        tool_call_id="call-1", name="lookup", arguments={}, output="查询结果", success=True,
    ))
    loop = agent_module.AgentLoop(trust_level="auto_all", execution_tenant_id=uuid4(),
        provider=Mock(model="test-model", stream=stream),
        tool_executor=executor,
        system_prompt="Existing custom prompt with an older clock",
    )
    history = [LLMMessage(role="user", content="昨天的对话")]
    original_history = deepcopy(history)
    original_input = deepcopy(user_input)

    for _ in range(2):
        events = [event async for event in loop.run(
            user_input=user_input, user_id=uuid4(), session_id=uuid4(),
            conversation_history=history, tools=[],
        )]
        assert events[-1].final_output == "完成"

    assert len(requests) == 4
    for messages, current in zip(requests, times, strict=True):
        assert messages[-1].role == "user"
        assert messages[-1].transient is True
        assert f"当前时间（北京时间）：{current}" in messages[-1].content
        assert sum("[平台时间上下文]" in str(m.content) for m in messages) == 1
        assert messages[0].content == loop.system_prompt
        assert messages[2].content == original_input
        # Anthropic keeps the static system block as the cache breakpoint and
        # appends the clock after the conversation, never inside the prefix.
        system, converted = AnthropicProvider._to_anthropic_format(None, messages)
        assert len(system) == 1
        assert system[0]["text"] == loop.system_prompt
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert len(converted) == len(messages) - 1
        assert "[平台时间上下文]" in converted[-1]["content"]
        assert "cache_control" not in converted[-1]
        if original_input is not None:
            assert "cache_control" in converted[-2]["content"][-1]

    assert requests[1][-2].role == "tool"
    assert requests[1][-2].tool_call_id == "call-1"
    assert requests[1][-2].content == "查询结果"
    assert history == original_history
    assert user_input == original_input
