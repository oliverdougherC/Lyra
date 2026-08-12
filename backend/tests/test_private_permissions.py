"""The private-permissions contract for Lyra-owned state.

Every creation path runs under a deliberately permissive umask, so what the tests pin down
is that the modes come from the contract in `storage.private` and not from whatever umask
the process happened to inherit. POSIX-only: the modes are meaningless on a filesystem that
does not carry them, which is the same reason `storage.private` treats a failed chmod as a
no-op rather than an error.
"""

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.config import settings
from backend.core import ingestion
from backend.storage import private
from backend.storage.database import connect

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def wide_open_umask() -> Iterator[None]:
    """Run the body with a umask that masks nothing, then restore it."""
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def test_secure_mkdir_creates_owner_only_directories(tmp_path: Path, wide_open_umask: None) -> None:
    created = private.secure_mkdir(tmp_path / "a" / "b" / "c")

    assert _mode(created) == 0o700
    assert _mode(tmp_path / "a") == 0o700
    assert _mode(tmp_path / "a" / "b") == 0o700


def test_secure_mkdir_leaves_a_preexisting_parent_alone(
    tmp_path: Path, wide_open_umask: None
) -> None:
    # A data directory may live inside a folder the user chose to share; only the
    # directories Lyra itself creates are its to harden.
    outer = tmp_path / "outer"
    outer.mkdir(mode=0o755)
    os.chmod(outer, 0o755)  # noqa: S103 - deliberately broad: the user's own parent folder

    private.secure_mkdir(outer / "lyra-data")

    assert _mode(outer) == 0o755
    assert _mode(outer / "lyra-data") == 0o700


def test_write_private_bytes_is_owner_only_from_creation(
    tmp_path: Path, wide_open_umask: None
) -> None:
    path = tmp_path / "secret.bin"

    private.write_private_bytes(path, b"course notes")

    assert path.read_bytes() == b"course notes"
    assert _mode(path) == 0o600


def test_write_private_bytes_tightens_an_existing_broad_file(
    tmp_path: Path, wide_open_umask: None
) -> None:
    path = tmp_path / "was-broad.txt"
    path.write_text("old")
    os.chmod(path, 0o644)

    private.write_private_text(path, "new")

    assert path.read_text() == "new"
    assert _mode(path) == 0o600


def test_harden_data_tree_tightens_the_tree_but_keeps_executables(tmp_path: Path) -> None:
    # A tree as an old, umask-broad installation would have left it. Named away from the
    # autouse data directory the shared fixture already created under tmp_path.
    root = tmp_path / "old-install"
    (root / "uploads" / "3").mkdir(parents=True)
    upload = root / "uploads" / "3" / "1-lecture.pdf"
    upload.write_bytes(b"%PDF")
    models = root / "models"
    models.mkdir()
    binary = models / "llama-server"
    binary.write_bytes(b"\x7fELF")
    for path in (root, root / "uploads", root / "uploads" / "3", models):
        os.chmod(path, 0o755)  # noqa: S103 - simulating an old umask-broad install
    os.chmod(upload, 0o644)  # noqa: S103 - simulating an old umask-broad install
    os.chmod(binary, 0o755)  # noqa: S103 - the executable this contract must not break

    private.harden_data_tree(root, keep_file_modes=(models,))

    assert _mode(root) == 0o700
    assert _mode(root / "uploads" / "3") == 0o700
    assert _mode(upload) == 0o600
    # The models directory is private, but its executable keeps the owner execute bit a
    # 0o600 file would have stripped, so the bundled binary still runs.
    assert _mode(models) == 0o700
    assert _mode(binary) == 0o755


def test_harden_data_tree_does_not_follow_symlinks(tmp_path: Path) -> None:
    # A link pointing at an attached workspace must not become a route to rewrite the
    # user's own files.
    root = tmp_path / "old-install"
    root.mkdir()
    outside = tmp_path / "workspace" / "project.py"
    outside.parent.mkdir()
    outside.write_text("print()")
    os.chmod(outside, 0o644)
    (root / "link.py").symlink_to(outside)

    private.harden_data_tree(root)

    assert _mode(outside) == 0o644


def test_ensure_directories_leaves_the_data_tree_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_open_umask: None
) -> None:
    root = tmp_path / "fresh-data"
    monkeypatch.setattr(settings, "data_dir", root)
    monkeypatch.setattr(settings, "db_path", root / "lyra.db")

    settings.ensure_directories()

    for directory in (root, settings.uploads_dir, settings.text_dir, settings.pages_dir):
        assert _mode(directory) == 0o700
    # The one-time upgrade marker is recorded privately, so a second startup is a no-op.
    assert _mode(root / ".permissions-hardened") == 0o600


def test_connect_creates_the_database_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_open_umask: None
) -> None:
    db_path = tmp_path / "db" / "lyra.db"
    monkeypatch.setattr(settings, "db_path", db_path)

    connect().close()

    assert _mode(db_path.parent) == 0o700
    assert _mode(db_path) == 0o600


def test_connect_tightens_a_sidecar_left_broad_by_a_prior_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_open_umask: None
) -> None:
    db_path = tmp_path / "db" / "lyra.db"
    monkeypatch.setattr(settings, "db_path", db_path)
    connect().close()
    # A WAL sidecar that a pre-contract run left world-readable is brought back to 0o600 the
    # next time Lyra opens the database.
    wal = db_path.with_name(db_path.name + "-wal")
    wal.write_bytes(b"")
    os.chmod(wal, 0o644)

    conn = connect()
    try:
        assert _mode(wal) == 0o600
    finally:
        conn.close()


def test_extracted_text_is_written_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_open_umask: None
) -> None:
    text_root = tmp_path / "data" / "text"
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    ingestion._write_extracted_text(7, "extracted coursework")

    written = text_root / "7.txt"
    assert written.read_text() == "extracted coursework"
    assert _mode(written) == 0o600
    assert _mode(text_root) == 0o700
