"""Durable journal and per-project locks for local apply, snapshots and recovery (Linux).

Renames are atomic per file; external readers that do not honor the lock may observe a partial batch.
The journal is durable before any change and never restores unknown content.
"""
from __future__ import annotations

from intentflow.i18n import tr

import base64
import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

class Busy(ValueError):
    """Transient: the project lock is held or the host is at its validation cap. A queued request is retried, not dropped."""


DIRECTORY = ".intentflow-transactions"
MAX_BYTES = 64 * 1024 * 1024


def project_root(proposal: dict) -> Path:
    paths = [Path(c["file"]).absolute() for c in proposal.get("changes", [])]
    if not paths or len(paths) > 256 or len(set(paths)) != len(paths):
        raise ValueError(tr("a proposal needs 1 to 256 distinct files"))
    if any(p != p.resolve() for p in paths):
        raise ValueError(tr("path with a symlink or ambiguous; create a new proposal"))
    common = Path(os.path.commonpath([str(p.parent) for p in paths]))
    root = Path(proposal.get("project_root") or common).resolve()
    if not proposal.get("project_root"):
        for parent in (common, *common.parents):
            if (parent / "dbt_project.yml").is_file():
                root = parent
                break
    if root == Path(root.anchor) or not all(p.is_relative_to(root) for p in paths):
        raise ValueError(tr("files outside the project root"))
    return root


def _store_id(store) -> str:
    value = str(store.path)
    if store.backend == "sqlite" and value != ":memory:":
        value = str(Path(value).resolve())
    return hashlib.sha256((store.backend + ":" + value).encode()).hexdigest()


