"""Local dbt validation without network/credentials, in an already installed Docker image.

It never pulls images and never falls back to running on the host if Docker fails.
"""
from __future__ import annotations

from intentflow.i18n import tr

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import yaml

from . import file_transaction

ISOLATION = "docker-offline-v1"
DEFAULT_IMAGE = "intentflow-dbt-sandbox:local"
MAX_INPUT = 128 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
TIMEOUT = 600
_SLOTS = threading.BoundedSemaphore(int(os.environ.get("INTENTFLOW_DBT_MAX_VALIDATIONS", "2")))   # host-wide cap
_SUFFIXES = {".sql", ".yml", ".yaml", ".csv", ".md", ".py"}
_SKIP = {"target", "logs", "node_modules", "__pycache__", "profiles.yml"}
_PATHS = {"model-paths": ["models"], "macro-paths": ["macros"], "seed-paths": ["seeds"],
          "snapshot-paths": ["snapshots"], "test-paths": ["tests"], "analysis-paths": ["analyses"]}

# Run with python -I; never imports project modules in the controlling process.
_ENTRY = r'''
import os, pathlib, shutil, subprocess, sys, time
shutil.copytree('/input', '/work/project')
pathlib.Path('/work/home').mkdir()
pathlib.Path('/work/profiles').mkdir()
pathlib.Path('/work/profiles/profiles.yml').write_text("intentflow_sandbox:\n  target: isolated\n  outputs:\n    isolated:\n      type: duckdb\n      path: /work/validation.duckdb\n      schema: main\n      threads: 2\n")
os.chdir('/work/project')
env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/work/home', 'TMPDIR': '/tmp',
       'DBT_SEND_ANONYMOUS_USAGE_STATS': 'false', 'DO_NOT_TRACK': '1',
       'DBT_LOG_PATH': '/work/logs', 'DBT_TARGET_PATH': '/work/target',
       'DBT_PARTIAL_PARSE': 'false', 'PYTHONDONTWRITEBYTECODE': '1'}
base = [sys.executable, '-I', '-c', 'from dbt.cli.main import cli; cli()']
flags = ['--profiles-dir', '/work/profiles', '--profile', 'intentflow_sandbox', '--target', 'isolated']
mode = sys.argv[1]
commands = [['parse']] if mode == 'parse' else [['seed'], ['build', '--indirect-selection', 'buildable', '--select', *sys.argv[2:]]]
deadline = time.monotonic() + 570
for command in commands:
    result = subprocess.run(base + command + flags, env=env, timeout=max(1, deadline - time.monotonic()))
    if result.returncode:
        sys.exit(result.returncode)
'''


def _read_file(path: Path, maximum: int = MAX_FILE) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError(tr("input is not regular, is a hardlink or is too large: ") + path.name)
        data = f.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError(tr("input is not regular, is a hardlink or is too large: ") + path.name)
        return data


def _stage_unlocked(root: Path, destination: Path, changes: list[dict]):
    config_data = _read_file(root / "dbt_project.yml", 1024 * 1024)
    config = yaml.safe_load(config_data)
    if not isinstance(config, dict):
        raise ValueError(tr("invalid dbt_project.yml"))
    relative_paths = []
    for key, default in _PATHS.items():
        paths = config.get(key, default)
        if not isinstance(paths, list):
            raise ValueError(tr("dbt paths must be literal lists"))
        relative_paths.extend(paths)
    relative_paths.append(config.get("packages-install-path", "dbt_packages"))
    total, count, included = 0, 0, set()
    destination.mkdir(mode=0o755)
    destination.chmod(0o755)  # the outer temporary directory stays 700 on the host

    def copy(path):
        nonlocal total, count
        rel = path.relative_to(root)
        if path.resolve() != path or path.is_symlink():
            raise ValueError(tr("symlink not allowed in the validation input"))
        if rel in included:
            return
        data = _read_file(path)
        total += len(data)
        count += 1
        if total > MAX_INPUT or count > 20000:
            raise ValueError(tr("project exceeds the validation input limit (128 MiB/20000 files)"))
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        for parent in (target.parent, *target.parent.parents):
            if not parent.is_relative_to(destination):
                break
            parent.chmod(0o755)
        target.write_bytes(data)
        target.chmod(0o444)
        included.add(rel)

    for name in ("dbt_project.yml", "packages.yml", "dependencies.yml", "package-lock.yml"):
        if (root / name).exists():
            copy(root / name)
    for value in relative_paths:
        if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
            raise ValueError(tr("dbt path must be inside the project"))
        directory = root / value
        if directory.resolve() != directory or directory.is_symlink():
            raise ValueError(tr("symlink not allowed in the validation input"))
        if not directory.exists():
            continue
        for current, dirs, names in os.walk(directory, followlinks=False):
            base = Path(current)
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _SKIP]
            if any((base / d).is_symlink() for d in dirs):
                raise ValueError(tr("symlink not allowed in the validation input"))
            for name in names:
                if not name.startswith(".") and name not in _SKIP and Path(name).suffix in _SUFFIXES:
                    copy(base / name)
    selectors = []
    for change in changes:
        relative = Path(change["file"]).relative_to(root)
        if relative not in included or relative.suffix != ".sql":
            raise ValueError(tr("validation only accepts SQL models in the paths declared by dbt"))
        target = destination / relative
        original = target.read_text()
        if hashlib.sha256(original.encode()).hexdigest()[:16] != change["before_hash"]:
            raise ValueError(tr("file changed during the copy; create a new proposal"))
        data = change["after"].encode("utf-8")
        total += len(data) - target.stat().st_size
        if total > MAX_INPUT:
            raise ValueError(tr("edited project exceeds the 128 MiB input limit"))
        if len(data) > MAX_FILE:
            raise ValueError(tr("edit exceeds the size limit"))
        target = destination / relative
        target.chmod(0o600)
        target.write_bytes(data)
        target.chmod(0o444)
        # dbt selectors do not accept names that are ambiguous with operators/separators.
        if not re.fullmatch(r"[\w./-]+", relative.as_posix()):
            raise ValueError(tr("model name not supported for isolated selection"))
        selectors.append("+path:" + relative.as_posix() + "+")
    return selectors


