"""Lifecycle management for the local reranking server.

The third `llama-server` this project owns, and the same shape as the other two. Modelled
on `embed_server.py` for the same reason `ocr_server.py` is: two processes with one model
each are easier to reason about than one process swapping models, and the embedding
server's hard-won behaviour - adopt whatever already answers, own the process group,
escalate on shutdown - is worth repeating rather than generalising prematurely.

**Why a second model at all.** The embedder is a bi-encoder: it turns the passage into a
vector before it has seen the question, so everything it could have noticed about the pair
has to survive being compressed into 768 numbers with no idea what will be asked. That is
what makes it fast enough to search a whole class, and it is also why it cannot tell a
problem set from its own answer key - measured on a real course, the answer to
"what is the Fourier transform of e^-4|t|, worked out" did not appear in the top thirty-two
because eleven near-identical exponential-transform problems from a different week
outscored it.

A cross-encoder reads the question and the passage together and scores the pair. It cannot
search - scoring a thousand chunks would mean a thousand forward passes - which is exactly
why it goes second, over an over-fetch small enough to afford.

It is optional. The weights are 640 MB, `rag/rerank.py` degrades to the embedding order
when they are absent, and that is a supported configuration rather than a broken one.
"""

import logging
import subprocess
import threading
import time
from pathlib import Path

import httpx

from backend.config import settings
from backend.core.errors import ConfigurationError
from backend.llm.embed_server import _terminate

logger = logging.getLogger(__name__)

_BINARY_NAMES = ("llama-server", "llama-server.exe")
_HEALTH_TIMEOUT_SECONDS = 120.0
_HEALTH_POLL_SECONDS = 1.0
_HEALTH_REQUEST_TIMEOUT_SECONDS = 5.0

# Two above the embedding server, because one above it is the OCR server's.
RERANK_PORT_OFFSET = 2

# The context one query-and-passage pair may take. A chunk is held to 1024 estimated tokens
# and the question is short, so 2048 would very nearly do; 4096 is taken because the
# estimate floors and this model tokenizes mathematics far worse than four characters to
# the token. Overflowing here would silently truncate the end of a passage, which is a
# ranking that looks fine and is wrong.
CONTEXT_TOKENS = 4096

# The whole over-fetch is sent in one request, so the batch has to hold all of it at once.
BATCH_TOKENS = 32768

MISSING_WEIGHTS_MESSAGE = (
    "The reranking model is not installed. Run `python scripts/fetch_models.py` to "
    "download it, or leave it out and Lyra will rank search results by embedding "
    "similarity alone."
)
MISSING_BINARY_MESSAGE = (
    "The local model runtime is not installed yet. Run `python scripts/fetch_models.py`."
)
START_FAILED_MESSAGE = "The local reranking model failed to start."


class RerankServer:
    """Owns the `llama-server` subprocess that answers `/v1/rerank` on loopback."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._binary: Path | None = None
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        return settings.llama_port + RERANK_PORT_OFFSET

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def available(self) -> bool:
        """Whether the weights are on disk. Absent is a configuration, not a fault."""
        return settings.rerank_installed

    def ensure_running(self) -> None:
        """Start the server unless something is already answering on the port.

        Raises:
            ConfigurationError: The weights or the binary are missing, or the server did
                not become healthy in time.
        """
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            if self._healthy():
                return
            self._start()

    def stop(self) -> None:
        """Terminate the server. Safe to call when nothing is running."""
        with self._lock:
            process = self._process
            self._process = None
        if process is not None:
            _terminate(process)

    def _healthy(self) -> bool:
        try:
            with httpx.Client(timeout=_HEALTH_REQUEST_TIMEOUT_SECONDS) as client:
                return client.get(f"{self.base_url}/health").status_code == 200
        except httpx.HTTPError:
            return False

    def _start(self) -> None:
        """Spawn `llama-server` in reranking mode and block until it reports healthy."""
        if not self.available:
            raise ConfigurationError(MISSING_WEIGHTS_MESSAGE)
        binary = self._find_binary()
        if binary is None:
            raise ConfigurationError(MISSING_BINARY_MESSAGE)

        argv = [
            str(binary),
            "-m",
            str(settings.rerank_model_path),
            # Without this the model loads and `/v1/rerank` answers 501. It is the flag
            # that puts llama-server into ranking mode rather than completion mode.
            "--reranking",
            "-c",
            str(CONTEXT_TOKENS),
            "-b",
            str(BATCH_TOKENS),
            "-ub",
            str(BATCH_TOKENS),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--no-warmup",
        ]
        # S603: argv is built from settings and a binary this project downloaded, never
        # from user input, and it is a list, so no shell is involved.
        try:
            process = subprocess.Popen(  # noqa: S603
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise ConfigurationError(START_FAILED_MESSAGE) from exc
        self._process = process
        self._await_health(process)

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
        """Poll /health until it answers, or kill the process and raise."""
        deadline = time.monotonic() + _HEALTH_TIMEOUT_SECONDS
        with httpx.Client(timeout=_HEALTH_REQUEST_TIMEOUT_SECONDS) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self._process = None
                    raise ConfigurationError(START_FAILED_MESSAGE)
                try:
                    ready = client.get(f"{self.base_url}/health").status_code == 200
                except httpx.HTTPError:
                    ready = False
                if ready:
                    return
                time.sleep(_HEALTH_POLL_SECONDS)
        _terminate(process)
        self._process = None
        raise ConfigurationError(START_FAILED_MESSAGE)


rerank_server = RerankServer()
