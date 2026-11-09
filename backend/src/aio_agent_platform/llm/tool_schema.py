"""Validate untrusted tool definitions before they reach an LLM provider."""

import json
import re
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from typing import Any

import anthropic
import openai
import structlog
from jsonschema import Draft202012Validator, SchemaError
from jsonschema.validators import validator_for

logger = structlog.get_logger()

_NUMBER_KEYWORDS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
_INTEGER_KEYWORDS = {
    "minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties",
    "minContains", "maxContains",
}
_SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
_SCHEMA_VALUES = {
    "items", "additionalItems", "additionalProperties", "contains", "propertyNames",
    "not", "if", "then", "else", "unevaluatedItems", "unevaluatedProperties", "contentSchema",
}
_SCHEMA_LISTS = {"allOf", "anyOf", "oneOf", "prefixItems"}


def _normalize_schema(schema: Any, path: str, changes: list[str]) -> None:
    if not isinstance(schema, dict):
        return
    for key, value in schema.items():
        field_path = f"{path}/{key}"
        if key in _NUMBER_KEYWORDS | _INTEGER_KEYWORDS and isinstance(value, str):
            try:
                number = json.loads(value)
                if type(number) not in (int, float):
                    continue
                # Reject NaN/Infinity/overflow rather than forwarding invalid JSON numbers.
                json.dumps(number, allow_nan=False)
                if key in _INTEGER_KEYWORDS:
                    if number != int(number):
                        continue
                    number = int(number)
                schema[key] = number
                changes.append(field_path)
            except (ValueError, OverflowError):
                pass
        elif key in _SCHEMA_MAPS and isinstance(value, dict):
            for name, child in value.items():
                _normalize_schema(child, f"{field_path}/{name}", changes)
        elif key == "dependencies" and isinstance(value, dict):
            for name, child in value.items():
                if isinstance(child, dict):
                    _normalize_schema(child, f"{field_path}/{name}", changes)
        elif key in _SCHEMA_VALUES:
            if key == "items" and isinstance(value, list):
                for index, child in enumerate(value):
                    _normalize_schema(child, f"{field_path}/{index}", changes)
            else:
                _normalize_schema(value, field_path, changes)
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            for index, child in enumerate(value):
                _normalize_schema(child, f"{field_path}/{index}", changes)
    # Never traverse default/enum/const/examples or arbitrary extension data.


def prepare_tools(tools: list[dict] | None, *, provider: str, model: str) -> list[dict] | None:
    """Copy, conservatively repair and validate each tool; isolate invalid ones.

    Only the outgoing request changes. Stored configuration, MCP caches and
    argument values are never rewritten. Log metadata, never schema contents.
    """
    prepared = []
    for tool in tools or []:
        name = "<unknown>"
        path = "/"
        try:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise ValueError("invalid_function_tool")
            function = tool.get("function")
            if not isinstance(function, dict):
                raise ValueError("invalid_function_definition")
            name = function.get("name")
            if not isinstance(name, str) or not name:
                name = "<unknown>"
                raise ValueError("invalid_function_name")
            # Detect cycles, non-JSON values and non-finite numbers before copying.
            json.dumps(tool, allow_nan=False)
            candidate = deepcopy(tool)
            function = candidate["function"]
            schema = function.setdefault("parameters", {"type": "object", "properties": {}})
            path = "/parameters"
            if not isinstance(schema, dict):
                raise ValueError("parameters_must_be_object_schema")
            changes: list[str] = []
            if not schema:
                schema["properties"] = {}
                changes.append("/parameters/properties")
            if "type" not in schema:
                schema["type"] = "object"
                changes.append("/parameters/type")
            if schema["type"] != "object":
                raise ValueError("parameters_must_be_object_schema")
            _normalize_schema(schema, path, changes)
            validator_for(schema, default=Draft202012Validator).check_schema(schema)
            prepared.append(candidate)
            if changes:
                logger.warning(
                    "llm_tool_schema_normalized", tool_name=name, provider=provider,
                    model=model, paths=changes, action="normalized",
                )
        except SchemaError as exc:
            logger.error(
                "llm_tool_schema_invalid", tool_name=name, provider=provider, model=model,
                path="/parameters/" + "/".join(map(str, exc.absolute_path)),
                reason=f"schema_{exc.validator}", action="excluded",
            )
        except (TypeError, ValueError, RecursionError, OverflowError) as exc:
            logger.error(
                "llm_tool_schema_invalid", tool_name=name, provider=provider, model=model,
                path=path, reason=type(exc).__name__, action="excluded",
            )
    return prepared or None


def _exclude_rejected_tool(
    tools: list[dict] | None, error: Exception, *, provider: str, model: str,
) -> bool:
    """Recover only from explicit provider schema errors naming a sent tool."""
    if not tools or not isinstance(error, (openai.BadRequestError, anthropic.BadRequestError)):
        return False
    body = error.body
    if not isinstance(body, dict):
        return False
    detail = body.get("error", body)
    message = detail.get("message", "") if isinstance(detail, dict) else ""
    if not isinstance(message, str):
        return False
    match = re.search(r"Invalid schema for (?:function|tool) ['\"]([^'\"]+)['\"]", message, re.I)
    if match is None:
        return False
    for index, tool in enumerate(tools):
        if tool["function"]["name"] == match[1]:
            tools.pop(index)
            logger.error(
                "llm_tool_schema_rejected", tool_name=match[1], provider=provider,
                model=model, reason="provider_schema_rejection", action="excluded_and_retry",
            )
            return True
    return False


async def call_with_schema_fallback(
    call: Callable[[list[dict] | None], Awaitable[Any]],
    tools: list[dict] | None, *, provider: str, model: str,
) -> Any:
    # Each recovery removes one tool, so attempts are bounded by the tool count.
    while True:
        try:
            return await call(tools or None)
        except (openai.BadRequestError, anthropic.BadRequestError) as exc:
            if not _exclude_rejected_tool(tools, exc, provider=provider, model=model):
                raise


@asynccontextmanager
async def stream_with_schema_fallback(create_stream, tools, *, provider: str, model: str):
    """Anthropic sends the request on context entry, before yielding any events."""
    async with AsyncExitStack() as stack:
        async def enter(current_tools):
            return await stack.enter_async_context(create_stream(current_tools))

        stream = await call_with_schema_fallback(enter, tools, provider=provider, model=model)
        yield stream
