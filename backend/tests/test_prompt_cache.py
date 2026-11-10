"""Prompt-cache segmentation: stable prefix, per-request context, rolling breakpoint."""

import json
from types import SimpleNamespace
from uuid import uuid4

import anthropic
import httpx
import openai
import pytest

from agent_platform.core.prompt import SystemPrompt, build_system_prompt
from agent_platform.llm import LLMMessage, ToolCall
from agent_platform.llm.client import AnthropicProvider, OpenAIProvider

_SSE = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"id":"msg_test","type":"message",'
    '"role":"assistant","content":[],"model":"claude-sonnet-4-5","stop_reason":null,'
    '"stop_sequence":null,"usage":{"input_tokens":1200,"cache_creation_input_tokens":100,'
    '"cache_read_input_tokens":1000,"output_tokens":1}}}\n\n'
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
    'event: content_block_stop\n'
    'data: {"type":"content_block_stop","index":0}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},'
    '"usage":{"output_tokens":2}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)


def _provider() -> AnthropicProvider:
    return AnthropicProvider(model="claude-sonnet-4-5", api_key="test")


def _memory() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), version=1, layer="L1", agent_id=None, content="用户偏好中文")


def test_default_prompt_separates_stable_prefix_from_context():
    prompt = build_system_prompt(persistent_memories=[_memory()], user_portrait="画像内容")

    assert isinstance(prompt, SystemPrompt)
    assert 0 < prompt.stable_length < len(prompt)
    stable = prompt[:prompt.stable_length]
    volatile = prompt[prompt.stable_length:]
    assert "Behavior Guidelines" in stable
    assert "用户偏好中文" not in stable and "用户偏好中文" in volatile
    assert "画像内容" not in stable and "画像内容" in volatile


def test_custom_prompt_separates_stable_prefix_from_context():
    prompt = build_system_prompt(agent_prompt="你是测试智能体。", persistent_memories=[_memory()])

    assert 0 < prompt.stable_length < len(prompt)
    stable = prompt[:prompt.stable_length]
    volatile = prompt[prompt.stable_length:]
    assert "你是测试智能体。" in stable
    assert "用户偏好中文" not in stable and "用户偏好中文" in volatile


def test_anthropic_splits_system_at_stable_boundary():
    provider = _provider()
    prompt = build_system_prompt(agent_prompt="角色设定", persistent_memories=[_memory()])

    system, _ = provider._to_anthropic_format([
        LLMMessage(role="system", content=prompt),
        LLMMessage(role="user", content="你好"),
    ])

    assert len(system) == 2
    assert system[0]["text"] == prompt[:prompt.stable_length]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[1]
    assert "用户偏好中文" in system[1]["text"]


def test_plain_system_prompt_remains_a_single_cached_block():
    system, _ = _provider()._to_anthropic_format([
        LLMMessage(role="system", content="plain system"),
        LLMMessage(role="user", content="你好"),
    ])

    assert len(system) == 1
    assert system[0]["text"] == "plain system"
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_rolling_breakpoint_lands_on_last_stable_message():
    provider = _provider()
    _, converted = provider._to_anthropic_format([
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="user", content="第一问"),
        LLMMessage(
            role="assistant",
            tool_calls=[ToolCall(id="call-1", name="lookup", arguments={})],
        ),
        LLMMessage(role="tool", content="查询结果", tool_call_id="call-1"),
        LLMMessage(role="user", content="[平台时间上下文] 现在", transient=True),
    ])

    assert converted[-1]["content"] == "[平台时间上下文] 现在"
    assert "cache_control" not in converted[-1]
    assert converted[-2]["role"] == "user"
    assert converted[-2]["content"][-1]["type"] == "tool_result"
    assert converted[-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in converted[0]
    assert "cache_control" not in converted[1]["content"][-1]


def test_tool_block_keeps_single_breakpoint():
    tools = [
        {"type": "function", "function": {"name": "a", "description": "", "parameters": {}}},
        {"type": "function", "function": {"name": "b", "description": "", "parameters": {}}},
    ]

    converted = _provider()._convert_tools(tools)

    assert "cache_control" not in converted[0]
    assert converted[1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_anthropic_request_payload_carries_three_breakpoints():
    """The payload the SDK actually sends has cached prefix + rolling message breakpoint."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=_SSE,
        )

    provider = _provider()
    provider.client = anthropic.AsyncAnthropic(
        api_key="test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    prompt = build_system_prompt(agent_prompt="角色设定", persistent_memories=[_memory()])
    system, messages = provider._to_anthropic_format([
        LLMMessage(role="system", content=prompt),
        LLMMessage(role="user", content="第一问"),
        LLMMessage(
            role="assistant",
            tool_calls=[ToolCall(id="call-1", name="lookup", arguments={})],
        ),
        LLMMessage(role="tool", content="查询结果", tool_call_id="call-1"),
        LLMMessage(role="user", content="[平台时间上下文] 现在", transient=True),
    ])
    tools = provider._convert_tools([
        {"type": "function", "function": {"name": "lookup", "description": "d", "parameters": {"type": "object", "properties": {}}}},
    ])

    async with provider.client.messages.stream(
        model=provider.model, system=system, messages=messages, tools=tools, max_tokens=64,
    ) as stream:
        async for _event in stream:
            pass

    body = captured["body"]
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["system"][0]["text"] == prompt[:prompt.stable_length]
    assert "cache_control" not in body["system"][1]
    assert body["messages"][-1]["content"] == "[平台时间上下文] 现在"
    assert body["messages"][-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    breakpoints = (
        int("cache_control" in body["tools"][-1])
        + int("cache_control" in body["system"][0])
        + int("cache_control" in body["messages"][-2]["content"][-1])
    )
    assert breakpoints == 3


@pytest.mark.asyncio
async def test_openai_payload_keeps_clock_at_tail_and_stays_plain():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-4o",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        })

    provider = OpenAIProvider(model="gpt-4o", base_url="https://api.openai.com/v1", api_key="test")
    provider.client = openai.AsyncOpenAI(
        api_key="test",
        base_url="https://api.openai.com/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    prompt = build_system_prompt(agent_prompt="角色设定", persistent_memories=[_memory()])
    messages = provider._to_openai_messages([
        LLMMessage(role="system", content=prompt),
        LLMMessage(role="user", content="第一问"),
        LLMMessage(role="user", content="[平台时间上下文] 现在", transient=True),
    ])

    await provider.client.chat.completions.create(model=provider.model, messages=messages)

    body = captured["body"]
    assert body["messages"][0]["content"] == str(prompt)
    assert body["messages"][-1] == {"role": "user", "content": "[平台时间上下文] 现在"}
    assert "cache_control" not in json.dumps(body, ensure_ascii=False)
