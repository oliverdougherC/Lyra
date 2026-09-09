"""Streaming migration backup (PLA-514): bounded copy and hashing, verified against a
changing or hostile source, with failure cleanup and a bounded working set."""

import contextlib
import hashlib
import json
import os
import sqlite3
import tracemalloc
from pathlib import Path

import pytest

from backend.config import settings
from backend.storage import database, private

MiB = 1024 * 1024


def _snapshot_dir() -> Path:
    return settings.db_path.parent / "migration-backups"


def _assert_backups_removed() -> None:
    """A failed backup must leave no snapshot and no staging partial behind."""
    backups = _snapshot_dir()
    if not backups.exists():
        return
    leftovers = [path for path in backups.rglob("*") if path.name.endswith(".partial")]
    assert not leftovers, f"staging partials survived a failed backup: {leftovers}"
    assert not any(backups.iterdir()), "a failed backup left a snapshot behind"


def test_streamed_backup_verifies_large_sources(db: sqlite3.Connection) -> None:
    payload_a = os.urandom(4 * MiB)
    payload_b = os.urandom(2 * MiB)
    original = settings.uploads_dir / "deck.pdf"
    nested = settings.text_dir / "units" / "lecture.md"
    nested.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(payload_a)
    nested.write_bytes(payload_b)

    database._backup_before_migration(db, database.latest_schema_version())

    snapshot = next(_snapshot_dir().iterdir())
    manifest = json.loads((snapshot / "backup-manifest.json").read_text())
    assert manifest["files"]["uploads/deck.pdf"] == hashlib.sha256(payload_a).hexdigest()
    assert manifest["files"]["text/units/lecture.md"] == hashlib.sha256(payload_b).hexdigest()
    # The saved copies are the exact bytes, and they stay private from the first byte.
    assert (snapshot / "uploads/deck.pdf").read_bytes() == payload_a
    assert (snapshot / "text/units/lecture.md").read_bytes() == payload_b
    assert (snapshot / "uploads/deck.pdf").stat().st_mode & 0o777 == 0o600
    assert (snapshot / "text/units/lecture.md").stat().st_mode & 0o777 == 0o600


