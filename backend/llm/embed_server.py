"""Lifecycle management for the local llama.cpp embedding server.

Embedding is infrastructure: it always runs on this machine, in a `llama-server`
subprocess this module owns. The server is started lazily on the first embedding call and
never at app startup, so the API stays usable before the weights have been downloaded.

The lifecycle itself - spawn, adopt-and-verify, watch stderr, escalate on shutdown -
lives in `llama_server.py`, shared with the OCR and reranking servers. This module is
the embedding facts: which model, which flags, and what to tell the student when
something is missing.
"""

from pathlib import Path

from backend.config import settings
from backend.core.errors import ConfigurationError
from backend.llm.llama_server import LlamaServer

_HEALTH_TIMEOUT_SECONDS = 120.0

_MISSING_BINARY_MESSAGE = (
    "The local embedding server is not installed yet. "
    "Run `python scripts/fetch_models.py` to download it."
)
_MISSING_WEIGHTS_MESSAGE = (
    "The local embedding model is not downloaded yet. "
    "Run `python scripts/fetch_models.py` to download it."
)
_START_FAILED_MESSAGE = "Local embedding model failed to start."


class EmbeddingServer(LlamaServer):
    """Owns the `llama-server` subprocess that answers `/v1/embeddings` on loopback.

    A single instance is shared process-wide as `embedding_server`, because the server
    holds one model and one port. Adoption of an already-running server is doubly
    guarded: the shared identity check refuses a stranger's model, and `rag/embed.py`
    refuses any vector that is not the expected width, so a wrong model on the port
    fails loudly instead of quietly poisoning the index.
    """

    def __init__(self) -> None:
        super().__init__(
            display_name="embedding",
            # The base llama port itself; the OCR and reranking servers offset from it.
            port_offset=0,
            health_timeout_seconds=_HEALTH_TIMEOUT_SECONDS,
            missing_binary_message=_MISSING_BINARY_MESSAGE,
            start_failed_message=_START_FAILED_MESSAGE,
        )

    def _model_path(self) -> Path:
        return settings.embedding_model_path

    def _check_installed(self) -> None:
        if not settings.embedding_model_path.exists():
            raise ConfigurationError(_MISSING_WEIGHTS_MESSAGE)

    def _argv(self, binary: Path) -> list[str]:
        return [
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
            str(self.port),
            "--no-warmup",
        ]


embedding_server = EmbeddingServer()
