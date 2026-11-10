"""file_query must expose the file as table `data` and keep the query out of shell syntax."""

import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aio_agent_platform.tools.executor import ToolExecutor
from aio_agent_platform.tools.registry import Tool, ToolRegistry


@pytest.fixture
def query_executor(monkeypatch):
    monkeypatch.setattr("aio_agent_platform.tools.executor.get_hook_manager", lambda: Mock())
    monkeypatch.setattr("aio_agent_platform.tools.executor.get_recorder", lambda: Mock())

    async def execute(_sandbox, command, timeout=None):
        return SimpleNamespace(stdout="", stderr="", exit_code=0)

    registry = ToolRegistry()
    registry.register(Tool("file_query", "test", {}, requires_sandbox=True))
    manager = SimpleNamespace(
        get_or_create=AsyncMock(return_value=object()), execute=AsyncMock(side_effect=execute),
    )
    return ToolExecutor(registry, manager)


async def run_query(executor, path, query):
    return await executor.execute(
        "file_query", {"path": path, "query": query},
        "call", "user", "session", workspace_id="workspace", workspace_slug="project",
    )


def sandbox_script(executor) -> tuple[str, list[str]]:
    """Return (python script, argv after 'python3 -c') from the last sandbox command."""
    command = executor.sandbox_mgr.execute.await_args.args[1]
    argv = shlex.split(command)
    assert argv[:2] == ["python3", "-c"]
    return argv[2], argv[3:]


async def test_csv_file_is_registered_as_table_data(query_executor):
    result = await run_query(query_executor, "people.csv", "SELECT name FROM data")

    assert result.success
    script, rest = sandbox_script(query_executor)
    compile(script, "<file-query>", "exec")
    assert "CREATE VIEW data AS SELECT * FROM read_csv_auto('/workspace/project/people.csv')" in script
    assert rest[0] == "SELECT name FROM data"


@pytest.mark.parametrize(("path", "reader"), [
    ("people.csv", "read_csv_auto('/workspace/project/people.csv')"),
    ("people.jsonl", "read_json_auto('/workspace/project/people.jsonl')"),
])
async def test_reader_matches_file_extension(query_executor, path, reader):
    result = await run_query(query_executor, path, "SELECT * FROM data")

    assert result.success
    script, _ = sandbox_script(query_executor)
    assert f"CREATE VIEW data AS SELECT * FROM {reader}" in script


async def test_tsv_uses_tab_delimiter(query_executor):
    result = await run_query(query_executor, "people.tsv", "SELECT * FROM data")

    assert result.success
    script, _ = sandbox_script(query_executor)
    compile(script, "<file-query>", "exec")
    assert "read_csv_auto('/workspace/project/people.tsv', delim='\\t')" in script


async def test_query_is_passed_as_single_argv_not_interpolated(query_executor):
    query = "SELECT * FROM data WHERE name = 'x; $(touch /tmp/pwned) \"quoted\"'"

    result = await run_query(query_executor, "people.csv", query)

    assert result.success
    script, rest = sandbox_script(query_executor)
    assert rest[0] == query
    assert "pwned" not in script


async def test_unsupported_extension_returns_hint_without_running(query_executor):
    result = await run_query(query_executor, "notes.txt", "SELECT * FROM data")

    assert result.success
    assert "unsupported extension" in result.output
    query_executor.sandbox_mgr.execute.assert_not_awaited()
