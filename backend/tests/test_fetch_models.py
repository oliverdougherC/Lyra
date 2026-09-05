"""The release archive is pinned by digest, and the pin is enforced before extraction.

`_download` hashes the bytes while they stream into the temporary `.part` file, and only
an archive whose SHA-256 matches the fixed mapping for the pinned release is promoted to
its final name. A tampered or corrupted download is deleted and refused before anything
is extracted - so the runtime layer of verification (`verify_llama_build`) never runs on
bytes that were never the pin - and a failure leaves no completed archive that a later
run can mistake for the real thing.
"""

import hashlib
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.fetch_models as fetch_models  # noqa: E402

_ASSET = f"llama-{fetch_models.LLAMA_RELEASE_TAG}-bin-macos-arm64.tar.gz"
_URL = f"{fetch_models.RELEASE_BASE_URL}/{fetch_models.LLAMA_RELEASE_TAG}/{_ASSET}"


class _FakeResponse:
    """A stand-in for the response `httpx.stream` yields: it emits exactly `chunks`."""

    def __init__(self, chunks: Sequence[bytes]) -> None:
        self._chunks = list(chunks)

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, _size: int) -> Iterator[bytes]:
        yield from self._chunks

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _fake_stream(chunks: Sequence[bytes], url: str = _URL) -> Callable[..., _FakeResponse]:
    """A `fetch_models.httpx.stream` replacement whose transfer yields `chunks`."""

    def _stream(method: str, target: str, **kwargs) -> _FakeResponse:
        assert method == "GET"
        assert target == url
        return _FakeResponse(chunks)

    return _stream


def test_every_supported_asset_has_a_valid_pinned_sha256() -> None:
    """The mapping covers exactly the six assets `ASSET_SUFFIXES` resolves, and every
    entry is a well-formed 64-character SHA-256. A missing or malformed pin would make
    the fetch fail closed as an unpinned download, so the shape is pinned too."""
    assert set(fetch_models.ASSET_SHA256) == set(fetch_models.ASSET_SUFFIXES.values())
    assert len(fetch_models.ASSET_SHA256) == 6
    for suffix, digest in fetch_models.ASSET_SHA256.items():
        assert re.fullmatch(r"[0-9a-f]{64}", digest), suffix


def test_a_correct_digest_streams_and_promotes_the_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The digest is computed while the bytes stream - across chunk boundaries - and a
    match promotes `.part` to the final archive name, leaving no partial behind."""
    content = b"llama release archive\n" * 512  # 10 KiB, split mid-line
    destination = tmp_path / _ASSET
    monkeypatch.setattr(fetch_models.httpx, "stream", _fake_stream([content[:777], content[777:]]))

    fetch_models._download(_URL, destination, hashlib.sha256(content).hexdigest())

    assert destination.read_bytes() == content
    assert not destination.with_name(destination.name + ".part").exists()


def test_a_one_byte_corruption_fails_before_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One flipped byte changes the digest: the download raises, the extraction step
    never runs, and neither the final archive nor the partial survives."""
    content = b"llama release archive\n" * 512
    corrupted = bytearray(content)
    corrupted[len(corrupted) // 2] ^= 0x01
    destination = tmp_path / _ASSET
    extracted: list[Path] = []
    monkeypatch.setattr(fetch_models.httpx, "stream", _fake_stream([bytes(corrupted)]))
    monkeypatch.setattr(fetch_models, "_extract", lambda archive, where: extracted.append(archive))

    with pytest.raises(fetch_models.LlamaChecksumMismatchError):
        fetch_models._download(_URL, destination, hashlib.sha256(content).hexdigest())

    assert extracted == []
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()


def test_the_fetch_path_refuses_a_poisoned_archive_before_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end, through the developer fetch: the real `_download` runs against the
    pinned mapping, a tampered transfer fails the fetch, and extraction never runs."""
    monkeypatch.setattr(fetch_models, "resolve_asset_suffix", lambda: "macos-arm64.tar.gz")
    destination = tmp_path / "llama" / _ASSET
    extracted: list[Path] = []
    monkeypatch.setattr(fetch_models.httpx, "stream", _fake_stream([b"not the pinned bytes"]))
    monkeypatch.setattr(fetch_models, "_extract", lambda archive, where: extracted.append(archive))

    with pytest.raises(fetch_models.LlamaChecksumMismatchError):
        fetch_models.fetch_llama_server(target_dir=tmp_path / "llama")

    assert extracted == []
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".part").exists()


def test_a_failed_digest_leaves_nothing_a_later_run_can_trust(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed digest deletes both the would-be archive and the `.part`: a later run
    starts clean, and a correct download still lands under the final name."""
    content = b"the genuine pinned bytes"
    corrupted = content[:-1] + b"X"
    expected = hashlib.sha256(content).hexdigest()
    destination = tmp_path / "archive.tar.gz"
    monkeypatch.setattr(fetch_models.httpx, "stream", _fake_stream([corrupted], _URL))

    with pytest.raises(fetch_models.LlamaChecksumMismatchError):
        fetch_models._download(_URL, destination, expected)

    assert not destination.exists()
    assert not (tmp_path / "archive.tar.gz.part").exists()

    monkeypatch.setattr(fetch_models.httpx, "stream", _fake_stream([content], _URL))
    fetch_models._download(_URL, destination, expected)
    assert destination.read_bytes() == content


def test_an_interrupted_transfer_deletes_the_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transfer that dies mid-stream is not a completed download: the partial file
    is removed, and the transport error is what the caller sees."""

    class _BrokenResponse(_FakeResponse):
        def iter_bytes(self, _size: int) -> Iterator[bytes]:
            yield b"half of the archive"
            raise ConnectionError("transfer reset")

    monkeypatch.setattr(
        fetch_models.httpx,
        "stream",
        lambda method, url, **kwargs: _BrokenResponse([]),
    )
    destination = tmp_path / "archive.tar.gz"

    with pytest.raises(ConnectionError, match="transfer reset"):
        fetch_models._download(_URL, destination, "0" * 64)

    assert not destination.exists()
    assert not (tmp_path / "archive.tar.gz.part").exists()