def stage(root: Path, destination: Path, changes: list[dict], *, lock_held: bool = False):
    """Copy a stable view of the project; refuses snapshots during a partial transaction."""
    root = Path(root).resolve()
    if lock_held:
        return _stage_unlocked(root, destination, changes)
    proposal = {"project_root": str(root), "changes": changes}
    with file_transaction.read_locked(proposal):
        return _stage_unlocked(root, destination, changes)


def _run_bounded(command, env, timeout):
    tail = deque(maxlen=16)                       # <=64 KiB, even with endless output
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

    def drain():
        while chunk := process.stdout.read(4096):
            tail.append(chunk)
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
        raise
    finally:
        reader.join(timeout=10)
        process.stdout.close()
    return code, b"".join(tail).decode("utf-8", errors="replace")[-4000:]


def run(root: Path, changes: list[dict], mode: str, *, source_lock_held: bool = False) -> dict:
    if mode not in ("parse", "build"):
        raise ValueError(tr("invalid validation mode"))
    if not _SLOTS.acquire(blocking=False):
        raise file_transaction.Busy(tr("too many validations running; try again in a moment"))
    try:
        return _run(root, changes, mode, source_lock_held)
    finally:
        _SLOTS.release()


def _run(root: Path, changes: list[dict], mode: str, source_lock_held: bool) -> dict:
    result = {"ok": False, "mode": mode, "isolation": ISOLATION, "adapter": "duckdb", "scope": "local-demo"}
    if mode == "build":
        result["test_selection"] = "buildable"
    docker = shutil.which("docker")
    if not docker:
        return {**result, "output": tr("Docker unavailable; install the local image described in docs/VALIDATION_RECOVERY.md; validation runs only on a host or runner that has Docker, not in the slim container")}
    socket = os.environ.get("INTENTFLOW_DBT_DOCKER_SOCKET", "/var/run/docker.sock")
    try:
        local_socket = socket.startswith("/") and stat.S_ISSOCK(os.stat(socket).st_mode)
    except OSError:
        local_socket = False
    if not local_socket:
        raise ValueError(tr("Docker requires a local Unix socket; a remote daemon is not allowed"))
    image = os.environ.get("INTENTFLOW_DBT_IMAGE", DEFAULT_IMAGE)
    with tempfile.TemporaryDirectory(prefix="intentflow-validation-") as temporary:
        temp = Path(temporary)
        env = {"PATH": os.defpath, "HOME": str(temp), "DOCKER_CONFIG": str(temp / "docker-config")}
        cli = [docker, "--host", "unix://" + socket]
        try:
            inspected = subprocess.run(cli + ["image", "inspect", "--format", "{{.Id}}", image],
                                       env=env, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return {**result, "output": tr("could not reach the local Docker; nothing was run on the host")}
        image_id = inspected.stdout.strip()
        if inspected.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            return {**result, "output": tr("local dbt image missing; build intentflow-dbt-sandbox:local as described in docs/VALIDATION_RECOVERY.md")}
        selectors = stage(root, temp / "input", changes, lock_held=source_lock_held)
        name = "intentflow-validation-" + uuid.uuid4().hex
        command = cli + ["run", "--rm", "--label", "io.intentflow.validation=true", "--name", name, "--pull=never", "--network=none", "--read-only",
                         "--user=10001:10001", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
                         "--pids-limit=128", "--cpus=2", "--memory=1g", "--memory-swap=1g", "--log-driver=none",
                         "--ulimit", "nofile=1024:1024", "--ulimit", "core=0:0",
                         "--tmpfs", "/work:rw,nosuid,nodev,noexec,size=1g,uid=10001,gid=10001,mode=0700",
                         "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=128m,mode=1777",
                         "--mount", f"type=bind,src={temp / 'input'},dst=/input,readonly",
                         "--entrypoint", "python", image_id, "-I", "-c", _ENTRY, mode, *selectors]
        started = time.monotonic()
        try:
            code, output = _run_bounded(command, env, TIMEOUT)
            return {**result, "ok": code == 0, "output": output, "image_id": image_id,
                    "elapsed_seconds": round(time.monotonic() - started, 3)}
        except subprocess.TimeoutExpired:
            return {**result, "output": tr("validation exceeded 600 seconds; container stopped"), "image_id": image_id}
        except OSError as error:
            return {**result, "output": tr("could not start the isolated validation: ") + str(error)}
        finally:
            # Also stops the container if the Docker client dies or times out.
            try:
                subprocess.run(cli + ["rm", "-f", name], env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                pass
