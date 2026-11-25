"""The same standalone command supervisor is used inside all sandbox images."""

import asyncio
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from agent_platform.sandbox import command_runner
from agent_platform.sandbox.models import _run_docker_command


async def start_runner(command, cancel, timeout, limit=1024, cleanup_file=""):
    return await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-S", str(Path(command_runner.__file__)),
        str(timeout), str(cancel), str(limit), str(cleanup_file), command,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )


async def test_timeout_includes_children_holding_output_after_shell_exit(tmp_path):
    marker = tmp_path / "late"
    proc = await start_runner(
        f"(sleep 0.5; touch {shlex.quote(str(marker))}) &", tmp_path / "cancel", 0.15,
    )
    _out, err = await asyncio.wait_for(proc.communicate(), 3)
    assert proc.returncode == 124 and b"timed out" in err
    await asyncio.sleep(0.5)
    assert not marker.exists()


async def test_cancellation_stops_the_command_group_before_reuse(tmp_path):
    cancel = tmp_path / "cancel"
    marker = tmp_path / "late"
    script = tmp_path / "program.sh"
    script.write_text(f"echo ready; sleep 0.5; touch {shlex.quote(str(marker))}")
    proc = await start_runner(
        f"bash {shlex.quote(str(script))}", cancel, 30, cleanup_file=script,
    )
    assert await asyncio.wait_for(proc.stdout.readline(), 2) == b"ready\n"
    cancel.touch()
    await asyncio.wait_for(proc.communicate(), 2)
    assert proc.returncode == 130
    assert not script.exists()
    await asyncio.sleep(0.5)
    assert not marker.exists()


async def test_large_output_is_bounded_and_preserves_process_exit_code(tmp_path):
    proc = await start_runner("printf %s abcdefghij; exit 7", tmp_path / "cancel", 5, limit=5)
    out, err = await asyncio.wait_for(proc.communicate(), 2)
    assert proc.returncode == 7
    assert out == b"abcde"
    assert b"truncated at 5 bytes" in err


def test_host_caps_docker_stream_independently(monkeypatch):
    monkeypatch.setattr("agent_platform.sandbox.models._COMMAND_OUTPUT_LIMIT", 64)

    def stream(exec_id, *, stream, demux):
        assert stream and demux
        for _ in range(150):
            yield bytes(256), None

    api = SimpleNamespace(exec_create=Mock(return_value={"Id": "test"}), exec_start=stream,
                          exec_inspect=Mock(return_value={"Running": False, "ExitCode": 9}))
    result = _run_docker_command(api, "container", "echo hello", 2, "/tmp/test-marker")
    assert len(result.stdout) == 64 + 1024
    assert "truncated" in result.stderr
    assert result.output_truncated
    assert result.exit_code == 9
