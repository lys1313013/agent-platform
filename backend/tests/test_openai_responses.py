"""Run Responses API requests through the SDK and the normal agent loop."""

import json
from contextlib import aclosing, asynccontextmanager
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import openai
import pytest

from agent_platform.core.agent import AgentLoop
from agent_platform.interface.routes.admin_models import ProviderCreate, ProviderUpdate
from agent_platform.llm import LLMMessage, LLMStreamError, create_provider
from agent_platform.llm.responses import OpenAIResponsesProvider
from agent_platform.tools.executor import ToolResult


def _function(name: str) -> dict:
    return {"type": "function", "function": {
        "name": name,
        "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
    }}


def _call(name: str, value: int) -> dict:
    return {
        "type": "function_call", "id": f"fc_{name}", "call_id": f"call_{name}",
        "name": name, "arguments": json.dumps({"value": value}), "status": "completed",
    }


def _text(text: str) -> dict:
    return {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def _response(*output: dict, status: str = "completed") -> dict:
    return {
        "id": "resp_1", "object": "response", "created_at": 1, "status": status,
        "model": "gpt-6.2-sol", "output": list(output),
        "usage": {"input_tokens": 23, "input_tokens_details": {"cached_tokens": 11},
                  "output_tokens": 7, "output_tokens_details": {"reasoning_tokens": 1}, "total_tokens": 30},
    }


def _event(event_type: str, **data) -> str:
    return f"event: {event_type}\ndata: {json.dumps({'type': event_type, 'sequence_number': 1, **data})}\n\n"


def _sse(data: str) -> httpx.Response:
    return httpx.Response(200, text=data, headers={"Content-Type": "text/event-stream"})


@asynccontextmanager
async def _provider(handler, **options):
    provider = create_provider(
        provider="openai_responses", model="gpt-6.2-sol", base_url="https://example.test/v1",
        api_key="test", enable_retry=False, **options,
    )
    assert isinstance(provider, OpenAIResponsesProvider)
    await provider.client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider.client = openai.AsyncOpenAI(api_key="test", base_url="https://example.test/v1", http_client=http)
        yield provider


@pytest.fixture(autouse=True)
def quiet_observers(monkeypatch):
    monkeypatch.setattr("agent_platform.core.agent.get_hook_manager", Mock(return_value=Mock()))
    monkeypatch.setattr("agent_platform.core.agent.get_recorder", Mock(return_value=Mock()))
    monkeypatch.setattr("agent_platform.core.agent.record_llm_usage", Mock())
    monkeypatch.setattr("agent_platform.llm.responses.get_recorder", Mock(return_value=Mock()))


def test_admin_accepts_responses_for_create_and_update():
    assert ProviderCreate(name="Responses", provider_type="openai_responses").provider_type == "openai_responses"
    assert ProviderUpdate(provider_type="openai_responses").provider_type == "openai_responses"


async def test_agent_replays_parallel_calls_and_reasoning_without_storing_them():
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
    calls = [_call("first", 12), _call("second", 34)]
    requests = []

    def handler(request):
        assert request.url.path == "/v1/responses"
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return _sse(_event("response.completed", response=_response(reasoning, *calls)))
        return _sse(
            _event("response.output_text.delta", item_id="msg_1", output_index=0, content_index=0, delta="done")
            + _event("response.completed", response=_response(_text("done")))
        )

    executor = Mock()
    executor.registry.get.return_value = None
    trace = Mock()
    async with _provider(handler, temperature=0.7, langfuse_client=trace) as provider:
        loop = AgentLoop(provider=provider, tool_executor=executor, execution_tenant_id=uuid4(), trust_level="auto_all")
        loop.execution_agent_id = uuid4()

        async def execute(call, _context):
            return ToolResult(tool_call_id=call.id, name=call.name, arguments=call.arguments,
                              output=str(call.arguments["value"]), success=True)

        loop._execute_tool_traced = AsyncMock(side_effect=execute)
        tools = [_function("first"), _function("second")]
        output = [item async for item in loop.run("test", uuid4(), uuid4(), [], tools)]

    assert output[-1].final_output == "done"
    assert len(requests) == 2
    for request in requests:
        assert request["stream"] is True and request["store"] is False
        assert "previous_response_id" not in request
        assert "max_tokens" not in request and "max_output_tokens" not in request
        assert "temperature" not in request
        assert request["include"] == ["reasoning.encrypted_content"]
        assert request["tools"] == [
            {"type": "function", "strict": False, **tool["function"]} for tool in tools
        ]
    continuation = [item for item in requests[1]["input"] if "type" in item]
    assert continuation == [reasoning, *calls,
                            {"type": "function_call_output", "call_id": "call_first", "output": "12"},
                            {"type": "function_call_output", "call_id": "call_second", "output": "34"}]
    assert "opaque" not in repr(trace.mock_calls)
    assert "opaque" not in repr(output)


async def test_non_stream_maps_text_images_limit_and_usage():
    def handler(request):
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        assert body["stream"] is False
        assert body["max_output_tokens"] == 80
        assert "max_tokens" not in body and "tools" not in body
        assert body["input"] == [
            {"role": "developer", "content": "system prompt"},
            {"role": "user", "content": [
                {"type": "input_text", "text": "Describe the image"},
                {"type": "input_image", "image_url": "data:image/png;base64,dGVzdA==", "detail": "auto"},
            ]},
        ]
        return httpx.Response(200, json=_response(_text("ok")))

    async with _provider(handler) as provider:
        result = await provider.complete([
            LLMMessage(role="system", content="system prompt"),
            LLMMessage(role="user", content=[
                {"type": "text", "text": "Describe the image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,dGVzdA=="}},
            ]),
        ], max_tokens=80)
    assert result.content == "ok"
    assert result.usage == {"prompt_tokens": 23, "completion_tokens": 7, "total_tokens": 30, "cache_read_tokens": 11}


async def test_incomplete_stream_does_not_emit_tool_calls():
    response = _response(_text("partial"), _call("first", 12), status="incomplete")
    response["incomplete_details"] = {"reason": "max_output_tokens"}
    async with _provider(lambda _: _sse(_event("response.incomplete", response=response))) as provider:
        chunks = [chunk async for chunk in provider.stream([LLMMessage(role="user", content="test")])]
    assert [chunk.type for chunk in chunks] == ["text_delta", "done"]
    assert chunks[-1].stop_reason == "length"
    assert chunks[-1].response_items is None


async def test_stream_closes_transport_when_consumer_closes():
    class Wire(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield _event("response.output_text.delta", item_id="msg_1", output_index=0,
                         content_index=0, delta="partial").encode()
            pytest.fail("the stream should not be advanced after closure")

        async def aclose(self):
            self.closed = True

    wire = Wire()
    async with _provider(lambda _: httpx.Response(200, stream=wire)) as provider:
        async with aclosing(provider.stream([LLMMessage(role="user", content="test")])) as stream:
            assert (await anext(stream)).content == "partial"
        assert wire.closed


async def test_unterminated_stream_fails():
    async with _provider(lambda _: _sse("")) as provider:
        with pytest.raises(LLMStreamError, match="未收到结束事件"):
            _ = [chunk async for chunk in provider.stream([LLMMessage(role="user", content="test")])]
