"""Download the local runtimes: the llama.cpp server, the nomic GGUF weights, and OCR.

Run once after installing the backend:

    python scripts/fetch_models.py

Every download is skipped when its target already exists, so re-running is cheap.

The specialist OCR weights are **not** included in that. They are 2.8 GB, they are only
worth having for bulk transcription of long scanned documents, and text recognition works
without them through the configured vision model. Ask for them explicitly:

    python scripts/fetch_models.py --ocr
"""

import argparse
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


def installed_build(binary: Path) -> str | None:
    """The build number a llama.cpp binary reports, or None if it will not say.

    `llama-server --version` prints `version: 10287 (b06aa774c)` to stderr. Asking the
    binary is the only way to know what is installed: the directory it was extracted into
    is named after whatever tag was pinned when it was downloaded, and that name does not
    change when this file's pin moves.
    """
    try:
        # S603: `binary` was located under the models directory by this script.
        result = subprocess.run(  # noqa: S603
            [str(binary), "--version"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stderr + result.stdout).decode("utf-8", "replace")
    match = re.search(r"version:\s*(\d+)", output)
    return f"b{match.group(1)}" if match else None


def fetch_llama_server(target_dir: Path | None = None) -> Path:
    """Download and extract the pinned llama.cpp release, unless it is already installed.

    "Already present" is not the same as "already correct", and the difference matters
    here: the specialist OCR path needs the `max_tiles` fix that landed in b10287, and an
    older binary reads dense pages at a coarser tile grid without saying so. So the
    existing binary is asked what it is, and replaced when it is not the pin.

    `target_dir` is where the build lands: the models directory for the checkout, or the
    Tauri resource tree when the same pin is being staged into the application bundle -
    one download, one extraction, and one build verification for both.
    """
    root = settings.llama_dir if target_dir is None else target_dir
    existing = find_llama_server(root)
    if existing is not None:
        found = installed_build(existing)
        if found == LLAMA_RELEASE_TAG:
            print(f"llama-server {found} already present, skipping download.")
            return existing
        print(
            f"llama-server reports {found or 'an unknown build'}, and the pin is "
            f"{LLAMA_RELEASE_TAG}. Downloading the pinned build."
        )

    asset = f"llama-{LLAMA_RELEASE_TAG}-bin-{resolve_asset_suffix()}"
    root.mkdir(parents=True, exist_ok=True)
    _remove_stale_builds(root)
    archive = root / asset

    print(f"Downloading {asset} ...")
    _download(f"{RELEASE_BASE_URL}/{LLAMA_RELEASE_TAG}/{asset}", archive)
    print(f"Extracting {asset} ...")
    _extract(archive, root)
    archive.unlink()

    # The archive layout differs per platform, so the binary is found, not assumed.
    binary = find_llama_server(root)
    if binary is None:
        raise ConfigurationError(f"The archive {asset} did not contain a llama-server binary.")
    binary.chmod(binary.stat().st_mode | 0o111)
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


def _download(url: str, destination: Path) -> None:
    """Stream a URL to disk, moving into place only once it is complete."""
    partial = destination.with_name(destination.name + ".part")
    with httpx.stream(
        "GET", url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS
    ) as response:
        response.raise_for_status()
        with partial.open("wb") as handle:
            for block in response.iter_bytes(_CHUNK_BYTES):
                handle.write(block)
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
