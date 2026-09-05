"""The bundle staging script installs the runtime the product needs on a clean install.

The contract under test is that staging is not a second download implementation: it must
install exactly the release `scripts/fetch_models.py` pins - one pin, one download - and
only a build the extracted binary proves is that pin (tag and commit) may be staged. A
wrong or republished release asset must fail the build, not land in the bundle.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.fetch_models as fetch_models  # noqa: E402
import scripts.stage_llama_runtime as stage  # noqa: E402


def _stage_success(monkeypatch: pytest.MonkeyPatch, downloads: list[tuple[str, Path]]) -> None:
    """Fakes a one-shot download of the pinned release whose extracted binary reports
    the pin - the shape of a correct release asset."""

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
        fetch_models,
        "reported_build",
        lambda binary: (fetch_models.LLAMA_RELEASE_TAG, fetch_models.LLAMA_COMMIT[:9]),
    )


def test_staging_installs_the_fetched_pin_into_the_resource_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One download, exactly the pinned release, verified as the pin, in the
    `resources/llama` layout the bundle and the backend resolution agree on."""
    downloads: list[tuple[str, Path]] = []
    _stage_success(monkeypatch, downloads)

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
    answers with the pin (tag and commit), and is kept."""
    build = tmp_path / "llama" / f"llama-{fetch_models.LLAMA_RELEASE_TAG}"
    build.mkdir(parents=True)
    (build / "llama-server").write_bytes(b"not a real binary")
    downloads: list[tuple[str, Path]] = []
    _stage_success(monkeypatch, downloads)

    code = stage.main(["--destination", str(tmp_path / "llama")])

    assert code == 0
    assert downloads == []


def test_an_extracted_binary_that_reports_the_wrong_build_fails_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A release asset whose bytes are not the pin must fail the build, not get staged:
    the tag is one half of the identity, and the wrong half is a refusal."""

    def fake_extract(archive: Path, destination: Path) -> None:
        build = destination / f"llama-{fetch_models.LLAMA_RELEASE_TAG}"
        build.mkdir(parents=True, exist_ok=True)
        (build / "llama-server").write_bytes(b"not a real binary")

    downloads: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        fetch_models,
        "_download",
        lambda url, destination: (
            downloads.append((url, destination)),
            destination.write_bytes(b"archive"),
        ),
    )
    monkeypatch.setattr(fetch_models, "_extract", fake_extract)
    monkeypatch.setattr(
        fetch_models,
        "reported_build",
        lambda binary: ("b90000", fetch_models.LLAMA_COMMIT[:9]),
    )

    with pytest.raises(fetch_models.LlamaBuildMismatchError):
        stage.main(["--destination", str(tmp_path / "llama")])


def test_an_extracted_binary_with_the_right_tag_and_the_wrong_commit_fails_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tag alone was the old check, and it was not enough: a build cut from a
    different commit reports the same number's neighbourhood and would still run.
    The commit is the other half of the identity, and it must match too."""

    def fake_extract(archive: Path, destination: Path) -> None:
        build = destination / f"llama-{fetch_models.LLAMA_RELEASE_TAG}"
        build.mkdir(parents=True, exist_ok=True)
        (build / "llama-server").write_bytes(b"not a real binary")

    monkeypatch.setattr(
        fetch_models,
        "_download",
        lambda url, destination: destination.write_bytes(b"archive"),
    )
    monkeypatch.setattr(fetch_models, "_extract", fake_extract)
    monkeypatch.setattr(
        fetch_models,
        "reported_build",
        lambda binary: (fetch_models.LLAMA_RELEASE_TAG, "deadbeef0"),
    )

    with pytest.raises(fetch_models.LlamaBuildMismatchError):
        stage.main(["--destination", str(tmp_path / "llama")])


def test_an_extracted_binary_that_will_not_report_fails_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A binary that cannot say what it is fails closed: silence is not the pin."""

    def fake_extract(archive: Path, destination: Path) -> None:
        build = destination / f"llama-{fetch_models.LLAMA_RELEASE_TAG}"
        build.mkdir(parents=True, exist_ok=True)
        (build / "llama-server").write_bytes(b"not a real binary")

    monkeypatch.setattr(
        fetch_models, "_download", lambda url, destination: destination.write_bytes(b"archive")
    )
    monkeypatch.setattr(fetch_models, "_extract", fake_extract)
    monkeypatch.setattr(fetch_models, "reported_build", lambda binary: None)

    with pytest.raises(fetch_models.LlamaBuildMismatchError):
        stage.main(["--destination", str(tmp_path / "llama")])


def test_the_ordinary_developer_fetch_refuses_a_build_that_is_not_the_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same gate, not a second one: the checkout fetch and the bundle staging share
    `fetch_llama_server`, so a poisoned asset is refused for a developer too, not only
    when it would reach a bundle."""

    def fake_extract(archive: Path, destination: Path) -> None:
        build = destination / f"llama-{fetch_models.LLAMA_RELEASE_TAG}"
        build.mkdir(parents=True, exist_ok=True)
        (build / "llama-server").write_bytes(b"not a real binary")

    monkeypatch.setattr(
        fetch_models,
        "_download",
        lambda url, destination: destination.write_bytes(b"archive"),
    )
    monkeypatch.setattr(fetch_models, "_extract", fake_extract)
    monkeypatch.setattr(
        fetch_models,
        "reported_build",
        lambda binary: ("b90000", "000000000"),
    )

    with pytest.raises(fetch_models.LlamaBuildMismatchError):
        fetch_models.fetch_llama_server(target_dir=tmp_path / "llama")


def test_an_existing_build_that_is_not_the_pin_is_replaced_not_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A build left behind by an older or interrupted run is asked the same question on
    re-run: if it is not the pin, the fetch downloads the pin again instead of trusting
    the directory name."""
    build = tmp_path / "llama" / f"llama-{fetch_models.LLAMA_RELEASE_TAG}"
    build.mkdir(parents=True)
    (build / "llama-server").write_bytes(b"not a real binary")

    downloads: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        fetch_models,
        "_download",
        lambda url, destination: (
            downloads.append((url, destination)),
            destination.write_bytes(b"archive"),
        ),
    )
    # The first answer is the impostor's (the existing binary); the mismatch notice asks
    # it once more; after the re-download, the extracted one is the genuine pin.
    impostor = ("b90000", fetch_models.LLAMA_COMMIT[:9])
    pin = (fetch_models.LLAMA_RELEASE_TAG, fetch_models.LLAMA_COMMIT[:9])
    answers = [impostor, impostor, pin]
    monkeypatch.setattr(
        fetch_models, "reported_build", lambda binary: answers.pop(0)
    )

    def fake_extract(archive: Path, destination: Path) -> None:
        build = destination / f"llama-{fetch_models.LLAMA_RELEASE_TAG}"
        build.mkdir(parents=True, exist_ok=True)
        (build / "llama-server").write_bytes(b"not a real binary")

    monkeypatch.setattr(fetch_models, "_extract", fake_extract)

    binary = fetch_models.fetch_llama_server(target_dir=tmp_path / "llama")

    assert len(downloads) == 1
    assert binary == build / "llama-server"
