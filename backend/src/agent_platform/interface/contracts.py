"""Public streaming contracts. SSE framing remains a string on the wire."""

from typing import Literal

from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict


class EventStreamResponse(StreamingResponse):
    media_type = "text/event-stream"


class ChatStreamEvent(BaseModel):
    """Common envelope; event-specific and forward-compatible fields are retained."""

    model_config = ConfigDict(extra="allow")
    type: Literal[
        "session", "session_title", "run", "run_status", "thinking", "text", "text_delta",
        "tool_call", "tool_result", "done", "error", "access_revoked", "closed",
        "confirmation_required", "confirmation_resolved", "command_result", "file_changes",
        "delegation_start", "delegation_end", "delegation_thinking", "delegation_text_delta",
        "delegation_tool_call", "delegation_tool_result", "delegation_heartbeat",
        "ui_action_required", "ui_action_resolved", "ui_action_heartbeat", "browser_status", "warning", "pet_action",
    ]
    content: str | None = None
    message: str | None = None
    session_id: str | None = None
    tool_call_id: str | None = None
    arguments: dict | None = None
    confirmation_id: str | None = None
    context: dict | None = None
    sequence: int | None = None
    replay: bool | None = None


def sse_contract(event_schema: dict | None = None, *, named_events: bool = False) -> dict:
    """Document frames and JSON data without misrepresenting the HTTP body as JSON.

    The extension describes each decoded data frame. Published API uses the SSE
    event field and text_delta.text; the web protocol uses data.type/content.
    """
    schema = event_schema or ChatStreamEvent.model_json_schema()
    if named_events:
        schema = {
            "type": "object", "additionalProperties": True,
            "properties": {"text": {"type": "string"}, "content": {"type": "string"},
                           "message": {"type": "string"}, "session_id": {"type": "string"},
                           "message_id": {"type": "string"}, "tool_call_id": {"type": "string"}},
        }
    return {
        "responses": {"200": {"description": (
            "SSE frames separated by a blank line. "
            + ("Event name is in event:, JSON payload in data:; text_delta uses text."
               if named_events else "JSON payload in data:; type identifies the event. id is the replay cursor when present.")
        ), "content": {"text/event-stream": {"schema": {"type": "string"},
                                                "x-event-schema": schema}}}},
    }


class StatusOut(BaseModel):
    status: str


class SuccessOut(BaseModel):
    success: bool
