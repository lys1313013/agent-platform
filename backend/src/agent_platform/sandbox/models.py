"""Sandbox execution — Docker container lifecycle management (stateless).

Containers are fully ephemeral: no Docker volumes are mounted.
/workspace is backed by tmpfs and files are synchronized to/from MinIO
via WorkspaceStorage on container creation and destruction.
"""

import asyncio
import fcntl
import hashlib
import json
import os
import shlex
import time
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import docker
import structlog
from docker.errors import DockerException, NotFound

from agent_platform.core.config import settings
from agent_platform.sandbox_docker_client import UnixSandboxClient
from agent_platform.sandbox_policy import container_name, container_options

if TYPE_CHECKING:
    import docker.models.containers

    from agent_platform.storage.workspace import WorkspaceStorage

logger = structlog.get_logger()

_COMMAND_RUNNER = Path(__file__).with_name("command_runner.py").read_text(encoding="utf-8")
# Includes room for image previews and skill file transfers. The model-facing
# text budget is applied separately by ToolExecutor.
_COMMAND_OUTPUT_LIMIT = 8 * 1024 * 1024


def docker_unavailable_reason(exc: Exception) -> str:
    """Return a useful reason without leaking connection URLs or credentials."""
    cause = exc
    seen: set[int] = set()
    while id(cause) not in seen:
        seen.add(id(cause))
        nested = cause.__cause__ or cause.__context__
        if nested is None or id(nested) in seen:
            break
        cause = nested
    if isinstance(cause, FileNotFoundError):
        return "Docker socket 或配置文件不存在"
    if isinstance(cause, PermissionError):
        return "没有访问 Docker socket 或配置文件的权限"
    if isinstance(cause, ConnectionRefusedError):
        return "Docker 拒绝连接，服务可能尚未启动"
    if isinstance(cause, TimeoutError) or "timeout" in type(cause).__name__.lower():
        return "连接 Docker 超时"
    return "Docker 不可用或连接配置无效"


class SandboxUnavailableError(RuntimeError):
    """Docker is unavailable; backend-only features can still run."""


@dataclass
class ExecResult:
    """Result from sandbox command execution."""

    stdout: str
    stderr: str
    exit_code: int
    output_truncated: bool = False


def _run_docker_command(api, container_id: str, command: str, timeout: float, cancel_path: str,
                        cleanup_file: str | None = None) -> ExecResult:
    argv = ["python3", "-I", "-S", "-c", _COMMAND_RUNNER,
            str(timeout), cancel_path, str(_COMMAND_OUTPUT_LIMIT), cleanup_file or "", command]
    exec_id = api.exec_create(container_id, argv, workdir="/workspace", user="sandbox")["Id"]
    stdout, stderr = bytearray(), bytearray()
    # Independently bound the Docker connection; the container also caps its
    # output before transmission. Reserve room for runner diagnostics.
    remaining = _COMMAND_OUTPUT_LIMIT + 1024
    truncated = False
    frames = api.exec_start(exec_id, stream=True, demux=True)
    # The docker SDK's CancellableStream.close() forcibly shuts down a live
    # socket. After natural EOF the daemon has already disconnected, so close
    # the owning HTTP response (as the SDK does for non-streaming execs).
    with closing(getattr(frames, "_response", frames)):
        for out, err in frames:
            for chunk, target in ((out, stdout), (err, stderr)):
                if chunk:
                    accepted = min(len(chunk), remaining)
                    target.extend(memoryview(chunk)[:accepted])
                    remaining -= accepted
                    truncated |= accepted < len(chunk)
    if truncated:
        stderr.extend(b"\n[Docker command output truncated]")
    # Docker can close the output stream just before publishing ExitCode.
    deadline = time.monotonic() + timeout + 2
    state = api.exec_inspect(exec_id)
    while state["Running"] and time.monotonic() < deadline:
        time.sleep(0.05)
        state = api.exec_inspect(exec_id)
    if state["Running"] or state["ExitCode"] is None:
        raise RuntimeError("Docker did not confirm command completion")
    clipped_notice = f"[Sandbox output truncated at {_COMMAND_OUTPUT_LIMIT} bytes]".encode()
    return ExecResult(
        stdout.decode(errors="replace"), stderr.decode(errors="replace"), state["ExitCode"],
        output_truncated=truncated or stderr.rstrip().endswith(clipped_notice),
    )


