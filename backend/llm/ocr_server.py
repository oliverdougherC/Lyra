"""Lifecycle management for the local Unlimited-OCR server.

The specialist half of the transcription interface in `rag/transcribe.py`. The general
half sends a page to whatever model the student configured; this one sends it to a
`llama-server` subprocess on this machine, holding weights Lyra downloaded.

**It is optional, and Lyra is complete without it.** Recognition works through the
configured vision model at 13.8 seconds a page, which is under a minute for a scanned
homework sheet. This exists for the case that measurement ruled out: a 608-page book at
that rate is 2.3 hours. The weights are 2.8 GB and are downloaded only when asked for, so
every path here has to cope with them being absent, which is the normal state.

The lifecycle - spawn, adopt-and-verify, watch stderr, escalate on shutdown - lives in
`llama_server.py`, shared with the embedding and reranking servers. The identity check
on adoption matters most here: `/health` answers 200 for any llama-server, and a stale
text-only server adopted as this one would store wrong transcriptions shaped exactly
like right ones. What remains below is the OCR facts: the publisher's reference
invocation, and the flags it is not safe to lose.
"""

from pathlib import Path

from backend.config import settings
from backend.core.errors import ConfigurationError
from backend.llm.llama_server import PACKAGED_MISSING_RUNTIME_MESSAGE, LlamaServer

# Loading several gigabytes rather than 146 megabytes, so a longer health deadline than
# the embedding server's.
_HEALTH_TIMEOUT_SECONDS = 180.0

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
# A packaged install has no checkout to run scripts in: the same sentence, minus the
# step the student cannot take.
_PACKAGED_MISSING_WEIGHTS_MESSAGE = (
    "The specialist text-recognition model is not installed. "
    "Lyra will read scanned pages with the model configured in Settings instead."
)
MISSING_BINARY_MESSAGE = (
    "The local model runtime is not installed yet. Run `python scripts/fetch_models.py`."
)
START_FAILED_MESSAGE = "The specialist text-recognition model failed to start."


def _missing_weights_message() -> str:
    if settings.packaged_mode:
        return _PACKAGED_MISSING_WEIGHTS_MESSAGE
    return MISSING_WEIGHTS_MESSAGE


def _missing_binary_message() -> str:
    """The packaged product ships the runtime in the app, so there is no fetch step there."""
    if settings.packaged_mode:
        return PACKAGED_MISSING_RUNTIME_MESSAGE
    return MISSING_BINARY_MESSAGE


class OcrServer(LlamaServer):
    """Owns the `llama-server` subprocess that serves Unlimited-OCR on loopback."""

    def __init__(self) -> None:
        super().__init__(
            display_name="text-recognition",
            port_offset=OCR_PORT_OFFSET,
            health_timeout_seconds=_HEALTH_TIMEOUT_SECONDS,
            missing_binary_message=_missing_binary_message(),
            start_failed_message=START_FAILED_MESSAGE,
        )

    @property
    def available(self) -> bool:
        """Whether the weights are on disk. False is the ordinary state, not an error."""
        return settings.ocr_installed

    def _model_path(self) -> Path:
        return settings.ocr_model_path

    def _check_installed(self) -> None:
        if not self.available:
            raise ConfigurationError(_missing_weights_message())

    def _argv(self, binary: Path) -> list[str]:
        return [
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


ocr_server = OcrServer()
