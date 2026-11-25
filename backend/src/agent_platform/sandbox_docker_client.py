"""Docker SDK client for Unix sockets, preserving buffered exec output."""

import struct

import docker
from docker.utils.socket import consume_socket_output, demux_adaptor


class _UnixAPIClient(docker.APIClient):
    def _read_from_socket(self, response, stream, tty=True, demux=False):
        if tty:
            return super()._read_from_socket(response, stream, tty, demux)
        self._raise_for_status(response)
        # HTTP header parsing can already have buffered the first stdout frame.
        # The SDK reads the underlying file descriptor directly, losing those
        # bytes when a short-running exec completed in the same socket read.
        reader = response.raw._fp.fp
        self._disable_socket_timeout(reader.raw)

        def frames():
            while header := reader.read(8):
                if len(header) != 8:
                    raise OSError("Incomplete Docker exec frame header")
                channel, remaining = struct.unpack(">BxxxI", header)
                while remaining:
                    block = reader.read(min(remaining, 64 * 1024))
                    if not block:
                        raise OSError("Incomplete Docker exec frame")
                    remaining -= len(block)
                    yield demux_adaptor(channel, block) if demux else block

        if stream:
            return frames()
        try:
            return consume_socket_output(frames(), demux)
        finally:
            response.close()


class UnixSandboxClient(docker.DockerClient):
    """Use the SDK's container models and its Unix adapter for sandbox operations."""

    def __init__(self, base_url, timeout=60):
        self.api = _UnixAPIClient(base_url=base_url, timeout=timeout)
