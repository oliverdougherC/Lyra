"""Shared lifecycle for the `llama-server` subprocesses this project owns.

Lyra runs up to three llama.cpp servers - embedding, specialist OCR, reranking - each
holding one model on one loopback port. The first two copies of this lifecycle argued
that the duplication was "worth repeating rather than generalising prematurely"; the
third copy is the counter-argument. What lives here is everything the three had in
common, which turned out to be everything that was hard to get right:

- **Adopt, but verify.** The subprocess is spawned in its own session, so it outlives
  the backend that started it and a restarted backend routinely finds a healthy server
  already on the port. Adopting it is the only behaviour that does not tell the student
  to download a model they already have. But `/health` answers 200 for *any*
  llama-server, so adoption asks `/props` (falling back to `/v1/models`) which model is
  actually loaded and refuses a stranger: a text-only server adopted as the OCR server
  would store wrong transcriptions that look like right ones.
- **Own the process group, escalate on shutdown.** SIGTERM the group, wait, SIGKILL.
- **Keep the child's last words.** stderr goes to a bounded tail rather than DEVNULL,
  because "failed to start" with zero context sends whoever reads it nowhere.
- **Lose the port race gracefully.** Two concurrent starts race for one bind; the loser
  exits. Before reporting that exit as a failure, look again: if the winner is healthy
  and holds the right model, the loser's death was success.
- **Remember a failed start.** `ensure_running` is called per request, so a corrupt
  GGUF would otherwise spawn-and-fail on every retrieval. One loud failure, then the
  remembered error for a cooldown.
- **Health-aware supervision.** A tracked child must pass periodic health checks, not
  just be alive. A wedged-but-live process is terminated and restarted rather than
  permanently selected.
- **Durable ownership.** Every spawned server is recorded with its PID, birth token,
  port, and model, so a restarted backend can distinguish its own surviving child from
  PID reuse or an unrelated process. Ownership is recorded immediately after Popen
  and before health/model initialization; if ownership cannot be established the child
  is terminated rather than left unrecoverable.
- **Adopted-process reclamation.** An adopted Lyra-owned server is tracked for shutdown,
  so `stop()` reclaims every Lyra-owned helper, not just the one this backend instance
  spawned. A durable record is also inspected at shutdown even when the feature was
  never used during this backend lifetime.
- **Ownership survives failed termination.** The durable record is cleared only when
  the process is confirmed dead or its PID has been reused.
- **Configuration-drift reconciliation.** A port or model change does not silently
  orphan an old Lyra-owned helper; it is reclaimed before the replacement launches.

Each concrete server contributes only its facts: which model, which flags, which port
offset, how long a healthy start may take, and what to tell the student when something
is missing.
"""

import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import IO

import httpx

from backend.config import settings
from backend.core.errors import ConfigurationError

logger = logging.getLogger(__name__)

_BINARY_NAMES = ("llama-server", "llama-server.exe")
_HEALTH_POLL_SECONDS = 1.0
_HEALTH_REQUEST_TIMEOUT_SECONDS = 5.0
_SHUTDOWN_GRACE_SECONDS = 5.0

# How much of the child's stderr to keep. llama.cpp's real failure reason - a corrupt
# GGUF, an unknown flag, an out-of-memory abort - is in its last few lines, and keeping
# only a bounded tail means a chatty healthy server cannot grow memory forever.
_STDERR_TAIL_LINES = 40

# How long a failed start is remembered before another spawn is attempted.
# `ensure_running` is called per request (`rag/rerank.py` calls it on every retrieval),
# so without this a corrupt model file costs a spawn-load-crash cycle on every question.
_START_FAILURE_COOLDOWN_SECONDS = 300.0

# How often to re-verify health for a tracked child. A wedged server is detected within
# this window rather than permanently selected (which is what poll()-only allowed).
_HEALTH_RECHECK_SECONDS = 30.0

# After this many consecutive unhealthy-terminate-restart cycles without a successful
# health check in between, enter the failure cooldown rather than restarting forever.
_MAX_UNHEALTHY_RESTARTS = 3

# Durable ownership records so a restarted backend can distinguish its own surviving
# child from PID reuse or an unrelated process.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RUNTIME_DIR = _PROJECT_ROOT / ".lyra"
_OWNERSHIP_FILE = _RUNTIME_DIR / "server_ownership.json"
_ownership_lock = threading.Lock()