def _pack(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unpack(data: str) -> bytes:
    return base64.b64decode(data, validate=True)


class Journal:
    def __init__(self, root: Path, root_fd: int, journal_fd: int):
        self.root, self.root_fd, self.fd = root, root_fd, journal_fd

    def relative(self, path: str) -> str:
        p = Path(path)
        rel = p.relative_to(self.root) if p.is_absolute() else p
        if not rel.parts or any(x in ("..", ".") for x in rel.parts) or rel.parts[0] == DIRECTORY:
            raise ValueError(tr("invalid path in the journal"))
        return str(rel)

    @contextmanager
    def parent(self, relative: str):
        parts = Path(self.relative(relative)).parts
        fd = os.dup(self.root_fd)
        try:
            for part in parts[:-1]:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def read(self, relative: str) -> tuple[bytes, int]:
        with self.parent(relative) as (fd, name):
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(handle, "rb") as f:
                info = os.fstat(f.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
                    raise ValueError(tr("not a regular file, a hardlink or too large"))
                data = f.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError(tr("file exceeds the limit"))
                return data, stat.S_IMODE(info.st_mode)

    @staticmethod
    def _replace(fd: int, name: str, data: bytes, mode: int):
        temp = ".intentflow-" + uuid.uuid4().hex + ".tmp"
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(handle, "wb") as f:
                f.write(data)
                os.fchmod(f.fileno(), mode & 0o777)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temp, dir_fd=fd)
            except FileNotFoundError:
                pass

    def write(self, relative: str, data: bytes, mode: int, expected: bytes):
        # The lock coordinates IntentFlow instances; an external editor still has to respect the project.
        if self.read(relative)[0] != expected:
            raise ValueError(tr("file changed during the operation; keep the edit and recover"))
        with self.parent(relative) as (fd, name):
            self._replace(fd, name, data, mode)

    def load(self):
        try:
            handle = os.open("active.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        with os.fdopen(handle, "r") as f:
            return json.load(f)

    def save(self, record):
        self._replace(self.fd, "active.json", json.dumps(record).encode(), 0o600)

    def clear(self):
        os.unlink("active.json", dir_fd=self.fd)
        os.fsync(self.fd)

    def require_idle(self):
        if self.load() is not None:
            raise ValueError(tr("there is an interrupted operation in this project; run fix recover for the matching proposal"))


@contextmanager
def locked_root(root: str | Path, *, shared: bool = False):
    """Lock a project directory before a proposal exists or when reading its source files."""
    root = Path(root).resolve()
    if root == Path(root.anchor) or not root.is_dir():
        raise ValueError(tr("invalid project root"))
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    directory_fd = lock_fd = None
    try:
        try:
            os.mkdir(DIRECTORY, 0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
        except PermissionError:
            raise ValueError(tr("the project directory is not writable by this process (uid {uid}); fixes need write access to create {dir}",
                                uid=os.getuid(), dir=DIRECTORY)) from None
        directory_fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError(tr("the recovery directory must belong to the user and have mode 700"))
        lock_fd = os.open("lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        try:
            operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(lock_fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy(tr("another operation is in progress in this project; try again")) from None
        journal = Journal(root, root_fd, directory_fd)
        if shared and journal.load() is not None:
            raise ValueError(tr("there is an interrupted operation in this project; run fix recover before reading the input"))
        yield journal
    except OSError as e:
        raise ValueError(tr("could not access the files safely: {error}", error=e.strerror)) from e
    finally:
        for fd in (lock_fd, directory_fd, root_fd):
            if fd is not None:
                os.close(fd)


@contextmanager
def locked(proposal, *, shared: bool = False):
    with locked_root(project_root(proposal), shared=shared) as journal:
        yield journal


@contextmanager
def read_locked(proposal):
    """Consistent snapshot for IntentFlow readers that cooperate with the project lock."""
    with locked(proposal, shared=True) as journal:
        yield journal


def recover_locked(journal: Journal, store, pid: str, actor: str = "system"):
    record = journal.load()
    if record is None:
        return store.proposal(pid)
    if record.get("store") != _store_id(store) or record.get("pid") != pid or record.get("version") != 1:
        raise ValueError(tr("the journal belongs to another store/proposal; use the original store"))
    current = store.proposal(pid)
    marker = (current or {}).get("file_transaction") or {}
    committed = marker.get("id") == record["id"] and marker.get("phase") == "committed"
    if not committed and not (marker.get("id") == record["id"] and marker.get("phase") == "prepared") and current != record["before_proposal"]:
        raise ValueError(tr("the proposal changed since the interruption; manual recovery needed"))
    # Preflight all of them before restoring any file.
    snapshots = []
    for entry in record["entries"]:
        now, _ = journal.read(entry["file"])
        before, after = _unpack(entry["before"]), _unpack(entry["after"])
        allowed = (after,) if committed else (before, after)
        if now not in allowed:
            raise ValueError(tr("recovery refused to preserve a later edit: ") + entry["file"])
        snapshots.append((entry, now, before))
    if not committed:
        for entry, now, before in snapshots:
            if now != before:
                journal.write(entry["file"], before, entry["mode"], now)
        previous = record["before_proposal"]
        store.put_proposal(pid, previous, previous["status"])
    store.audit(actor, "file_transaction_recovered", {"id": pid, "transaction": record["id"], "committed": committed})
    journal.clear()
    return store.proposal(pid)


def execute(journal: Journal, store, before: dict, after: dict, contents: dict[str, bytes], status: str, actor: str, expected: dict[str, bytes]):
    journal.require_idle()
    entries, total = [], 0
    for path, content in contents.items():
        relative = journal.relative(path)
        original, mode = journal.read(relative)
        if original != expected[path]:
            raise ValueError(tr("file changed before preparing the operation; keep the edit and create a new proposal"))
        total += len(original) + len(content)
        if total > MAX_BYTES:
            raise ValueError(tr("batch exceeds 64 MiB; split the proposal"))
        entries.append({"file": relative, "before": _pack(original), "after": _pack(content), "mode": mode})
    tid = uuid.uuid4().hex
    record = {"version": 1, "id": tid, "pid": before["id"], "store": _store_id(store),
              "before_proposal": before, "entries": entries}
    marker = {"id": tid, "phase": "prepared"}
    prepared = {**before, "project_root": str(journal.root), "file_transaction": marker}
    after = {**after, "project_root": str(journal.root), "file_transaction": {**marker, "phase": "committed"}}
    journal.save(record)                         # fsync before DB and files
    try:
        store.put_proposal(before["id"], prepared, "applying" if status == "applied" else "rolling_back")
        for entry in entries:
            journal.write(entry["file"], _unpack(entry["after"]), entry["mode"], _unpack(entry["before"]))
        store.put_proposal(before["id"], after, status)  # recoverable commit decision
        store.audit(actor, status, {"id": before["id"], "transaction": tid, "files": list(contents)})
        journal.clear()
        return store.proposal(before["id"])
    except Exception as error:
        try:
            store.con.rollback()
            result = recover_locked(journal, store, before["id"])
        except Exception as recovery_error:
            raise ValueError(tr("operation interrupted; run fix recover ") + before["id"] + ": " + str(recovery_error)) from error
        if (result.get("file_transaction") or {}).get("id") == tid:
            return result                      # the commit already happened; only the cleanup had failed
        raise ValueError(tr("operation failed; previous files were restored: ") + str(error)) from error
