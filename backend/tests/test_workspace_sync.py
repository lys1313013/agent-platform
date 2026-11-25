"""Workspace synchronization contracts with a real local tree and an in-memory object store."""

import asyncio
import hashlib
import json
import shlex
import sys
import threading
from types import SimpleNamespace

import pytest
from minio.error import S3Error

from agent_platform.storage.client import ObjectInfo
from agent_platform.storage.workspace import WorkspaceStorage


def storage_error(code):
    return S3Error(code, code, "", "", "", None)


class Objects:
    def __init__(self):
        self.rows = {}
        self.body_reads = []
        self.io_threads = set()
        self.before_write = None

    def head(self, key):
        if key not in self.rows:
            raise storage_error("NoSuchKey")
        data, mime = self.rows[key]
        return ObjectInfo(key, len(data), hashlib.sha256(data).hexdigest(), mime)

    def list(self, prefix, recursive=True):
        self.io_threads.add(threading.get_ident())
        return [self.head(key) for key in self.rows if key.startswith(prefix)]

    def put(self, key, data, mime):
        self.rows[key] = (data, mime)

    def put_version(self, key, data, mime, etag):
        if self.before_write is not None:
            hook, self.before_write = self.before_write, None
            hook(key)
        current = self.head(key).etag if key in self.rows else None
        if current != etag:
            raise storage_error("PreconditionFailed")
        self.put(key, data, mime)
        return self.head(key).etag

    def get(self, key):
        return self.rows[key][0]

    def get_version(self, key, etag):
        self.body_reads.append(key)
        if etag != self.head(key).etag:
            raise storage_error("PreconditionFailed")
        return self.get(key)

    def get_optional(self, key):
        return self.get(key) if key in self.rows else None


class Workspace:
    def __init__(self, root):
        self.root = root
        self.objects = Objects()
        self.store = WorkspaceStorage(self.objects)
        self.box = SimpleNamespace(container_id="container-one")

    async def execute(self, box, cmd):
        cmd = cmd.replace("/workspace/project", str(self.root)).replace(
            "python3 -c", f"{shlex.quote(sys.executable)} -c",
        )
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return SimpleNamespace(exit_code=proc.returncode, stdout=out.decode(), stderr=err.decode())

    def upload(self, name, data):
        self.store.put_file("w", name, data)

    def content(self, name):
        return self.store.get_file("w", name)

    async def mount(self):
        stats = await self.store.inject_files(self, self.box, "w", "project")
        assert stats.errors == []

    async def sync(self):
        return await self.store.extract_and_sync(self, self.box, "w", "project")


@pytest.mark.parametrize(("initial", "edited"), [
    (b"abc", b"xyz"), (b"abc", b"longer"), (b"abc", b"abc"), (b"", b""), (b"abc", b""),
])
async def test_sync_detects_changed_content_and_preserves_empty_files(tmp_path, initial, edited):
    work = Workspace(tmp_path)
    work.upload("notes", initial)
    await work.mount()
    (tmp_path / "notes").write_bytes(edited)
    work.objects.body_reads.clear()
    stats = await work.sync()
    assert not stats.errors
    assert stats.files_synced == int(edited != initial)
    assert stats.bytes_transferred == (len(edited) if edited != initial else 0)
    assert not work.objects.body_reads
    assert threading.get_ident() not in work.objects.io_threads
    assert work.content("notes") == edited


async def test_upload_and_overwrite_after_mount_survive_sync_and_refresh_sandbox(tmp_path):
    work = Workspace(tmp_path)
    work.upload("notes", b"old")
    await work.mount()
    work.upload("notes", b"uploaded")
    work.upload("nested/.config", b"new")
    assert not (tmp_path / "nested").exists()
    stats = await work.sync()
    assert not stats.errors and stats.files_deleted == 0
    assert (tmp_path / "notes").read_bytes() == work.content("notes") == b"uploaded"
    assert (tmp_path / "nested/.config").read_bytes() == work.content("nested/.config") == b"new"


