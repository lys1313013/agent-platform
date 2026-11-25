"""Normal SDK lifecycle on the broker's Unix socket with an isolated engine fixture."""

import tempfile
import threading
from unittest.mock import Mock

from agent_platform.sandbox_broker import Broker
from agent_platform.sandbox_docker_client import UnixSandboxClient
from agent_platform.sandbox_policy import SandboxSettings


def test_scoped_broker_lifecycle():
    from contextlib import closing

    container_id, exec_id = "a" * 64, "b" * 64
    daemon = Mock()
    daemon.api._version = "1.48"
    settings = SandboxSettings(image="workspace:local", namespace="test-broker", network_disabled=True)
    labels = {"aio.namespace": settings.namespace, "aio.config": "fingerprint", "aio.user_id": "user",
              "aio.session_id": "session", "aio.workspace_id": "workspace", "aio.workspace_slug": "default",
              "aio.ephemeral": "true"}
    container = Mock(id=container_id, labels=labels, attrs={
        "Id": container_id, "Config": {"Labels": labels}, "State": {"Status": "running"},
    })
    daemon.containers.get.return_value = container
    daemon.containers.create.return_value = container
    daemon.api.containers.return_value = [{"Id": container_id}]
    daemon.api.exec_create.return_value = {"Id": exec_id}
    daemon.api.exec_inspect.return_value = {"ContainerID": container_id, "Running": False, "ExitCode": 0}

    def frames():
        yield b"hello", None
        yield None, b"diagnostic"

    daemon.api.exec_start.return_value = frames()
    # AF_UNIX on macOS limits the complete path to 103 bytes.
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp, Broker(tmp + "/control.sock", daemon, settings) as broker:
        socket_path = broker.server_address
        thread = threading.Thread(target=broker.serve_forever)
        thread.start()
        try:
            with closing(UnixSandboxClient(base_url="unix://" + socket_path)) as client:
                instance = client.containers.create(settings.image, "sleep infinity", labels=labels)
                spec = daemon.containers.create.call_args.kwargs
                assert spec["read_only"] and spec["user"] == "sandbox" and spec["mounts"] == []
                assert spec["cap_drop"] == ["ALL"] and spec["network_disabled"]
                instance.start()
                items = client.containers.list(filters={"label": ["aio.namespace=test-broker"]})
                assert [item.id for item in items] == [container_id]
                assert "aio.ephemeral=true" in daemon.api.containers.call_args.kwargs["filters"]["label"]
                result = instance.exec_run(["echo", "hello"], user="sandbox", demux=True)
                assert result.output == (b"hello", b"diagnostic") and result.exit_code == 0
                instance.stop()
                instance.remove(force=True)
                container.stop.assert_called_once_with(timeout=5)
                container.remove.assert_called_once_with(force=True)
        finally:
            broker.shutdown()
            thread.join()


def test_broker_restricts_control_to_its_own_ephemeral_containers():
    import pytest

    daemon = Mock()
    daemon.containers.get.return_value.labels = {"aio.namespace": "other", "aio.ephemeral": "true"}
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp, Broker(
        tmp + "/control.sock", daemon, SandboxSettings(namespace="ours"),
    ) as broker:
        with pytest.raises(PermissionError):
            broker.owned("outside")
        daemon.containers.get.return_value.labels = {"aio.namespace": "ours", "aio.ephemeral": "false"}
        with pytest.raises(PermissionError):
            broker.owned("not-ephemeral")
