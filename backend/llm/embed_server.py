"""Lifecycle management for the local llama.cpp embedding server.

Embedding is infrastructure: it always runs on this machine, in a `llama-server`
subprocess this module owns. The server is started lazily on the first embedding call and
never at app startup, so the API stays usable before the weights have been downloaded.

The weights themselves are also self-provisioning. Embedding is required, unlike the
optional OCR and reranking paths, so a missing model is not a configuration the caller
degrades to: the first request that needs it downloads the weights to the
application-managed models directory and continues, with no restart and no command to
run. The download metadata and mechanics live in `model_provisioning.py`, shared with
`scripts/fetch_models.py`, so the manual and automatic paths can never name a different
file or a different folder.

The runtime is the other half of the same promise. In the packaged product it ships
inside the application (the Tauri bundle stages it next to the frozen backend), so a
clean install can resolve it with nothing but its own files on disk; in a source
checkout the developer fetches it with the script. Either way, nothing here tells a
user to run a repo command to make embedding work.

The lifecycle itself - spawn, adopt-and-verify, watch stderr, escalate on shutdown -
lives in `llama_server.py`, shared with the OCR and reranking servers. This module is
the embedding facts: which model, which flags, and what to tell the student when
something is missing.
"""

from pathlib import Path

from backend.config import settings
from backend.core.errors import ConfigurationError
from backend.llm import model_provisioning
from backend.llm.llama_server import PACKAGED_MISSING_RUNTIME_MESSAGE, LlamaServer
from backend.llm.model_provisioning import EMBEDDING_WEIGHTS

_HEALTH_TIMEOUT_SECONDS = 120.0


def _missing_binary_message() -> str:
    """What to say when the runtime that serves embeddings is absent.

    In the packaged product the runtime ships inside the application, so absence means a
    broken installation, not a step to run. In a source checkout the developer fetches
    it with the script.
    """
    if settings.packaged_mode:
        return PACKAGED_MISSING_RUNTIME_MESSAGE
    return (
        "The local embedding server is not installed yet. "
        "Run `python scripts/fetch_models.py` to download it."
    )


# Before first use the model is simply not there yet; it arrives on first use. This is
# the pre-attempt state, so it says what will happen, not what went wrong. A provision
# attempt that actually fails reports itself, in `model_provisioning`.
_MISSING_WEIGHTS_MESSAGE = "The local embedding model will be downloaded on first use."
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
            missing_binary_message=_missing_binary_message(),
            start_failed_message=_START_FAILED_MESSAGE,
            weights_provisioner=self._provision_weights,
        )

    def _provision_weights(self) -> None:
        """Download the nomic weights on first use, in the shared download path.

        Deduplicated process-wide (one download per burst of first-use requests),
        atomic (an interruption leaves nothing that looks installed), and reported with
        a retryable error that names no command and no path.
        """
        model_provisioning.ensure_weight(EMBEDDING_WEIGHTS)

    def _download_state(self) -> str | None:
        if model_provisioning.download_in_progress(EMBEDDING_WEIGHTS.filename):
            return "Downloading the local embedding model"
        return None

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
