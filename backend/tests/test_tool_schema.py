"""Tool schema regressions: isolate bad tools without losing a conversation."""

from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import Mock

import anthropic
import httpx
import openai
import pytest
from structlog.testing import capture_logs

from agent_platform.llm.client import AnthropicProvider, LLMResponse, OpenAIProvider
from agent_platform.llm.tool_schema import (
    call_with_schema_fallback,
    prepare_tools,
    stream_with_schema_fallback,
)
from agent_platform.tools.builtin import register_builtin_tools
from agent_platform.tools.registry import ToolRegistry


def tool(name="b_video_query", schema=None):
    return {"type": "function", "function": {
        "name": name, "description": "test tool",
        "parameters": schema if schema is not None else {"type": "object", "properties": {}},
    }}


def prepare(tools):
    return prepare_tools(tools, provider="test", model="test")


@pytest.mark.parametrize("keyword,value,expected", [
    ("minimum", "1", 1), ("maximum", "1.5", 1.5), ("multipleOf", "0.1", 0.1),
    ("exclusiveMinimum", "1", 1), ("exclusiveMaximum", "1e2", 100),
    ("minLength", "1", 1), ("maxLength", "2", 2),
    ("minItems", "1", 1), ("maxItems", "2", 2),
    ("minProperties", "1", 1), ("maxProperties", "2", 2),
    ("minContains", "1", 1), ("maxContains", "2", 2),
])
def test_repairs_numeric_constraints_without_mutating_source(keyword, value, expected):
    source = [tool(schema={"type": "object", "properties": {"page": {keyword: value}}})]
    snapshot = deepcopy(source)
    with capture_logs() as logs:
        result = prepare(source)
    assert result[0]["function"]["parameters"]["properties"]["page"][keyword] == expected
    assert source == snapshot
    assert logs[0]["event"] == "llm_tool_schema_normalized"
    assert logs[0]["paths"] == [f"/parameters/properties/page/{keyword}"]


def test_nested_definitions_compositions_and_data_annotations():
    literal = {"minimum": "1", "password": "do-not-log"}
    schema = {
        "type": "object",
        "$defs": {"page": {"type": "integer", "minimum": "1"}},
        "properties": {"pages": {"type": "array", "items": {
            "anyOf": [{"$ref": "#/$defs/page"}, {"type": "null"}], "minItems": "1",
        }}, "minimum": {"type": "string", "default": "1", "enum": ["1"]}},
        "allOf": [{"properties": {"limit": {"maximum": "100"}}}],
        "default": literal, "const": literal, "examples": [literal], "x-metadata": literal,
    }
    result = prepare([tool(schema=schema)])[0]["function"]["parameters"]
    assert result["$defs"]["page"]["minimum"] == 1
    assert result["properties"]["pages"]["items"]["minItems"] == 1
    assert result["allOf"][0]["properties"]["limit"]["maximum"] == 100
    assert result["properties"]["minimum"]["default"] == "1"
    assert result["properties"]["minimum"]["enum"] == ["1"]
    for key in ("default", "const", "x-metadata"):
        assert result[key] == literal
    assert result["examples"] == [literal]


@pytest.mark.parametrize("schema", [
    {"type": "object", "minimum": "oops"},
    {"type": "object", "multipleOf": "0"},
    {"type": "object", "minItems": "-1"},
    {"type": "object", "minLength": "1.5"},
    {"type": "object", "minimum": "NaN"},
    {"type": "object", "minimum": "Infinity"},
    {"type": "object", "minimum": "1e999"},
    {"type": "object", "minimum": True},
    {"type": "object", "properties": {"p": {"type": "unknown"}}},
    {"type": "object", "required": "p"},
    {"type": "object", "properties": []},
    {"type": "array"}, [], False, "invalid schema",
])
def test_invalid_schema_is_excluded_and_good_tools_survive(schema):
    with capture_logs() as logs:
        result = prepare([tool(schema=schema), tool("healthy")])
    assert [t["function"]["name"] for t in result] == ["healthy"]
    assert logs[-1]["event"] == "llm_tool_schema_invalid"
    assert logs[-1]["tool_name"] == "b_video_query"
    assert logs[-1]["log_level"] == "error"


def test_invalid_schema_logs_no_descriptions_or_values():
    with capture_logs() as logs:
        assert prepare([tool(schema={"minimum": "SECRET_VALUE"})]) is None
    assert "SECRET_VALUE" not in str(logs)
    assert logs[0]["path"] == "/parameters/minimum"


def test_legacy_declared_draft_and_booleans_are_preserved():
    schema = {"$schema": "http://json-schema.org/draft-04/schema#", "type": "object",
              "properties": {"n": {"minimum": "1", "exclusiveMinimum": True}}}
    result = prepare([tool(schema=schema)])[0]["function"]["parameters"]
    assert result["properties"]["n"] == {"minimum": 1, "exclusiveMinimum": True}


