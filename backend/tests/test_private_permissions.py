"""The private-permissions contract for Lyra-owned state.

Every creation path runs under a deliberately permissive umask, so what the tests pin down
is that the modes come from the contract in `storage.private` and not from whatever umask
the process happened to inherit. POSIX-only: the modes are meaningless on a filesystem that
does not carry them, which is the same reason `storage.private` tolerates a chmod a
mode-less filesystem cannot honour rather than treating it as an error.

The symlink regressions below all share one shape: a symlink is planted where Lyra is about
to create, write, harden, or walk owned state, and the test asserts that the operation
fails closed (or skips the link) and that the link's external target is never created
through, truncated, chmodded, or traversed. That boundary is the whole point of the privacy
contract - Lyra must not follow a link out of its own tree and quietly rewrite or expose a
user's unrelated file.
"""

import errno
import os
import sqlite3
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
    root = tmp_path / "a"
    created = private.secure_mkdir(root / "b" / "c", root=root)

    assert _mode(created) == 0o700
    assert _mode(root) == 0o700
    assert _mode(root / "b") == 0o700


def test_secure_mkdir_leaves_a_preexisting_parent_alone(
    tmp_path: Path, wide_open_umask: None
) -> None:
    # A data directory may live inside a folder the user chose to share; only the
    # directories Lyra itself creates are its to harden. The owned root is the data
    # directory, and its already-existing ancestors are left as the user set them.
    outer = tmp_path / "outer"
    outer.mkdir(mode=0o755)
    os.chmod(outer, 0o755)  # noqa: S103 - deliberately broad: the user's own parent folder

    data_root = outer / "lyra-data"
    private.secure_mkdir(data_root, root=data_root)

    assert _mode(outer) == 0o755
    assert _mode(data_root) == 0o700


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


def test_harden_data_tree_restores_owner_access_to_unusable_directories(tmp_path: Path) -> None:
    root = tmp_path / "old-install"
    child = root / "uploads"
    child.mkdir(parents=True)
    os.chmod(child, 0o000)

    private.harden_data_tree(root)

    assert _mode(child) == 0o700


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


def test_connect_creates_external_database_and_active_sidecars_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_open_umask: None
) -> None:
    external_parent = tmp_path / "user-chosen-db-dir"
    external_parent.mkdir(mode=0o755)
    os.chmod(external_parent, 0o755)  # noqa: S103 - explicit external-parent threat model
    db_path = external_parent / "lyra.db"
    monkeypatch.setattr(settings, "data_dir", tmp_path / "separate-data")

    conn = connect(db_path)
    try:
        assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
        assert _mode(db_path) == 0o600
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            assert sidecar.is_file()
            assert _mode(sidecar) == 0o600
        # An explicit external parent remains user-owned and is not chmodded by Lyra.
        assert _mode(external_parent) == 0o755
    finally:
        conn.close()


def test_connect_rejects_an_external_database_in_an_other_writable_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external_parent = tmp_path / "unsafe-db-dir"
    external_parent.mkdir()
    os.chmod(external_parent, 0o777)  # noqa: S103 - unsafe configuration under test
    db_path = external_parent / "lyra.db"
    monkeypatch.setattr(settings, "data_dir", tmp_path / "separate-data")

    with pytest.raises(private.PrivacyContractError, match="writable by other users"):
        connect(db_path)

    assert not db_path.exists()
    assert _mode(external_parent) == 0o777


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_connect_refuses_planted_sidecar_symlinks_before_sqlite_touches_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wide_open_umask: None,
    suffix: str,
) -> None:
    db_path = tmp_path / "external-db" / "lyra.db"
    db_path.parent.mkdir()
    external = tmp_path / f"outside{suffix}"
    external.write_bytes(b"recognizable external data")
    os.chmod(external, 0o644)
    db_path.with_name(db_path.name + suffix).symlink_to(external)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "separate-data")

    with pytest.raises(private.PrivacyContractError):
        connect(db_path)

    assert external.read_bytes() == b"recognizable external data"
    assert _mode(external) == 0o644


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
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_open_umask: None
) -> None:
    # A real row behind the write: publication is guarded on the document's identity.
    db.execute("insert into classes (id, name) values (1, 'Calc')")
    db.execute(
        "insert into documents (id, class_id, filename, stored_path, mime, byte_size, state) "
        "values (7, 1, 'notes.pdf', '', 'application/pdf', 0, 'ready')"
    )
    db.commit()
    created_at = str(
        db.execute("select created_at from documents where id = 7").fetchone()["created_at"]
    )
    text_root = tmp_path / "fresh-data" / "text"
    monkeypatch.setattr(settings, "data_dir", tmp_path / "fresh-data")

    assert ingestion._write_extracted_text(7, "extracted coursework", created_at)

    written = text_root / "7.txt"
    assert written.read_text() == "extracted coursework"
    assert _mode(written) == 0o600
    assert _mode(text_root) == 0o700