# ------------------------------------------------------------------ process identity


def _process_start_token(pid: int) -> str | None:
    """Return an OS process birth identity, not merely a reusable PID.

    On Linux, reads /proc/{pid}/stat field 22 (kernel start-time ticks).
    On macOS, reads the microsecond birth time through the stable libproc API.
    Returns None if the identity cannot be established.
    """
    if pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text()
    except OSError:
        raw = ""
    if raw:
        close = raw.rfind(")")
        fields = raw[close + 2 :].split() if close >= 0 else []
        if len(fields) > 19:
            return f"proc:{fields[19]}"
    if sys.platform == "darwin":
        return _darwin_start_token(pid)
    return None


def _darwin_start_token(pid: int) -> str | None:
    """Read macOS's microsecond process birth time through the stable libproc API."""
    import ctypes

    class _ProcBsdInfo(ctypes.Structure):
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
        info = _ProcBsdInfo()
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


def _process_group(pid: int) -> int | None:
    """Read the process group for a PID, or None if inaccessible."""
    try:
        return os.getpgid(pid)
    except (OSError, AttributeError):
        return None


def _token_matches_pid(pid: int, token: str | None) -> bool:
    """True iff the PID is alive and its birth identity matches the stored token."""
    if token is None:
        return False
    live_token = _process_start_token(pid)
    return live_token is not None and live_token == token


# ------------------------------------------------------------------ ownership file


def _load_ownership() -> dict:
    """Load the server ownership file, returning {} if absent or corrupt."""
    try:
        return json.loads(_OWNERSHIP_FILE.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_ownership(data: dict) -> None:
    """Atomically write the server ownership file with restrictive permissions."""
    _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _OWNERSHIP_FILE.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(data, indent=2).encode())
    finally:
        os.close(fd)
    tmp.replace(_OWNERSHIP_FILE)


def _record_server(service: str, pid: int, port: int, model: str) -> None:
    """Record ownership of a spawned server in the durable ownership file.

    Raises RuntimeError if the process birth identity cannot be established,
    and OSError if the ownership file cannot be written.  Both are fatal to
    the caller: a process whose identity cannot be proven is unrecoverable.
    """
    token = _process_start_token(pid)
    if token is None:
        raise RuntimeError(
            f"Cannot establish birth identity for PID {pid}; ownership of "
            f"the {service} server cannot be recorded"
        )
    with _ownership_lock:
        data = _load_ownership()
        data[service] = {
            "pid": pid,
            "start_token": token,
            "pgid": _process_group(pid),
            "port": port,
            "model": model,
            "started_at": time.time(),
        }
        _save_ownership(data)


def _read_server_record(service: str) -> dict | None:
    """Read the ownership record for a service, or None if absent."""
    with _ownership_lock:
        data = _load_ownership()
    record = data.get(service)
    return record if isinstance(record, dict) else None


def _remove_server_record(service: str) -> None:
    """Remove the ownership record for a service."""
    with _ownership_lock:
        data = _load_ownership()
        if service in data:
            del data[service]
            _save_ownership(data)


# ------------------------------------------------------------------ process management


def _terminate(process: "subprocess.Popen[bytes]") -> None:
    """Stop a process and its children, escalating from terminate to kill."""
    if process.poll() is not None:
        return
    _stop_tree(process, kill=False)
    try:
        process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _stop_tree(process, kill=True)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_SHUTDOWN_GRACE_SECONDS)


def _stop_tree(process: "subprocess.Popen[bytes]", *, kill: bool) -> None:
    """Signal the whole process group on POSIX, the process alone elsewhere."""
    if os.name == "posix":
        sig = signal.SIGKILL if kill else signal.SIGTERM
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (OSError, ValueError):
            pass
        else:
            return
    if kill:
        process.kill()
    else:
        process.terminate()