async def test_partial_mutation_retries_preserve_both_revisions(tmp_path):
    work = Workspace(tmp_path)
    work.upload("report", b"original")
    await work.mount()
    (tmp_path / "report").write_bytes(b"sandbox")
    work.objects.before_write = lambda key: work.objects.put(key, b"direct PUT", "text/plain")
    stats = await work.sync()
    assert stats.errors and work.content("report") == b"direct PUT"
    assert (tmp_path / "report").read_bytes() == b"sandbox"
    assert not (await work.sync()).errors
    assert (tmp_path / "report").read_bytes() == b"direct PUT"
    assert not (await work.sync()).errors
    conflicts = list(tmp_path.glob(".sandbox-conflict-*"))
    assert len(conflicts) == 1
    assert conflicts[0].read_bytes() == b"sandbox"


async def test_delete_is_conditional_retry_keeps_concurrent_upload(tmp_path):
    work = Workspace(tmp_path)
    work.upload("report", b"old")
    await work.mount()
    (tmp_path / "report").unlink()
    work.objects.before_write = lambda key: work.objects.put(key, b"uploaded", "text/plain")
    assert (await work.sync()).errors
    assert work.content("report") == b"uploaded"
    assert not (await work.sync()).errors
    assert (tmp_path / "report").read_bytes() == b"uploaded"

    (tmp_path / "report").unlink()
    assert (await work.sync()).files_deleted == 1
    assert not work.store.file_exists("w", "report")
    assert work.store.list_files("w") == []
    assert work.store.get_workspace_stats("w") == (0, 0)
    (tmp_path / "report").write_bytes(b"recreated")
    assert not (await work.sync()).errors
    assert work.content("report") == b"recreated"


async def test_explicit_remote_delete_propagates_without_restoring_old_local(tmp_path):
    work = Workspace(tmp_path)
    work.upload("report", b"old")
    await work.mount()
    work.store.delete_file("w", "report")
    work.store = WorkspaceStorage(work.objects)  # fresh worker, persisted baseline
    assert not (await work.sync()).errors
    assert not (tmp_path / "report").exists()
    assert not work.store.file_exists("w", "report")


async def test_legacy_manifest_does_not_authorize_deletions_or_overwrites(tmp_path):
    work = Workspace(tmp_path)
    work.upload("report", b"remote")
    work.upload("remote-only", b"remote-only")
    (tmp_path / "report").write_bytes(b"local")
    (tmp_path / ".env").write_bytes(b"private")
    work.objects.put("workspaces/w/meta.json", json.dumps({"files": []}).encode(), "application/json")
    stats = await work.sync()
    assert not stats.errors and not stats.files_deleted
    assert work.content("remote-only") == (tmp_path / "remote-only").read_bytes()
    assert work.content("report") == b"remote"
    assert work.content(".env") == b"private"
    assert any(row[0] == b"local" for row in work.objects.rows.values())


async def test_failed_scan_does_not_mutate_remote(tmp_path):
    work = Workspace(tmp_path / "missing")
    work.upload("keep", b"original")
    assert (await work.sync()).errors
    assert work.content("keep") == b"original"
    assert work.objects.get_optional("workspaces/w/meta.json") is None


@pytest.mark.parametrize("path", ["../outside", "nested/../outside", "nested//file", "./name"])
async def test_invalid_remote_inventory_rejected_before_any_mutation(tmp_path, path):
    work = Workspace(tmp_path)
    work.objects.put("workspaces/w/files/" + path, b"bad", "text/plain")
    assert (await work.sync()).errors
    assert not list(tmp_path.iterdir())
    assert work.objects.get_optional("workspaces/w/meta.json") is None


async def test_hidden_names_and_large_file_read_write_roundtrip(tmp_path):
    work = Workspace(tmp_path)
    for name in ["env", ".env", ".git/config", "git/config", "folder/a b'c"]:
        work.upload(name, (name.encode() + b"\x00") * 17000)
    await work.mount()
    (tmp_path / ".env").write_bytes(b"new")
    assert not (await work.sync()).errors
    assert work.content(".env") == b"new"
    assert work.content("env") == (tmp_path / "env").read_bytes()
    assert {entry.path for entry in work.store.list_files("w")} == {
        "env", ".env", ".git", "git", "folder",
    }