# --- Symlink boundary regressions -------------------------------------------------------
#
# Each of these fails on the pre-hardening implementation, where an ordinary path-following
# open/chmod/mkdir/walk would reach the link's target.


def _external_dir(tmp_path: Path, mode: int = 0o755) -> Path:
    """A directory outside any Lyra tree, holding a file, both left deliberately broad."""
    external = tmp_path / "external"
    external.mkdir()
    resident = external / "someone-elses-notes.txt"
    resident.write_text("not Lyra's to touch")
    os.chmod(external, mode)  # noqa: S103 - the user's own directory, mode is theirs
    os.chmod(resident, 0o644)  # noqa: S103 - and so is the file inside it
    return external


def test_symlinked_data_root_is_refused_and_its_target_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_open_umask: None
) -> None:
    # A data root that is itself a symlink must never be followed: hardening its target would
    # walk and chmod a whole external directory the user only linked to.
    external = _external_dir(tmp_path)
    resident = external / "someone-elses-notes.txt"
    # Named away from the autouse fixture's own `tmp_path/data` real directory.
    link_root = tmp_path / "linked-data"
    link_root.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(settings, "data_dir", link_root)
    monkeypatch.setattr(settings, "db_path", link_root / "lyra.db")

    with pytest.raises(private.PrivacyContractError):
        settings.ensure_directories()
    # The tree walk helper refuses a symlinked root just as directly.
    with pytest.raises(private.PrivacyContractError):
        private.harden_data_tree(link_root)

    assert link_root.is_symlink()
    assert _mode(external) == 0o755
    assert _mode(resident) == 0o644
    assert resident.read_text() == "not Lyra's to touch"


def test_secure_mkdir_refuses_a_symlinked_component_beneath_the_root(
    tmp_path: Path, wide_open_umask: None
) -> None:
    # An old install may have linked a cache/upload subdirectory out to another disk. Creating
    # beneath it must fail closed, not create (and later chmod) inside the link's target.
    data = tmp_path / "install"
    data.mkdir()
    external = _external_dir(tmp_path)
    (data / "uploads").symlink_to(external, target_is_directory=True)

    with pytest.raises(private.PrivacyContractError):
        private.secure_mkdir(data / "uploads" / "3", root=data)

    assert list(external.iterdir()) == [external / "someone-elses-notes.txt"]
    assert _mode(external) == 0o755


def test_runtime_write_beneath_a_symlinked_owned_dir_creates_nothing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_open_umask: None
) -> None:
    # The real ingestion write path, with `text/` linked out of the tree: it must refuse
    # rather than drop extracted coursework into the external target.
    data = tmp_path / "install"
    data.mkdir()
    external = _external_dir(tmp_path)
    (data / "text").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(settings, "data_dir", data)

    with pytest.raises(private.PrivacyContractError):
        # The refusal fires in `secure_mkdir`, before any identity check would run.
        ingestion._write_extracted_text(7, "extracted coursework", "2026-01-01T00:00:00.000Z")

    assert list(external.iterdir()) == [external / "someone-elses-notes.txt"]
    assert _mode(external) == 0o755


def test_write_private_bytes_refuses_a_symlink_target(
    tmp_path: Path, wide_open_umask: None
) -> None:
    # A sensitive write must not follow a symlink and truncate, rewrite, or chmod whatever it
    # points at. O_NOFOLLOW turns the write into a clean refusal.
    external = tmp_path / "outside.txt"
    external.write_text("original contents")
    os.chmod(external, 0o644)  # noqa: S103 - an unrelated external file, left as it was
    link = tmp_path / "link-to-api-key"
    link.symlink_to(external)

    with pytest.raises(private.PrivacyContractError):
        private.write_private_bytes(link, b"a fresh secret")

    assert link.is_symlink()
    assert external.read_text() == "original contents"
    assert _mode(external) == 0o644


