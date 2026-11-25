"""File operations executed inside the sandbox by WorkspaceStorage."""

import base64
import hashlib
import json
import os
import shutil
import stat
import sys
from contextlib import contextmanager
from uuid import uuid4


@contextmanager
def workspace_parent(root, path, *, create=False):
    """Walk relative to directory descriptors; never traverse user-created symlinks."""
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid workspace path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(root, flags)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, dir_fd=parent)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        yield parent, parts[-1]
    finally:
        os.close(parent)


def scan(root):
    files = {}

    def fail(error):
        raise error

    # fwalk would silently yield an empty inventory for a symlink root.
    # An unreadable or replaced mount must never look like a mass deletion.
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for directory, _, names, dir_fd in os.fwalk(".", dir_fd=root_fd, onerror=fail, follow_symlinks=False):
            for name in names:
                if not stat.S_ISREG(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                    continue
                path = os.path.normpath(os.path.join(directory, name))
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(fd)
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError("workspace changed during scan")
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    after = os.fstat(fd)
                    if (before.st_size, before.st_ctime_ns) != (after.st_size, after.st_ctime_ns):
                        raise ValueError("workspace changed during scan")
                    files[path] = [digest, before.st_size]
        if not os.path.samestat(os.fstat(root_fd), os.stat(root, follow_symlinks=False)):
            raise OSError("workspace directory changed during scan")
    finally:
        os.close(root_fd)
    print(json.dumps(files, separators=(",", ":")))


def run():
    action, root, *args = sys.argv[1:]
    if action == "scan":
        scan(root)
        return
    path, *options = args
    with workspace_parent(root, path, create=action == "replace") as (parent, name):
        if action == "read":
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("not a regular file")
                stream.seek(int(options[0]))
                print(base64.b64encode(stream.read(int(options[1]))).decode())
        elif action == "delete":
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(name, dir_fd=parent)
            else:
                os.unlink(name, dir_fd=parent)
            print("removed")
        elif action == "remove":
            os.unlink(name, dir_fd=parent)
        elif action == "replace":
            # Stage on the target filesystem so rename is atomic.
            temporary = ".aio-stage-" + uuid4().hex
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as out, open(options[0], "rb") as source:
                    while chunk := source.read(65536):
                        out.write(base64.b64decode(chunk, validate=True))
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        else:
            raise ValueError("unsupported workspace operation")


if __name__ == "__main__":
    try:
        run()
    except FileNotFoundError:
        if len(sys.argv) < 2 or sys.argv[1] != "delete":
            raise
