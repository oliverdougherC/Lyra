"""The bundle staging script installs the runtime the product needs on a clean install.

The contract under test is that staging is not a second download implementation: it must
install exactly the release `scripts/fetch_models.py` pins - one pin, one download, one
build verification - and land it where the Tauri resource tree expects it, where the
backend will resolve it at first use.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.fetch_models as fetch_models  # noqa: E402
import scripts.stage_llama_runtime as stage  # noqa: E402


def test_staging_installs_the_fetched_pin_into_the_resource_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One download, exactly the pinned release, verified as the pin, in the
    `resources/llama` layout the bundle and the backend resolution agree on."""
    downloads: list[tuple[str, Path]] = []

    def fake_download(url: str, destination: Path) -> None:
        downloads.append((url, destination))
        destination.write_bytes(b"archive")

    def fake_extract(archive: Path, destination: Path) -> None:
        build = destination / f"llama-{fetch_models.LLAMA_RELEASE_TAG}"
        build.mkdir(parents=True, exist_ok=True)
        (build / "llama-server").write_bytes(b"not a real binary")

    monkeypatch.setattr(fetch_models, "_download", fake_download)
    monkeypatch.setattr(fetch_models, "_extract", fake_extract)
    monkeypatch.setattr(
        fetch_models, "installed_build", lambda binary: fetch_models.LLAMA_RELEASE_TAG
    )

    destination = tmp_path / "resources" / "llama"
    code = stage.main(["--destination", str(destination)])

    assert code == 0
    assert len(downloads) == 1
    url, archive = downloads[0]
    assert url.startswith(fetch_models.RELEASE_BASE_URL)
    assert f"/{fetch_models.LLAMA_RELEASE_TAG}/llama-{fetch_models.LLAMA_RELEASE_TAG}-bin-" in url
    assert archive.parent == destination
    assert (destination / f"llama-{fetch_models.LLAMA_RELEASE_TAG}" / "llama-server").is_file()


def test_staging_is_a_noop_when_the_pin_is_already_staged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A re-run of the build must not re-download: the staged build is asked what it is,
    and the pin answers."""
    build = tmp_path / "llama" / f"llama-{fetch_models.LLAMA_RELEASE_TAG}"
    build.mkdir(parents=True)
    (build / "llama-server").write_bytes(b"not a real binary")
    monkeypatch.setattr(
        fetch_models, "installed_build", lambda binary: fetch_models.LLAMA_RELEASE_TAG
    )
    downloads: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        fetch_models, "_download", lambda url, destination: downloads.append((url, destination))
    )

    code = stage.main(["--destination", str(tmp_path / "llama")])

    assert code == 0
    assert downloads == []
