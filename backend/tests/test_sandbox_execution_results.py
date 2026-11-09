"""Execute real Bash commands without Docker to check quoting and result propagation."""

import asyncio
import shlex
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aio_agent_platform.tools.executor import ToolExecutor
from aio_agent_platform.tools.registry import Tool, ToolRegistry


@pytest.fixture
def sandbox_execution(tmp_path, monkeypatch):
    hooks, recorder = Mock(), Mock()
    monkeypatch.setattr("aio_agent_platform.tools.executor.get_hook_manager", lambda: hooks)
    monkeypatch.setattr("aio_agent_platform.tools.executor.get_recorder", lambda: recorder)

    async def execute(_sandbox, command):
        command = command.replace("/tmp/_agent_code", str(tmp_path / "code"))
        command = command.replace("/workspace/project", str(tmp_path / "project"))
        if command.startswith("python3 "):
            command = shlex.quote(sys.executable) + command[len("python3"):]
        process = await asyncio.create_subprocess_exec(
            "bash", "-c", command, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return SimpleNamespace(
            stdout=stdout.decode(), stderr=stderr.decode(), exit_code=process.returncode,
        )

    manager = SimpleNamespace(
        get_or_create=AsyncMock(return_value=object()), execute=AsyncMock(side_effect=execute),
    )
    registry = ToolRegistry()
    for name in ["run_shell", "run_code"]:
        registry.register(Tool(name, "test", {}, requires_sandbox=True))
    executor = ToolExecutor(registry, manager)
    executor._workspace_manifest = AsyncMock(return_value={})
    return SimpleNamespace(executor=executor, manager=manager, hooks=hooks, recorder=recorder)


async def execute_tool(setup, tool_name, arguments):
    return await setup.executor.execute(
        tool_name, arguments, "call", "user", "session",
        workspace_id="workspace", workspace_slug="project",
    )


@pytest.mark.parametrize(("command", "expected"), [
    ("python3 -c \"print('hello')\"", "hello\n"),
    ("printf '%s\\n' \"hello\"", "hello\n"),
    ("printf 'first\\n'\nprintf '%s\\n' \"$(printf second)\"", "first\nsecond\n"),
    ("printf '[exit code: 7]\\n'", "[exit code: 7]\n"),
])
async def test_shell_preserves_quoting_and_successful_output(sandbox_execution, command, expected):
    setup = sandbox_execution
    result = await execute_tool(setup, "run_shell", {"command": command})

    assert result.success and result.error is None
    assert result.output == expected
    assert setup.manager.execute.await_args.args[1] == command
    assert setup.executor.skill_execution_evidence[("user", "session", "call")]["success"]


@pytest.mark.parametrize(("tool_name", "arguments", "exit_code"), [
    ("run_shell", {"command": "false"}, 1),
    ("run_shell", {"command": "printf 'before\\n'; printf 'failure\\n' >&2; exit 7"}, 7),
    ("run_code", {"code": "print('before'); raise SystemExit(5)", "language": "python"}, 5),
])
async def test_nonzero_exit_reaches_model_hooks_and_observation(
    sandbox_execution, tool_name, arguments, exit_code,
):
    setup = sandbox_execution
    result = await execute_tool(setup, tool_name, arguments)

    assert not result.success
    assert result.error == result.output
    assert f"[exit code: {exit_code}]" in result.error
    if exit_code in {5, 7}:
        assert "before" in result.error
    if exit_code == 7:
        assert "failure" in result.error
    assert setup.recorder.record_tool_call.call_args.kwargs["is_error"]
    assert not setup.hooks.fire_nowait.call_args.kwargs["data"]["success"]
    assert not setup.executor.skill_execution_evidence[("user", "session", "call")]["success"]


async def test_run_code_success(sandbox_execution):
    result = await execute_tool(
        sandbox_execution, "run_code", {"code": "print('hello')", "language": "python"},
    )
    assert result.success and result.error is None
    assert result.output == "hello\n"


async def test_run_code_does_not_execute_after_failed_upload(sandbox_execution):
    setup = sandbox_execution
    setup.manager.execute.side_effect = None
    setup.manager.execute.return_value = SimpleNamespace(
        stdout="", stderr="cannot write code", exit_code=1,
    )

    result = await execute_tool(setup, "run_code", {"code": "print('must not run')"})

    assert not result.success
    assert "cannot write code" in result.error
    assert setup.manager.execute.await_count == 1


async def test_failed_shell_keeps_real_workspace_file_changes(sandbox_execution, tmp_path):
    setup = sandbox_execution
    (tmp_path / "project").mkdir()
    del setup.executor._workspace_manifest

    result = await execute_tool(setup, "run_shell", {
        "command": "printf 'created' > /workspace/project/result.txt; exit 7",
    })

    assert not result.success
    assert len(result.file_changes) == 1
    assert result.file_changes[0]["path"] == "result.txt"
    assert result.file_changes[0]["action"] == "created"
    assert result.file_changes[0]["size"] == 7


async def test_file_edit_handles_quotes_and_multiline_content(sandbox_execution, tmp_path):
    setup = sandbox_execution
    workspace = tmp_path / "project"
    workspace.mkdir()
    filename = "quote's \"file\".txt"
    old = "first 'single' and \"double\"\n中文"
    new = "replacement 'single' and \"double\"\nmore lines"
    (workspace / filename).write_text(old)
    setup.executor.registry.register(Tool("edit_file", "test", {}, requires_sandbox=True))
    del setup.executor._workspace_manifest

    result = await execute_tool(setup, "edit_file", {
        "path": filename, "old_str": old, "new_str": new,
    })

    assert result.success
    assert (workspace / filename).read_text() == new
    assert len(result.file_changes) == 1
    assert result.file_changes[0]["action"] == "modified"
