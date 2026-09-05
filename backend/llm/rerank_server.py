"""Lifecycle management for the local reranking server.

The third `llama-server` this project owns. The first two copies of the lifecycle each
argued the duplication was worth repeating rather than generalising prematurely; this
one is why it now lives once, in `llama_server.py`, shared with the embedding and OCR
servers. What remains here is the reranking facts: which model, which flags, and why a
second model exists at all.

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

from pathlib import Path

from backend.config import settings
from backend.core.errors import ConfigurationError
from backend.llm.llama_server import PACKAGED_MISSING_RUNTIME_MESSAGE, LlamaServer

_HEALTH_TIMEOUT_SECONDS = 120.0

# Two above the embedding server, because one above it is the OCR server's.
RERANK_PORT_OFFSET = 2

# The context one query-and-passage pair may take. A chunk is held to 1024 estimated tokens
# and the question is short, so 2048 would very nearly do; 4096 is taken because the
# estimate floors and this model tokenizes mathematics far worse than four characters to
# the token. Overflowing here would silently truncate the end of a passage, which is a
# ranking that looks fine and is wrong.
CONTEXT_TOKENS = 4096

# `-b` and `-ub` both match `-c`, and that is the whole requirement. Reranking is
# non-causal, so one query-and-passage pair must fit inside a single physical batch: the
# default `-ub 512` would refuse most pairs outright. The over-fetch as a whole does NOT
# need to fit at once - llama-server splits a multi-document request across its own
# batches internally, exactly as `rag/embed.py` measured for embeddings. Asking for more
# would also be a lie: llama.cpp clamps `n_batch` to `n_ctx`, so the 32768 this once
# requested "to hold the whole over-fetch" was silently 4096 all along.

MISSING_WEIGHTS_MESSAGE = (
    "The reranking model is not installed. Run `python scripts/fetch_models.py` to "
    "download it, or leave it out and Lyra will rank search results by embedding "
    "similarity alone."
)
# A packaged install has no checkout to run scripts in: the same sentence, minus the
# step the student cannot take.
_PACKAGED_MISSING_WEIGHTS_MESSAGE = (
    "The reranking model is not installed. "
    "Lyra will rank search results by embedding similarity alone."
)
MISSING_BINARY_MESSAGE = (
    "The local model runtime is not installed yet. Run `python scripts/fetch_models.py`."
)
START_FAILED_MESSAGE = "The local reranking model failed to start."


def _missing_weights_message() -> str:
    if settings.packaged_mode:
        return _PACKAGED_MISSING_WEIGHTS_MESSAGE
    return MISSING_WEIGHTS_MESSAGE


def _missing_binary_message() -> str:
    """The packaged product ships the runtime in the app, so there is no fetch step there."""
    if settings.packaged_mode:
        return PACKAGED_MISSING_RUNTIME_MESSAGE
    return MISSING_BINARY_MESSAGE


class RerankServer(LlamaServer):
    """Owns the `llama-server` subprocess that answers `/v1/rerank` on loopback."""

    def __init__(self) -> None:
        super().__init__(
            display_name="reranking",
            port_offset=RERANK_PORT_OFFSET,
            health_timeout_seconds=_HEALTH_TIMEOUT_SECONDS,
            missing_binary_message=_missing_binary_message(),
            start_failed_message=START_FAILED_MESSAGE,
        )

    @property
    def available(self) -> bool:
        """Whether the weights are on disk. Absent is a configuration, not a fault."""
        return settings.rerank_installed

    def _model_path(self) -> Path:
        return settings.rerank_model_path

    def _check_installed(self) -> None:
        if not self.available:
            raise ConfigurationError(_missing_weights_message())

    def _argv(self, binary: Path) -> list[str]:
        return [
            str(binary),
            "-m",
            str(settings.rerank_model_path),
            # Without this the model loads and `/v1/rerank` answers 501. It is the flag
            # that puts llama-server into ranking mode rather than completion mode.
            "--reranking",
            "-c",
            str(CONTEXT_TOKENS),
            "-b",
            str(CONTEXT_TOKENS),
            "-ub",
            str(CONTEXT_TOKENS),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--no-warmup",
        ]


rerank_server = RerankServer()
