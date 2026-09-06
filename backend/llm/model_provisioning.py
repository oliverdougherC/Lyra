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

import hashlib
import logging
import os
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from backend.config import settings
from backend.core.errors import ConfigurationError
from backend.storage import private

logger = logging.getLogger(__name__)

# The embedding model. The name in `settings.embedding_model_path` and the file name
# below are one fact said twice; `test_model_provisioning.py` pins them together.
EMBEDDING_REPO_ID = "nomic-ai/nomic-embed-text-v1.5-GGUF"
EMBEDDING_FILENAME = "nomic-embed-text-v1.5.Q8_0.gguf"
# The exact commit of the repository this file is downloaded from - the full SHA, never a
# branch or a tag. A repository's default branch is mutable: a fresh install that downloads
# from `main` receives whatever upstream holds today, and the bytes of a model file are the
# model. This commit was the one the weights were downloaded, verified (146,146,432 bytes),
# and run from on this feature, and the file is unchanged there. Immutable by construction:
# a commit SHA in a git repository is a content address and cannot be moved.
EMBEDDING_REVISION = "0188c9bf409793f810680a5a431e7b899c46104c"
# Size and SHA-256 are the Git LFS object identity from the immutable repository tree:
# https://huggingface.co/api/models/nomic-ai/nomic-embed-text-v1.5-GGUF/tree/0188c9bf409793f810680a5a431e7b899c46104c
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
    # Immutable revision (full commit SHA) the file is downloaded from. `None` means
    # "no pin configured" - a file whose bytes are not load-bearing may float; the
    # embedding model, which is, carries one.
    revision: str | None = None
    expected_bytes: int | None = None
    sha256: str | None = None

    @property
    def path(self) -> Path:
        """Where the file lives once installed. `settings` owns the models directory."""
        return settings.models_dir / self.filename


