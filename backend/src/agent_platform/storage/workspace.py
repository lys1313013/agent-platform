"""Workspace file synchronization between MinIO and sandbox containers.

Handles:
- Injecting files from MinIO into a sandbox container on creation
- Extracting files from a sandbox container back to MinIO on destruction
- Periodic sync as a safety net against container crashes
- File browsing and presigned URL generation (no container required)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import posixpath
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog
from minio.error import S3Error

if TYPE_CHECKING:
    from agent_platform.sandbox.models import Sandbox, SandboxManager

from agent_platform.storage.client import ObjectInfo, ObjectStorage

logger = structlog.get_logger()

_DELETED = b"AIO workspace file deletion marker v1\n"
_DELETED_TYPE = "application/vnd.aio.workspace-deleted"
# Reads return base64 on stdout. 3 MiB expands to 4 MiB under the 8 MiB
# capture cap. Writes carry base64 in one argv string; 72 KiB expands to
# 96 KiB, leaving room under Linux's 128 KiB per-argument limit.
_READ_CHUNK_BYTES = 3 * 1024 * 1024
_WRITE_CHUNK_BYTES = 72 * 1024
_SANDBOX_IO = Path(__file__).with_name("sandbox_workspace_io.py").read_text()


def _command(*args: str) -> str:
    return shlex.join(["python3", "-c", _SANDBOX_IO, *args])


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _io(fn, *args):
    # The manager owns a cross-process lock during sync. Cancellation must not
    # release it while a worker thread is still modifying workspace objects.
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        raise


class WorkspaceDeleted(Exception):
    """The database workspace has been removed; old sandboxes must not publish files."""


@dataclass
class SyncStats:
    """Statistics from a sync operation."""

    files_synced: int = 0
    files_deleted: int = 0
    bytes_transferred: int = 0
    duration_ms: float = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class FileEntry:
    """A file entry for listing workspace contents."""

    path: str
    size: int
    is_dir: bool = False
    last_modified: str = ""


class WorkspaceStorage:
    """
    Manages workspace file synchronization between MinIO and sandbox containers.

    Object key layout in MinIO:
        workspaces/{workspace_id}/files/{relative_path}  — workspace files
        workspaces/{workspace_id}/meta.json              — file manifest
    """

    FILES_PREFIX = "workspaces/{workspace_id}/files"
    META_KEY = "workspaces/{workspace_id}/meta.json"

    def __init__(self, storage: ObjectStorage) -> None:
        self._storage = storage

    # ---- Key helpers ----

    def _files_prefix(self, workspace_id: str) -> str:
        return f"workspaces/{workspace_id}/files/"

    def _object_key(self, workspace_id: str, path: str) -> str:
        return f"workspaces/{workspace_id}/files/{self._valid_file(path.lstrip('/'))}"

    def _meta_key(self, workspace_id: str) -> str:
        return f"workspaces/{workspace_id}/meta.json"

    def _relative_path(self, workspace_id: str, object_key: str) -> str:
        """Extract the relative file path from a full object key."""
        prefix = self._files_prefix(workspace_id)
        if object_key.startswith(prefix):
            return object_key[len(prefix):]
        return object_key

    # ================================================================
    # MinIO → Sandbox (inject on container creation)
    # ================================================================

    @staticmethod
    def _valid_file(path: str) -> str:
        if not path or "\x00" in path or any(part in {"", ".", ".."} for part in path.split("/")):
            raise ValueError(f"Invalid workspace file path: {path!r}")
        return path

    def _inventory(self, workspace_id: str) -> dict[str, ObjectInfo]:
        prefix = self._files_prefix(workspace_id)
        return {self._valid_file(self._relative_path(workspace_id, obj.key)): obj
                for obj in self._storage.list(prefix) if not obj.key.endswith("/")}

    def _is_marker(self, obj: ObjectInfo | None) -> bool:
        if obj is None or obj.size != len(_DELETED):
            return False
        # Only same-length objects need HEAD; LIST generally omits Content-Type.
        # The MIME type distinguishes a marker even when a user file has the same bytes.
        if obj.content_type:
            return obj.content_type == _DELETED_TYPE
        info = self._storage.head(obj.key)
        if info.etag != obj.etag:
            raise OSError(f"Workspace object changed while listing: {obj.key!r}")
        return info.content_type == _DELETED_TYPE

    @staticmethod
    def _entry(path, data_hash, size, etag):
        return {"path": path, "sha256": data_hash, "size": size, "etag": etag}

    def _read_baseline(self, workspace_id: str, container_id: str) -> dict:
        raw = self._storage.get_optional(self._meta_key(workspace_id))
        if raw is None:
            return {}
        manifest = json.loads(raw)
        if manifest.get("deleted"):
            raise WorkspaceDeleted(workspace_id)
        if manifest.get("container_id") != container_id or manifest.get("version") != 2:
            # A legacy manifest cannot establish which revisions this container saw.
            return {}
        return {self._valid_file(item["path"]): item for item in manifest["files"]}

    def remote_changed(self, workspace_id: str, container_id: str) -> bool:
        baseline = self._read_baseline(workspace_id, container_id)
        current = self._inventory(workspace_id)
        return (
            set(current) != set(baseline)
            or any(
                info.etag != baseline[path]["etag"]
                or (info.size == len(_DELETED) and self._is_marker(info) != (baseline[path]["sha256"] is None))
                for path, info in current.items()
            )
        )

    def _write_baseline(self, workspace_id: str, container_id: str, files: dict) -> None:
        manifest = {
            "version": 2, "workspace_id": workspace_id, "container_id": container_id,
            "file_count": sum(item["sha256"] is not None for item in files.values()),
            "total_size": sum(item["size"] for item in files.values()),
            "files": [files[path] for path in sorted(files)],
        }
        self._storage.put(self._meta_key(workspace_id), json.dumps(manifest).encode(), "application/json")

    async def inject_files(self, sandbox_mgr, sandbox, workspace_id: str, workspace_slug: str | None = None) -> SyncStats:
        """Restore authoritative remote versions and record exactly what was mounted."""
        stats = SyncStats()
        started = time.monotonic()
        try:
            await _io(self._read_baseline, workspace_id, sandbox.container_id)
            files = {}
            objects = await _io(self._inventory, workspace_id)
            for path, obj in objects.items():
                if await self._marker(obj):
                    files[path] = self._entry(path, None, 0, obj.etag)
                    continue
                data = await _io(self._storage.get_version, obj.key, obj.etag)
                digest = await _io(_sha256, data)
                if not await self.write_file_live(sandbox_mgr, sandbox, path, data, workspace_slug):
                    raise OSError(f"Could not restore workspace file {path!r}")
                files[path] = {"path": path, "size": len(data), "sha256": digest, "etag": obj.etag}
                stats.files_synced += 1
                stats.bytes_transferred += len(data)
            await _io(self._write_baseline, workspace_id, sandbox.container_id, files)
        except Exception as exc:
            stats.errors.append(str(exc))
            logger.warning("workspace_inject_failed", workspace_id=workspace_id, error=str(exc))
        stats.duration_ms = (time.monotonic() - started) * 1000
        return stats

    async def _marker(self, obj: ObjectInfo | None) -> bool:
        return bool(obj and obj.size == len(_DELETED) and await _io(self._is_marker, obj))

    async def _scan_live(self, manager, sandbox, root: str) -> dict:
        result = await manager.execute(sandbox, _command("scan", root))
        if result.exit_code:
            raise OSError(f"Could not scan workspace: {result.stderr}")
        rows = json.loads(result.stdout)
        return {self._valid_file(path): (digest, size) for path, (digest, size) in rows.items()}

    async def extract_and_sync(self, sandbox_mgr, sandbox, workspace_id: str, workspace_slug: str | None = None) -> SyncStats:
        """Merge the mounted baseline, live file hashes and current object versions.

        The sandbox manager serializes these calls under its cross-worker user
        lock. Direct uploads are independent writers; mutations of object keys
        consequently use the observed ETag even while that lock is held.
        """
        stats = SyncStats()
        started = time.monotonic()
        try:
            base = await _io(self._read_baseline, workspace_id, sandbox.container_id)
            root = f"/workspace/{workspace_slug}" if workspace_slug else "/workspace"
            local = await self._scan_live(sandbox_mgr, sandbox, root)
            objects = await _io(self._inventory, workspace_id)
            merged = {}
            for path in sorted(set(local) | set(objects) | set(base)):
                entry = await self._merge_file(
                    sandbox_mgr, sandbox, workspace_id, workspace_slug, path,
                    base.get(path), local.get(path), objects.get(path), stats,
                )
                if entry:
                    merged[path] = entry
            if merged != base:
                await _io(self._write_baseline, workspace_id, sandbox.container_id, merged)
        except WorkspaceDeleted:
            # The container may still mount an old workspace until its TTL.
            pass
        except Exception as exc:
            stats.errors.append(str(exc))
            logger.warning("workspace_sync_failed", workspace_id=workspace_id, error=str(exc))
        stats.duration_ms = (time.monotonic() - started) * 1000
        return stats

    async def _merge_file(self, manager, sandbox, wid, slug, path, base, local, obj, stats):
        base_hash = base["sha256"] if base else None
        local_hash = local[0] if local else None
        etag = obj.etag if obj else None
        remote = None if await self._marker(obj) else obj
        remote_unchanged = (
            (base["etag"] if base else None) == etag
            and (base_hash is not None) == (remote is not None)
        )
        if remote_unchanged and local_hash == base_hash:
            return base
        marker = self._entry(path, None, 0, etag) if obj and not remote else None
        if not remote and not local:
            return marker

        # Publish local-only edits with a precondition on the observed object,
        # including deletion markers and first creation.
        key = self._object_key(wid, path)

        async def local_bytes():
            data = await self.read_file_live(manager, sandbox, path, slug)
            if data is None or await _io(_sha256, data) != local_hash:
                raise OSError(f"Workspace file changed during sync: {path!r}")
            return data

        if remote_unchanged:
            if local:
                data = await local_bytes()
                version = await _io(self._storage.put_version, key, data, self._guess_content_type(path), etag)
                stats.files_synced += 1
                stats.bytes_transferred += len(data)
                return {"path": path, "size": len(data), "sha256": local_hash, "etag": version}
            if remote:
                # Conditional DELETE is ignored by some MinIO versions. A
                # conditional marker PUT cannot erase a concurrently finished upload.
                version = await _io(self._storage.put_version, key, _DELETED, _DELETED_TYPE, etag)
                stats.files_deleted += 1
                return self._entry(path, None, 0, version)
            return marker

        # A direct upload/delete happened after mounting. The remote revision
        # keeps its name; retain any divergent sandbox edit before replacing it.
        data = await _io(self._storage.get_version, key, etag) if remote else None
        digest = await _io(_sha256, data) if data is not None else None
        if local_hash != digest:
            if local and local_hash != base_hash:
                await _io(self._save_conflict, wid, path, await local_bytes(), local_hash)
            if data is not None:
                if not await self.write_file_live(manager, sandbox, path, data, slug):
                    raise OSError(f"Could not refresh workspace file {path!r}")
            elif local:
                root = f"/workspace/{slug}" if slug else "/workspace"
                result = await manager.execute(sandbox, _command("remove", root, path))
                if result.exit_code:
                    raise OSError(f"Could not remove workspace file {path!r}: {result.stderr}")
        if remote:
            return self._entry(path, digest, len(data), etag)
        return marker

    def _save_conflict(self, workspace_id, path, data, digest):
        directory, filename = posixpath.split(path)
        # Include the complete digest. Replaying a partial sync is idempotent.
        prefix = filename.encode()[:112].decode(errors="ignore")
        name = posixpath.join(directory, f".sandbox-conflict-{prefix}-{_sha256(filename.encode())[:12]}-{digest}")
        key = self._object_key(workspace_id, name)
        try:
            self._storage.put_version(key, data, self._guess_content_type(path), None)
        except S3Error as exc:
            if exc.code != "PreconditionFailed" or self._storage.get(key) != data:
                raise
        logger.warning("workspace_sync_conflict_saved", path=path, conflict=name)

    # ================================================================
    # Live sandbox operations (container must be running)
    # ================================================================

    @staticmethod
    def _validate_rel_path(path: str) -> str:
        """Normalize a workspace-relative path, rejecting traversal."""
        parts = [seg for seg in path.strip().lstrip("/").split("/") if seg not in ("", ".")]
        if any(seg == ".." for seg in parts):
            raise ValueError("path traversal not allowed")
        return "/".join(parts)

    @staticmethod
    async def list_files_live(
        sandbox_mgr: SandboxManager,
        sandbox: Sandbox,
        path: str = "",
        workspace_slug: str | None = None,
    ) -> list[FileEntry] | None:
        """
        List directory contents directly from the running sandbox container.

        Args:
            path: Relative path within the workspace
            workspace_slug: Directory name in sandbox (e.g., "default").
                           If None, lists from /workspace/{path} (backward compat).
                           Otherwise lists from /workspace/{workspace_slug}/{path}.

        Returns None if the directory cannot be listed (missing dir, dead container),
        so callers can fall back to MinIO.
        """
        rel = WorkspaceStorage._validate_rel_path(path)
        # Build target path with workspace directory prefix
        if workspace_slug:
            target = f"/workspace/{workspace_slug}/{rel}" if rel else f"/workspace/{workspace_slug}"
        else:
            target = f"/workspace/{rel}" if rel else "/workspace"
        script = (
            "import os,json,sys\n"
            "out=[]\n"
            "for e in os.scandir(sys.argv[1]):\n"
            "    d=e.is_dir(follow_symlinks=False)\n"
            "    out.append({'path':e.name,'is_dir':d,'size':0 if d else e.stat(follow_symlinks=False).st_size})\n"
            "print(json.dumps(out))"
        )
        cmd = f"python3 -c {shlex.quote(script)} {shlex.quote(target)}"
        result = await sandbox_mgr.execute(sandbox, cmd)
        if result.exit_code != 0:
            return None
        try:
            raw = json.loads(result.stdout.strip() or "[]")
        except json.JSONDecodeError:
            return None
        return [
            FileEntry(path=item["path"], size=item["size"], is_dir=item["is_dir"])
            for item in raw
        ]

    @staticmethod
    async def read_file_live(
        sandbox_mgr: SandboxManager,
        sandbox: Sandbox,
        path: str,
        workspace_slug: str | None = None,
    ) -> bytes | None:
        """Read a file directly from the running sandbox. Returns None on failure.

        Args:
            path: Relative file path within the workspace
            workspace_slug: Directory name in sandbox (e.g., "default").
        """
        rel = WorkspaceStorage._validate_rel_path(path)
        if not rel:
            return None
        root = f"/workspace/{workspace_slug}" if workspace_slug else "/workspace"
        output = bytearray()
        while True:
            result = await sandbox_mgr.execute(
                sandbox, _command("read", root, rel, str(len(output)), str(_READ_CHUNK_BYTES)),
            )
            if result.exit_code or getattr(result, "output_truncated", False):
                return None
            chunk = base64.b64decode(result.stdout.strip(), validate=True)
            output.extend(chunk)
            if len(chunk) < _READ_CHUNK_BYTES:
                return bytes(output)

    @staticmethod
    async def write_file_live(
        sandbox_mgr: SandboxManager, sandbox: Sandbox, path: str, content: bytes,
        workspace_slug: str | None = None,
    ) -> bool:
        """Stage outside the workspace, then atomically replace one regular file."""
        rel = WorkspaceStorage._validate_rel_path(path)
        if not rel:
            return False
        root = f"/workspace/{workspace_slug}" if workspace_slug else "/workspace"
        staged = f"/tmp/_workspace_upload_{uuid4().hex}"
        try:
            # Each command is below the Docker argument-size limit. Empty
            # files also create an empty staging object on the first pass.
            result = await sandbox_mgr.execute(sandbox, f": > {staged}")
            if result.exit_code:
                return False
            for offset in range(0, len(content), _WRITE_CHUNK_BYTES):
                encoded = base64.b64encode(content[offset:offset + _WRITE_CHUNK_BYTES]).decode()
                result = await sandbox_mgr.execute(
                    sandbox, f"printf %s {shlex.quote(encoded)} >> {staged}",
                )
                if result.exit_code:
                    return False
            result = await sandbox_mgr.execute(sandbox, _command("replace", root, rel, staged))
            return result.exit_code == 0
        finally:
            await sandbox_mgr.execute(sandbox, f"rm -f {staged}")

    @staticmethod
    async def delete_file_live(
        sandbox_mgr: SandboxManager,
        sandbox: Sandbox,
        path: str,
        workspace_slug: str | None = None,
    ) -> bool:
        """Delete a file or directory from the running sandbox.

        Args:
            path: Relative file path within the workspace
            workspace_slug: Directory name in sandbox (e.g., "default").
        """
        rel = WorkspaceStorage._validate_rel_path(path)
        if not rel:
            return False
        root = f"/workspace/{workspace_slug}" if workspace_slug else "/workspace"
        result = await sandbox_mgr.execute(sandbox, _command("delete", root, rel))
        if result.exit_code:
            raise OSError(f"Could not delete workspace path: {result.stderr}")
        return result.stdout.strip() == "removed"

    # ================================================================
    # File browsing (no container required — reads from MinIO)
    # ================================================================

    def list_files(self, workspace_id: str, path: str = "") -> list[FileEntry]:
        """
        List files and directories in a workspace path.

        Args:
            workspace_id: The workspace ID.
            path: Subdirectory path (e.g. "src"). Empty for root.

        Returns:
            List of FileEntry items at this directory level.
        """
        prefix = self._files_prefix(workspace_id)
        if path:
            prefix = f"{prefix}{path.strip('/')}/"

        # List non-recursively to get immediate children
        objects = self._storage.list(prefix, recursive=True)

        entries: list[FileEntry] = []
        seen_dirs: set[str] = set()

        for obj in objects:
            if self._is_marker(obj):
                continue
            rel = self._relative_path(workspace_id, obj.key)
            # Remove the base path prefix to get the relative name
            if path:
                base = f"{path.strip('/')}/"
                if rel.startswith(base):
                    rel = rel[len(base):]
                else:
                    continue

            if not rel:
                continue

            # Check if this is a directory marker
            if rel.endswith("/"):
                dir_name = rel.rstrip("/")
                if dir_name and dir_name not in seen_dirs:
                    seen_dirs.add(dir_name)
                    entries.append(FileEntry(path=dir_name, size=0, is_dir=True))
            elif "/" in rel:
                # Subdirectory — extract the top-level dir name
                dir_name = rel.split("/")[0]
                if dir_name not in seen_dirs:
                    seen_dirs.add(dir_name)
                    entries.append(FileEntry(path=dir_name, size=0, is_dir=True))
            else:
                entries.append(FileEntry(path=rel, size=obj.size))

        return entries

    def get_file(self, workspace_id: str, path: str) -> bytes:
        """Read a single file from MinIO."""
        key = self._object_key(workspace_id, path)
        info = self._storage.head(key)
        if self._is_marker(info):
            raise FileNotFoundError(path)
        return self._storage.get_version(key, info.etag)

    def put_file(self, workspace_id: str, path: str, content: bytes) -> None:
        """Upload a single file to MinIO."""
        key = self._object_key(workspace_id, path)
        content_type = self._guess_content_type(path)
        self._storage.put(key, content, content_type)

    def delete_file(self, workspace_id: str, path: str) -> bool:
        """Delete the observed exact path and its children using conditional markers."""
        key = self._object_key(workspace_id, path)
        removed = False
        for obj in self._storage.list(key):
            if (obj.key == key or obj.key.startswith(key + "/")) and not self._is_marker(obj):
                self._storage.put_version(obj.key, _DELETED, _DELETED_TYPE, obj.etag)
                removed = True
        return removed

    def file_exists(self, workspace_id: str, path: str) -> bool:
        """Check if a file exists in the workspace."""
        key = self._object_key(workspace_id, path)
        try:
            return not self._is_marker(self._storage.head(key))
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return False
            raise

    # ---- Presigned URLs ----

    def presign_download(self, workspace_id: str, path: str) -> str:
        """Generate a presigned download URL for a workspace file."""
        key = self._object_key(workspace_id, path)
        return self._storage.presign_download(key)

    def presign_upload(self, workspace_id: str, path: str) -> str:
        """Generate a presigned upload URL for a workspace file."""
        key = self._object_key(workspace_id, path)
        return self._storage.presign_upload(key)

    # ---- Workspace-level operations ----

    def delete_workspace(self, workspace_id: str) -> int:
        """Delete all files in a workspace from MinIO."""
        marker = json.dumps({"workspace_id": workspace_id, "deleted": True}).encode()
        self._storage.put(self._meta_key(workspace_id), marker, "application/json")
        return self._storage.delete_prefix(self._files_prefix(workspace_id))

    def get_workspace_stats(self, workspace_id: str) -> tuple[int, int]:
        """Get file count and total size for a workspace."""
        prefix = self._files_prefix(workspace_id)
        objects = [obj for obj in self._storage.list(prefix, recursive=True) if not self._is_marker(obj)]
        total_size = sum(obj.size for obj in objects)
        return len(objects), total_size

    # ---- Helpers ----

    @staticmethod
    def _guess_content_type(path: str) -> str:
        """Guess content type from file extension."""
        ext = posixpath.splitext(path)[1].lower()
        types = {
            ".py": "text/x-python",
            ".js": "application/javascript",
            ".ts": "application/typescript",
            ".json": "application/json",
            ".yaml": "text/yaml",
            ".yml": "text/yaml",
            ".md": "text/markdown",
            ".txt": "text/plain",
            ".html": "text/html",
            ".css": "text/css",
            ".xml": "application/xml",
            ".csv": "text/csv",
            ".sh": "application/x-sh",
            ".zip": "application/zip",
            ".tar": "application/x-tar",
            ".gz": "application/gzip",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".pdf": "application/pdf",
        }
        return types.get(ext, "application/octet-stream")
