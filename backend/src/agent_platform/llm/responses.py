"""OpenAI Responses transport for the platform's chat and tool loop."""

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx
import openai
from openai.types.responses import Response, ResponseFunctionToolCall

from agent_platform.browser.context import is_private_browser_run
from agent_platform.llm.client import (
    LLMChunk,
    LLMMessage,
    LLMResponse,
    LLMStreamError,
    OpenAIProvider,
    ToolCall,
    _classify_llm_error,
    _is_retryable_error,
    _retry_with_backoff,
    sanitize_messages_for_trace,
)
from agent_platform.llm.tool_schema import call_with_schema_fallback, prepare_tools
from agent_platform.observation import get_current_observation
from agent_platform.observation.recorder import get_recorder


@dataclass
class _CallMetrics:
    started_at: float = field(default_factory=time.monotonic)
    first_output_at: float | None = None
    attempts: int = 0
    response: Response | None = None
    stop_reason: str | None = None
    output: str = ""


class OpenAIResponsesProvider(OpenAIProvider):
    """Use /v1/responses and replay tool continuations from the local transcript."""

    provider_type = "openai_responses"

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        tools = prepare_tools(tools, provider=self.provider_type, model=self.model)
        params = self._request_parameters(messages, tools, temperature, max_tokens)
        with self._observe(messages, tools, params) as metrics:
            response = await self._request(params, tools, metrics, stream=False)
            metrics.response = response
            content, calls, metrics.stop_reason = _read_response(response)
            metrics.output = content
            tool_calls = []
            for call in calls:
                arguments = json.loads(call.arguments)
                if not isinstance(arguments, dict):
                    raise ValueError(f"Arguments for tool '{call.name}' must be a JSON object")
                tool_calls.append(ToolCall(call.call_id, call.name, arguments))
            return LLMResponse(content, tool_calls, metrics.stop_reason, _usage(response))

    async def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[LLMChunk]:
        tools = prepare_tools(tools, provider=self.provider_type, model=self.model)
        params = self._request_parameters(messages, tools, temperature, max_tokens)
        with self._observe(messages, tools, params) as metrics:
            received_text = False
            try:
                stream = await self._request(params, tools, metrics, stream=True)
                async with stream:
                    async for event in stream:
                        if event.type in ("response.output_text.delta", "response.refusal.delta"):
                            if event.delta:
                                if metrics.first_output_at is None:
                                    metrics.first_output_at = time.monotonic()
                                received_text = True
                                yield LLMChunk(type="text_delta", content=event.delta)
                        elif event.type == "response.reasoning_summary_text.delta":
                            yield LLMChunk(type="reasoning_delta", content=event.delta)
                        elif event.type in ("response.completed", "response.incomplete", "response.failed"):
                            metrics.response = event.response
                            break
                        elif event.type == "error":
                            raise RuntimeError(f"Responses API 流式失败: {event.message}")
            except (openai.APIError, httpx.TransportError) as error:
                if _is_retryable_error(error):
                    raise LLMStreamError(f"LLM 流式连接失败: {error}") from error
                raise

            response = metrics.response
            if response is None:
                raise LLMStreamError("Responses API 流式连接中断，未收到结束事件")
            content, calls, metrics.stop_reason = _read_response(response)
            metrics.output = content
            if (content or calls) and metrics.first_output_at is None:
                metrics.first_output_at = time.monotonic()
            if content and not received_text:
                yield LLMChunk(type="text_delta", content=content)

            # The terminal response has complete arguments for every function call.
            # Do not forward partial calls from failed or incomplete responses.
            for index, call in enumerate(calls):
                yield LLMChunk(
                    type="tool_call_start",
                    tool_call=ToolCall(call.call_id, call.name, {}),
                    argument_delta=call.arguments,
                    tool_call_index=index,
                )
            yield LLMChunk(
                type="done",
                usage=_usage(response),
                stop_reason=metrics.stop_reason,
                response_items=[item.model_dump(mode="json", exclude_none=True) for item in response.output]
                if calls else None,
            )

    def _request_parameters(
        self, messages: list[LLMMessage], tools: list[dict] | None,
        temperature: float | None, max_tokens: int | None,
    ) -> dict:
        params = {
            "model": self.model,
            "input": _to_input(messages),
            "store": False,
            "include": ["reasoning.encrypted_content"],
            **self._prompt_cache_kwargs(messages, tools),
        }
        limit = max_tokens if max_tokens is not None else self.default_max_tokens
        if limit is not None:
            params["max_output_tokens"] = limit
        temp = temperature if temperature is not None else self.default_temperature
        # GPT-6 reasoning requests reject sampling parameters.
        if temp is not None and not self.model.startswith("gpt-6"):
            params["temperature"] = temp
        return params

    async def _request(self, params, tools, metrics: _CallMetrics, *, stream: bool):
        async def send(current_tools: list[dict] | None):
            if current_tools:
                functions = [
                    {"type": "function", "strict": False, **tool["function"]}
                    for tool in current_tools
                ]
                return await self.client.responses.create(stream=stream, tools=functions, **params)
            return await self.client.responses.create(stream=stream, **params)

        async def attempt():
            metrics.attempts += 1
            return await call_with_schema_fallback(send, tools, provider=self.provider_type, model=self.model)

        if self.enable_retry:
            return await _retry_with_backoff("OpenAI.responses", self.model, attempt)
        return await attempt()

    @contextmanager
    def _observe(self, messages: list[LLMMessage], tools: list[dict] | None, params: dict) -> Iterator[_CallMetrics]:
        metrics = _CallMetrics()
        error_type = None
        observation = None
        if self.langfuse_client and not is_private_browser_run():
            parent = get_current_observation()
            factory = parent.start_observation if parent else self.langfuse_client.start_observation
            trace_messages = self._to_openai_messages([message for message in messages if not message.omit_from_trace])
            observation = factory(
                name="OpenAI Responses",
                as_type="generation",
                model=self.model,
                model_parameters={name: params[name] for name in ("temperature", "max_output_tokens") if name in params},
                input=sanitize_messages_for_trace(trace_messages),
            )
        try:
            yield metrics
        except (asyncio.CancelledError, GeneratorExit):
            error_type = "interrupted"
            raise
        except Exception as error:
            error_type = _classify_llm_error(error.__cause__ or error)
            raise
        finally:
            usage = _usage(metrics.response) if metrics.response else None
            if observation:
                observation.update(
                    output=metrics.output,
                    level="ERROR" if error_type else "DEFAULT",
                    status_message=error_type,
                    usage_details={
                        "input": usage["prompt_tokens"],
                        "output": usage["completion_tokens"],
                        "total": usage["total_tokens"],
                    } if usage else None,
                )
                observation.end()
            duration_ms = int((time.monotonic() - metrics.started_at) * 1000)
            completion = (usage or {}).get("completion_tokens", 0)
            get_recorder().record_llm_call(
                model=self.model,
                provider=self.provider_type,
                prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                completion_tokens=completion,
                total_tokens=(usage or {}).get("total_tokens", 0),
                cache_read_tokens=(usage or {}).get("cache_read_tokens", 0),
                duration_ms=duration_ms,
                ttft_ms=int((metrics.first_output_at - metrics.started_at) * 1000)
                if metrics.first_output_at is not None else None,
                output_tokens_per_sec=completion / max(duration_ms / 1000, 0.001) if completion else None,
                retry_count=max(metrics.attempts - 1, 0),
                final_status="failed" if error_type else "success",
                error_type=error_type,
                stop_reason=metrics.stop_reason,
                temperature=params.get("temperature"),
                max_tokens=params.get("max_output_tokens"),
                tools_count=len(tools or []),
            )


