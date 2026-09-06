#!/usr/bin/env python3
"""Provision and supervise Lyra as a local, app-like web application.

The launcher is deliberately standard-library-only: it must be able to repair the
project environment before any project dependency is importable.  Backend and frontend
servers are detached after they pass readiness checks.  Their process start identities
are recorded so a later ``stop`` never signals a reused PID or an unrelated port owner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets as _secrets_mod
import shlex
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager, suppress
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# The same stdlib archive format is frozen into the desktop backend.
sys.path.insert(0, str(ROOT))
from backend import desktop_backup_archive as _backup_archive  # noqa: E402

BACKUP_DATA_PREFIX = _backup_archive.BACKUP_DATA_PREFIX
BACKUP_EXTERNAL_DB = _backup_archive.BACKUP_EXTERNAL_DB
BACKUP_MANIFEST = _backup_archive.BACKUP_MANIFEST
BACKUP_MAX_MEMBERS = _backup_archive.BACKUP_MAX_MEMBERS
BACKUP_MAX_MEMBER_BYTES = _backup_archive.BACKUP_MAX_MEMBER_BYTES
BACKUP_MAX_TOTAL_BYTES = _backup_archive.BACKUP_MAX_TOTAL_BYTES
BACKUP_VERSION = _backup_archive.BACKUP_VERSION
LauncherError = _backup_archive.LauncherError
copy_tree_without_symlinks = _backup_archive.copy_tree_without_symlinks
extract_archive_file = _backup_archive.extract_archive_file
extract_archive_prefix = _backup_archive.extract_archive_prefix
path_is_within = _backup_archive.path_is_within
private_restore_mkdir = _backup_archive.private_restore_mkdir
read_backup_manifest = _backup_archive.read_backup_manifest
safe_archive_member = _backup_archive.safe_archive_member
snapshot_sqlite_database = _backup_archive.snapshot_sqlite_database
sqlite_sidecars = _backup_archive.sqlite_sidecars
stage_backup_tree = _backup_archive.stage_backup_tree
staged_backup_manifest = _backup_archive.staged_backup_manifest


def validate_backup_members(bundle, manifest):
    return _backup_archive.validate_backup_members(
        bundle,
        manifest,
        max_members=BACKUP_MAX_MEMBERS,
        max_member_bytes=BACKUP_MAX_MEMBER_BYTES,
        max_total_bytes=BACKUP_MAX_TOTAL_BYTES,
    )


FRONTEND = ROOT / "frontend"
RUNTIME_DIR = ROOT / ".lyra"
RUNTIME_FILE = RUNTIME_DIR / "runtime.json"
INSTALL_FILE = RUNTIME_DIR / "install.json"
LOCK_FILE = RUNTIME_DIR / "launcher.lock"
LOG_DIR = ROOT / "logs"
BACKEND_LOG = LOG_DIR / "backend.log"
FRONTEND_LOG = LOG_DIR / "frontend.log"
SUPERVISOR_LOG = LOG_DIR / "supervisor.log"

BACKEND_PORT = 8000
FRONTEND_PORT = 3000
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}/api/health/ready"
FRONTEND_URL = f"http://127.0.0.1:{FRONTEND_PORT}"
APP_URL = FRONTEND_URL
STATE_VERSION = 1
SUPERVISOR_POLL_SECONDS = 0.5
CORE_COMPONENTS = (
    ("frontend", FRONTEND_PORT),
    ("backend", BACKEND_PORT),
)


class BundledService:
    """A local service whose lifecycle is coupled to the Lyra application."""

    __slots__ = ("name", "helper")

    def __init__(self, name: str, helper: Path) -> None:
        self.name = name
        self.helper = helper


class RuntimeStateError(LauncherError):
    """The persisted launcher runtime state cannot be trusted, so the launcher refuses to guess.

    Raised only for a state the launcher cannot interpret with confidence (unreadable or
    truncated JSON, a non-object document, a wrongly typed field, or an unsupported state
    version). It is a subclass of ``LauncherError`` so existing lifecycle error handling still
    reports it, but ``status`` and ``doctor`` catch it specifically to degrade into a read-only,
    signal-free port report instead of aborting.
    """


def say(message: str = "") -> None:
    print(message, flush=True)


def step(message: str) -> None:
    say(f"==> {message}")


def ok(message: str) -> None:
    say(f"  ✓ {message}")


def warn(message: str) -> None:
    say(f"  ! {message}")


def sha256_files(paths: Iterable[Path]) -> str:
    """Hash paths and contents in a stable order, including missing-file markers."""

    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda item: str(item)):
        try:
            label = str(path.relative_to(ROOT))
        except ValueError:
            label = str(path)
        digest.update(label.encode())
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default.copy()
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LauncherError(
            f"{path.relative_to(ROOT)} is unreadable ({exc}). Move it aside and retry."
        ) from exc
    if not isinstance(value, dict):
        raise LauncherError(f"{path.relative_to(ROOT)} must contain a JSON object.")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def rooted_path(path: Path) -> Path:
    """Resolve launcher-managed relative paths from the repository root."""

    return path if path.is_absolute() else ROOT / path


def configured_data_paths(environment: Mapping[str, str] | None = None) -> tuple[Path, Path]:
    """Return the effective Lyra data directory and database path."""

    env = os.environ if environment is None else environment
    data_dir = rooted_path(Path(env.get("LYRA_DATA_DIR", "data"))).expanduser()
    db_override = env.get("LYRA_DB_PATH")
    db_path = rooted_path(Path(db_override)).expanduser() if db_override else data_dir / "lyra.db"
    return data_dir, db_path


def resolved_existing_directory(path: Path, *, label: str) -> Path:
    """Return one existing directory, rejecting symlink roots."""

    candidate = path.expanduser()
    if candidate.is_symlink():
        raise LauncherError(f"{label} may not be a symlink: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LauncherError(f"{label} is not available: {candidate}") from exc
    if not resolved.is_dir():
        raise LauncherError(f"{label} is not a directory: {resolved}")
    return resolved


def resolved_existing_file(path: Path, *, label: str) -> Path:
    """Return one existing regular file, rejecting symlink roots."""

    candidate = path.expanduser()
    if candidate.is_symlink():
        raise LauncherError(f"{label} may not be a symlink: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LauncherError(f"{label} is not available: {candidate}") from exc
    if not resolved.is_file():
        raise LauncherError(f"{label} is not a file: {resolved}")
    return resolved


def ensure_existing_parent(path: Path, *, label: str) -> Path:
    """Require that a target path's parent already exists and is a real directory."""

    parent = path.expanduser().parent
    if parent == Path():
        parent = Path.cwd()
    return resolved_existing_directory(parent, label=label)


def empty_runtime() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "mode": None,
        "desired_state": "stopped",
        "processes": {},
        "bundled_services": [],
    }


# The launcher persists one on-disk ownership contract, STATE_VERSION. It has only ever been
# version 1, and every field the launcher writes has been stable since the contract was
# introduced. See docs/local-deployment.md ("Runtime state versioning and recovery") for the
# field inventory and the compatibility boundaries that must stay manual.
SUPPORTED_STATE_VERSIONS: frozenset[int] = frozenset({STATE_VERSION})


def _runtime_state_hint() -> str:
    """Shared, signal-free remediation for a runtime-state file the launcher cannot trust."""

    return (
        "No process is signaled while the launcher cannot trust this file. Run './run status' "
        f"to see what is listening on ports {BACKEND_PORT} and {FRONTEND_PORT}, stop any "
        "still-running Lyra with the launcher that started it, then move "
        f"{helper_label(RUNTIME_FILE)} aside and run './run' again. Your data directory and "
        "database are never touched by this recovery."
    )