def test_backup_stays_correct_under_forced_short_reads(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tiny chunks exercise every boundary of the copy/hash loops: a partial final read,
    # a loop that runs hundreds of times, and the ceiling check on each landing chunk.
    monkeypatch.setattr(private, "STREAM_CHUNK_BYTES", 96)
    payload = os.urandom(256 * 1024)
    (settings.uploads_dir / "notes.pdf").write_bytes(payload)

    database._backup_before_migration(db, database.latest_schema_version())

    snapshot = next(_snapshot_dir().iterdir())
    assert (snapshot / "uploads/notes.pdf").read_bytes() == payload


def test_stream_publish_copies_sources_that_short_read(
    tmp_path: Path,
) -> None:
    data = os.urandom(37)

    class ShortSource:
        def __init__(self, content: bytes) -> None:
            self._content = content
            self._position = 0

        def read(self, size: int) -> bytes:
            # Always returns less than asked, like a stream mid-read.
            chunk = self._content[self._position : self._position + 3]
            self._position += len(chunk)
            return chunk

    destination = tmp_path / "out.bin"
    total = private.publish_private_stream(destination, ShortSource(data), max_bytes=1024)
    assert total == len(data)
    assert destination.read_bytes() == data


def test_oversized_source_aborts_backup_and_cleans_up(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A source that has grown past the ceiling is refused while streaming: the read
    # aborts before the copy begins, the snapshot is removed, and nothing is left open.
    monkeypatch.setattr(database, "_BACKUP_FILE_MAX_BYTES", 1024)
    (settings.uploads_dir / "too-big.pdf").write_bytes(os.urandom(2048))

    with pytest.raises(ValueError, match="exceeds the"):
        database._backup_before_migration(db, database.latest_schema_version())
    _assert_backups_removed()


def test_mid_copy_failure_removes_snapshot_and_partials(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first open_owned_bytes call per file is the pre-copy digest; the second is the
    # copy. Fail the copy's second read, like a disk error mid-stream.
    (settings.uploads_dir / "video.pdf").write_bytes(os.urandom(64 * 1024))
    real_open = private.open_owned_bytes
    opens = {"count": 0}

    @contextlib.contextmanager
    def flaky(path: Path, *, root: Path, max_bytes: int):
        opens["count"] += 1
        with real_open(path, root=root, max_bytes=max_bytes) as reader:
            if opens["count"] == 1:
                yield reader
                return

            class FailingRead:
                def __init__(self) -> None:
                    self.calls = 0

                def read(self, size: int) -> bytes:
                    self.calls += 1
                    if self.calls > 1:
                        raise OSError("simulated copy failure")
                    return reader.read(size)

            yield FailingRead()

    monkeypatch.setattr(private, "open_owned_bytes", flaky)
    with pytest.raises(OSError, match="simulated copy failure"):
        database._backup_before_migration(db, database.latest_schema_version())
    _assert_backups_removed()


def test_replaced_source_fails_verification_and_cleans_up(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The digest pins the original; a source swapped between the digest and the copy must
    # be caught by comparing the saved copy against the pinned digest.
    original = settings.uploads_dir / "swapped.pdf"
    replacement = settings.uploads_dir / "swap-src.bin"
    original.write_bytes(os.urandom(4096))
    replacement.write_bytes(os.urandom(4096))
    real_open = private.open_owned_bytes
    opens = {"count": 0}

    @contextlib.contextmanager
    def swapping(path: Path, *, root: Path, max_bytes: int):
        opens["count"] += 1
        if opens["count"] == 2 and path == original:
            with real_open(replacement, root=root, max_bytes=max_bytes) as reader:
                yield reader
            return
        with real_open(path, root=root, max_bytes=max_bytes) as reader:
            yield reader

    monkeypatch.setattr(private, "open_owned_bytes", swapping)
    with pytest.raises(RuntimeError, match="Backup file verification failed"):
        database._backup_before_migration(db, database.latest_schema_version())
    _assert_backups_removed()


def test_symlinked_source_is_refused_and_snapshot_removed(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    # A symlinked file is a path out of the data tree: it is refused before a single byte
    # is read, the upgrade stops, and the in-progress snapshot is removed.
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside the data tree")
    os.symlink(outside, settings.uploads_dir / "linked.pdf")

    with pytest.raises(private.PrivacyContractError, match="symlink"):
        database._backup_before_migration(db, database.latest_schema_version())
    _assert_backups_removed()
    assert outside.read_bytes() == b"outside the data tree"


def test_open_owned_bytes_refuses_paths_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "inside.bin").write_bytes(b"1234")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"5678")

    with (
        pytest.raises(ValueError, match="not within"),
        private.open_owned_bytes(outside, root=root, max_bytes=100),
    ):
        pass


def test_owned_reader_enforces_ceiling_while_streaming(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    growing = root / "growing.bin"
    growing.write_bytes(b"x" * 100)

    with (
        pytest.raises(ValueError, match="exceeds the"),
        private.open_owned_bytes(growing, root=root, max_bytes=10) as reader,
    ):
        reader.read(64)
    # A source within the ceiling streams to EOF and then stops.
    small = root / "small.bin"
    small.write_bytes(b"y" * 8)
    with private.open_owned_bytes(small, root=root, max_bytes=10) as reader:
        assert reader.read(64) == b"y" * 8
        assert reader.read(64) == b""


def test_hash_file_streams_large_content(tmp_path: Path) -> None:
    data = os.urandom(4 * MiB)
    path = tmp_path / "blob.bin"
    path.write_bytes(data)
    assert private.hash_file(path, chunk_size=4096) == hashlib.sha256(data).hexdigest()


def test_hash_helpers_reject_invalid_chunk_sizes(tmp_path: Path) -> None:
    # chunk 0 would read nothing and report the digest of an empty file; a negative
    # size would pull the whole file in one allocation. Both silently undo the bounded
    # working set, so both are refused before a single byte is read.
    root = tmp_path / "root"
    root.mkdir()
    (root / "file.bin").write_bytes(b"123456789")
    for chunk_size in (0, -1):
        with pytest.raises(ValueError, match="positive"):
            private.hash_owned_file(
                root / "file.bin", root=root, max_bytes=100, chunk_size=chunk_size
            )
        with pytest.raises(ValueError, match="positive"):
            private.hash_file(root / "file.bin", chunk_size=chunk_size)


def test_hash_file_refuses_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.bin"
    real.write_bytes(b"1234")
    link = tmp_path / "link.bin"
    os.symlink(real, link)
    with pytest.raises(private.PrivacyContractError, match="symlink"):
        private.hash_file(link)


def test_backup_working_set_stays_bounded(db: sqlite3.Connection) -> None:
    # The pre-optimization working set preallocated the 512 MiB read buffer per file
    # (~553 MiB peak for this fixture). Streaming must keep the peak a small fraction of
    # the data it carries.
    size = 8 * MiB
    (settings.uploads_dir / "large-a.pdf").write_bytes(os.urandom(size))
    (settings.text_dir / "large-b.md").write_bytes(os.urandom(size))

    tracemalloc.start()
    try:
        database._backup_before_migration(db, database.latest_schema_version())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 2 * size // 2
    assert peak < 50 * MiB