def test_empty_missing_and_malformed_tool_definitions():
    missing = tool()
    del missing["function"]["parameters"]
    assert prepare([missing, tool(schema={})]) == [tool(), tool()]
    for value in (None, [], [None], [{"type": "function"}], [tool(schema={"minimum": float("nan")})]):
        assert prepare(value) is None
    cyclic = tool()
    cyclic["function"]["parameters"]["properties"]["self"] = cyclic
    assert prepare([cyclic]) is None


def test_existing_builtin_tools_remain_usable():
    registry = ToolRegistry()
    register_builtin_tools(registry)
    tools = registry.to_openai_tools()
    with capture_logs() as logs:
        assert prepare(tools) == tools
    assert logs == []


def rejection(message="Invalid schema for function 'b_video_query': unsupported keyword", cls=openai.BadRequestError):
    response = httpx.Response(400, request=httpx.Request("POST", "https://example.test/messages"))
    return cls(message, response=response, body={"error": {"message": message}})


@pytest.mark.parametrize("provider_cls", [OpenAIProvider, AnthropicProvider])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("server_rejects", [False, True])
async def test_provider_boundaries_keep_conversation_running(
    provider_cls, streaming, server_rejects, monkeypatch,
):
    monkeypatch.setattr("agent_platform.llm.client.get_recorder", Mock(return_value=Mock()))
    provider = object.__new__(provider_cls)
    provider.model = "test"
    provider.default_temperature = provider.default_max_tokens = provider.langfuse_client = None
    provider.enable_retry = False
    provider._parse_response = Mock(return_value=LLMResponse(content="ok"))
    source = [tool(schema={"properties": {"page": {"type": "integer", "minimum": "1"}}}),
              tool("invalid", {"type": "object", "minimum": "bad"}), tool("healthy")]
    snapshot = deepcopy(source)
    calls = []
    closed = []

    def request(kwargs):
        calls.append(deepcopy(kwargs["tools"]))
        if server_rejects and len(calls) == 1:
            raise rejection(cls=(openai.BadRequestError if provider_cls is OpenAIProvider
                                 else anthropic.BadRequestError))

    async def events():
        if provider_cls is OpenAIProvider:
            yield NS(usage=None, choices=[NS(finish_reason="stop", delta=NS(
                content="ok", reasoning_content=None, tool_calls=None))])
        else:
            yield NS(type="content_block_delta", delta=NS(type="text_delta", text="ok"))

    async def create(**kwargs):
        request(kwargs)
        return events() if streaming else NS(usage=None)

    @asynccontextmanager
    async def stream_context(**kwargs):
        request(kwargs)
        try:
            yield events()
        finally:
            closed.append(True)

    provider.client = NS(chat=NS(completions=NS(create=create)),
                         messages=NS(create=create, stream=stream_context))
    if streaming:
        chunks = [chunk async for chunk in provider.stream([], tools=source)]
        assert "".join(c.content or "" for c in chunks) == "ok"
        if provider_cls is AnthropicProvider:
            assert closed == [True]
    else:
        assert (await provider.complete([], tools=source)).content == "ok"
    assert source == snapshot
    assert len(calls) == (2 if server_rejects else 1)
    if provider_cls is OpenAIProvider:
        assert calls[0][0]["function"]["parameters"]["properties"]["page"]["minimum"] == 1
        assert [t["function"]["name"] for t in calls[-1]] == (
            ["healthy"] if server_rejects else ["b_video_query", "healthy"])
    else:
        assert calls[0][0]["input_schema"]["properties"]["page"]["minimum"] == 1
        assert [t["name"] for t in calls[-1]] == (
            ["healthy"] if server_rejects else ["b_video_query", "healthy"])


async def test_schema_fallback_is_bounded_and_sends_none_when_all_tools_removed():
    requests = []

    async def call(tools):
        requests.append(deepcopy(tools))
        if tools:
            raise rejection(f"Invalid schema for function '{tools[0]['function']['name']}'")
        return "ok"

    with capture_logs() as logs:
        result = await call_with_schema_fallback(
            call, prepare([tool(), tool("second")]), provider="openai", model="test")
    assert result == "ok"
    assert len(requests) == 3
    assert requests[-1] is None
    assert [log["tool_name"] for log in logs] == ["b_video_query", "second"]


@pytest.mark.parametrize("message", [
    "Invalid schema for function 'unknown'", "Invalid API key", "Context length exceeded",
    "Invalid schema", "Invalid value for function 'b_video_query'",
])
async def test_other_bad_requests_are_not_suppressed_or_retried(message):
    calls = 0

    async def call(tools):
        nonlocal calls
        calls += 1
        raise rejection(message)

    with pytest.raises(openai.BadRequestError):
        await call_with_schema_fallback(call, prepare([tool()]), provider="openai", model="test")
    assert calls == 1


async def test_midstream_schema_error_is_never_replayed():
    opened = []
    closed = []

    @asynccontextmanager
    async def create(tools):
        opened.append(True)

        async def events():
            yield "partial output"
            raise rejection()

        try:
            yield events()
        finally:
            closed.append(True)

    with pytest.raises(openai.BadRequestError):
        async with stream_with_schema_fallback(
            create, prepare([tool()]), provider="test", model="test",
        ) as stream:
            assert await anext(stream) == "partial output"
            await anext(stream)
    assert opened == closed == [True]