async def test_deleted_workspace_cannot_be_recreated_by_late_sync(tmp_path):
    work = Workspace(tmp_path)
    work.upload("notes", b"old")
    await work.mount()
    work.objects.delete_prefix = lambda prefix: [
        work.objects.rows.pop(key) for key in list(work.objects.rows) if key.startswith(prefix)
    ]
    work.store.delete_workspace("w")
    (tmp_path / "late").write_bytes(b"late")
    assert not (await work.sync()).errors
    assert not work.store.list_files("w")


async def test_symlink_replacing_root_never_publishes_an_empty_workspace(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    work = Workspace(root)
    work.upload("notes", b"old")
    await work.mount()
    root.rename(tmp_path / "relocated")
    empty = tmp_path / "empty"
    empty.mkdir()
    root.symlink_to(empty, target_is_directory=True)
    assert (await work.sync()).errors
    assert work.content("notes") == b"old"


async def test_explicit_directory_delete_preserves_other_prefixes(tmp_path):
    work = Workspace(tmp_path)
    for name in ["dir/one", "dir/nested/two", "directory/keep"]:
        work.upload(name, name.encode())
    await work.mount()
    assert work.store.delete_file("w", "dir")
    assert not (await work.sync()).errors
    assert not (tmp_path / "dir/one").exists()
    assert not (tmp_path / "dir/nested/two").exists()
    assert (tmp_path / "directory/keep").read_bytes() == b"directory/keep"


@pytest.mark.parametrize("state", ["no_manager", "unmounted", "refreshed", "pending", "write_failed"])
async def test_proxy_upload_reports_persistence_and_live_refresh(monkeypatch, tmp_path, state):
    from unittest.mock import AsyncMock, Mock
    from uuid import uuid4

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from agent_platform.auth.dependencies import get_current_user
    from agent_platform.db.connection import get_db
    from agent_platform.workspaces import routes

    work = Workspace(tmp_path)
    wid = uuid4()
    user = SimpleNamespace(id=uuid4())
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: object()
    refresh = AsyncMock(return_value=work.box if state == "refreshed" else None)
    if state != "no_manager":
        app.state.sandbox_mgr = SimpleNamespace(reconcile_uploaded_workspace=refresh)
    if state == "pending":
        refresh.side_effect = RuntimeError("sandbox offline")
    if state == "write_failed":
        monkeypatch.setattr(work.store, "put_file", Mock(side_effect=OSError("storage offline")))
    monkeypatch.setattr(routes.WorkspaceService, "get_workspace", AsyncMock(return_value=SimpleNamespace(slug="project")))
    monkeypatch.setattr(routes, "_get_workspace_storage", lambda: work.store)
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.post(
            f"/api/workspaces/{wid}/files/content", params={"path": "report"}, files={"file": ("report", b"new")},
        )
    if state == "write_failed":
        assert response.status_code == 500
        assert not work.objects.rows
        refresh.assert_not_awaited()
        return
    assert response.status_code == 201
    expected = {"path": "report", "size": 3}
    if state == "pending":
        expected["sandbox_sync_status"] = "pending"
    assert response.json() == expected
    assert work.store.get_file(str(wid), "report") == b"new"
    if state != "no_manager":
        refresh.assert_awaited_once_with(str(user.id), str(wid), "project")


async def test_large_binary_transfers_use_separate_read_and_write_budgets(tmp_path):
    from unittest.mock import AsyncMock

    work = Workspace(tmp_path)
    manager = SimpleNamespace(execute=AsyncMock(wraps=work.execute))
    data = bytes(range(256)) * (3 * 4096 + 1)
    assert await work.store.write_file_live(manager, work.box, "binary", data, "project")
    writes = [call.args[1] for call in manager.execute.await_args_list if call.args[1].startswith("printf")]
    assert len(writes) == 43
    assert max(len(cmd.encode()) for cmd in writes) < 100 * 1024
    manager.execute.reset_mock()
    assert await work.store.read_file_live(manager, work.box, "binary", "project") == data
    assert manager.execute.await_count == 2
