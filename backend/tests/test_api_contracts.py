"""Exercise public route registration and request/response contracts without IO."""

from collections import Counter
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from aio_agent_platform.interface.api import create_app
from aio_agent_platform.interface.routes.agents import AgentUpdate
from aio_agent_platform.rooms.schemas import RoomMessageOut


def test_routes_are_unique_and_delete_has_one_contract():
    app = create_app()
    keys = Counter((method, route.path) for route in app.routes if isinstance(route, APIRoute)
                   for method in route.methods)
    assert {key: count for key, count in keys.items() if count > 1} == {}
    operation = app.openapi()["paths"]["/api/sessions/{session_id}"]["delete"]
    assert "204" in operation["responses"]
    assert "content" not in operation["responses"]["204"]


def test_sse_and_control_plane_openapi_matches_wire_types():
    paths = create_app().openapi()["paths"]
    streams = [("/api/chat/stream", "post"), ("/api/chat/runs/{run_id}/events", "get"),
               ("/api/chat/runs/{run_id}/resume", "post"), ("/api/sessions/{session_id}/events", "get"),
               ("/api/sessions/{session_id}/chat", "post"), ("/api/rooms/{room_id}/events", "get")]
    for path, method in streams:
        content = paths[path][method]["responses"]["200"]["content"]
        assert set(content) == {"text/event-stream"}, path
        assert content["text/event-stream"]["schema"] == {"type": "string"}
        assert content["text/event-stream"]["x-event-schema"]
    for path, operations in paths.items():
        if not path.startswith(("/api/rooms", "/api/local-browser")) or path.endswith("/events"):
            continue
        for operation in operations.values():
            for status, response in operation["responses"].items():
                if status.startswith("2") and status != "204":
                    assert response["content"]["application/json"]["schema"], (path, status)


@pytest.mark.parametrize("field", ["name", "icon", "enable_memory_extraction", "enable_retry",
                                    "enable_auto_title", "is_active", "visibility"])
async def test_explicit_null_returns_422_before_handler(field):
    app = FastAPI()
    called = []

    @app.patch("/agent")
    async def update(body: AgentUpdate):
        called.append(True)
        return body.model_dump(exclude_unset=True)

    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        assert (await client.patch("/agent", json={field: None})).status_code == 422
        assert not called
        assert (await client.patch("/agent", json={})).json() == {}
        assert (await client.patch("/agent", json={"description": None})).json() == {"description": None}


def test_room_message_schema_preserves_payload_extensions():
    payload = {"id": uuid4(), "run_id": None, "member_id": None, "sequence": 1, "role": "assistant",
               "name": None, "icon": None, "content": "hello", "status": "completed", "reply_to_id": None,
               "created_at": "2026-10-31T00:00:00Z", "tool_calls": [{"id": "tool"}]}
    assert RoomMessageOut.model_validate(payload).model_dump()["tool_calls"] == payload["tool_calls"]


async def test_preparation_uses_one_model_for_history_and_execution(monkeypatch):
    from unittest.mock import AsyncMock, Mock

    from aio_agent_platform.core import chat
    from aio_agent_platform.core.execution import ChatExecutionService
    from aio_agent_platform.core.runtime_config import AgentRuntimeConfig

    uid, tid, live, override, published = [uuid4() for _ in range(5)]
    model = SimpleNamespace(id=override, is_multimodal=True, provider=SimpleNamespace(provider_type="anthropic"))
    resolve = AsyncMock(return_value=model)
    history = AsyncMock(return_value=([], None))
    build = AsyncMock()
    monkeypatch.setattr(chat, "resolve_model", resolve)
    monkeypatch.setattr(chat, "load_conversation_history", history)
    monkeypatch.setattr(chat, "build_agent_loop", build)
    agent = SimpleNamespace(model_id=live, temperature=0.0, max_iterations=4, enable_retry=False)
    session = SimpleNamespace(model_id=override)
    db = Mock()
    execution = await ChatExecutionService.prepare(db, tenant_id=tid, user_id=uid, agent=agent, session=session)
    await execution.load_history(db, uuid4())
    await execution.build_loop(Mock(), "prompt", db)
    assert resolve.await_count == 1
    assert resolve.call_args.kwargs["model_id"] == override
    assert history.call_args.kwargs == {"provider_type": "anthropic", "allow_images": True}
    assert build.call_args.kwargs["resolved_model"] is model
    assert build.call_args.kwargs["user_id"] == uid
    assert build.call_args.kwargs["runtime_config"].temperature == 0.0
    await ChatExecutionService.prepare(db, tenant_id=tid, user_id=uid, agent=agent, session=session,
                                       config=AgentRuntimeConfig(model_id=published))
    assert resolve.call_args.kwargs["model_id"] == published
