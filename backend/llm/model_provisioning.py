"""Weights metadata and downloads for the local runtimes.

The one place that knows which model files the local `llama-server`s load, where they
come from, and how they get to disk. Two consumers, one implementation:

- `scripts/fetch_models.py`, the manual path a developer runs once after installing the
  backend;
- the embedding server, the automatic path a student never sees. Its weights are
  required infrastructure, so a clean install's first message downloads them and
  continues instead of refusing with a command to run (PLA-402).

Both go through `ensure_weight`, so repository, file name, destination, and download
mechanics cannot drift apart: there is no second implementation that remembers a
different file name or a different folder.
"""

import logging
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from huggingface_hub import hf_hub_download

from backend.config import settings
from backend.core.errors import ConfigurationError

logger = logging.getLogger(__name__)

# The embedding model. The name in `settings.embedding_model_path` and the file name
# below are one fact said twice; `test_model_provisioning.py` pins them together.
EMBEDDING_REPO_ID = "nomic-ai/nomic-embed-text-v1.5-GGUF"
EMBEDDING_FILENAME = "nomic-embed-text-v1.5.Q8_0.gguf"
# Ceiling for the disk check, not the file size: the Q8_0 file weighs about 139 MB, and a
# ceiling with margin refuses a machine that truly cannot take it without guessing the
# file's exact byte count.
EMBEDDING_DOWNLOAD_BYTES = 200 * 1024 * 1024

# The reranker. A cross-encoder rather than a second embedder: it reads the question and
# a passage together and scores the pair, which is the one thing a bi-encoder cannot do,
# because a bi-encoder has to compress the passage before it has seen the question.
#
# `Q8_0` for the same reason the embedder is Q8_0. This model's whole job is to draw fine
# distinctions between passages that a coarser model already found equally plausible, and
# quantisation noise lands exactly there. 640 MB rather than 390 MB is a cheap way not to
# spend the measurement on the quantisation.
RERANK_REPO_ID = "gpustack/bge-reranker-v2-m3-GGUF"
RERANK_FILENAME = "bge-reranker-v2-m3-Q8_0.gguf"
RERANK_DOWNLOAD_BYTES = 700 * 1024 * 1024

# The specialist OCR path. Two files, because llama.cpp loads a multimodal model through
# MTMD and needs the language model and its projector separately.
#
# `Q4_K_M` for the model and `bf16` for the projector, which is what the GGUF publisher's
# own reference invocation uses. A smaller projector quantisation exists and is not taken:
# the projector is what turns the page image into tokens, so it is the last place to save
# 365 MB.
OCR_REPO_ID = "sabafallah/Unlimited-OCR-GGUF"
OCR_FILENAMES = ("unlimited-ocr-Q4_K_M.gguf", "mmproj-unlimited-ocr-bf16.gguf")

# What the pair weighs, from the repository listing. Used for the disk check before any of
# it is written, because running out of disk 2 GB into a 2.8 GB download leaves a partial
# file and a confusing error rather than a refusal.
OCR_DOWNLOAD_BYTES = 2_776_000_000

# And leave this much free afterwards. A machine with 200 MB left is not a working machine,
# and the ingestion pipeline writes rendered pages and extracted text as it goes.
DISK_HEADROOM_BYTES = 2 * (1 << 30)


@dataclass(frozen=True)
class ModelFile:
    """One weights file: where it comes from, where it lands, and what it weighs."""

    # How the file is named in an error a user can read: "local embedding model", never
    # the repository id or the path. The sentence supplies the article.
    display_name: str
    repo_id: str
    filename: str
    # Ceiling for the pre-download disk check.
    download_bytes: int

    @property
    def path(self) -> Path:
        """Where the file lives once installed. `settings` owns the models directory."""
        return settings.models_dir / self.filename


EMBEDDING_WEIGHTS = ModelFile(
    display_name="local embedding model",
    repo_id=EMBEDDING_REPO_ID,
    filename=EMBEDDING_FILENAME,
    download_bytes=EMBEDDING_DOWNLOAD_BYTES,
)
RERANK_WEIGHTS = ModelFile(
    display_name="local reranking model",
    repo_id=RERANK_REPO_ID,
    filename=RERANK_FILENAME,
    download_bytes=RERANK_DOWNLOAD_BYTES,
)


