"""Lifecycle management for the local llama.cpp embedding server.

Embedding is infrastructure: it always runs on this machine, in a `llama-server`
subprocess this module owns. The server is started lazily on the first embedding call and
never at app startup, so the API stays usable before the weights have been downloaded.
"""

import contextlib
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import httpx

from backend.config import settings
from backend.core.errors import ConfigurationError

_BINARY_NAMES = ("llama-server", "llama-server.exe")
_HEALTH_TIMEOUT_SECONDS = 120.0
_HEALTH_POLL_SECONDS = 1.0
_HEALTH_REQUEST_TIMEOUT_SECONDS = 5.0
_SHUTDOWN_GRACE_SECONDS = 5.0

_MISSING_BINARY_MESSAGE = (
    "The local embedding server is not installed yet. "
    "Run `python scripts/fetch_models.py` to download it."
)
_MISSING_WEIGHTS_MESSAGE = (
    "The local embedding model is not downloaded yet. "
    "Run `python scripts/fetch_models.py` to download it."
)
_START_FAILED_MESSAGE = "Local embedding model failed to start"


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
        # The child was spawned with start_new_session, so it leads its own group.
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


class EmbeddingServer:
    """Owns the `llama-server` subprocess that answers `/v1/embeddings` on loopback.

    A single instance is shared process-wide as `embedding_server`, because the server
    holds one model and one port.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._binary: Path | None = None
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        """Loopback origin the embedding server listens on."""
        return f"http://127.0.0.1:{settings.llama_port}"

    def ensure_running(self) -> None:
        """Start the server unless it is already up.

        Idempotent and thread-safe: concurrent embedding calls produce one subprocess.

        Raises:
            ConfigurationError: The binary or the weights are missing, or the server did
                not become healthy in time.
        """
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            self.start()

    def start(self) -> None:
        """Spawn `llama-server` and block until it reports healthy.

        Raises:
            ConfigurationError: The binary or the weights are missing, or the server did
                not become healthy within the startup timeout.
        """
        binary = self._find_binary()
        if binary is None:
            raise ConfigurationError(_MISSING_BINARY_MESSAGE)
        if not settings.embedding_model_path.exists():
            raise ConfigurationError(_MISSING_WEIGHTS_MESSAGE)

        argv = [
            str(binary),
            "-m",
            str(settings.embedding_model_path),
            "--embedding",
            "--pooling",
            "mean",
            "-c",
            "8192",
            "-b",
            "8192",
            # llama-server defaults -ub to 512, which is below the 2048-token chunk
            # ceiling, so long chunks would be truncated or rejected. Match -c instead.
            "-ub",
            "8192",
            "--host",
            "127.0.0.1",
            "--port",
            str(settings.llama_port),
            "--no-warmup",
        ]
        # S603: argv is built entirely from settings and a binary this project downloaded,
        # never from user input, and it is a list, so no shell is involved.
        try:
            process = subprocess.Popen(  # noqa: S603
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            # A truncated or non-executable download is the user's to fix, not a crash.
            raise ConfigurationError(_START_FAILED_MESSAGE) from exc
        self._process = process
        self._await_health(process)

    def stop(self) -> None:
        """Terminate the server. Safe to call when nothing is running."""
        with self._lock:
            process = self._process
            self._process = None
        if process is not None:
            _terminate(process)

    def _find_binary(self) -> Path | None:
        """Locate `llama-server` under the llama directory, caching the hit."""
        if self._binary is not None and self._binary.exists():
            return self._binary
        for name in _BINARY_NAMES:
            for candidate in sorted(settings.llama_dir.rglob(name)):
                if candidate.is_file():
                    self._binary = candidate
                    return candidate
        return None

    def _await_health(self, process: "subprocess.Popen[bytes]") -> None:
        """Poll /health until the server answers 200, or kill it and raise."""
        deadline = time.monotonic() + _HEALTH_TIMEOUT_SECONDS
        with httpx.Client(timeout=_HEALTH_REQUEST_TIMEOUT_SECONDS) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self._process = None
                    raise ConfigurationError(_START_FAILED_MESSAGE)
                try:
                    ready = client.get(f"{self.base_url}/health").status_code == 200
                except httpx.HTTPError:
                    ready = False
                if ready:
                    return
                time.sleep(_HEALTH_POLL_SECONDS)
        _terminate(process)
        self._process = None
        raise ConfigurationError(_START_FAILED_MESSAGE)


embedding_server = EmbeddingServer()