def _to_input(messages: list[LLMMessage]) -> list[dict]:
    items = []
    for message in messages:
        if message.role == "assistant" and message.response_items:
            items.extend(message.response_items)
            continue
        content = _input_content(message.content)
        if message.role == "tool":
            items.append({"type": "function_call_output", "call_id": message.tool_call_id, "output": content})
        else:
            if content:
                role = "developer" if message.role == "system" else message.role
                items.append({"role": role, "content": content})
            for call in message.tool_calls or []:
                items.append({
                    "type": "function_call", "call_id": call.id,
                    "name": call.name, "arguments": json.dumps(call.arguments),
                })
    return items


def _input_content(content: str | list[dict] | None) -> str | list[dict]:
    if not isinstance(content, list):
        return content or ""
    blocks = []
    for block in content:
        if block["type"] == "text":
            blocks.append({"type": "input_text", "text": block["text"]})
        elif block["type"] == "image_url":
            image = block["image_url"]
            blocks.append({"type": "input_image", "image_url": image["url"], "detail": image.get("detail", "auto")})
        else:
            raise ValueError(f"Unsupported Responses input content: {block['type']}")
    return blocks


def _read_response(response: Response) -> tuple[str, list[ResponseFunctionToolCall], str]:
    if response.error:
        raise RuntimeError(f"Responses API 调用失败: {response.error.message}")
    content = "".join(
        part.text if part.type == "output_text" else part.refusal
        for item in response.output if item.type == "message"
        for part in item.content if part.type in ("output_text", "refusal")
    )
    if response.status == "incomplete":
        reason = response.incomplete_details.reason if response.incomplete_details else "unknown"
        if not content:
            raise RuntimeError(f"Responses API 返回不完整的响应: {reason}")
        return content, [], "length" if reason == "max_output_tokens" else "content_filter"
    if response.status not in (None, "completed"):
        raise RuntimeError(f"Responses API 返回异常状态: {response.status}")
    calls = [item for item in response.output if item.type == "function_call"]
    if any(call.status not in (None, "completed") for call in calls):
        raise RuntimeError("Responses API 返回未完成的工具调用")
    return content, calls, "tool_calls" if calls else "stop"


def _usage(response: Response) -> dict | None:
    usage = response.usage
    if usage is None:
        return None
    return {
        "prompt_tokens": usage.input_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "cache_read_tokens": usage.input_tokens_details.cached_tokens if usage.input_tokens_details else 0,
    }
