"""宠物任务条的工作区隔离：注册表查询与事件分发都按 workspace 过滤。"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from agent_platform.core import task_events, task_registry


class _FakeRedis:
    def __init__(self, tasks: dict[str, str]) -> None:
        self.tasks = tasks
        self.deleted: list[tuple[str, ...]] = []

    async def hgetall(self, _key: str) -> dict[str, str]:
        return self.tasks

    async def hdel(self, _key: str, *fields: str) -> None:
        self.deleted.append(fields)


@pytest.mark.asyncio
async def test_list_tasks_filters_by_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    tasks = {
        "session-a": task_registry.RunningTask(
            session_id="session-a", label="A", source="feishu", workspace_id="ws-a"
        ).dumps(),
        "session-b": task_registry.RunningTask(
            session_id="session-b", label="B", source="feishu", workspace_id="ws-b"
        ).dumps(),
        "legacy": task_registry.RunningTask(
            session_id="legacy", label="L", source="feishu"
        ).dumps(),
    }
    fake = _FakeRedis(tasks)
    monkeypatch.setattr(task_registry, "_redis", lambda: fake)

    scoped = await task_registry.list_tasks(user_id, workspace_id="ws-a")
    assert [task.session_id for task in scoped] == ["session-a"]

    # 未传工作区时保持原行为，返回全部。
    unscoped = await task_registry.list_tasks(user_id)
    assert {task.session_id for task in unscoped} == {"session-a", "session-b", "legacy"}


@pytest.mark.asyncio
async def test_broker_dispatch_filters_by_workspace() -> None:
    broker = task_events.TaskEventBroker()
    matching: asyncio.Queue = asyncio.Queue()
    other: asyncio.Queue = asyncio.Queue()
    broker._subscribers["user-1"] = {matching: "ws-a", other: "ws-b"}

    broker._dispatch(json.dumps({
        "user_id": "user-1",
        "workspace_id": "ws-a",
        "type": "pet_task_tool",
        "session_id": "session-a",
    }))

    event = matching.get_nowait()
    assert event["session_id"] == "session-a"
    assert other.empty()


@pytest.mark.asyncio
async def test_broker_snapshot_is_workspace_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()

    async def fake_list_tasks(_user_id, workspace_id=None):
        assert workspace_id == "ws-a"
        return [task_registry.RunningTask(
            session_id="session-a", label="A", source="feishu", workspace_id="ws-a"
        )]

    async def no_op_ensure_running(self) -> None:
        return None

    monkeypatch.setattr(task_registry, "list_tasks", fake_list_tasks)
    monkeypatch.setattr(task_events.TaskEventBroker, "_ensure_running", no_op_ensure_running)

    broker = task_events.TaskEventBroker()
    stream = broker.stream(user_id, workspace_id="ws-a")
    snapshot = await anext(stream)
    await stream.aclose()

    assert snapshot["type"] == "pet_task_snapshot"
    assert [task["session_id"] for task in snapshot["tasks"]] == ["session-a"]
    assert snapshot["tasks"][0]["workspace_id"] == "ws-a"
