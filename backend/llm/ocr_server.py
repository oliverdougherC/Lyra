"""Lifecycle management for the local Unlimited-OCR server.

The specialist half of the transcription interface in `rag/transcribe.py`. The general
half sends a page to whatever model the student configured; this one sends it to a
`llama-server` subprocess on this machine, holding weights Lyra downloaded.

**It is optional, and Lyra is complete without it.** Recognition works through the
configured vision model at 13.8 seconds a page, which is under a minute for a scanned
homework sheet. This exists for the case that measurement ruled out: a 608-page book at
that rate is 2.3 hours. The weights are 2.8 GB and are downloaded only when asked for, so
every path here has to cope with them being absent, which is the normal state.

Modelled on `embed_server.py`, deliberately and closely. Two llama.cpp servers on two
ports with the same lifecycle is easier to reason about than one server that swaps models,
and the embedding server's hard-won behaviour - adopt whatever is already answering, own
the process group, escalate on shutdown - is worth having twice rather than worth
generalising prematurely.
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
_HEALTH_TIMEOUT_SECONDS = 180.0
_HEALTH_POLL_SECONDS = 1.0
_HEALTH_REQUEST_TIMEOUT_SECONDS = 5.0

# Its own port, one above the embedding server's, so the two never contend.
OCR_PORT_OFFSET = 1

# What the GGUF publisher's reference invocation asks for, kept together so the spike and
# the product run the same model in the same way.
#
# The DRY sampler stands in for `no_repeat_ngram_size`, which llama.cpp does not have.
# These values are a deliberately weak loop guard and must not be tightened: aggressive DRY
# settings garble the model's table output. Repetition loops on dense pages are the
# expected failure mode, which is why the caller enforces an output ceiling instead.
REFERENCE_ARGS = (
    "--chat-template",
    "deepseek-ocr",
    "--no-jinja",
    "--temp",
    "0",
    "--flash-attn",
    "off",
    "--no-warmup",
    "-c",
    "16384",
    "--dry-multiplier",
    "0.8",
    "--dry-base",
    "1.75",
    "--dry-allowed-length",
    "35",
    "--dry-penalty-last-n",
    "128",
    "--dry-sequence-breaker",
    "none",
)

# `--special`, and it is not optional, but it is a server flag only: `llama-mtmd-cli`
# rejects it outright and prints special tokens anyway.
#
# llama-server suppresses them by default, and this model carries its layout in them.
# Without this the `<|det|>` markers vanish and, because the table cell tags are special
# tokens too, a table arrives as one run of text with its cells fused: `Time Domain` and
# `Frequency Domain` come back as `Time DomainFrequency Domain`. The reference vLLM recipe
# asks for the same thing as `skip_special_tokens: false`. Measured: 1943 characters
# without it against 2457 with, on the same page.
SERVER_ARGS = (*REFERENCE_ARGS, "--special")

# The single-page prompt. `Multi page parsing.` exists and is not used: it depends on
# R-SWA, which is still a draft upstream (#24975).
OCR_PROMPT = "document parsing."

# Hard ceiling on one page's output, matching the reference `-n 4096`. A page that runs
# past it is a repetition loop rather than a long page, and the caller fails that page
# rather than letting it run.
MAX_OUTPUT_TOKENS = 4096

MISSING_WEIGHTS_MESSAGE = (
    "The specialist text-recognition model is not installed. "
    "Run `python scripts/fetch_models.py --ocr` to download it, or leave it out and Lyra "
    "will read scanned pages with the model configured in Settings."
)
MISSING_BINARY_MESSAGE = (
    "The local model runtime is not installed yet. Run `python scripts/fetch_models.py`."
)
START_FAILED_MESSAGE = "The specialist text-recognition model failed to start."


class OcrServer:
    """Owns the `llama-server` subprocess that serves Unlimited-OCR on loopback."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._binary: Path | None = None
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        return settings.llama_port + OCR_PORT_OFFSET

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def available(self) -> bool:
        """Whether the weights are on disk. False is the ordinary state, not an error."""
        return settings.ocr_installed

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
        """Spawn `llama-server` with the projector, and block until it reports healthy."""
        if not self.available:
            raise ConfigurationError(MISSING_WEIGHTS_MESSAGE)
        binary = self._find_binary()
        if binary is None:
            raise ConfigurationError(MISSING_BINARY_MESSAGE)

        argv = [
            str(binary),
            "-m",
            str(settings.ocr_model_path),
            "--mmproj",
            str(settings.ocr_mmproj_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            *SERVER_ARGS,
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
        """Poll /health until it answers, or kill the process and raise.

        A longer deadline than the embedding server's, because this loads several
        gigabytes rather than 146 megabytes.
        """
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


ocr_server = OcrServer()
