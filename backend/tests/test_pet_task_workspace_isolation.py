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


class _LifecycleRedis:
    def __init__(self, broker: task_events.TaskEventBroker) -> None:
        self.tasks: dict[str, str] = {}
        self.broker = broker

    def pipeline(self):
        return _LifecyclePipeline(self.tasks)

    async def hget(self, _key, field):
        return self.tasks.get(field)

    async def publish(self, _channel, payload):
        self.broker._dispatch(payload)


class _LifecyclePipeline:
    def __init__(self, tasks: dict[str, str]) -> None:
        self.tasks = tasks
        self.commands: list[tuple] = []

    def hset(self, _key, field, value):
        self.commands.append(("set", field, value))

    def hget(self, _key, field):
        self.commands.append(("get", field))

    def hdel(self, _key, field):
        self.commands.append(("delete", field))

    def expire(self, *_args):
        self.commands.append(("expire",))

    async def execute(self):
        results = []
        for op, *args in self.commands:
            if op == "set":
                self.tasks[args[0]] = args[1]
                results.append(1)
            elif op == "get":
                results.append(self.tasks.get(args[0]))
            elif op == "delete":
                results.append(self.tasks.pop(args[0], None) is not None)
            else:
                results.append(True)
        return results

@pytest.mark.asyncio
async def test_published_lifecycle_events_route_to_the_task_workspace(monkeypatch):
    user_id, session_id = uuid.uuid4(), uuid.uuid4()
    broker = task_events.TaskEventBroker()
    matching, other, unscoped = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
    broker._subscribers[str(user_id)] = {matching: "ws-a", other: "ws-b", unscoped: None}
    fake = _LifecycleRedis(broker)
    monkeypatch.setattr(task_registry, "_redis", lambda: fake)

    await task_registry.task_started(user_id, session_id, "Task", "feishu", workspace_id="ws-a")
    await task_registry.task_tool(user_id, session_id, "lookup")
    await task_registry.task_finished(user_id, session_id)

    for event_type in ("pet_task_started", "pet_task_tool", "pet_task_finished"):
        event = matching.get_nowait()
        assert event["type"] == event_type
        assert event["workspace_id"] == "ws-a"
        assert unscoped.get_nowait() == event
    assert matching.empty()
    assert other.empty()
    assert not fake.tasks


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
