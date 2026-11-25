"""Unix-only broker for the subset of the Docker SDK used by SandboxManager."""

import json
import logging
import os
import re
import socketserver
import struct
from contextlib import closing
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from docker.errors import APIError

from agent_platform.sandbox_docker_client import UnixSandboxClient
from agent_platform.sandbox_policy import SandboxSettings, container_options

SOCKET = "/run/aio-sandbox/control.sock"
LABELS = {"aio.namespace", "aio.config", "aio.user_id", "aio.session_id", "aio.workspace_id",
          "aio.workspace_slug", "aio.ephemeral"}
HEX_ID = r"[a-f0-9]{64}"
MAX_BODY = 4 * 1024 * 1024


class Broker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, daemon, config):
        self.daemon = daemon
        self.config = config
        super().__init__(path, Handler)

    def owned(self, container_id, *, inspect_only=False):
        container = self.daemon.containers.get(container_id)
        labels = container.labels
        if labels.get("aio.ephemeral") == "true":
            if labels.get("aio.namespace") == self.config.namespace:
                return container
            if inspect_only and not labels.get("aio.namespace") and (
                labels.get("com.docker.compose.project") == self.config.namespace
            ):
                return container
        raise PermissionError("Container is outside this sandbox deployment")


class Handler(BaseHTTPRequestHandler):
    """Translate each authorized operation into a new SDK call with fixed options."""

    def address_string(self):
        return "unix"

    def log_request(self, *args):
        pass

    def reply(self, status, value=None):
        if getattr(self, "upgraded", False):
            return
        data = b"" if value is None else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 <= size <= MAX_BODY:
            raise ValueError("Invalid request size")
        return json.loads(self.rfile.read(size)) if size else {}

    def handle_operation(self):
        try:
            url = urlsplit(self.path)
            path = re.sub(r"^/v[0-9]+\.[0-9]+/", "/", url.path)
            args = parse_qs(url.query)
            self.dispatch(self.command, path, args)
        except PermissionError:
            self.reply(403, {"message": "Operation outside sandbox policy"})
        except (KeyError, ValueError, TypeError):
            self.reply(400, {"message": "Invalid sandbox request"})
        except APIError as e:
            self.reply(e.status_code or 503, {"message": "Sandbox engine rejected operation"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logging.exception("Sandbox broker operation failed")
            self.reply(503, {"message": "Sandbox engine unavailable"})

    do_GET = do_POST = do_DELETE = handle_operation

    def dispatch(self, method, path, args):
        daemon = self.server.daemon
        config = self.server.config
        if method == "GET" and path == "/version":
            return self.reply(200, {"ApiVersion": daemon.api._version})
        if method == "GET" and path == "/containers/json":
            requested = json.loads(args.get("filters", ["{}"])[0]).get("label", [])
            if not isinstance(requested, list) or not all(isinstance(label, str) for label in requested):
                raise ValueError("Invalid label selectors")
            scope = f"aio.namespace={config.namespace}"
            if scope not in requested:
                # SandboxManager's legacy lookup only inspects containers bearing
                # this deployment's compose-project label.
                scope = f"com.docker.compose.project={config.namespace}"
            filters = {"label": [*requested, scope, "aio.ephemeral=true"]}
            rows = daemon.api.containers(all=args.get("all") == ["1"], filters=filters)
            return self.reply(200, [{"Id": row["Id"]} for row in rows])
        if method == "POST" and path == "/containers/create":
            labels = self.body()["Labels"]
            if not isinstance(labels, dict) or set(labels) != LABELS or not all(
                isinstance(value, str) and len(value) <= 512 for value in labels.values()
            ):
                raise ValueError("Invalid sandbox labels")
            if labels["aio.namespace"] != config.namespace or labels["aio.ephemeral"] != "true":
                raise PermissionError()
            # Image, command, name, mounts, user, network and limits are all broker-owned.
            instance = daemon.containers.create(
                config.image, "sleep infinity", **container_options(config, labels),
            )
            return self.reply(201, {"Id": instance.id, "Warnings": []})
        container_match = re.fullmatch(rf"/containers/({HEX_ID})(?:/(json|start|stop|exec))?", path)
        if container_match:
            container_id, action = container_match.groups()
            container = self.server.owned(container_id, inspect_only=(method, action) == ("GET", "json"))
            if (method, action) == ("GET", "json"):
                return self.reply(200, container.attrs)
            if (method, action) == ("POST", "start"):
                container.start()
                return self.reply(204)
            if (method, action) == ("POST", "stop"):
                container.stop(timeout=5)
                return self.reply(204)
            if (method, action) == ("DELETE", None):
                container.remove(force=args.get("force", ["false"])[0].lower() in {"true", "1"})
                return self.reply(204)
            if (method, action) == ("POST", "exec"):
                cmd = self.body()["Cmd"]
                if not isinstance(cmd, list) or not cmd or not all(isinstance(item, str) for item in cmd):
                    raise ValueError("Invalid command")
                result = daemon.api.exec_create(container_id, cmd, user="sandbox", workdir="/workspace")
                return self.reply(201, result)
        exec_match = re.fullmatch(rf"/exec/({HEX_ID})/(json|start)", path)
        if exec_match:
            exec_id, action = exec_match.groups()
            state = daemon.api.exec_inspect(exec_id)
            self.server.owned(state["ContainerID"])
            if (method, action) == ("GET", "json"):
                return self.reply(200, state)
            if (method, action) == ("POST", "start"):
                # The Docker SDK reads binary stdout/stderr frames directly from
                # the upgraded socket. No client input is forwarded.
                self.body()
                frames = daemon.api.exec_start(exec_id, stream=True, demux=True)
                # CancellableStream.close() shuts down a live socket and can
                # raise ENOTCONN after natural EOF. Close the owning response.
                with closing(getattr(frames, "_response", frames)):
                    self.upgraded = True
                    self.send_response(101)
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Upgrade", "tcp")
                    self.end_headers()
                    self.close_connection = True
                    for stdout, stderr in frames:
                        for channel, block in ((1, stdout), (2, stderr)):
                            if block:
                                self.wfile.write(struct.pack(">BxxxI", channel, len(block)))
                                self.wfile.write(block)
                return
        self.reply(403, {"message": "Unsupported sandbox operation"})


def main():
    path = Path(SOCKET)
    path.unlink(missing_ok=True)
    os.umask(0o077)
    daemon = UnixSandboxClient(base_url="unix:///var/run/docker.sock")
    try:
        with Broker(SOCKET, daemon, SandboxSettings()) as broker:
            broker.serve_forever()
    finally:
        daemon.close()
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