def require_disk_space(target: Path, needed: int) -> None:
    """Raise unless `target` has room for `needed` bytes plus the headroom.

    Checking afterwards is not a check: a download that fills the disk leaves a partial
    file and reports whatever the filesystem said, which is not a sentence anyone can act
    on.

    Raises:
        ConfigurationError: Naming both numbers, because "not enough space" without them
            leaves the reader to go and find out how short they are.
    """
    free = shutil.disk_usage(target).free
    if free >= needed + DISK_HEADROOM_BYTES:
        return
    raise ConfigurationError(
        f"Not enough free disk space: this needs {needed / 1e9:.1f} GB plus "
        f"{DISK_HEADROOM_BYTES / 1e9:.0f} GB of headroom, and {free / 1e9:.1f} GB is free. "
        "Free up some space and try again."
    )


# ------------------------------------------------------------------ in-flight downloads


class _DownloadInProgress:
    """One in-flight download of a file, shared by every thread that asked for it."""

    def __init__(self) -> None:
        # Set exactly once, by the leader's finally, so waiters can always wake.
        self.finished = threading.Event()


_IN_FLIGHT: dict[str, _DownloadInProgress] = {}
_REGISTRY_LOCK = threading.Lock()


def download_in_progress(filename: str) -> bool:
    """Whether any thread is downloading `filename` right now."""
    with _REGISTRY_LOCK:
        return filename in _IN_FLIGHT


# ------------------------------------------------------------------ the download


Progress = Callable[[str], None]


def ensure_weight(spec: ModelFile, *, progress: Progress | None = None) -> Path:
    """Return where `spec` lives, downloading it on first use if it is absent.

    Idempotent: a present file is returned as-is. Process-wide thread-safe: concurrent
    callers share one download - the first to arrive starts it and the rest wait for its
    outcome - so a burst of first-use requests never starts duplicate downloads. A failed
    download leaves nothing behind that a later check could mistake for installed, and
    the next caller retries.

    The download is atomic end to end. `hf_hub_download` writes to a process-unique
    temporary file in the models directory and moves it into place only when complete,
    verifying the transfer against the size the hub advertises on the way. An interrupted
    download leaves only its temporary file, whose name can never be mistaken for the
    model, and the next call starts fresh.

    Args:
        spec: Which file, and its ceiling for the disk check.
        progress: Called with one short human-readable line before the download starts;
            `None` means log the line instead.

    Returns:
        The local path of the weights.

    Raises:
        ConfigurationError: The disk cannot take the download, or the download failed.
            The message is written for a user: no path, no command, no stack trace.
    """
    target = spec.path
    if target.exists():
        return target

    while True:
        with _REGISTRY_LOCK:
            entry = _IN_FLIGHT.get(spec.filename)
            if entry is None:
                _IN_FLIGHT[spec.filename] = entry = _DownloadInProgress()
                is_leader = True
            else:
                is_leader = False

        if not is_leader:
            entry.finished.wait()
            if target.exists():
                return target
            # The leader's download failed; this caller makes the next attempt.
            continue

        try:
            _download(spec, target, progress)
        finally:
            with _REGISTRY_LOCK:
                _IN_FLIGHT.pop(spec.filename, None)
            entry.finished.set()
        return target


def _download(spec: ModelFile, target: Path, progress: Progress | None) -> None:
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    require_disk_space(settings.models_dir, spec.download_bytes)
    line = f"Downloading {spec.filename} ..."
    if progress is not None:
        progress(line)
    else:
        logger.info("%s", line)
    try:
        hf_hub_download(
            repo_id=spec.repo_id,
            filename=spec.filename,
            local_dir=settings.models_dir,
        )
    except Exception as exc:
        logger.warning("Download of %s failed: %s", spec.filename, exc)
        raise ConfigurationError(
            f"The {spec.display_name} could not be downloaded right now. "
            "Check your internet connection and try again."
        ) from exc
    if not target.exists():
        raise ConfigurationError(
            f"The {spec.display_name} could not be downloaded right now. "
            "Check your internet connection and try again."
        )