def load_runtime() -> dict[str, Any]:
    """Return the launcher's persisted ownership state, or fail safely and specifically.

    Automatic recovery is limited to cases that cannot make the launcher act on a process it
    does not provably own: a missing file, and a supported-version document whose *optional*
    fields are absent, both become an empty, stopped state. Everything the launcher cannot
    interpret with confidence - unreadable or truncated JSON, a non-object document, a wrongly
    typed field, or a state version this checkout does not support - is refused with specific
    remediation instead of guessed at. A wrong guess here could strand an owned service, signal
    a reused PID, or silently discard ownership of a live process.
    """

    if not RUNTIME_FILE.exists():
        return empty_runtime()
    try:
        raw = RUNTIME_FILE.read_text()
    except OSError as exc:
        raise RuntimeStateError(
            f"{helper_label(RUNTIME_FILE)} could not be read ({exc}). {_runtime_state_hint()}"
        ) from exc
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeStateError(
            f"{helper_label(RUNTIME_FILE)} is empty or not valid JSON ({exc}). "
            f"{_runtime_state_hint()}"
        ) from exc
    if not isinstance(state, dict):
        raise RuntimeStateError(
            f"{helper_label(RUNTIME_FILE)} must contain a JSON object. {_runtime_state_hint()}"
        )

    version = state.get("version")
    # ``bool`` is an ``int`` subclass and ``True == 1``; exclude it so a corrupted
    # ``"version": true`` can never masquerade as the supported version 1.
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in SUPPORTED_STATE_VERSIONS
    ):
        raise _unsupported_state_version_error(version)
    return _normalize_supported_runtime(state)


def _unsupported_state_version_error(version: object) -> RuntimeStateError:
    """Explain a version skew, distinguishing a newer writer from an unrecognized one."""

    label = helper_label(RUNTIME_FILE)
    supported = ", ".join(str(value) for value in sorted(SUPPORTED_STATE_VERSIONS))
    if isinstance(version, int) and not isinstance(version, bool) and version > STATE_VERSION:
        return RuntimeStateError(
            f"{label} was written by a newer Lyra (state version {version}; this checkout "
            f"supports {supported}). Do not downgrade to manage it: stop that app with the "
            f"newer Lyra that started it. {_runtime_state_hint()}"
        )
    return RuntimeStateError(
        f"{label} uses an unrecognized state version ({version!r}; this checkout supports "
        f"{supported}). {_runtime_state_hint()}"
    )


def _normalize_supported_runtime(state: dict[str, Any]) -> dict[str, Any]:
    """Fill absent optional fields on a supported state; refuse a present-but-invalid one.

    Individual process records are intentionally *not* rejected here. A record that no longer
    proves ownership - a missing or malformed birth token, or a reused PID - is treated as
    unowned by ``record_matches_process``, so it can never cause a signal. It can only produce
    an honest "stopped; stale record" report or a port-aware, ownership-checked recovery.
    """

    label = helper_label(RUNTIME_FILE)
    processes = state.get("processes", {})
    if not isinstance(processes, dict):
        raise RuntimeStateError(
            f"{label} has a 'processes' entry that is not an object. {_runtime_state_hint()}"
        )
    state["processes"] = processes
    state.setdefault("mode", None)
    state.setdefault("desired_state", "stopped")
    bundled_services = state.setdefault("bundled_services", [])
    if not isinstance(bundled_services, list) or not all(
        isinstance(name, str) for name in bundled_services
    ):
        raise RuntimeStateError(
            f"{label} has an invalid 'bundled_services' list. {_runtime_state_hint()}"
        )
    return state


def backup_archive_target(path: Path) -> Path:
    """Return a concrete archive path, refusing destructive destinations."""

    candidate = rooted_path(path).expanduser()
    parent = ensure_existing_parent(candidate, label="backup target parent")
    # Resolve only the already-validated parent. Resolving the final component would follow
    # a dangling symlink and turn `backup --archive link.tgz` into creation at its outside
    # target before O_EXCL ever got a chance to refuse the link itself.
    target = parent / candidate.name
    if target.exists() or target.is_symlink():
        raise LauncherError(f"backup target already exists: {target}")
    return target


def restore_target_directory(path: Path) -> Path:
    """Require an explicit non-existent directory path for restore output."""

    candidate = rooted_path(path).expanduser()
    if candidate.exists():
        raise LauncherError(f"restore target must not already exist: {candidate}")
    parent = ensure_existing_parent(candidate, label="restore target parent")
    return parent / candidate.name


def restore_target_file(path: Path) -> Path:
    """Require an explicit non-existent file path for an external database restore."""

    candidate = rooted_path(path).expanduser()
    parent = ensure_existing_parent(candidate, label="restore database parent")
    assert_safe_external_database_parent(parent)
    target = parent / candidate.name
    if target.exists() or target.is_symlink():
        raise LauncherError(f"restore database target already exists: {target}")
    return target