def _terminate_pid(pid: int, pgid: int | None, token: str | None, label: str) -> None:
    """Terminate an adopted process by PID, verifying identity before every signal.

    The birth token is re-checked before SIGTERM and again before SIGKILL, so a PID
    that was recycled between the two signals is never hit.
    """
    if not _token_matches_pid(pid, token):
        logger.info("Adopted %s (PID %d) already exited or was replaced", label, pid)
        return
    gid = pgid if pgid is not None else pid
    try:
        os.killpg(gid, signal.SIGTERM)
    except (OSError, ValueError):
        with contextlib.suppress(OSError, ValueError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _SHUTDOWN_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not _token_matches_pid(pid, token):
            return
        time.sleep(0.25)
    if _token_matches_pid(pid, token):
        try:
            os.killpg(gid, signal.SIGKILL)
        except (OSError, ValueError):
            with contextlib.suppress(OSError, ValueError):
                os.kill(pid, signal.SIGKILL)


def _drain(stream: IO[bytes], tail: "deque[str]") -> None:
    """Read a child's stderr to EOF, keeping only the tail.

    Runs on its own daemon thread for the child's whole life. The pipe has to be drained
    even while the server is healthy: an undrained pipe fills its buffer and blocks the
    writer, which would freeze the server mid-request months after it started fine.
    """
    with contextlib.suppress(OSError, ValueError):
        for raw in stream:
            tail.append(raw.decode("utf-8", errors="replace").rstrip())
    with contextlib.suppress(OSError):
        stream.close()


class LlamaServer:
    """One owned `llama-server` subprocess: spawn, adopt, verify, watch, stop.

    Subclasses supply the model-specific facts by overriding `_model_path`,
    `_check_installed`, and `_argv`; everything about process lifetime lives here. Each
    concrete server is a process-wide singleton, because it holds one model on one port.
    """

    def __init__(
        self,
        *,
        display_name: str,
        port_offset: int,
        health_timeout_seconds: float,
        missing_binary_message: str,
        start_failed_message: str,
    ) -> None:
        self._display_name = display_name
        self._port_offset = port_offset
        self._health_timeout_seconds = health_timeout_seconds
        self._missing_binary_message = missing_binary_message
        self._start_failed_message = start_failed_message
        self._process: subprocess.Popen[bytes] | None = None
        self._binary: Path | None = None
        self._lock = threading.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._stderr_thread: threading.Thread | None = None
        self._failure_message: str | None = None
        self._failed_at: float = 0.0
        self._adopted_pid: int | None = None
        self._adopted_pgid: int | None = None
        self._adopted_start_token: str | None = None
        self._last_health_ok: float = 0.0
        self._unhealthy_restarts: int = 0

    # ------------------------------------------------------------------ facts

    def _model_path(self) -> Path:
        """The GGUF this server is supposed to be serving."""
        raise NotImplementedError

    def _check_installed(self) -> None:
        """Raise ConfigurationError, in the server's own words, if weights are absent."""
        raise NotImplementedError

    def _argv(self, binary: Path) -> list[str]:
        """The full command line, including host and port."""
        raise NotImplementedError

    # ------------------------------------------------------------------ address

    @property
    def port(self) -> int:
        return settings.llama_port + self._port_offset

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------------ lifecycle

    def ensure_running(self) -> None:
        """Start the server unless the right one is already answering on the port.

        Idempotent and thread-safe: concurrent callers produce one subprocess.

        Raises:
            ConfigurationError: The binary or the weights are missing, the server did
                not become healthy in time (or failed recently and is in cooldown), or
                the port is held by a server running a different model.
        """
        with self._lock:
            # 0. Reconcile config drift before evaluating the current state.
            self._reconcile_stale_ownership()

            # 1. Tracked child spawned by this process instance.
            if self._process is not None:
                if self._process.poll() is None:
                    now = time.monotonic()
                    if now - self._last_health_ok < _HEALTH_RECHECK_SECONDS:
                        return
                    if self._healthy():
                        self._last_health_ok = now
                        self._unhealthy_restarts = 0
                        return
                    logger.warning(
                        "The %s server (PID %d) is alive but not answering health "
                        "checks; terminating for restart",
                        self._display_name,
                        self._process.pid,
                    )
                    _terminate(self._process)
                    self._process = None
                    self._unhealthy_restarts += 1
                    if self._unhealthy_restarts >= _MAX_UNHEALTHY_RESTARTS:
                        msg = (
                            f"The {self._display_name} server has failed health checks "
                            f"{self._unhealthy_restarts} consecutive times; not "
                            f"retrying for {_START_FAILURE_COOLDOWN_SECONDS:.0f} seconds."
                        )
                        self._failure_message = msg
                        self._failed_at = time.monotonic()
                        raise ConfigurationError(msg)
                else:
                    self._process = None

            # 2. Adopted process from a previous backend lifetime.
            if self._adopted_pid is not None:
                if _token_matches_pid(self._adopted_pid, self._adopted_start_token):
                    record = _read_server_record(self._display_name)
                    adopted_model = record.get("model") if record else None
                    if (
                        adopted_model is not None
                        and adopted_model != self._model_path().name
                    ):
                        logger.info(
                            "Adopted %s server (PID %d) serves model %s but %s is "
                            "configured; terminating for replacement",
                            self._display_name,
                            self._adopted_pid,
                            adopted_model,
                            self._model_path().name,
                        )
                        _terminate_pid(
                            self._adopted_pid,
                            self._adopted_pgid,
                            self._adopted_start_token,
                            self._display_name,
                        )
                        self._adopted_pid = None
                        self._adopted_pgid = None
                        self._adopted_start_token = None
                    else:
                        now = time.monotonic()
                        if now - self._last_health_ok < _HEALTH_RECHECK_SECONDS:
                            return
                        if self._healthy():
                            self._last_health_ok = now
                            self._unhealthy_restarts = 0
                            return
                        logger.warning(
                            "Adopted %s server (PID %d) is alive but unhealthy; "
                            "terminating for restart",
                            self._display_name,
                            self._adopted_pid,
                        )
                        _terminate_pid(
                            self._adopted_pid,
                            self._adopted_pgid,
                            self._adopted_start_token,
                            self._display_name,
                        )
                        self._adopted_pid = None
                        self._adopted_pgid = None
                        self._adopted_start_token = None
                        self._unhealthy_restarts += 1
                        if self._unhealthy_restarts >= _MAX_UNHEALTHY_RESTARTS:
                            msg = (
                                f"The {self._display_name} server has failed health "
                                f"checks {self._unhealthy_restarts} consecutive times; "
                                f"not retrying for "
                                f"{_START_FAILURE_COOLDOWN_SECONDS:.0f} seconds."
                            )
                            self._failure_message = msg
                            self._failed_at = time.monotonic()
                            raise ConfigurationError(msg)
                else:
                    self._adopted_pid = None
                    self._adopted_pgid = None
                    self._adopted_start_token = None

            # 3. Something already answering on the port? Verify and maybe adopt.
            if self._healthy():
                self._verify_and_adopt()
                return

            # 4. Start fresh.
            self._start_locked()

    def start(self) -> None:
        """Spawn the server and block until it reports healthy."""
        with self._lock:
            self._start_locked()

    def stop(self) -> None:
        """Terminate the server and conditionally clean its ownership record.

        Handles directly spawned, adopted, and un-adopted-but-owned processes.
        Ownership is removed only when the process is confirmed dead or its PID
        has been reused.
        """
        with self._lock:
            process = self._process
            self._process = None
            adopted_pid = self._adopted_pid
            adopted_pgid = self._adopted_pgid
            adopted_token = self._adopted_start_token
            self._adopted_pid = None
            self._adopted_pgid = None
            self._adopted_start_token = None
        if process is not None:
            _terminate(process)
        if adopted_pid is not None:
            _terminate_pid(adopted_pid, adopted_pgid, adopted_token, self._display_name)

        if process is None and adopted_pid is None:
            record = _read_server_record(self._display_name)
            if record is not None:
                rec_pid = record.get("pid")
                rec_token = record.get("start_token")
                rec_pgid = record.get("pgid")
                if (
                    isinstance(rec_pid, int)
                    and isinstance(rec_token, str)
                    and _token_matches_pid(rec_pid, rec_token)
                ):
                    _terminate_pid(
                        rec_pid,
                        rec_pgid if isinstance(rec_pgid, int) else None,
                        rec_token,
                        self._display_name,
                    )

        record = _read_server_record(self._display_name)
        if record is not None:
            rec_pid = record.get("pid")
            rec_token = record.get("start_token")
            if (
                isinstance(rec_pid, int)
                and isinstance(rec_token, str)
                and _token_matches_pid(rec_pid, rec_token)
            ):
                logger.warning(
                    "The %s server (PID %d) survived termination; preserving "
                    "ownership record for next recovery attempt",
                    self._display_name,
                    rec_pid,
                )
            else:
                _remove_server_record(self._display_name)

    # ------------------------------------------------------------------ health

    def _healthy(self) -> bool:
        try:
            with httpx.Client(timeout=_HEALTH_REQUEST_TIMEOUT_SECONDS) as client:
                return client.get(f"{self.base_url}/health").status_code == 200
        except httpx.HTTPError:
            return False

    def _served_model(self) -> str | None:
        try:
            with httpx.Client(timeout=_HEALTH_REQUEST_TIMEOUT_SECONDS) as client:
                with contextlib.suppress(httpx.HTTPError, ValueError):
                    payload = client.get(f"{self.base_url}/props").json()
                    if isinstance(payload, dict):
                        path = payload.get("model_path")
                        if isinstance(path, str) and path:
                            return path
                with contextlib.suppress(httpx.HTTPError, ValueError):
                    payload = client.get(f"{self.base_url}/v1/models").json()
                    data = payload.get("data") if isinstance(payload, dict) else None
                    if isinstance(data, list) and data and isinstance(data[0], dict):
                        model_id = data[0].get("id")
                        if isinstance(model_id, str) and model_id:
                            return model_id
        except httpx.HTTPError:
            pass
        return None

    def _verify_and_adopt(self) -> None:
        """Verify model identity and check ownership for shutdown responsibility."""
        expected = self._model_path().name
        served = self._served_model()
        if served is not None and Path(served).name == expected:
            record = _read_server_record(self._display_name)
            if record is not None:
                pid = record.get("pid")
                token = record.get("start_token")
                if (
                    isinstance(pid, int)
                    and isinstance(token, str)
                    and record.get("port") == self.port
                    and _token_matches_pid(pid, token)
                ):
                    self._adopted_pid = pid
                    self._adopted_pgid = _process_group(pid) or pid
                    self._adopted_start_token = token
                    logger.info(
                        "Adopted %s server (PID %d) from a previous backend lifetime",
                        self._display_name,
                        pid,
                    )
                    self._last_health_ok = time.monotonic()
                    self._unhealthy_restarts = 0
                    return
                if not (
                    isinstance(pid, int)
                    and isinstance(token, str)
                    and _token_matches_pid(pid, token)
                ):
                    _remove_server_record(self._display_name)
            logger.info(
                "Using compatible external %s server on port %d (not managed by Lyra)",
                self._display_name,
                self.port,
            )
            self._last_health_ok = time.monotonic()
            self._unhealthy_restarts = 0
            return
        if served is None:
            raise ConfigurationError(
                f"Port {self.port} is already in use by something that is not the "
                f"{self._display_name} server. Stop it or set LYRA_LLAMA_PORT to a "
                "free range."
            )
        raise ConfigurationError(
            f"Port {self.port} is already serving the model {Path(served).name!r}, but "
            f"the {self._display_name} server needs {expected!r}. Stop the other "
            "server or set LYRA_LLAMA_PORT to a free range."
        )

    # ------------------------------------------------------------------ ownership reconciliation

    def _reconcile_stale_ownership(self) -> None:
        """Reclaim a Lyra-owned helper whose port or model no longer matches config."""
        record = _read_server_record(self._display_name)
        if record is None:
            return
        rec_pid = record.get("pid")
        rec_token = record.get("start_token")
        rec_port = record.get("port")
        rec_model = record.get("model")
        if not (isinstance(rec_pid, int) and isinstance(rec_token, str)):
            return
        if rec_port == self.port and rec_model == self._model_path().name:
            return
        if not _token_matches_pid(rec_pid, rec_token):
            _remove_server_record(self._display_name)
            return
        logger.info(
            "Configuration changed for %s (port %s->%d, model %s->%s); "
            "reclaiming old owned process (PID %d)",
            self._display_name,
            rec_port,
            self.port,
            rec_model,
            self._model_path().name,
            rec_pid,
        )
        rec_pgid = record.get("pgid")
        _terminate_pid(
            rec_pid,
            rec_pgid if isinstance(rec_pgid, int) else None,
            rec_token,
            self._display_name,
        )
        if not _token_matches_pid(rec_pid, rec_token):
            _remove_server_record(self._display_name)
        else:
            logger.warning(
                "Old %s server (PID %d) survived termination after config "
                "change; preserving ownership record",
                self._display_name,
                rec_pid,
            )

    # ------------------------------------------------------------------ starting

    def _start_locked(self) -> None:
        """Spawn `llama-server` and block until healthy. Caller holds the lock."""
        self._check_installed()
        binary = self._find_binary()
        if binary is None:
            raise ConfigurationError(self._missing_binary_message)
        if (
            self._failure_message is not None
            and time.monotonic() - self._failed_at < _START_FAILURE_COOLDOWN_SECONDS
        ):
            raise ConfigurationError(self._failure_message)

        try:
            self._spawn_and_await(binary)
        except ConfigurationError as exc:
            self._failure_message = exc.message
            self._failed_at = time.monotonic()
            logger.error(
                "The %s server failed to start; not retrying for %.0f seconds. %s",
                self._display_name,
                _START_FAILURE_COOLDOWN_SECONDS,
                exc.message,
            )
            raise
        self._failure_message = None
        self._last_health_ok = time.monotonic()
        self._unhealthy_restarts = 0

    def _spawn_and_await(self, binary: Path) -> None:
        """Spawn the subprocess, record ownership, then wait for health.

        Ownership is recorded immediately after Popen succeeds and before the
        lengthy health/model initialization, so a crash between spawn and
        health does not create an unowned orphan.  If recording fails the
        child is terminated rather than left unrecoverable.
        """
        argv = self._argv(binary)
        self._stderr_tail = deque(maxlen=_STDERR_TAIL_LINES)
        try:
            process = subprocess.Popen(  # noqa: S603
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise ConfigurationError(self._start_failed_message) from exc
        stderr = getattr(process, "stderr", None)
        if stderr is not None:
            self._stderr_thread = threading.Thread(
                target=_drain,
                args=(stderr, self._stderr_tail),
                name=f"{self._display_name}-server-stderr",
                daemon=True,
            )
            self._stderr_thread.start()
        self._process = process

        try:
            _record_server(
                self._display_name,
                process.pid,
                self.port,
                self._model_path().name,
            )
        except (OSError, RuntimeError) as exc:
            logger.error(
                "Cannot establish ownership of %s server (PID %d); "
                "terminating to avoid an unrecoverable orphan: %s",
                self._display_name,
                process.pid,
                exc,
            )
            _terminate(process)
            self._process = None
            raise ConfigurationError(
                f"The {self._display_name} server started but its ownership "
                f"could not be recorded. Fix the underlying error and retry."
            ) from exc

        try:
            self._await_health(process)
        except ConfigurationError:
            rec = _read_server_record(self._display_name)
            if rec is not None:
                rp = rec.get("pid")
                rt = rec.get("start_token")
                if not (
                    isinstance(rp, int)
                    and isinstance(rt, str)
                    and _token_matches_pid(rp, rt)
                ):
                    _remove_server_record(self._display_name)
            raise

    def _find_binary(self) -> Path | None:
        if self._binary is not None and self._binary.exists():
            return self._binary
        for name in _BINARY_NAMES:
            for candidate in sorted(settings.llama_dir.rglob(name)):
                if candidate.is_file():
                    self._binary = candidate
                    return candidate
        return None

    def _await_health(self, process: "subprocess.Popen[bytes]") -> None:
        deadline = time.monotonic() + self._health_timeout_seconds
        with httpx.Client(timeout=_HEALTH_REQUEST_TIMEOUT_SECONDS) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self._process = None
                    if self._healthy():
                        self._verify_and_adopt()
                        return
                    raise self._start_failure()
                try:
                    ready = client.get(f"{self.base_url}/health").status_code == 200
                except httpx.HTTPError:
                    ready = False
                if ready:
                    return
                time.sleep(_HEALTH_POLL_SECONDS)
        _terminate(process)
        self._process = None
        raise self._start_failure()

    def _start_failure(self) -> ConfigurationError:
        thread = self._stderr_thread
        if thread is not None:
            thread.join(timeout=1.0)
        tail = "\n".join(self._stderr_tail)
        if tail:
            return ConfigurationError(f"{self._start_failed_message} Its last output:\n{tail}")
        return ConfigurationError(self._start_failed_message)
