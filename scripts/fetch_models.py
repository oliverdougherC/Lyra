"""Download the local runtimes: the llama.cpp server, the nomic GGUF weights, and OCR.

Run once after installing the backend:

    python scripts/fetch_models.py

Every download is skipped when its target already exists and identifies as the pin, so
re-running is cheap - and a target that does not identify as the pin is replaced, because
a directory name is a label, not a guarantee of what the bytes inside are.

The specialist OCR weights are **not** included in that. They are 2.8 GB, they are only
worth having for bulk transcription of long scanned documents, and text recognition works
without them through the configured vision model. Ask for them explicitly:

    python scripts/fetch_models.py --ocr
"""

import argparse
import hashlib
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import httpx
from huggingface_hub import hf_hub_download

from backend.config import settings
from backend.core.errors import ConfigurationError
from backend.llm.model_provisioning import (
    EMBEDDING_WEIGHTS,
    OCR_DOWNLOAD_BYTES,
    OCR_FILENAMES,
    OCR_REPO_ID,
    RERANK_WEIGHTS,
    ensure_weight,
    require_disk_space,
)

# Pinned deliberately, and now pinned to a commit as well as a tag.
#
# A tag alone was not enough. llama.cpp cuts a release per master commit, so a tag names a
# build rather than a state of the source, and this model's support surface is actively
# changing: `max_tiles` for Unlimited-OCR was wrong until #25614, which merged on
# 2026-08-05 as commit b06aa77. That commit is exactly tag b10287, and without it the
# projector's `preproc_max_tiles = 32` is ignored in favour of DeepSeek-OCR v1's 9, so tall
# or dense pages are tiled more coarsely than the reference and read less accurately.
#
# The commit is recorded so that a future reader can tell what is in this build rather than
# having to resolve a tag that may be retagged or deleted. Verify with:
#   gh api repos/ggml-org/llama.cpp/compare/{LLAMA_COMMIT}...{LLAMA_RELEASE_TAG}
LLAMA_RELEASE_TAG = "b10287"
LLAMA_COMMIT = "b06aa774c03dbbb624e726664b714a57d1f49815"
RELEASE_BASE_URL = "https://github.com/ggml-org/llama.cpp/releases/download"

# (sys.platform, platform.machine()) -> release asset suffix, verified against the asset
# list of the pinned release. Anything absent is an error, never a guess.
ASSET_SUFFIXES: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "macos-arm64.tar.gz",
    ("darwin", "x86_64"): "macos-x64.tar.gz",
    ("linux", "x86_64"): "ubuntu-x64.tar.gz",
    ("linux", "aarch64"): "ubuntu-arm64.tar.gz",
    ("win32", "AMD64"): "win-cpu-x64.zip",
    ("win32", "ARM64"): "win-cpu-arm64.zip",
}

# SHA-256 digest of every release asset Lyra supports, published by GitHub for the
# pinned release (https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/b10287,
# which records one `digest` per uploaded asset). A download is verified against these
# while it streams, before anything is extracted: a tampered or republished archive is
# refused at the bytes level, and the binary's own `--version` check stays as a second,
# independent layer. Keyed by the same suffix as `ASSET_SUFFIXES`, so an asset without a
# pinned digest is an error, never a guess.
ASSET_SHA256: dict[str, str] = {
    "macos-arm64.tar.gz": "32b99f35f7cf9f9bdb59ad5a5ae692015b92da5ceae64d39e35fd52a53bdbac6",
    "macos-x64.tar.gz": "c0d354be02ab7eedb7a88fc06f09b375218344f96d0e2f15f7d08270dabf5bc8",
    "ubuntu-arm64.tar.gz": "f0fa1f3f228b89c2692b90ac4691b7cd08948274b15dfb2c9b1f3825d4b04960",
    "ubuntu-x64.tar.gz": "901998eed6165efcc4d584e3e8da4366a18860869ef91d376a6783730504e4c8",
    "win-cpu-arm64.zip": "b35783d9052efc272c336117ffb131a1ea7697b1c0e4f7ad587a1c1bf0b7d449",
    "win-cpu-x64.zip": "6ae960c04bcc3b9f2083f21cbeb3543ee0dd2e1ce7e110e4146bab9b9283fb36",
}