EMBEDDING_WEIGHTS = ModelFile(
    display_name="local embedding model",
    repo_id=EMBEDDING_REPO_ID,
    filename=EMBEDDING_FILENAME,
    download_bytes=EMBEDDING_DOWNLOAD_BYTES,
    revision=EMBEDDING_REVISION,
    expected_bytes=146_146_432,
    sha256="3e24342164b3d94991ba9692fdc0dd08e3fd7362e0aacc396a9a5c54a544c3b7",
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


# One bounded transport attempt is shared by all concurrent callers. A cancelled
# caller does not cancel setup needed by other callers; transport stops within the
# overall deadline plus one bounded network read. No hidden automatic retry loop.
DOWNLOAD_DEADLINE_SECONDS = 600.0
NETWORK_TIMEOUT_SECONDS = 10.0
_CHUNK_BYTES = 1 << 20


class _DownloadInProgress:
    def __init__(self) -> None:
        self.finished = threading.Event()
        self.error: ConfigurationError | None = None


_IN_FLIGHT: dict[str, _DownloadInProgress] = {}
_REGISTRY_LOCK = threading.Lock()
# Rehash after replacement, size/metadata change, or process restart. ctime catches
# edits whose mtime was restored. Bound this cache for callers using disposable roots.
_VERIFIED: dict[tuple[str, str | None], tuple[int, ...]] = {}


def download_in_progress(filename: str) -> bool:
    with _REGISTRY_LOCK:
        return any(Path(path).name == filename for path in _IN_FLIGHT)


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _cache_error() -> ConfigurationError:
    return ConfigurationError(
        "Local model storage is not a readable regular file. "
        "Check the model storage location and its permissions, then try again."
    )


def verified_weight(spec: ModelFile, path: Path | None = None) -> bool:
    """Validate legacy/downloaded weights without following file or directory symlinks."""
    target = path if path is not None else spec.path
    key = (str(target.absolute()), spec.sha256)
    try:
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _cache_error() from exc
    try:
        try:
            descriptor = os.open(
                target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise _cache_error() from exc
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise _cache_error()
        with os.fdopen(descriptor, "rb") as stream:
            if info.st_size <= 0 or (
                spec.expected_bytes is not None and info.st_size != spec.expected_bytes
            ):
                return False
            stamp = _fingerprint(info)
            with _REGISTRY_LOCK:
                cached = _VERIFIED.get(key) == stamp
            if not cached:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(_CHUNK_BYTES), b""):
                    digest.update(chunk)
                if spec.sha256 is not None and digest.hexdigest() != spec.sha256:
                    return False
            # A file changed/replaced while hashing must not become a verified hit.
            current = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
            if _fingerprint(current) != stamp or not stat.S_ISREG(current.st_mode):
                return False
            with _REGISTRY_LOCK:
                if len(_VERIFIED) >= 32:
                    _VERIFIED.pop(next(iter(_VERIFIED)))
                _VERIFIED[key] = stamp
            return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _cache_error() from exc
    finally:
        os.close(directory)


Progress = Callable[[str], None]


def download_description(spec: ModelFile = EMBEDDING_WEIGHTS) -> str:
    size = spec.expected_bytes if spec.expected_bytes is not None else spec.download_bytes
    return f"Downloading the {spec.display_name} ({size / 1e6:.0f} MB) from Hugging Face"


def setup_disclosure() -> str:
    size = EMBEDDING_WEIGHTS.expected_bytes or EMBEDDING_WEIGHTS.download_bytes
    return (
        "The first document task downloads a required local embedding model "
        f"(about {size / 1e6:.0f} MB) "
        "from Hugging Face to Lyra's local model storage. Downloading weights does not upload "
        "course documents. Once cached and verified, local processing works offline; interrupted "
        "downloads can be retried by retrying the task. Optional OCR and reranking weights "
        "are not downloaded "
        "automatically. Your configured remote tutor and Exa have separate data-sharing rules."
    )


def ensure_weight(spec: ModelFile, *, progress: Progress | None = None) -> Path:
    """Return verified weights, sharing one success or failure with concurrent waiters.

    Invalid regular caches are replaced only after the candidate verifies. Unsafe
    entries are rejected. An interrupted/failed attempt leaves the previous file intact;
    a subsequent user request can retry. Optional assets are never requested here unless
    the caller explicitly supplies their spec.
    """
    target = spec.path
    key = str(target.absolute())
    with _REGISTRY_LOCK:
        entry = _IN_FLIGHT.get(key)
        is_leader = entry is None
        if entry is None:
            _IN_FLIGHT[key] = entry = _DownloadInProgress()
    if not is_leader:
        if not entry.finished.wait(DOWNLOAD_DEADLINE_SECONDS + NETWORK_TIMEOUT_SECONDS + 5):
            raise ConfigurationError("Local model setup timed out. Please try again.")
        if entry.error is not None:
            raise ConfigurationError(entry.error.message)
        if not verified_weight(spec):
            raise ConfigurationError("Local model verification failed. Please try again.")
        return target
    try:
        if not verified_weight(spec):
            _download(spec, target, progress)
        return target
    except ConfigurationError as exc:
        entry.error = exc
        raise
    except (OSError, private.PrivacyContractError) as exc:
        entry.error = _cache_error()
        raise entry.error from exc
    finally:
        with _REGISTRY_LOCK:
            _IN_FLIGHT.pop(key, None)
            entry.finished.set()


def _fetch_weight(
    *, repo_id: str, filename: str, local_dir: Path, revision: str | None, max_bytes: int
) -> str:
    """Stream public model bytes without credentials, with bounded time and disk use."""
    url = f"https://huggingface.co/{repo_id}/resolve/{revision or 'main'}/{filename}"
    target = local_dir / filename
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
    total = 0
    with httpx.stream(
        "GET",
        url,
        follow_redirects=True,
        timeout=NETWORK_TIMEOUT_SECONDS,
        headers={"Accept-Encoding": "identity"},
    ) as response:
        response.raise_for_status()
        if response.headers.get("content-encoding", "identity") != "identity":
            raise ValueError("unexpected model transfer encoding")
        with target.open("xb") as stream:
            # Check every received network chunk. Coalescing to 1 MiB can let a
            # slow trickle hide inside the transport's buffering indefinitely.
            for chunk in response.iter_raw():
                if time.monotonic() >= deadline:
                    raise TimeoutError("model download deadline exceeded")
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("model download exceeded the size limit")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    if time.monotonic() >= deadline:
        raise TimeoutError("model download deadline exceeded")
    return str(target)


def _download(spec: ModelFile, target: Path, progress: Progress | None) -> None:
    private.secure_mkdir(settings.models_dir, root=settings.models_dir)
    require_disk_space(settings.models_dir, spec.download_bytes)
    line = download_description(spec)
    if progress is not None:
        progress(line)
    else:
        logger.info("%s", line)
    try:
        # The Hub destination is never the installed path. Only verified bytes may
        # acquire that name; failed transfers cannot poison the next startup.
        with tempfile.TemporaryDirectory(prefix=".lyra-model-", dir=settings.models_dir) as staging:
            stage = Path(staging)
            _fetch_weight(
                repo_id=spec.repo_id,
                filename=spec.filename,
                local_dir=stage,
                revision=spec.revision,
                max_bytes=spec.expected_bytes or spec.download_bytes,
            )
            candidate = stage / spec.filename
            if not verified_weight(spec, candidate):
                raise ConfigurationError(
                    f"The {spec.display_name} download failed verification. Please try again."
                )
            # Do not replace a verified asset another process already published.
            if verified_weight(spec):
                return
            os.replace(candidate, target)
            directory = os.open(settings.models_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except ConfigurationError:
        raise
    except Exception as exc:
        # Do not retain provider URLs, local paths, or raw exception payloads in logs.
        logger.warning("Download of %s failed (%s)", spec.filename, type(exc).__name__)
        raise ConfigurationError(
            f"The {spec.display_name} could not be downloaded right now. "
            "Check your internet connection, free disk space and model-folder permissions, "
            "then try again."
        ) from exc
