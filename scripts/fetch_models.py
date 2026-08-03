"""Download the local embedding runtime: the llama.cpp server and the nomic GGUF weights.

Run once after installing the backend:

    python scripts/fetch_models.py

Both downloads are skipped when their target already exists, so re-running is cheap.
"""

import platform
import sys
import tarfile
import zipfile
from pathlib import Path

import httpx
from huggingface_hub import hf_hub_download

from backend.config import settings
from backend.core.errors import ConfigurationError

# Pinned deliberately. A floating `latest` would change the asset list under us; see the
# llama.cpp binaries section of the scaffold plan before moving this.
LLAMA_RELEASE_TAG = "b10235"
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

EMBEDDING_REPO_ID = "nomic-ai/nomic-embed-text-v1.5-GGUF"
EMBEDDING_FILENAME = "nomic-embed-text-v1.5.Q8_0.gguf"

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


def fetch_llama_server() -> Path:
    """Download and extract the pinned llama.cpp release, unless it is already present."""
    existing = find_llama_server(settings.llama_dir)
    if existing is not None:
        print("llama-server already present, skipping download.")
        return existing

    asset = f"llama-{LLAMA_RELEASE_TAG}-bin-{resolve_asset_suffix()}"
    settings.llama_dir.mkdir(parents=True, exist_ok=True)
    archive = settings.llama_dir / asset

    print(f"Downloading {asset} ...")
    _download(f"{RELEASE_BASE_URL}/{LLAMA_RELEASE_TAG}/{asset}", archive)
    print(f"Extracting {asset} ...")
    _extract(archive, settings.llama_dir)
    archive.unlink()

    # The archive layout differs per platform, so the binary is found, not assumed.
    binary = find_llama_server(settings.llama_dir)
    if binary is None:
        raise ConfigurationError(f"The archive {asset} did not contain a llama-server binary.")
    binary.chmod(binary.stat().st_mode | 0o111)
    return binary


def fetch_embedding_weights() -> Path:
    """Download the nomic GGUF weights, unless they are already present."""
    target = settings.embedding_model_path
    if target.exists():
        print("Embedding weights already present, skipping download.")
        return target

    settings.models_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {EMBEDDING_FILENAME} ...")
    downloaded = hf_hub_download(
        repo_id=EMBEDDING_REPO_ID,
        filename=EMBEDDING_FILENAME,
        local_dir=settings.models_dir,
    )
    return Path(str(downloaded))


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


def main() -> None:
    """Fetch both artifacts and print where they landed."""
    settings.ensure_directories()
    server_path = fetch_llama_server()
    weights_path = fetch_embedding_weights()
    print()
    print(f"llama-server:      {server_path.resolve()}")
    print(f"embedding weights: {weights_path.resolve()}")


if __name__ == "__main__":
    main()
