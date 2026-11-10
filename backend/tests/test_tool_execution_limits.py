"""Dispatch ceilings and file_info cache freshness."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aio_agent_platform.core.config import settings
from aio_agent_platform.tools.executor import ToolExecutor
from aio_agent_platform.tools.registry import Tool, ToolRegistry

FILE_INFO_PROBE = (
    "12\n---AIO-META---\ntext/plain\n---AIO-META---\nus-ascii\n---AIO-META---\n2\n"
    "---AIO-META---\nhello\nworld\n"
)


@pytest.fixture(autouse=True)
def quiet_observers(monkeypatch):
    monkeypatch.setattr("aio_agent_platform.tools.executor.get_hook_manager", lambda: Mock())
    monkeypatch.setattr("aio_agent_platform.tools.executor.get_recorder", lambda: Mock())


def make_executor(tools, execute, handlers=None):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    manager = SimpleNamespace(
        get_or_create=AsyncMock(return_value=object()), execute=AsyncMock(side_effect=execute),
    )
    return ToolExecutor(registry, manager, direct_handlers=handlers or {})


async def test_slow_direct_tool_is_cut_off_at_its_declared_timeout():
    async def slow(args, user_id, session_id, **kwargs):
        await asyncio.sleep(30)
        return "late"

    executor = make_executor(
        [Tool("slow_tool", "", {}, requires_sandbox=False, timeout=1)],
        execute=None,
        handlers={"slow_tool": slow},
    )

    result = await executor.execute("slow_tool", {}, "call", "user", "session")

    assert not result.success
    assert "timed out after 1s" in result.error


async def test_run_shell_ceiling_follows_sandbox_command_timeout():
    executor = make_executor(
        [Tool("run_shell", "", {}, requires_sandbox=True, permission_level="dangerous", timeout=5)],
        execute=None,
    )

    assert executor.tool_timeout("run_shell") >= settings.sandbox.command_timeout + 5


async def test_file_info_cache_is_invalidated_by_writes():
    calls = []

    async def execute(_sandbox, command, timeout=None):
        calls.append(command)
        if command.startswith("if [ -e "):
            return SimpleNamespace(stdout="__AIO_EXISTING__\n", stderr="", exit_code=0)
        return SimpleNamespace(stdout=FILE_INFO_PROBE, stderr="", exit_code=0)

    executor = make_executor([
        Tool("file_info", "", {}, requires_sandbox=True, permission_level="read"),
        Tool("write_file", "", {}, requires_sandbox=True, permission_level="write"),
    ], execute)

    async def file_info(call_id):
        return await executor.execute(
            "file_info", {"path": "notes.txt"}, call_id, "user", "session",
            workspace_id="ws", workspace_slug="project",
        )

    first = await file_info("c1")
    second = await file_info("c2")
    probes = sum("stat -c%s" in command for command in calls)
    assert first.output == second.output
    assert probes == 1, "second file_info must be served from cache"

    written = await executor.execute(
        "write_file", {"path": "notes.txt", "content": "new"}, "c3", "user", "session",
        workspace_id="ws", workspace_slug="project",
    )
    assert written.success
    assert written.file_changes[0]["action"] == "modified"
    assert written.file_changes[0]["size"] == 3

    await file_info("c4")
    assert sum("stat -c%s" in command for command in calls) == 2, "write must invalidate the cache"


def local_sandbox(workspace):
    async def execute(_sandbox, command, timeout=None):
        rewritten = command.replace("/workspace/project", str(workspace))
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", rewritten,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return SimpleNamespace(
            stdout=stdout.decode(), stderr=stderr.decode(), exit_code=process.returncode,
        )
    return execute


async def test_file_info_probe_reads_metadata_in_one_round_trip(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("alpha\nbeta\ngamma\n")

    executor = make_executor(
        [Tool("file_info", "", {}, requires_sandbox=True, permission_level="read")],
        local_sandbox(workspace),
    )

    meta = await executor._analyze_file(None, "notes.txt", "project")

    # byte_size comes from GNU stat, which the Linux sandbox image provides.
    assert meta["mime"].startswith("text/")
    assert meta["line_count"] == 3
    assert "alpha" in meta["preview"]
    assert executor.sandbox_mgr.execute.await_count == 1


async def test_read_file_range_uses_one_round_trip(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("one\ntwo\nthree\n")

    executor = make_executor([], local_sandbox(workspace))

    text = await executor._read_file_range(None, "notes.txt", 0, 2, "project")

    assert "one\ntwo" in text
    assert "共 3 行" in text
    assert executor.sandbox_mgr.execute.await_count == 1