def test_read_private_text_refuses_a_symlink_target(tmp_path: Path, wide_open_umask: None) -> None:
    external = tmp_path / "outside-key"
    external.write_text("not a Lyra secret")
    os.chmod(external, 0o644)
    link = tmp_path / ".api_key"
    link.symlink_to(external)

    with pytest.raises(private.PrivacyContractError):
        private.read_private_text(link)

    assert external.read_text() == "not a Lyra secret"
    assert _mode(external) == 0o644


def test_sentinel_symlink_neither_skips_migration_nor_touches_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_open_umask: None
) -> None:
    # A symlink at `.permissions-hardened` must not pass as "already migrated", must not be
    # followed and overwritten, and must not modify the file it points at.
    data = tmp_path / "install"
    (data / "uploads").mkdir(parents=True)
    external = tmp_path / "outside.txt"
    external.write_text("original contents")
    os.chmod(external, 0o644)  # noqa: S103 - an unrelated external file, left as it was
    (data / ".permissions-hardened").symlink_to(external)
    monkeypatch.setattr(settings, "data_dir", data)
    monkeypatch.setattr(settings, "db_path", data / "lyra.db")

    with pytest.raises(private.PrivacyContractError):
        settings.ensure_directories()

    # The link was refused, not trusted as a done-marker and not written through.
    assert (data / ".permissions-hardened").is_symlink()
    assert external.read_text() == "original contents"
    assert _mode(external) == 0o644


def test_connect_refuses_a_symlinked_database_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_open_umask: None
) -> None:
    # An explicit LYRA_DB_PATH that is a symlink must be handled explicitly: Lyra must not
    # open/create through it and then chmod the external target while reporting the configured
    # path as secured.
    external = tmp_path / "real-elsewhere.db"
    external.write_bytes(b"someone else's database")
    os.chmod(external, 0o644)  # noqa: S103 - an unrelated external file, left as it was
    link = tmp_path / "db" / "lyra.db"
    link.parent.mkdir()
    link.symlink_to(external)
    monkeypatch.setattr(settings, "db_path", link)

    with pytest.raises(private.PrivacyContractError):
        connect()

    assert link.is_symlink()
    assert external.read_bytes() == b"someone else's database"
    assert _mode(external) == 0o644


def test_harden_data_tree_does_not_follow_a_nested_directory_symlink(
    tmp_path: Path, wide_open_umask: None
) -> None:
    # The one-time migration walk encounters a linked-in workspace *directory* and must
    # neither descend into it nor chmod the link or its target.
    root = tmp_path / "old-install"
    (root / "uploads").mkdir(parents=True)
    os.chmod(root, 0o755)  # noqa: S103 - simulating an old umask-broad install
    os.chmod(root / "uploads", 0o755)  # noqa: S103 - simulating an old umask-broad install
    external = _external_dir(tmp_path)
    resident = external / "someone-elses-notes.txt"
    (root / "uploads" / "workspace").symlink_to(external, target_is_directory=True)

    private.harden_data_tree(root)

    assert (root / "uploads" / "workspace").is_symlink()
    assert _mode(external) == 0o755
    assert _mode(resident) == 0o644
    assert _mode(root / "uploads") == 0o700


def test_fchmod_permission_failure_fails_closed_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_open_umask: None
) -> None:
    # On a mode-carrying POSIX filesystem, a genuine failure to tighten owned state must
    # surface, not be swallowed into a "successful" startup that leaves the file broad.
    target = tmp_path / "coursework.bin"
    target.write_bytes(b"payload")
    os.chmod(target, 0o644)  # noqa: S103 - the broad state the harden is meant to fix

    def deny(_fd: int, _mode: int) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "fchmod", deny)

    with pytest.raises(private.PrivacyContractError):
        private.harden_file(target)


def test_fchmod_unsupported_filesystem_is_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_open_umask: None
) -> None:
    # A filesystem that cannot carry POSIX modes (ENOTSUP) is the one case the contract
    # tolerates: the data directory's location is the isolation there, so this is not an error.
    target = tmp_path / "coursework.bin"
    target.write_bytes(b"payload")
    os.chmod(target, 0o644)  # noqa: S103 - broad, and this filesystem cannot narrow it

    def unsupported(_fd: int, _mode: int) -> None:
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(os, "fchmod", unsupported)

    private.harden_file(target)  # tolerated: no exception
