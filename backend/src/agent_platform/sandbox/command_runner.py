"""Standalone program sent to the sandbox's Python interpreter for each exec."""

import os
import selectors
import signal
import subprocess
import sys
import time
from contextlib import suppress


def run(command: str, timeout: float, cancel_path: str, output_limit: int, cleanup_file: str) -> int:
    deadline = time.monotonic() + timeout
    if os.path.exists(cancel_path):
        return 130
    process = subprocess.Popen(
        ["bash", "-c", command],
        start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    remaining = output_limit
    truncated = False
    finished = False
    try:
        # Descendants inherit these pipes, not the Docker stream. Even when a
        # shell exits first, the deadline still covers its outstanding output.
        with process.stdout, process.stderr, selectors.DefaultSelector() as pipes:
            pipes.register(process.stdout, selectors.EVENT_READ, sys.stdout.buffer)
            pipes.register(process.stderr, selectors.EVENT_READ, sys.stderr.buffer)
            while pipes.get_map() or process.poll() is None:
                if os.path.exists(cancel_path):
                    return 130
                left = deadline - time.monotonic()
                if left <= 0:
                    print(f"Sandbox command timed out after {timeout:g}s", file=sys.stderr)
                    return 124
                for key, _ in pipes.select(min(left, 0.1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        pipes.unregister(key.fileobj)
                        continue
                    accepted = min(len(chunk), remaining)
                    if accepted:
                        key.data.write(chunk[:accepted])
                        key.data.flush()
                        remaining -= accepted
                    truncated |= accepted < len(chunk)
        code = process.wait()
        finished = True
        return code if code >= 0 else 128 - code
    finally:
        # Never release an interrupted command's user lock with its ordinary
        # children still executing. A normal detached job keeps its semantics.
        if not finished:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        if cleanup_file:
            with suppress(FileNotFoundError):
                os.unlink(cleanup_file)
        if truncated:
            print(f"\n[Sandbox output truncated at {output_limit} bytes]", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(run(sys.argv[5], float(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]))