@dataclass
class Sandbox:
    """A running sandbox container."""

    container_id: str
    user_id: str
    session_id: str
    workspace_id: str
    workspace_slug: str
    created_at: datetime

    last_used_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    workspaces: dict[str, str] = field(default_factory=dict)

    def is_expired(self) -> bool:
        """Check if sandbox has exceeded TTL."""
        ttl_seconds = settings.sandbox.session_ttl
        return (datetime.now(UTC) - self.last_used_at).total_seconds() > ttl_seconds


class SandboxManager:
    """
    Manages ephemeral Docker sandbox containers per user.

    Principles:
    1. All commands run in containers, never touch the host.
    2. Containers are fully ephemeral — /workspace is tmpfs (no Docker volumes).
    3. Files persisted via MinIO (WorkspaceStorage injects on create, extracts on destroy).
    4. User-bound: all sessions for the same user share one container.
    5. Periodic sync protects against container crashes.
    """

    def __init__(self, workspace_storage: "WorkspaceStorage | None" = None):
        # Connect lazily: Docker failure must not disable backend-only tools.
        self._client: docker.DockerClient | None = None
        self._client_lock = asyncio.Lock()
        # key: "user:{user_id}" — sandbox is user-bound, shared across sessions
        self._active: dict[str, Sandbox] = {}
        self._workspace_storage = workspace_storage
        self._sync_task: asyncio.Task | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._owners: dict[str, asyncio.Task] = {}
        self._namespace = settings.sandbox.namespace
        self._lock_dir = Path(settings.sandbox.lock_dir)
        self._lock_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _connect_client() -> docker.DockerClient:
        broker_socket = os.environ.get("SANDBOX_BROKER_SOCKET")
        if broker_socket:
            client = UnixSandboxClient(base_url=f"unix://{broker_socket}", timeout=3)
        else:
            client = docker.from_env(timeout=3)
        # A short connection probe must not shorten actual command execution.
        client.api.timeout = docker.constants.DEFAULT_TIMEOUT_SECONDS
        return client

    async def _ensure_client(self) -> None:
        if self._client is not None:
            return
        async with self._client_lock:
            if self._client is not None:
                return
            task = asyncio.create_task(asyncio.to_thread(self._connect_client))
            try:
                self._client = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Keep ownership of a connection that finishes after cancellation.
                try:
                    self._client = await self._finish_task(task)
                except Exception:
                    pass
                raise
            except DockerException as exc:
                raise SandboxUnavailableError(
                    f"沙箱不可用：{docker_unavailable_reason(exc)}。请启动或检查 Docker 后重试。"
                ) from None

    def _name(self, user_id: str) -> str:
        return container_name(self._namespace, user_id)

    def _lock_path(self, user_id: str) -> Path:
        return self._lock_dir / f"{self._name(user_id)}.lock"

    @asynccontextmanager
    async def _user_lock(self, user_id: str, *, wait: bool = True):
        # Storage sync calls execute() recursively in the SAME task. Child tasks
        # must acquire their own lock, even if they inherited context variables.
        task = asyncio.current_task()
        if self._owners.get(user_id) is task:
            yield True
            return
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        if not wait and lock.locked():
            yield False
            return
        async with lock:
            # flock also serializes local backend workers/managers and releases
            # automatically on process death. Never unlink these lock files.
            with self._lock_path(user_id).open("a+b") as handle:
                acquired = False
                try:
                    while not acquired:
                        try:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            acquired = True
                        except BlockingIOError:
                            if not wait:
                                yield False
                                return
                            await asyncio.sleep(0.05)
                    self._owners[user_id] = task
                    yield True
                finally:
                    if acquired:
                        self._owners.pop(user_id, None)
                        fcntl.flock(handle, fcntl.LOCK_UN)

    def _touch(self, sandbox: Sandbox) -> None:
        sandbox.last_used_at = datetime.now(UTC)
        os.utime(self._lock_path(sandbox.user_id), None)

    def _expired(self, sandbox: Sandbox) -> bool:
        # Other workers update this timestamp under the same file lock.
        last_used = datetime.fromtimestamp(self._lock_path(sandbox.user_id).stat().st_mtime, UTC)
        sandbox.last_used_at = max(sandbox.last_used_at, last_used)
        return sandbox.is_expired()

    @staticmethod
    async def _finish_task(task):
        # A shutdown may cancel the caller more than once. The Docker thread
        # still owns the operation until it finishes.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        return task.result()

    @staticmethod
    async def _docker_call(fn, *args, **kwargs):
        # Cancelling to_thread does not stop its Docker operation. Keep the lock
        # until the actual operation finishes, including during shutdown.
        task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await SandboxManager._finish_task(task)
            except Exception:
                pass
            raise

    async def _container(self, container_id: str):
        await self._ensure_client()
        try:
            return await self._docker_call(self._client.containers.get, container_id)
        except NotFound:
            return None

    def _fingerprint(self) -> str:
        config = {
            key: getattr(settings.sandbox, key)
            for key in (
                "image",
                "cpu_limit",
                "memory_limit",
                "tmpfs_size",
                "network_disabled",
                "workspace_quota_mb",
            )
        }
        return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

    def _from_container(self, container) -> Sandbox:
        labels = container.labels
        return Sandbox(
            container_id=container.id,
            user_id=labels["aio.user_id"],
            session_id=labels.get("aio.session_id", ""),
            workspace_id=labels["aio.workspace_id"],
            workspace_slug=labels["aio.workspace_slug"],
            created_at=datetime.fromisoformat(container.attrs["Created"].replace("Z", "+00:00")),
        )

    async def _discover(self, user_id: str):
        await self._ensure_client()
        containers = await self._docker_call(
            self._client.containers.list,
            all=True,
            filters={
                "label": [
                    f"aio.namespace={self._namespace}",
                    f"aio.user_id={user_id}",
                    "aio.ephemeral=true",
                ]
            },
        )
        running = []
        for container in containers:
            if container.status in {"created", "exited", "dead"}:
                # No force: a concurrent start must not be killed.
                try:
                    await self._docker_call(container.remove)
                except NotFound:
                    pass
            else:
                running.append(container)
        if len(running) > 1:
            raise RuntimeError(
                "Multiple running sandboxes for user; preserve files and resolve duplicates first"
            )
        if running:
            container = running[0]
            if (
                container.status != "running"
                or container.labels.get("aio.config") != self._fingerprint()
            ):
                raise RuntimeError(
                    "Existing sandbox is not ready or its configuration changed; preserved for recovery"
                )
            if not container.labels.get("aio.workspace_slug"):
                raise RuntimeError(
                    "Existing sandbox lacks workspace metadata; preserved for recovery"
                )
            sandbox = self._from_container(container)
            await self._read_workspaces(sandbox)
            return sandbox
        return None

    def _key(self, user_id: str) -> str:
        return f"user:{user_id}"

    # ---- Public API ----

    async def get_or_create(
        self,
        user_id: str,
        session_id: str,
        workspace_id: str,
        workspace_slug: str,
    ) -> Sandbox:
        """Get or create a sandbox for the given user (user-bound, shared across sessions).

        Args:
            workspace_slug: Directory name in sandbox (e.g., "default").
                           Files are stored in /workspace/{workspace_slug}/.
        """
        async with self._user_lock(user_id):
            key = self._key(user_id)
            sandbox = self._active.get(key)
            if sandbox:
                container = await self._container(sandbox.container_id)
                if container and container.status == "running":
                    await self._ensure_workspace(sandbox, workspace_id, workspace_slug)
                    self._touch(sandbox)
                    return sandbox
                self._active.pop(key, None)

            sandbox = await self._discover(user_id)
            if sandbox is None:
                # Legacy containers have no namespace/slug. Do not create another
                # alongside one that may contain unsynchronized user files.
                legacy = await self._docker_call(
                    self._client.containers.list,
                    filters={"label": [f"aio.user_id={user_id}", "aio.ephemeral=true"]},
                )
                if any(
                    not c.labels.get("aio.namespace")
                    and c.labels.get("com.docker.compose.project") == self._namespace
                    for c in legacy
                ):
                    raise RuntimeError(
                        "Running legacy sandbox found; synchronize its files before upgrading"
                    )
                sandbox = await self._create(user_id, session_id, workspace_id, workspace_slug)
            else:
                await self._ensure_workspace(sandbox, workspace_id, workspace_slug)
            self._active[key] = sandbox
            self._touch(sandbox)
            return sandbox

    def get_active_for_user(self, user_id: str) -> Sandbox | None:
        """Find a live (non-expired) sandbox for the given user."""
        key = self._key(user_id)
        sandbox = self._active.get(key)
        if sandbox is not None and not sandbox.is_expired():
            return sandbox
        return None

    async def execute(
        self,
        sandbox: Sandbox,
        command: str,
        timeout: float | None = None,
        *,
        cleanup_file: str | None = None,
    ) -> ExecResult:
        """Execute a command in the sandbox container."""
        internal = self._owners.get(sandbox.user_id) is asyncio.current_task()
        async with self._user_lock(sandbox.user_id):
            try:
                container = await self._container(sandbox.container_id)
                if container is None or container.status != "running":
                    raise RuntimeError(
                        "Sandbox is no longer running; obtain a new sandbox before executing"
                    )
                cancel_path = f"/tmp/.aio-command-{uuid4().hex}.cancel"
                duration = settings.sandbox.command_timeout if timeout is None else timeout
                task = asyncio.create_task(asyncio.to_thread(
                    _run_docker_command, self._client.api, container.id, command, duration, cancel_path, cleanup_file,
                ))
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    async def cancel_and_wait():
                        try:
                            await asyncio.to_thread(container.exec_run, ["touch", "--", cancel_path], user="sandbox")
                        finally:
                            try:
                                await task
                            finally:
                                # Also handles a cancellation racing natural completion.
                                await asyncio.to_thread(container.exec_run, ["rm", "-f", "--", cancel_path], user="sandbox")

                    try:
                        await self._finish_task(asyncio.create_task(cancel_and_wait()))
                    except Exception as error:
                        logger.warning("sandbox_command_cancel_failed", error=str(error))
                    raise
            finally:
                if not internal:
                    self._touch(sandbox)

    async def reconcile_uploaded_workspace(
        self, user_id: str, workspace_id: str, workspace_slug: str,
    ) -> Sandbox | None:
        """Merge external uploads before serving a mounted workspace, across workers."""
        async with self._user_lock(user_id):
            sandbox = self._active.get(self._key(user_id)) or await self._discover(user_id)
            if sandbox is None:
                return None
            await self._read_workspaces(sandbox)
            if sandbox.workspaces.get(workspace_id) != workspace_slug:
                return None
            storage = self._workspace_storage
            if storage is None:
                raise RuntimeError("Object storage unavailable")
            if await self._docker_call(storage.remote_changed, workspace_id, sandbox.container_id):
                stats = await storage.extract_and_sync(self, sandbox, workspace_id, workspace_slug)
                if stats.errors:
                    raise RuntimeError("Workspace refresh failed: " + "; ".join(stats.errors))
            return sandbox

    async def delete_workspace_data(self, user_id: str, workspace_id: str, storage) -> None:
        async with self._user_lock(user_id):
            await self._docker_call(storage.delete_workspace, workspace_id)

    async def delete_workspace_path(
        self, user_id: str, workspace_id: str, workspace_slug: str, path: str, storage,
    ) -> bool:
        """Serialize deletion against local commands, sync and other backend workers."""
        from agent_platform.storage.workspace import WorkspaceStorage

        async with self._user_lock(user_id):
            # Keep the live copy intact if object storage rejects the delete.
            deleted = await self._docker_call(storage.delete_file, workspace_id, path)
            sandbox = self._active.get(self._key(user_id)) or await self._discover(user_id)
            if sandbox is None:
                return deleted
            await self._read_workspaces(sandbox)
            if sandbox.workspaces.get(workspace_id) != workspace_slug:
                return deleted
            local = await WorkspaceStorage.delete_file_live(self, sandbox, path, workspace_slug)
            return local or deleted

    async def inject_uploaded_file(
        self, user_id: str, workspace_id: str, workspace_slug: str, path: str, content: bytes,
    ) -> bool:
        """Refresh an already-running workspace after upload; do not start a container.

        The caller has persisted the file in object storage. New containers load it
        on initialization; existing containers need this explicit, chunked write.
        """
        from agent_platform.storage.workspace import WorkspaceStorage

        async with self._user_lock(user_id):
            sandbox = self._active.get(self._key(user_id)) or await self._discover(user_id)
            if sandbox is None:
                return False
            container = await self._container(sandbox.container_id)
            if container is None or container.status != "running":
                return False
            await self._ensure_workspace(sandbox, workspace_id, workspace_slug)
            written = await WorkspaceStorage.write_file_live(
                self, sandbox, path, content, workspace_slug,
            )
            if not written:
                raise RuntimeError("Could not inject uploaded file into the active workspace")
            self._touch(sandbox)
            return True

    async def write_workspace_file(
        self, sandbox: Sandbox, workspace_id: str, workspace_slug: str,
        path: str, content: bytes,
    ) -> bool:
        """Write a result and immediately sync it, excluding concurrent reclamation.

        Returns whether durable object storage was available. A failed write
        raises so callers can retain the original output instead of truncating.
        """
        from agent_platform.storage.workspace import WorkspaceStorage

        async with self._user_lock(sandbox.user_id):
            written = await WorkspaceStorage.write_file_live(
                self, sandbox, path, content, workspace_slug,
            )
            if not written:
                raise RuntimeError("Could not write full tool output to the sandbox")
            if self._workspace_storage is None:
                return False
            await self._docker_call(self._workspace_storage.put_file, workspace_id, path, content)
            return True

    async def _read_workspaces(self, sandbox: Sandbox) -> None:
        # Storage IDs must never come from files writable by sandbox commands.
        path = self._lock_path(sandbox.user_id).with_suffix(".json")
        if not path.exists():
            raise RuntimeError("Sandbox initialization is incomplete; preserved for recovery")
        state = json.loads(path.read_text())
        if state.get("container_id") != sandbox.container_id:
            raise RuntimeError("Sandbox initialization is incomplete; preserved for recovery")
        workspaces = state.get("workspaces")
        if (
            not isinstance(workspaces, dict)
            or not workspaces
            or not all(
                isinstance(key, str) and isinstance(value, str) and value
                for key, value in workspaces.items()
            )
        ):
            raise RuntimeError("Invalid sandbox workspace metadata; preserved for recovery")
        sandbox.workspaces = workspaces

    async def _ensure_workspace(
        self, sandbox: Sandbox, workspace_id: str, workspace_slug: str
    ) -> None:
        await self._read_workspaces(sandbox)
        if workspace_id in sandbox.workspaces:
            if sandbox.workspaces[workspace_id] != workspace_slug:
                raise RuntimeError("Workspace slug changed; synchronize the existing sandbox first")
            return
        if workspace_slug in sandbox.workspaces.values():
            raise RuntimeError("Workspace slug already belongs to a different workspace")
        await self._inject_workspace(sandbox, workspace_id, workspace_slug)

    async def _inject_workspace(
        self, sandbox: Sandbox, workspace_id: str, workspace_slug: str
    ) -> None:
        if self._workspace_storage is None:
            raise RuntimeError("Object storage unavailable; cannot safely initialize workspace")
        if not workspace_slug or workspace_slug in {".", ".."} or "/" in workspace_slug:
            raise ValueError("Invalid workspace slug")
        result = await self.execute(
            sandbox, f"mkdir -p {shlex.quote('/workspace/' + workspace_slug)}"
        )
        if result.exit_code:
            raise RuntimeError("Could not initialize sandbox workspace directory")
        stats = await self._workspace_storage.inject_files(
            self, sandbox, workspace_id, workspace_slug
        )
        if stats.errors:
            raise RuntimeError("Sandbox initialization failed: " + "; ".join(stats.errors))
        workspaces = {**sandbox.workspaces, workspace_id: workspace_slug}
        path = self._lock_path(sandbox.user_id).with_suffix(".json")
        pending = path.with_suffix(".new")
        pending.write_text(
            json.dumps({"container_id": sandbox.container_id, "workspaces": workspaces})
        )
        pending.replace(path)
        sandbox.workspaces = workspaces

    async def _sync(self, sandbox: Sandbox) -> None:
        if self._workspace_storage is None:
            raise RuntimeError("Object storage unavailable; retaining sandbox files")
        await self._read_workspaces(sandbox)
        for workspace_id, workspace_slug in sandbox.workspaces.items():
            stats = await self._workspace_storage.extract_and_sync(
                self, sandbox, workspace_id, workspace_slug
            )
            if stats.errors:
                raise RuntimeError("Sandbox file sync failed: " + "; ".join(stats.errors))

    async def destroy(self, sandbox: Sandbox, sync: bool = True) -> None:
        """Remove only after successful sync; keep failed sandboxes recoverable."""
        async with self._user_lock(sandbox.user_id):
            container = await self._container(sandbox.container_id)
            if container:
                if container.status == "running":
                    if sync:
                        await self._sync(sandbox)
                    await self._docker_call(container.stop, timeout=5)
                await self._docker_call(container.remove)
            key = self._key(sandbox.user_id)
            if self._active.get(key) is sandbox:
                self._active.pop(key, None)
            logger.info("sandbox_destroyed", container_id=sandbox.container_id[:12])

    async def destroy_all_for_user(self, user_id: str) -> None:
        """Destroy all active sandboxes for a user."""
        for _key, sandbox in list(self._active.items()):
            if sandbox.user_id == user_id:
                await self.destroy(sandbox)

    async def cleanup_expired(self) -> int:
        """Reclaim idle containers, skipping users with an operation in progress."""
        destroyed = 0
        for sandbox in list(self._active.values()):
            try:
                async with self._user_lock(sandbox.user_id, wait=False) as acquired:
                    if acquired and self._expired(sandbox):
                        await self.destroy(sandbox)
                        destroyed += 1
            except Exception as exc:
                logger.warning(
                    "sandbox_cleanup_failed", container_id=sandbox.container_id, error=str(exc)
                )
        return destroyed

    async def shutdown(self) -> None:
        """Sync and leave containers available for another worker or a restart."""
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            self._sync_task = None
        for sandbox in list(self._active.values()):
            try:
                async with self._user_lock(sandbox.user_id):
                    container = await self._container(sandbox.container_id)
                    if container and container.status == "running":
                        await self._sync(sandbox)
            except Exception as exc:
                logger.warning(
                    "sandbox_shutdown_sync_failed",
                    container_id=sandbox.container_id,
                    error=str(exc),
                )
        self._active.clear()
        if self._client is not None:
            await self._docker_call(self._client.close)
            self._client = None

    async def start_periodic_sync(self, interval_seconds: int | None = None) -> None:
        """Recover this deployment's containers and start sync/idle cleanup."""
        if self._sync_task is not None:
            return
        try:
            await self._recover()
        except (SandboxUnavailableError, DockerException) as exc:
            logger.warning(
                "sandbox_startup_degraded",
                reason=str(exc) if isinstance(exc, SandboxUnavailableError) else docker_unavailable_reason(exc),
            )
        interval = interval_seconds or settings.storage.sync_interval_seconds
        self._sync_task = asyncio.create_task(self._periodic_sync_loop(interval))

    async def _recover(self) -> None:
        await self._ensure_client()
        containers = await self._docker_call(
            self._client.containers.list,
            all=True,
            filters={"label": [f"aio.namespace={self._namespace}", "aio.ephemeral=true"]},
        )
        for user_id in {c.labels.get("aio.user_id") for c in containers} - {None, ""}:
            try:
                async with self._user_lock(user_id, wait=False) as acquired:
                    if not acquired:
                        continue
                    sandbox = await self._discover(user_id)
                    current = self._active.get(self._key(user_id))
                    if sandbox and (
                        current is None or current.container_id != sandbox.container_id
                    ):
                        self._active[self._key(user_id)] = sandbox
                        # Discovery and periodic sync are not user activity.
                        sandbox.last_used_at = datetime.fromtimestamp(
                            self._lock_path(user_id).stat().st_mtime, UTC
                        )
            except Exception as exc:
                logger.warning("sandbox_recovery_failed", user_id=user_id, error=str(exc))

    async def _periodic_sync_loop(self, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                # Also recover a container whose creator died before registration.
                await self._recover()
                await self.cleanup_expired()
                for sandbox in list(self._active.values()):
                    async with self._user_lock(sandbox.user_id, wait=False) as acquired:
                        if acquired:
                            try:
                                await self._sync(sandbox)
                            except Exception as exc:
                                logger.warning("sandbox_periodic_sync_failed", error=str(exc))
            except Exception as exc:
                logger.warning("sandbox_maintenance_failed", error=str(exc))

    # ---- Internal ----

    async def _create(
        self, user_id: str, session_id: str, workspace_id: str, workspace_slug: str
    ) -> Sandbox:
        """Create a new sandbox container with tmpfs /workspace (no Docker volumes)."""
        labels = {
            "aio.namespace": self._namespace,
            "aio.config": self._fingerprint(),
            "aio.user_id": user_id,
            "aio.session_id": session_id,
            "aio.workspace_id": workspace_id,
            "aio.workspace_slug": workspace_slug,
            "aio.ephemeral": "true",
        }
        create_task = asyncio.create_task(asyncio.to_thread(
            self._client.containers.create, settings.sandbox.image, "sleep infinity",
            **container_options(settings.sandbox, labels),
        ))
        try:
            container = await asyncio.shield(create_task)
        except asyncio.CancelledError:
            # The thread may still be creating the container. Wait for its ID.
            try:
                container = await self._finish_task(create_task)
                await self._docker_call(container.remove, force=True)
            except Exception as exc:
                logger.warning("sandbox_cancel_cleanup_failed", error=str(exc))
            raise

        sandbox = Sandbox(
            container_id=container.id,
            user_id=user_id,
            session_id=session_id,
            workspace_id=workspace_id,
            workspace_slug=workspace_slug,
            created_at=datetime.now(UTC),
        )

        try:
            await self._docker_call(container.start)
            await self._inject_workspace(sandbox, workspace_id, workspace_slug)
        except BaseException:
            # Never sync a partially initialized workspace back over stored files.
            await self._docker_call(container.remove, force=True)
            raise
        logger.info("sandbox_created", container_id=container.id[:12], user_id=user_id)
        return sandbox