# The weights metadata - repositories, file names, weights, and the download itself -
# lives in `backend/llm/model_provisioning.py`, shared with the embedding server's
# first-use download, so the manual path and the automatic path can never name a
# different file or a different folder.

BINARY_NAMES = ("llama-server", "llama-server.exe")
DOWNLOAD_TIMEOUT_SECONDS = 600.0
_CHUNK_BYTES = 1 << 20


def resolve_asset_suffix() -> str:
    """Map this machine to a llama.cpp release asset suffix.

    Raises:
        ConfigurationError: No prebuilt asset is known for this platform.
    """
    key = (sys.platform, platform.machine())
    suffix = ASSET_SUFFIXES.get(key)
    if suffix is None:
        raise ConfigurationError(
            f"No prebuilt llama.cpp asset is known for platform {key[0]} on {key[1]}. "
            "Build llama.cpp from source and place llama-server under the models "
            "directory instead."
        )
    return suffix


def find_llama_server(root: Path) -> Path | None:
    """Locate the llama-server binary anywhere under `root`."""
    if not root.exists():
        return None
    for name in BINARY_NAMES:
        for candidate in sorted(root.rglob(name)):
            if candidate.is_file():
                return candidate
    return None


def reported_build(binary: Path) -> tuple[str, str] | None:
    """The build a llama.cpp binary reports for itself as `(tag, short commit)`, or None.

    `llama-server --version` prints `version: 10287 (b06aa774c)` to stderr. Asking the
    binary is the only way to know what is installed: the directory it was extracted into
    is named after whatever tag was pinned when it was downloaded, and that name does not
    change when this file's pin moves - the name of the file is a label, not its contents.
    """
    try:
        # S603: `binary` was located under the models directory by this script.
        result = subprocess.run(  # noqa: S603
            [str(binary), "--version"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stderr + result.stdout).decode("utf-8", "replace")
    match = re.search(r"version:\s*(\d+)\s*\(([0-9a-fA-F]+)\)", output)
    return (f"b{match.group(1)}", match.group(2)) if match else None


def installed_build(binary: Path) -> str | None:
    """The build tag a llama.cpp binary reports, or None if it will not say."""
    reported = reported_build(binary)
    return reported[0] if reported is not None else None


class LlamaBuildMismatchError(RuntimeError):
    """An extracted or installed llama-server does not identify as the pinned build.

    Raised only, and always, from `verify_llama_build`. A fetch or staging run that hits
    this has installed nothing usable: the binary must not be run, and the bundle must
    not be shipped.
    """


class LlamaChecksumMismatchError(RuntimeError):
    """A downloaded release archive does not hash to its pinned SHA-256 digest.

    Raised only, and always, from `_download`, before the archive is extracted. The
    partial file has been deleted: there is no completed archive for a later run to
    trust, and the runtime layer of verification never runs on bytes that were never
    the pin.
    """


def verify_llama_build(binary: Path) -> str:
    """Require the binary to identify as the configured pin - tag and commit - or fail.

    This is the second, independent layer of pin enforcement (the first is the
    SHA-256 pin on the archive itself): the archive's name is what the release URL
    said, not what the bytes inside are, and a wrong or republished asset must not
    reach a checkout or a bundle. It fails closed: a binary that will not answer is a
    mismatch, never a warning.

    The binary reports a short commit; the pin records the full one. The comparison is
    case-normalized equality against the pin's first nine commit characters: a prefix
    test is not a check, so a reported commit that is shorter, longer, or different is
    a mismatch, as is any other tag.

    Args:
        binary: The extracted (or installed) llama-server.

    Returns:
        The reported build, e.g. `b10287 (b06aa774c)`.

    Raises:
        LlamaBuildMismatchError: The binary is not the pin.
    """
    reported = reported_build(binary)
    if reported is None:
        raise LlamaBuildMismatchError(
            f"llama-server at {binary} did not report a version, and cannot be treated as "
            f"{LLAMA_RELEASE_TAG}."
        )
    tag, commit = reported
    # Both constants are lowercase, so comparing the normalized halves against them is
    # a case-normalized equality check.
    if tag.lower() != LLAMA_RELEASE_TAG or commit.lower() != LLAMA_COMMIT[:9]:
        raise LlamaBuildMismatchError(
            f"llama-server at {binary} reports {tag} ({commit}); the pin is "
            f"{LLAMA_RELEASE_TAG} ({LLAMA_COMMIT[:9]}). The asset does not match the "
            "configured build and was refused."
        )
    return f"{tag} ({commit})"


def fetch_llama_server(target_dir: Path | None = None) -> Path:
    """Download and extract the pinned llama.cpp release, unless it is already installed.

    "Already present" is not the same as "already correct", and the difference matters
    here: the specialist OCR path needs the `max_tiles` fix that landed in b10287, and an
    older binary reads dense pages at a coarser tile grid without saying so. So the
    existing binary is asked what it is, and replaced when it is not the pin.

    `target_dir` is where the build lands: the models directory for the checkout, or the
    Tauri resource tree when the same pin is being staged into the application bundle -
    one download, one extraction, and one build verification for both.

    A fresh download must pass two independent checks before it is trusted: its
    SHA-256 must match the pinned digest for the asset (checked while it streams,
    before anything is extracted), and the extracted binary must identify itself as
    the pin (tag and commit, via `verify_llama_build`). A wrong or republished release
    asset fails the
    fetch rather than getting installed and run, and the same identity gate decides
    whether an already-present build is skipped or replaced, so a corrupted or swapped
    artifact can neither be staged into the app nor run from a checkout.
    """
    root = settings.llama_dir if target_dir is None else target_dir
    existing = find_llama_server(root)
    if existing is not None:
        try:
            verified = verify_llama_build(existing)
        except LlamaBuildMismatchError:
            print(
                f"llama-server reports {installed_build(existing) or 'an unknown build'}, "
                f"and the pin is {LLAMA_RELEASE_TAG} ({LLAMA_COMMIT[:9]}). "
                "Downloading the pinned build."
            )
        else:
            print(f"llama-server {verified} already present, skipping download.")
            return existing

    suffix = resolve_asset_suffix()
    expected_sha256 = ASSET_SHA256.get(suffix)
    if expected_sha256 is None:
        raise ConfigurationError(
            f"No pinned SHA-256 digest is recorded for the release asset {suffix}; "
            "refusing to download an unpinned archive."
        )
    asset = f"llama-{LLAMA_RELEASE_TAG}-bin-{suffix}"
    root.mkdir(parents=True, exist_ok=True)
    _remove_stale_builds(root)
    archive = root / asset

    print(f"Downloading {asset} ...")
    _download(f"{RELEASE_BASE_URL}/{LLAMA_RELEASE_TAG}/{asset}", archive, expected_sha256)
    print(f"Extracting {asset} ...")
    _extract(archive, root)
    archive.unlink()

    # The archive layout differs per platform, so the binary is found, not assumed.
    binary = find_llama_server(root)
    if binary is None:
        raise ConfigurationError(f"The archive {asset} did not contain a llama-server binary.")
    binary.chmod(binary.stat().st_mode | 0o111)
    # The download is only trusted once the extracted binary identifies as the pin:
    # a wrong or republished asset fails the fetch, never the first spawn.
    verify_llama_build(binary)
    return binary


def fetch_embedding_weights() -> Path:
    """Download the nomic GGUF weights, unless they are already present."""
    if EMBEDDING_WEIGHTS.path.exists():
        print("Embedding weights already present, skipping download.")
        return EMBEDDING_WEIGHTS.path
    return ensure_weight(EMBEDDING_WEIGHTS, progress=print)


def fetch_rerank_weights() -> Path:
    """Download the reranker GGUF, unless it is already present."""
    if RERANK_WEIGHTS.path.exists():
        print("Reranker weights already present, skipping download.")
        return RERANK_WEIGHTS.path
    return ensure_weight(RERANK_WEIGHTS, progress=print)


def fetch_ocr_weights() -> list[Path]:
    """Download the Unlimited-OCR pair, unless it is already present.

    Refuses before writing anything if the disk cannot take it. Checking afterwards is not
    a check: a download that fills the disk leaves a partial file behind and reports
    whatever the filesystem said, which is not a sentence anyone can act on.

    Returns:
        Where both files landed, in the order they were fetched.

    Raises:
        ConfigurationError: There is not enough free space.
    """
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    missing = [name for name in OCR_FILENAMES if not (settings.models_dir / name).exists()]
    if not missing:
        print("OCR weights already present, skipping download.")
        return [settings.models_dir / name for name in OCR_FILENAMES]

    require_disk_space(settings.models_dir, OCR_DOWNLOAD_BYTES)
    print(
        f"Downloading {len(missing)} OCR file(s), about "
        f"{OCR_DOWNLOAD_BYTES / 1e9:.1f} GB. This is the specialist text-recognition path; "
        "Lyra reads scanned pages without it."
    )
    landed = []
    for name in OCR_FILENAMES:
        target = settings.models_dir / name
        if target.exists():
            landed.append(target)
            continue
        print(f"Downloading {name} ...")
        landed.append(
            Path(
                str(
                    hf_hub_download(
                        repo_id=OCR_REPO_ID, filename=name, local_dir=settings.models_dir
                    )
                )
            )
        )
    return landed


def _remove_stale_builds(root: Path | None = None) -> None:
    """Delete previously extracted builds that are not the pin.

    Exactly one build is kept, and that is the point rather than tidiness. Every consumer
    finds the binary by walking the llama directory and taking the first match, so leaving
    an old extraction next to a new one means the old one keeps being used: `llama-b10235`
    sorts before `llama-b10287`, so downloading the pinned build changed nothing at all
    until this ran. Only directories this script created are touched.
    """
    llama_root = settings.llama_dir if root is None else root
    for existing in sorted(llama_root.glob("llama-b*")):
        if not existing.is_dir() or existing.name == f"llama-{LLAMA_RELEASE_TAG}":
            continue
        if find_llama_server(existing) is None:
            continue
        print(f"Removing the previously pinned build {existing.name} ...")
        shutil.rmtree(existing)


def _download(url: str, destination: Path, expected_sha256: str) -> None:
    """Stream a URL to disk, promoting it only once its SHA-256 matches the pin.

    The digest is computed incrementally while the bytes stream into the temporary
    `.part` file, so a tampered or corrupted archive is detected without a second pass
    over the data, and an unverified archive is never written under its final name:
    neither extracted nor trusted by a later run.

    Raises:
        LlamaChecksumMismatchError: The downloaded bytes do not hash to
            `expected_sha256`. The partial file is deleted first.
    """
    partial = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    try:
        with httpx.stream(
            "GET", url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            response.raise_for_status()
            with partial.open("wb") as handle:
                for block in response.iter_bytes(_CHUNK_BYTES):
                    digest.update(block)
                    handle.write(block)
    except BaseException:
        # An interrupted transfer is not a completed download: nothing may survive.
        partial.unlink(missing_ok=True)
        raise
    actual = digest.hexdigest()
    if actual != expected_sha256.lower():
        partial.unlink(missing_ok=True)
        raise LlamaChecksumMismatchError(
            f"SHA-256 of the downloaded {destination.name} is {actual}, but the pinned "
            f"digest is {expected_sha256.lower()}. The partial file was deleted and the "
            "archive was refused before extraction."
        )
    partial.replace(destination)


def _extract(archive: Path, destination: Path) -> None:
    """Extract a .tar.gz or .zip release archive."""
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(destination)  # noqa: S202 - trusted pinned release archive
        return
    with tarfile.open(archive) as bundle:
        # filter="data" rejects absolute paths, parent traversal, and device entries.
        bundle.extractall(destination, filter="data")


def main(argv: list[str] | None = None) -> None:
    """Fetch what Lyra needs, and print where it landed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="Also download the specialist OCR weights (about 2.8 GB, optional)",
    )
    args = parser.parse_args(argv)

    settings.ensure_directories()
    server_path = fetch_llama_server()
    weights_path = fetch_embedding_weights()
    rerank_path = fetch_rerank_weights()
    if args.ocr:
        fetch_ocr_weights()
    print()
    print(f"llama-server:      {server_path.resolve()}  ({installed_build(server_path)})")
    print(f"embedding weights: {weights_path.resolve()}")
    print(f"reranker weights:  {rerank_path.resolve()}")
    # Reported from the disk rather than from the flag: a run without `--ocr` on a machine
    # that already has the weights was telling the student they were missing.
    if settings.ocr_installed:
        print(f"OCR weights:       {settings.ocr_model_path.resolve()}")
        print(f"OCR projector:     {settings.ocr_mmproj_path.resolve()}")
    else:
        print()
        print("Specialist OCR weights not installed. Scanned pages are read through the")
        print("model configured in Settings. Add them with: fetch_models.py --ocr")


if __name__ == "__main__":
    main()