def assert_safe_external_database_parent(path: Path) -> None:
    """Match the backend security boundary before staging an external restored database.

    The launcher intentionally stays standard-library-only, so this is the local equivalent
    of `storage.private.assert_safe_external_writer_parent`. SQLite later reopens predictable
    WAL/SHM names itself; accepting a directory another OS user can modify would let that
    user replace a prepared pathname between Lyra securing it and SQLite opening it.
    """
    if os.name != "posix":
        return

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LauncherError(f"restore database parent must be a real directory: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise LauncherError(f"restore database parent must be a real directory: {path}")
        if info.st_uid != os.geteuid():
            raise LauncherError(
                f"restore database directory must be owned by the current user: {path}"
            )
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise LauncherError(
                f"restore database directory is writable by other users: {path}. Choose a "
                "current-user-owned directory without group or world write permission, or "
                "fix its permissions before restoring."
            )
    finally:
        os.close(descriptor)


class LauncherLock(AbstractContextManager["LauncherLock"]):
    """Prevent two lifecycle commands from racing over one ownership file."""

    def __init__(self) -> None:
        self._handle: Any = None

    def __enter__(self) -> LauncherLock:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        self._handle = LOCK_FILE.open("a+")
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            # Lyra currently targets macOS/Linux. The ownership checks still protect a
            # Windows run; only simultaneous-launch serialization is unavailable there.
            return self
        except BlockingIOError as exc:
            self._handle.close()
            raise LauncherError(
                "another Lyra setup or lifecycle command is already running"
            ) from exc
        return self

    def __exit__(self, *args: object) -> None:
        if self._handle is None:
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except ImportError:
            pass
        self._handle.close()


def process_start_token(pid: int) -> str | None:
    """Return an OS process birth identity, not merely a reusable PID."""

    if pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text()
    except OSError:
        raw = ""
    if raw:
        # The command is parenthesized and may contain spaces. Field 22 is index 19 in
        # the portion beginning with field 3 after the final closing parenthesis.
        close = raw.rfind(")")
        fields = raw[close + 2 :].split() if close >= 0 else []
        if len(fields) > 19:
            return f"proc:{fields[19]}"

    if sys.platform == "darwin":
        return _darwin_process_start_token(pid)
    return None


def _darwin_process_start_token(pid: int) -> str | None:
    """Read macOS's microsecond process birth time through the stable libproc API."""

    import ctypes

    class ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("flags", ctypes.c_uint32),
            ("status", ctypes.c_uint32),
            ("xstatus", ctypes.c_uint32),
            ("pid", ctypes.c_uint32),
            ("ppid", ctypes.c_uint32),
            ("uid", ctypes.c_uint32),
            ("gid", ctypes.c_uint32),
            ("ruid", ctypes.c_uint32),
            ("rgid", ctypes.c_uint32),
            ("svuid", ctypes.c_uint32),
            ("svgid", ctypes.c_uint32),
            ("reserved", ctypes.c_uint32),
            ("command", ctypes.c_char * 16),
            ("name", ctypes.c_char * 32),
            ("nfiles", ctypes.c_uint32),
            ("pgid", ctypes.c_uint32),
            ("job_control_count", ctypes.c_uint32),
            ("terminal_device", ctypes.c_uint32),
            ("terminal_pgid", ctypes.c_uint32),
            ("nice", ctypes.c_int32),
            ("start_seconds", ctypes.c_uint64),
            ("start_microseconds", ctypes.c_uint64),
        ]

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        info = ProcBsdInfo()
        size = libproc.proc_pidinfo(
            pid,
            3,  # PROC_PIDTBSDINFO
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
    except (AttributeError, OSError):
        return None
    if size != ctypes.sizeof(info) or info.start_seconds <= 0:
        return None
    return f"darwin:{info.start_seconds}:{info.start_microseconds}"


def process_group(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except (OSError, AttributeError):
        return None


def record_matches_process(record: dict[str, Any]) -> bool:
    """Prove that a record still names the exact process the launcher started."""

    pid = record.get("pid")
    token = record.get("start_token")
    pgid = record.get("pgid")
    if not isinstance(pid, int) or not isinstance(token, str) or not token:
        return False
    if process_start_token(pid) != token:
        return False
    actual_group = process_group(pid)
    return pgid is None or actual_group == pgid


def port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def listener_description(port: int) -> str:
    fallback = f"an unowned process is listening on 127.0.0.1:{port}"
    lsof = shutil.which("lsof")
    if not lsof:
        return fallback
    try:
        result = subprocess.run(  # noqa: S603
            [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpct"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return fallback
    details = " ".join(line[1:] for line in result.stdout.splitlines() if line[:1] in {"p", "c"})
    return details or fallback


def listener_pids(port: int) -> tuple[int, ...]:
    """Return the distinct processes listening on a local TCP port."""

    lsof = shutil.which("lsof")
    if not lsof:
        return ()
    try:
        result = subprocess.run(  # noqa: S603
            [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("p"):
            continue
        with suppress(ValueError):
            pid = int(line[1:])
            if pid > 0:
                pids.add(pid)
    return tuple(sorted(pids))


def process_cwd(pid: int) -> Path | None:
    """Read a process working directory without adding a runtime dependency."""

    proc_cwd = Path(f"/proc/{pid}/cwd")
    try:
        if proc_cwd.exists():
            return proc_cwd.resolve(strict=True)
    except OSError:
        return None

    lsof = shutil.which("lsof")
    if not lsof:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [lsof, "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n") and len(line) > 1:
            with suppress(OSError):
                return Path(line[1:]).resolve(strict=True)
    return None


def process_command(pid: int) -> list[str]:
    """Return a process command as tokens, or an empty list when it cannot be proven."""

    ps = shutil.which("ps")
    if not ps:
        return []
    try:
        result = subprocess.run(  # noqa: S603
            [ps, "-ww", "-o", "command=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        return shlex.split(result.stdout.strip())
    except ValueError:
        return []


def command_has_option(command: Sequence[str], option: str, value: str) -> bool:
    return any(
        command[index] == option and command[index + 1] == value
        for index in range(len(command) - 1)
    )


def command_matches_component(name: str, command: Sequence[str], port: int) -> bool:
    """Recognize only launcher commands tied to this exact checkout and fixed port."""

    # Preserve recovery of pre-Vite launcher records while using Vite's host flag.
    host_options = ("--host", "--hostname") if name == "frontend" else ("--host",)
    if not any(command_has_option(command, option, "127.0.0.1") for option in host_options):
        return False
    if not command_has_option(command, "--port", str(port)):
        return False
    if name == "backend":
        return any(
            command[index : index + 3] == ["-m", "uvicorn", "backend.main:app"]
            for index in range(len(command) - 2)
        )
    if name == "frontend":
        return command_has_option(command, "--dir", str(FRONTEND)) and any(
            token in {"start", "dev"} for token in command
        )
    return False


def recover_checkout_component(name: str, port: int) -> dict[str, Any] | None:
    """Rebuild ownership only when the live listener is provably this checkout's Lyra."""

    listeners = listener_pids(port)
    if len(listeners) != 1:
        return None
    listener_pid = listeners[0]
    pgid = process_group(listener_pid)
    if not isinstance(pgid, int) or pgid <= 0 or process_group(pgid) != pgid:
        return None
    checkout_directories = {ROOT.resolve()}
    if name == "frontend":
        checkout_directories.add(FRONTEND.resolve())
    if process_cwd(pgid) not in checkout_directories:
        return None
    command = process_command(pgid)
    if not command_matches_component(name, command, port):
        return None
    token = process_start_token(pgid)
    if token is None:
        return None
    return {
        "pid": pgid,
        "pgid": pgid,
        "start_token": token,
        "command": command,
        "recovered": True,
        "recovered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def url_ready(url: str, timeout: float = 2.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            response.read(1)
            return 200 <= response.status < 400
    except (OSError, urllib.error.URLError, ValueError):
        return False


def tail(path: Path, lines: int = 30) -> str:
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return "(log unavailable)"
    return "\n".join(content[-lines:]) or "(log is empty)"


def run_checked(command: Sequence[str], label: str, *, cwd: Path = ROOT) -> None:
    say(f"  {label}...")
    try:
        result = subprocess.run(list(command), cwd=cwd, check=False)  # noqa: S603
    except OSError as exc:
        raise LauncherError(f"could not run {label}: {exc}") from exc
    if result.returncode != 0:
        raise LauncherError(f"{label} failed with exit code {result.returncode}")


def venv_python() -> Path:
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return ROOT / ".venv" / relative


def backend_imports_work(python: Path) -> bool:
    if not python.is_file():
        return False
    try:
        result = subprocess.run(  # noqa: S603
            [
                str(python),
                "-c",
                (
                    "import sqlite3; import fastapi, httpx, pymupdf, sqlite_vec, uvicorn; "
                    "connection = sqlite3.connect(':memory:'); "
                    "connection.enable_load_extension(True); sqlite_vec.load(connection); "
                    "connection.execute('select vec_version()').fetchone(); connection.close()"
                ),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def ensure_python_environment(metadata: dict[str, Any]) -> Path:
    if sys.version_info < (3, 12):  # noqa: UP036 - launcher must diagnose an old host Python
        raise LauncherError(
            f"Python 3.12 or newer is required; this launcher is using {sys.version.split()[0]}"
        )
    python = venv_python()
    if not python.exists():
        step("Creating the Python environment")
        run_checked([sys.executable, "-m", "venv", str(ROOT / ".venv")], "create .venv")
    venv_version = executable_version(str(python))
    if not venv_version or venv_version < (3, 12):
        found = ".".join(map(str, venv_version)) if venv_version else "unreadable"
        raise LauncherError(
            f"the existing .venv uses an unsupported Python ({found}). Move .venv aside "
            "and run ./run again so it can be recreated with Python 3.12+."
        )

    fingerprint = sha256_files([ROOT / "pyproject.toml"])
    needs_install = metadata.get("backend_fingerprint") != fingerprint
    if needs_install or not backend_imports_work(python):
        step("Installing backend dependencies")
        run_checked(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-e", "."],
            "install backend dependencies",
        )
        if not backend_imports_work(python):
            raise LauncherError(
                "backend dependencies installed, but their import check still fails; "
                "inspect the pip output above"
            )
        metadata["backend_fingerprint"] = fingerprint
        atomic_write_json(INSTALL_FILE, metadata)
    else:
        ok("backend dependencies are current")
    return python


def executable_version(executable: str) -> tuple[int, ...] | None:
    try:
        result = subprocess.run(  # noqa: S603
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(?:^|\s)v?(\d+)\.(\d+)(?:\.(\d+))?", result.stdout.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups(default="0"))


def ensure_frontend_environment(metadata: dict[str, Any]) -> str:
    pnpm = shutil.which("pnpm")
    if not pnpm:
        raise LauncherError(
            "pnpm is required but was not found. Install Node.js 22.13+ and pnpm, "
            "then run ./run again. See https://pnpm.io/installation"
        )
    node = shutil.which("node")
    node_version = executable_version(node) if node else None
    if not node_version or node_version < (22, 13):
        found = ".".join(map(str, node_version)) if node_version else "not found"
        raise LauncherError(f"Node.js 22.13 or newer is required (found {found})")

    install_inputs = [
        FRONTEND / "package.json",
        FRONTEND / "pnpm-lock.yaml",
        FRONTEND / "pnpm-workspace.yaml",
    ]
    fingerprint = sha256_files(install_inputs)
    node_modules = FRONTEND / "node_modules"
    if not node_modules.is_dir() or metadata.get("frontend_fingerprint") != fingerprint:
        step("Installing frontend dependencies")
        run_checked(
            [pnpm, "--dir", str(FRONTEND), "install", "--frozen-lockfile"],
            "install frontend dependencies",
            cwd=FRONTEND,
        )
        metadata["frontend_fingerprint"] = fingerprint
        atomic_write_json(INSTALL_FILE, metadata)
    else:
        ok("frontend dependencies are current")
    return pnpm


def frontend_build_inputs() -> list[Path]:
    inputs = [
        FRONTEND / ".env.local",
        FRONTEND / "index.html",
        FRONTEND / "package.json",
        FRONTEND / "pnpm-lock.yaml",
        FRONTEND / "tsconfig.json",
        FRONTEND / "vite.config.ts",
    ]
    public = FRONTEND / "public"
    if public.is_dir():
        inputs.extend(path for path in public.rglob("*") if path.is_file())
    source = FRONTEND / "src"
    if source.is_dir():
        inputs.extend(path for path in source.rglob("*") if path.is_file())
    return inputs


def remove_frontend_build_artifacts() -> None:
    target = FRONTEND / "dist"
    if not target.exists():
        return
    if target.is_symlink() or target.resolve().parent != FRONTEND.resolve():
        raise LauncherError(f"refusing to remove unexpected build-cache path: {target}")
    shutil.rmtree(target)
    ok("removed frontend/dist")


def ensure_frontend_build(pnpm: str, metadata: dict[str, Any]) -> None:
    fingerprint = sha256_files(frontend_build_inputs())
    build_output = FRONTEND / "dist" / "index.html"
    if build_output.is_file() and metadata.get("build_fingerprint") == fingerprint:
        ok("production frontend build is current")
        return
    step("Building the production frontend")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with FRONTEND_LOG.open("a") as log:
        log.write(f"\n--- production build {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        result = subprocess.run(  # noqa: S603
            [pnpm, "--dir", str(FRONTEND), "build"],
            cwd=FRONTEND,
            check=False,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    if result.returncode != 0:
        raise LauncherError("the frontend build failed. Last output:\n\n" + tail(FRONTEND_LOG))
    metadata["build_fingerprint"] = fingerprint
    atomic_write_json(INSTALL_FILE, metadata)
    ok("production frontend built")


def bundled_services() -> tuple[BundledService, ...]:
    """Return the optional helper services shipped with this checkout."""

    return ()


def configured_bundled_services(names: Iterable[str]) -> tuple[BundledService, ...]:
    available = {service.name: service for service in bundled_services()}
    configured: list[BundledService] = []
    for name in names:
        service = available.get(name)
        if service is None:
            warn(f"unknown bundled service {name!r} in runtime state; ignoring it")
            continue
        configured.append(service)
    return tuple(configured)


def invoke_bundled_service(
    service: BundledService,
    command: str,
    *,
    required: bool,
    wait: bool = True,
) -> int:
    if not service.helper.is_file():
        if required:
            try:
                helper_label = service.helper.relative_to(ROOT)
            except ValueError:
                helper_label = service.helper
            raise LauncherError(
                f"the bundled {service.name} helper is missing at "
                f"{helper_label}; restore the complete Lyra checkout"
            )
        warn(f"bundled service {service.name} helper is missing: {service.helper}")
        return 1
    invocation = [sys.executable, str(service.helper), command]
    if wait:
        try:
            result = subprocess.run(invocation, cwd=ROOT, check=False)  # noqa: S603
        except OSError as exc:
            raise LauncherError(f"could not run bundled {service.name} {command}: {exc}") from exc
        return result.returncode
    try:
        subprocess.Popen(invocation, cwd=ROOT)  # noqa: S603
    except OSError as exc:
        raise LauncherError(f"could not run bundled {service.name} {command}: {exc}") from exc
    return 0


def start_bundled_services(
    services: Sequence[BundledService], *, preserve_on_failure: Iterable[str] = ()
) -> None:
    """Start every service transactionally, rolling partial startup back."""

    attempted: list[BundledService] = []
    preserved_names = set(preserve_on_failure)
    try:
        for service in services:
            attempted.append(service)
            step(f"Provisioning bundled service: {service.name}")
            if invoke_bundled_service(service, "start", required=True) != 0:
                raise LauncherError(
                    f"bundled service {service.name} did not pass startup and readiness checks"
                )
            ok(f"{service.name} is ready")
    except (LauncherError, KeyboardInterrupt):
        rollback = [service for service in attempted if service.name not in preserved_names]
        if rollback:
            warn("bundled-service startup failed; rolling back the partial stack")
            stop_bundled_services(rollback)
        raise


def stop_bundled_services(services: Sequence[BundledService]) -> bool:
    """Stop services in reverse dependency order and preserve persistent state."""

    success = True
    for service in reversed(services):
        say(f"  stopping bundled service {service.name}")
        try:
            result = invoke_bundled_service(service, "stop", required=False)
        except LauncherError as exc:
            warn(str(exc))
            success = False
            continue
        if result != 0:
            warn(f"bundled service {service.name} reported a stop failure")
            success = False
    return success


def stop_configured_bundled_services(names: Iterable[str]) -> bool:
    configured_names = tuple(names)
    services = configured_bundled_services(configured_names)
    all_known = len(services) == len(configured_names)
    return stop_bundled_services(services) and all_known


def process_record(
    process: subprocess.Popen[bytes], command: Sequence[str], log: Path
) -> dict[str, Any]:
    token = process_start_token(process.pid)
    if token is None:
        raise LauncherError(f"could not establish ownership of new process {process.pid}")
    return {
        "pid": process.pid,
        "pgid": process_group(process.pid),
        "start_token": token,
        "command": list(command),
        "log": str(log.relative_to(ROOT)),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def spawn_component(
    name: str,
    command: Sequence[str],
    log_path: Path,
    runtime: dict[str, Any],
) -> dict[str, Any]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    with log_path.open("a") as log:
        log.write(f"\n--- launcher start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        try:
            process = subprocess.Popen(  # noqa: S603
                list(command),
                cwd=FRONTEND if name == "frontend" else ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise LauncherError(f"could not start {name}: {exc}") from exc
    time.sleep(0.05)
    if process.poll() is not None:
        raise LauncherError(f"{name} exited immediately. Last output:\n\n{tail(log_path)}")
    record = process_record(process, command, log_path)
    runtime["processes"][name] = record
    atomic_write_json(RUNTIME_FILE, runtime)
    return record


def wait_for_component(
    name: str,
    record: dict[str, Any],
    url: str,
    log_path: Path,
    timeout: int,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not record_matches_process(record):
            raise LauncherError(f"{name} exited during startup. Last output:\n\n{tail(log_path)}")
        if url_ready(url):
            return
        time.sleep(0.5)
    raise LauncherError(
        f"{name} did not become ready at {url} within {timeout}s. Last output:\n\n{tail(log_path)}"
    )


def signal_owned_record(record: dict[str, Any], sig: signal.Signals) -> bool:
    """Signal only after revalidating the saved process birth identity."""

    if not record_matches_process(record):
        return False
    pid = record["pid"]
    pgid = record.get("pgid")
    try:
        if isinstance(pgid, int) and hasattr(os, "killpg"):
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        return True
    except PermissionError as exc:
        raise LauncherError(f"permission denied while signaling owned process {pid}") from exc
    return True


def stop_owned_component(name: str, record: dict[str, Any], port: int) -> bool:
    if not record_matches_process(record):
        if port_is_open(port):
            warn(
                f"{name}: ownership record is stale and {listener_description(port)}; "
                "it was not signaled"
            )
            return False
        warn(f"{name}: discarded a stale ownership record; no process was signaled")
        return True

    pid = record["pid"]
    say(f"  stopping {name} (owned pid {pid})")
    signal_owned_record(record, signal.SIGTERM)
    deadline = time.monotonic() + 8
    while record_matches_process(record) and time.monotonic() < deadline:
        time.sleep(0.1)
    if record_matches_process(record):
        warn(f"{name} ignored SIGTERM; sending SIGKILL to the same verified process group")
        signal_owned_record(record, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while record_matches_process(record) and time.monotonic() < deadline:
            time.sleep(0.05)
    if record_matches_process(record) or port_is_open(port):
        warn(
            f"{name} did not release port {port}; no unverified process will be killed. "
            "Inspect it with ./run status."
        )
        return False
    ok(f"{name} stopped")
    return True


def component_state(
    name: str,
    record: dict[str, Any] | None,
    port: int,
    url: str,
) -> tuple[str, bool]:
    open_port = port_is_open(port)
    healthy = url_ready(url) if open_port else False
    if record and record_matches_process(record):
        if healthy:
            return f"{name}: running, launcher-owned, healthy", True
        return f"{name}: launcher-owned process is running but not healthy", False
    if record:
        if open_port:
            return (
                f"{name}: stale ownership record; unowned port conflict "
                f"({listener_description(port)})",
                False,
            )
        return f"{name}: stopped; stale ownership record", False
    if healthy:
        return (
            f"{name}: a healthy server is present but is not launcher-owned; "
            "it will not be adopted or stopped",
            False,
        )
    if open_port:
        return f"{name}: unowned port conflict ({listener_description(port)})", False
    return f"{name}: stopped; port {port} is available", False


def component_state_is_blocking(description: str) -> bool:
    """Return whether a reported core-component state prevents a safe launch."""

    return any(
        marker in description
        for marker in ("not healthy", "not launcher-owned", "unowned port conflict")
    )


def ensure_port_available(
    name: str,
    record: dict[str, Any] | None,
    port: int,
    url: str,
) -> bool:
    """Return True for a healthy owned process; fail for every unowned listener."""

    if record and record_matches_process(record):
        if url_ready(url):
            ok(f"{name} is already running and healthy")
            return True
        raise LauncherError(
            f"the launcher-owned {name} process is running but unhealthy; "
            "run ./run stop, inspect its log, and retry"
        )
    if port_is_open(port):
        state, _ = component_state(name, record, port, url)
        raise LauncherError(
            f"{state}. Lyra will never kill or adopt an unowned listener; stop it with "
            "its original supervisor or configure it away from this fixed port."
        )
    return False


def reconcile_component_record(
    name: str,
    record: dict[str, Any] | None,
    port: int,
    url: str,
    runtime: dict[str, Any],
) -> dict[str, Any] | None:
    """Repair stale ownership after a launcher or terminal exits unexpectedly."""

    if record and record_matches_process(record):
        return record
    if not port_is_open(port):
        return record
    recovered = recover_checkout_component(name, port)
    if recovered is None:
        return record
    runtime["processes"][name] = recovered
    atomic_write_json(RUNTIME_FILE, runtime)
    if url_ready(url):
        ok(f"recovered ownership of the existing healthy {name}")
        return recovered
    warn(f"recovered an unhealthy {name} from this checkout; restarting it safely")
    if not stop_owned_component(name, recovered, port):
        raise LauncherError(f"could not safely stop the unhealthy recovered {name}")
    runtime["processes"].pop(name, None)
    atomic_write_json(RUNTIME_FILE, runtime)
    return None


def stop_lyra(runtime: dict[str, Any]) -> bool:
    processes = runtime["processes"]
    success = True
    for name, port in CORE_COMPONENTS:
        record = processes.get(name)
        if not isinstance(record, dict):
            continue
        try:
            stopped = stop_owned_component(name, record, port)
        except LauncherError as exc:
            warn(str(exc))
            success = False
            continue
        success = stopped and success
        if stopped:
            processes.pop(name, None)
            atomic_write_json(RUNTIME_FILE, runtime)
    if not any(name in processes for name, _port in CORE_COMPONENTS):
        runtime["mode"] = None
        atomic_write_json(RUNTIME_FILE, runtime)
    return success


def ensure_supervisor(runtime: dict[str, Any]) -> None:
    """Keep one detached process enforcing the whole-stack lifecycle."""

    existing = runtime["processes"].get("supervisor")
    if isinstance(existing, dict) and record_matches_process(existing):
        ok("bundled-service supervisor is already running")
        return
    if existing is not None:
        runtime["processes"].pop("supervisor", None)
        atomic_write_json(RUNTIME_FILE, runtime)

    step("Starting the bundled-service supervisor")
    command = [sys.executable, str(Path(__file__).resolve()), "__supervise"]
    spawn_component("supervisor", command, SUPERVISOR_LOG, runtime)
    ok("bundled-service supervisor is running")


def supervise() -> int:
    """Converge Lyra's processes and bundled services to one lifecycle state."""

    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, request_stop)

    runtime = load_runtime()
    while not stopping:
        runtime = load_runtime()
        processes = runtime["processes"]
        core_records = [processes.get(name) for name, _port in CORE_COMPONENTS]
        core_running = all(
            isinstance(record, dict) and record_matches_process(record) for record in core_records
        )
        if runtime.get("desired_state") != "running" or not core_running:
            break
        time.sleep(SUPERVISOR_POLL_SECONDS)

    success = True
    processes = runtime["processes"]
    for name, port in CORE_COMPONENTS:
        record = processes.get(name)
        if not isinstance(record, dict) or not record_matches_process(record):
            continue
        try:
            stopped = stop_owned_component(name, record, port)
        except LauncherError as exc:
            warn(str(exc))
            success = False
            continue
        success = stopped and success
    success = stop_configured_bundled_services(runtime.get("bundled_services", [])) and success
    return 0 if success else 1


def wait_for_supervisor(record: dict[str, Any], timeout: float = 75) -> bool:
    deadline = time.monotonic() + timeout
    while record_matches_process(record) and time.monotonic() < deadline:
        time.sleep(0.1)
    return not record_matches_process(record)


def stop_supervised_stack(runtime: dict[str, Any]) -> bool:
    """Request one whole-stack shutdown, with an ownership-checked fallback."""

    runtime["desired_state"] = "stopped"
    atomic_write_json(RUNTIME_FILE, runtime)
    success = True
    supervisor = runtime["processes"].get("supervisor")
    if (
        isinstance(supervisor, dict)
        and record_matches_process(supervisor)
        and not wait_for_supervisor(supervisor)
    ):
        warn("bundled-service supervisor did not finish shutdown; terminating it safely")
        signal_owned_record(supervisor, signal.SIGTERM)
        if not wait_for_supervisor(supervisor, timeout=10):
            signal_owned_record(supervisor, signal.SIGKILL)
            wait_for_supervisor(supervisor, timeout=2)
        success = False

    success = stop_lyra(runtime) and success
    names = tuple(runtime.get("bundled_services", []))
    bundles_stopped = stop_configured_bundled_services(names)
    success = bundles_stopped and success
    if bundles_stopped:
        runtime["bundled_services"] = []
    current_supervisor = runtime["processes"].get("supervisor")
    if isinstance(current_supervisor, dict) and not record_matches_process(current_supervisor):
        runtime["processes"].pop("supervisor", None)
    if not any(name in runtime["processes"] for name, _port in CORE_COMPONENTS):
        runtime["mode"] = None
    atomic_write_json(RUNTIME_FILE, runtime)
    return success


def start(args: argparse.Namespace) -> int:
    runtime = load_runtime()
    selected_services = bundled_services()
    previous_bundle_names = tuple(runtime["bundled_services"])
    previously_healthy_bundles: set[str] = set()
    bundle_start_attempted = False
    bundle_start_completed = False
    stack_was_running = all(
        isinstance(runtime["processes"].get(name), dict)
        and record_matches_process(runtime["processes"][name])
        for name, _port in CORE_COMPONENTS
    )
    try:
        if args.clean:
            step("Stopping the supervised stack before cleaning")
            if not stop_supervised_stack(runtime):
                raise LauncherError("could not safely stop the existing app for --clean")
            remove_frontend_build_artifacts()

        # Recover only listeners that can be proven to be this checkout's exact Lyra
        # command. Every other port conflict remains untouched.
        backend_record = runtime["processes"].get("backend")
        frontend_record = runtime["processes"].get("frontend")
        backend_record = reconcile_component_record(
            "backend",
            backend_record if isinstance(backend_record, dict) else None,
            BACKEND_PORT,
            BACKEND_URL,
            runtime,
        )
        frontend_record = reconcile_component_record(
            "frontend",
            frontend_record if isinstance(frontend_record, dict) else None,
            FRONTEND_PORT,
            FRONTEND_URL,
            runtime,
        )
        backend_running = ensure_port_available(
            "backend",
            backend_record if isinstance(backend_record, dict) else None,
            BACKEND_PORT,
            BACKEND_URL,
        )
        frontend_running = ensure_port_available(
            "frontend",
            frontend_record if isinstance(frontend_record, dict) else None,
            FRONTEND_PORT,
            FRONTEND_URL,
        )
        supervisor = runtime["processes"].get("supervisor")
        if (
            isinstance(supervisor, dict)
            and record_matches_process(supervisor)
            and not (backend_running and frontend_running)
        ):
            step("Finishing shutdown of the previous supervised stack")
            if not stop_supervised_stack(runtime):
                raise LauncherError("the previous supervised stack did not stop cleanly")
            backend_running = False
            frontend_running = False
        stack_was_running = backend_running and frontend_running

        step("Checking and repairing project dependencies")
        metadata = load_json(INSTALL_FILE, {})
        python = ensure_python_environment(metadata)
        pnpm = ensure_frontend_environment(metadata)

        if selected_services:
            if stack_was_running:
                for service in selected_services:
                    try:
                        healthy = invoke_bundled_service(service, "status", required=False) == 0
                    except LauncherError as exc:
                        warn(str(exc))
                        healthy = False
                    if healthy:
                        previously_healthy_bundles.add(service.name)
            bundle_start_attempted = True
            runtime["bundled_services"] = [service.name for service in selected_services]
            atomic_write_json(RUNTIME_FILE, runtime)
            try:
                start_bundled_services(
                    selected_services,
                    preserve_on_failure=previously_healthy_bundles,
                )
            except LauncherError as exc:
                runtime["bundled_services"] = sorted(previously_healthy_bundles)
                atomic_write_json(RUNTIME_FILE, runtime)
                if previously_healthy_bundles:
                    warn(f"{exc}; keeping the already-running bundled services")
                else:
                    warn(f"{exc}; continuing without web research")
            else:
                bundle_start_completed = True
        elif not backend_running and not frontend_running:
            runtime["bundled_services"] = []
            atomic_write_json(RUNTIME_FILE, runtime)

        mode = "development" if args.dev else "production"
        existing_mode = runtime.get("mode")
        if (backend_running or frontend_running) and existing_mode not in {None, mode}:
            if backend_running and frontend_running:
                warn(
                    f"the existing app is in {existing_mode} mode; keeping it running. "
                    f"Use ./run stop before switching to {mode}."
                )
            else:
                raise LauncherError(
                    f"only part of the existing {existing_mode} app is running. "
                    f"Use ./run stop before restarting it in {mode} mode."
                )

        if not args.dev and not frontend_running:
            ensure_frontend_build(pnpm, metadata)

        runtime["mode"] = existing_mode if backend_running or frontend_running else mode
        if not backend_running:
            step("Starting the backend")
            command = [
                str(python),
                "-m",
                "uvicorn",
                "backend.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(BACKEND_PORT),
            ]
            if args.dev:
                command.append("--reload")
            record = spawn_component("backend", command, BACKEND_LOG, runtime)
            wait_for_component("backend", record, BACKEND_URL, BACKEND_LOG, 60)
            ok(f"backend ready at {BACKEND_URL}")

        if not frontend_running:
            step("Starting the frontend")
            script = "dev" if args.dev else "start"
            command = [
                pnpm,
                "--dir",
                str(FRONTEND),
                script,
                "--host",
                "127.0.0.1",
                "--port",
                str(FRONTEND_PORT),
            ]
            record = spawn_component("frontend", command, FRONTEND_LOG, runtime)
            wait_for_component("frontend", record, FRONTEND_URL, FRONTEND_LOG, 150)
            ok(f"frontend ready at {FRONTEND_URL}")

        runtime["mode"] = runtime.get("mode") or mode
        runtime["desired_state"] = "running"
        atomic_write_json(RUNTIME_FILE, runtime)
        if runtime["bundled_services"]:
            ensure_supervisor(runtime)
    except (LauncherError, KeyboardInterrupt):
        if stack_was_running:
            newly_started = tuple(
                service
                for service in selected_services
                if service.name not in previously_healthy_bundles
            )
            if bundle_start_completed and newly_started:
                stop_bundled_services(newly_started)
            restored_names = dict.fromkeys((*previous_bundle_names, *previously_healthy_bundles))
            runtime["bundled_services"] = list(restored_names)
            atomic_write_json(RUNTIME_FILE, runtime)
        elif bundle_start_attempted or any(
            isinstance(runtime["processes"].get(name), dict) for name, _port in CORE_COMPONENTS
        ):
            warn("startup did not complete; stopping the supervised stack")
            stop_supervised_stack(runtime)
        raise

    say()
    say("Lyra is running.")
    say(f"  app       {APP_URL}")
    say(f"  api       http://127.0.0.1:{BACKEND_PORT}")
    say("  logs      ./run logs")
    say("  stop      ./run stop")
    say()
    say("The app is detached and remains running after this terminal closes.")
    if not args.no_browser:
        if webbrowser.open(APP_URL):
            ok("opened Lyra in the default browser")
        else:
            warn(f"could not open a browser automatically; open {APP_URL}")
    return 0


def stop(args: argparse.Namespace) -> int:
    runtime = load_runtime()
    step("Stopping the supervised Lyra stack")
    success = stop_supervised_stack(runtime)
    return 0 if success else 1


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    """Fsync a directory entry to establish rename/link durability on POSIX.

    On non-POSIX platforms the call is a no-op (the OS provides no directory-fsync
    primitive).  On POSIX, errors that indicate the filesystem genuinely does not
    support directory fsync (EINVAL, ENOTSUP, EOPNOTSUPP, ENOSYS, EBADF on an
    O_RDONLY directory fd) are tolerated — durability is best-effort on those mounts.
    Real I/O errors (EIO, ENOSPC, EROFS, …) propagate so callers never silently
    claim durable publication after an actual storage failure.
    """
    if os.name != "posix":
        return
    import errno

    fsync_unsupported = frozenset(
        (
            errno.EINVAL,
            errno.ENOSYS,
            getattr(errno, "ENOTSUP", 0),
            getattr(errno, "EOPNOTSUPP", 0),
            errno.EBADF,
        )
    )
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in fsync_unsupported:
            return
        raise
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in fsync_unsupported:
            raise
    finally:
        os.close(fd)


def backup(args: argparse.Namespace) -> int:
    runtime = load_runtime()
    step("Stopping the supervised Lyra stack before backup")
    if not stop_supervised_stack(runtime):
        raise LauncherError("could not safely stop the existing app for backup")

    configured_data_dir, configured_db_path = configured_data_paths()
    data_dir = resolved_existing_directory(configured_data_dir, label="Lyra data directory")
    db_path = resolved_existing_file(configured_db_path, label="Lyra database")
    archive = backup_archive_target(args.archive)
    if path_is_within(archive, data_dir):
        raise LauncherError(
            f"backup target must not live inside the data directory being archived: {archive}"
        )

    staging = archive.parent / f".lyra-backup-{_secrets_mod.token_hex(16)}.tmp"

    with tempfile.TemporaryDirectory(prefix="lyra-backup-") as temporary:
        stage_root = Path(temporary)
        manifest = stage_backup_tree(stage_root, data_dir, db_path)
        try:
            descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with (
                os.fdopen(descriptor, "wb") as raw,
                tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.PAX_FORMAT) as bundle,
            ):
                bundle.add(
                    stage_root / BACKUP_MANIFEST,
                    arcname=BACKUP_MANIFEST,
                    recursive=False,
                )
                bundle.add(stage_root / BACKUP_DATA_PREFIX, arcname=BACKUP_DATA_PREFIX)
                db_member = str(manifest["db"]["member"])  # type: ignore[index]
                if db_member == BACKUP_EXTERNAL_DB:
                    bundle.add(stage_root / BACKUP_EXTERNAL_DB, arcname=BACKUP_EXTERNAL_DB)

            with tarfile.open(staging, mode="r:gz") as check:
                verified_manifest = read_backup_manifest(check)
                validate_backup_members(check, verified_manifest)

            _fsync_path(staging)
            os.link(staging, archive)
            _fsync_directory(archive.parent)
        except BaseException:
            if staging.exists() and archive.exists():
                try:
                    is_ours = os.path.samefile(staging, archive)
                except OSError:
                    is_ours = False
                if is_ours:
                    with suppress(OSError):
                        archive.unlink()
            raise
        finally:
            with suppress(OSError):
                staging.unlink(missing_ok=True)

    say()
    say(f"Lyra backup created at {archive}")
    say(f"  data dir  {data_dir}")
    say(f"  database  {db_path}")
    say("Restore into an empty directory with: ./run restore --archive ... --data-dir ...")
    return 0


def restore(args: argparse.Namespace) -> int:
    runtime = load_runtime()
    step("Stopping the supervised Lyra stack before restore")
    if not stop_supervised_stack(runtime):
        raise LauncherError("could not safely stop the existing app for restore")

    archive = resolved_existing_file(rooted_path(args.archive), label="backup archive")
    data_dir = restore_target_directory(args.data_dir)
    db_override = restore_target_file(args.db_path) if args.db_path is not None else None

    stage_data = Path(
        tempfile.mkdtemp(prefix=f".{data_dir.name}.restore-", dir=str(data_dir.parent))
    )
    stage_db = (
        db_override.with_name(f".{db_override.name}.restore-{os.getpid()}")
        if db_override is not None
        else None
    )

    try:
        with tarfile.open(archive, mode="r:*") as bundle:
            manifest = read_backup_manifest(bundle)
            validate_backup_members(bundle, manifest)
            db = manifest["db"]
            if not isinstance(db, dict):
                raise LauncherError("backup archive manifest is missing database metadata")
            inside_data_dir = bool(db["inside_data_dir"])
            db_member = str(db["member"])
            relative = db.get("relative_path")
            if inside_data_dir:
                if db_override is not None:
                    raise LauncherError(
                        "this backup keeps its database inside the data directory; omit --db-path"
                    )
                extract_archive_prefix(bundle, prefix=BACKUP_DATA_PREFIX, destination=stage_data)
                if not isinstance(relative, str):
                    raise LauncherError("backup archive is missing the database relative path")
                restored_db = stage_data.joinpath(*PurePosixPath(relative).parts)
            else:
                if stage_db is None:
                    raise LauncherError(
                        "this backup used LYRA_DB_PATH outside LYRA_DATA_DIR; pass --db-path to "
                        "restore the database safely"
                    )
                extract_archive_prefix(bundle, prefix=BACKUP_DATA_PREFIX, destination=stage_data)
                extract_archive_file(bundle, member_name=db_member, destination=stage_db)
                restored_db = stage_db

        if not restored_db.is_file():
            raise LauncherError("backup archive did not restore a database file")
        try:
            with sqlite3.connect(f"{restored_db.resolve().as_uri()}?mode=rw", uri=True) as restored:
                result = restored.execute("pragma quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise LauncherError("restored database failed SQLite quick_check")
        except sqlite3.Error as exc:
            raise LauncherError(f"restored database could not be verified: {exc}") from exc

        stage_data.replace(data_dir)
        if stage_db is not None and db_override is not None:
            try:
                stage_db.replace(db_override)
            except OSError as exc:
                try:
                    data_dir.replace(stage_data)
                except OSError as rollback_exc:
                    raise LauncherError(
                        "restore failed while finalizing the external database path, and "
                        "rollback of the restored data directory also failed"
                    ) from rollback_exc
                raise LauncherError(
                    "restore failed while finalizing the external database path; the requested "
                    "targets were rolled back"
                ) from exc
            restored_db = db_override
        elif isinstance(relative, str):
            restored_db = data_dir.joinpath(*PurePosixPath(relative).parts)
        else:
            restored_db = data_dir / "lyra.db"
    except Exception:
        shutil.rmtree(stage_data, ignore_errors=True)
        if stage_db is not None:
            with suppress(OSError):
                stage_db.unlink()
        raise

    say()
    say(f"Lyra backup restored into {data_dir}")
    say(f"  database  {restored_db}")
    return 0


def helper_label(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def core_stack_is_running(runtime: dict[str, Any]) -> bool:
    processes = runtime["processes"]
    return all(
        isinstance(processes.get(name), dict) and record_matches_process(processes[name])
        for name, _port in CORE_COMPONENTS
    )


def report_core_ports_without_state(*, indent: str = "") -> bool:
    """Report core-port state when ownership records cannot be trusted, signaling nothing.

    Used by ``status`` and ``doctor`` after the runtime-state file is refused: the launcher can
    no longer claim ownership of anything, so every listener is reported through the unowned
    path of ``component_state`` (which never signals or adopts). Returns True when a reported
    state would block a safe launch.
    """

    blocking = False
    for name, port, url in (
        ("backend", BACKEND_PORT, BACKEND_URL),
        ("frontend", FRONTEND_PORT, FRONTEND_URL),
    ):
        description, _ = component_state(name, None, port, url)
        say(f"{indent}{description}")
        blocking = component_state_is_blocking(description) or blocking
    return blocking


def status(args: argparse.Namespace) -> int:
    try:
        runtime = load_runtime()
    except RuntimeStateError as exc:
        warn(str(exc))
        report_core_ports_without_state()
        return 1
    processes = runtime["processes"]
    healthy = True
    blocking_issue = False
    for name, port, url in (
        ("backend", BACKEND_PORT, BACKEND_URL),
        ("frontend", FRONTEND_PORT, FRONTEND_URL),
    ):
        value = processes.get(name)
        description, component_healthy = component_state(
            name,
            value if isinstance(value, dict) else None,
            port,
            url,
        )
        say(description)
        healthy = healthy and component_healthy
    services = configured_bundled_services(
        runtime["bundled_services"] or [service.name for service in bundled_services()]
    )
    for service in services:
        if not service.helper.is_file():
            say(f"{service.name}: misconfigured; helper missing at {helper_label(service.helper)}")
            blocking_issue = True
            continue
        if invoke_bundled_service(service, "status", required=False) == 0:
            ok(f"{service.name} is available; web research is enabled for this app session")
        else:
            warn(
                f"{service.name} is temporarily unavailable; core Lyra remains usable without "
                "web research"
            )
    if services:
        supervisor = processes.get("supervisor")
        supervisor_running = isinstance(supervisor, dict) and record_matches_process(supervisor)
        say(
            "supervisor: running and launcher-owned"
            if supervisor_running
            else "supervisor: stopped; bundled lifecycle is not being enforced"
        )
        blocking_issue = blocking_issue or (
            bool(runtime["bundled_services"]) and not supervisor_running
        )
    return 0 if healthy and not blocking_issue else 1


def doctor(args: argparse.Namespace) -> int:
    failures = 0
    say("Lyra installation diagnostics")
    if sys.version_info >= (3, 12):  # noqa: UP036 - doctor reports the host interpreter
        ok(f"launcher Python {sys.version.split()[0]}")
    else:
        warn(f"Python 3.12+ required; found {sys.version.split()[0]}")
        failures += 1

    python = venv_python()
    if backend_imports_work(python):
        ok(".venv exists and backend imports pass")
    else:
        warn(".venv is missing or incomplete; ./run will create/repair it")
        failures += 1

    pnpm = shutil.which("pnpm")
    node = shutil.which("node")
    node_version = executable_version(node) if node else None
    if pnpm and node_version and node_version >= (22, 13):
        ok(f"Node {'.'.join(map(str, node_version))} and pnpm are available")
    else:
        warn("Node.js 22.13+ and pnpm are required")
        failures += 1
    if (FRONTEND / "node_modules").is_dir():
        ok("frontend dependencies are installed")
    else:
        warn("frontend dependencies are absent; ./run will install them")
        failures += 1

    try:
        runtime = load_runtime()
    except RuntimeStateError as exc:
        failures += 1
        warn(str(exc))
        if report_core_ports_without_state(indent="  "):
            failures += 1
        return 1
    for name, port, url in (
        ("backend", BACKEND_PORT, BACKEND_URL),
        ("frontend", FRONTEND_PORT, FRONTEND_URL),
    ):
        value = runtime["processes"].get(name)
        description, _ = component_state(
            name,
            value if isinstance(value, dict) else None,
            port,
            url,
        )
        say(f"  {description}")
        if component_state_is_blocking(description):
            failures += 1

    return 0 if failures == 0 else 1


def logs(args: argparse.Namespace) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for path in (BACKEND_LOG, FRONTEND_LOG, SUPERVISOR_LOG):
        path.touch(exist_ok=True)
    processes: list[subprocess.Popen[bytes]] = []
    tail_executable = shutil.which("tail")
    if not tail_executable:
        for path in (BACKEND_LOG, FRONTEND_LOG, SUPERVISOR_LOG):
            say(f"==> {path.relative_to(ROOT)}\n{tail(path, 100)}")
        raise LauncherError("live log following requires the standard 'tail' utility")
    processes.append(
        subprocess.Popen(  # noqa: S603
            [
                tail_executable,
                "-n",
                "100",
                "-F",
                str(BACKEND_LOG),
                str(FRONTEND_LOG),
                str(SUPERVISOR_LOG),
            ],
            cwd=ROOT,
        )
    )
    say("Following Lyra logs. Press Ctrl-C to stop following; services remain running.")
    try:
        while all(process.poll() is None for process in processes):
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
    return 0


DIAGNOSTICS_URL = f"http://127.0.0.1:{BACKEND_PORT}/api/health/diagnostics"
DIAGNOSTICS_FILE = LOG_DIR / "diagnostics.json"


def _fetch_diagnostics_endpoint(timeout: float = 2.0) -> str | None:
    """The running backend's diagnostics bundle as text, or None when it is not reachable."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(DIAGNOSTICS_URL, timeout=timeout) as response:
            if 200 <= response.status < 300:
                return response.read().decode("utf-8")
    except (OSError, urllib.error.URLError, ValueError):
        return None
    return None


def _build_diagnostics_offline() -> dict[str, Any] | None:
    """Build the same bundle through the venv when the HTTP server is down.

    A bug report is most needed exactly when the backend will not start, so the offline
    path runs the real `build_diagnostics` in the venv rather than a second, drifting copy
    of it: the redaction rules are identical because the code is identical.
    """
    python = venv_python()
    if not python.is_file():
        return None
    code = (
        "import json;"
        "from backend.storage.database import connect;"
        "from backend.core.diagnostics import build_diagnostics;"
        "print(json.dumps(build_diagnostics(connect())))"
    )
    try:
        result = subprocess.run(  # noqa: S603
            [str(python), "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _launcher_only_diagnostics() -> dict[str, Any]:
    """A last-resort bundle when neither the endpoint nor the venv can be reached.

    Deliberately thin, and it reads nothing out of the runtime file: with the real builder
    unavailable there is no vetted redaction here, so it reports only facts that cannot
    carry a private path - the interpreter and an instruction to bring the app up and retry.
    """
    return {
        "bundle_version": None,
        "backend_reachable": False,
        "python": sys.version.split()[0],
        "note": (
            "The backend was not reachable and the offline builder could not run. "
            "Start Lyra with ./run and try diagnostics again for the full bundle."
        ),
    }


def diagnostics_command(args: argparse.Namespace) -> int:
    """Write a redacted diagnostics bundle to a file for pasting into a bug report.

    Prefers the running backend's endpoint, falls back to building the same bundle offline
    through the venv, and only then to a launcher-only note. The written file never carries
    document text, the tutor key, or a private path.
    """
    endpoint = _fetch_diagnostics_endpoint()
    if endpoint is not None:
        try:
            bundle, source = json.loads(endpoint), "backend endpoint"
        except ValueError:
            bundle, source = _build_diagnostics_offline() or {}, "offline"
    else:
        offline = _build_diagnostics_offline()
        if offline is not None:
            bundle, source = offline, "offline (backend not reachable)"
        else:
            bundle, source = _launcher_only_diagnostics(), "launcher only"

    DIAGNOSTICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DIAGNOSTICS_FILE.write_text(json.dumps(bundle, indent=2), encoding="utf-8")

    say("Lyra diagnostics")
    ok(f"source: {source}")
    ok(f"written to {helper_label(DIAGNOSTICS_FILE)}")
    schema = bundle.get("schema")
    if isinstance(schema, dict):
        state = (
            "current" if schema.get("current") else f"behind (at version {schema.get('version')})"
        )
        say(f"  schema: {state}")
    say("  No document text, tutor key, or private path is in this file; safe to attach.")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="./run",
        description="Provision and run Lyra as a local application.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "start",
            "stop",
            "status",
            "doctor",
            "diagnostics",
            "logs",
            "backup",
            "restore",
        ),
        default="start",
    )
    parser.add_argument("--dev", action="store_true", help="use hot-reloading dev servers")
    parser.add_argument("--clean", action="store_true", help="rebuild the frontend build output")
    parser.add_argument("--no-browser", action="store_true", help="do not open the app")
    parser.add_argument("--archive", type=Path, help="explicit backup archive path")
    parser.add_argument("--data-dir", type=Path, help="explicit restore target directory")
    parser.add_argument("--db-path", type=Path, help="explicit restore database path")
    parser.add_argument("--stop", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--prod", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.stop:
        if args.command != "start":
            parser.error("--stop cannot be combined with a command")
        args.command = "stop"
    if args.dev and args.command != "start":
        parser.error("--dev is valid only with start")
    if args.clean and args.command != "start":
        parser.error("--clean is valid only with start")
    if args.archive is not None and args.command not in {"backup", "restore"}:
        parser.error("--archive is valid only with backup or restore")
    if args.data_dir is not None and args.command != "restore":
        parser.error("--data-dir is valid only with restore")
    if args.db_path is not None and args.command != "restore":
        parser.error("--db-path is valid only with restore")
    if args.command == "backup":
        if args.archive is None:
            parser.error("backup requires --archive")
        if args.data_dir is not None or args.db_path is not None:
            parser.error("backup accepts only --archive")
    if args.command == "restore" and (args.archive is None or args.data_dir is None):
        parser.error("restore requires --archive and --data-dir")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args == ["__supervise"]:
        try:
            return supervise()
        except (LauncherError, KeyboardInterrupt) as exc:
            say(f"supervisor error: {exc}")
            return 1

    args = parse_args(raw_args)
    handlers = {
        "start": start,
        "stop": stop,
        "status": status,
        "doctor": doctor,
        "diagnostics": diagnostics_command,
        "logs": logs,
        "backup": backup,
        "restore": restore,
    }
    try:
        if args.command == "logs":
            return handlers[args.command](args)
        with LauncherLock():
            return handlers[args.command](args)
    except KeyboardInterrupt:
        say("\nInterrupted. Any app that had already reached readiness remains running.")
        return 130
    except LauncherError as exc:
        say(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
