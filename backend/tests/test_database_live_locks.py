"""Real process regression: privacy preparation must preserve SQLite's WAL locks."""

import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from backend.config import settings
from backend.storage import private
from backend.storage.database import connect

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process-associated locks")


def _competing_writer(path: Path) -> str:
    script = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], timeout=0.1)
try:
    conn.execute('begin immediate')
    print('acquired')
    conn.rollback()
except sqlite3.OperationalError as exc:
    print(str(exc))
finally:
    conn.close()
"""
    result = subprocess.run(  # noqa: S603 - fixed program, isolated synthetic database
        [sys.executable, "-c", script, str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("broaden_sidecar", [False, True])
def test_new_request_connection_cannot_release_an_existing_transaction_lock(
    db: sqlite3.Connection, broaden_sidecar: bool
) -> None:
    db.execute("begin immediate")
    db.execute("insert into classes (name) values ('Synthetic uncommitted class')")
    try:
        assert _competing_writer(settings.db_path) == "database is locked"
        if broaden_sidecar:
            os.chmod(str(settings.db_path) + "-shm", 0o644)
        request_connection = connect()
        try:
            assert _competing_writer(settings.db_path) == "database is locked"
        finally:
            request_connection.close()
        assert _competing_writer(settings.db_path) == "database is locked"
    finally:
        db.rollback()
    assert _competing_writer(settings.db_path) == "acquired"
    assert db.execute("pragma integrity_check").fetchone()[0] == "ok"


def test_sqlite_preparation_creates_private_file_without_touching_existing_content(tmp_path):
    path = tmp_path / "synthetic.db"
    previous_umask = os.umask(0)
    try:
        private.secure_sqlite_file(path)
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_bytes(b"synthetic durable bytes")
    private.secure_sqlite_file(path)
    assert path.read_bytes() == b"synthetic durable bytes"
    assert list(tmp_path.glob(".lyra-sqlite-*")) == []


def test_sqlite_preparation_refuses_symlink_substitution_during_chmod(tmp_path, monkeypatch):
    target = tmp_path / "synthetic.db-shm"
    target.write_bytes(b"")
    target.chmod(0o644)
    external = tmp_path / "outside"
    external.write_bytes(b"untouched synthetic bytes")
    external.chmod(0o644)
    real_chmod = os.chmod

    def raced_chmod(path, mode, **kwargs):
        if Path(path) == target:
            target.unlink()
            target.symlink_to(external)
        real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(os, "chmod", raced_chmod)
    with pytest.raises(private.PrivacyContractError):
        private.secure_sqlite_file(target)
    assert external.read_bytes() == b"untouched synthetic bytes"
    assert stat.S_IMODE(external.stat().st_mode) == 0o644


def test_sqlite_preparation_retries_disappearing_sidecar(tmp_path, monkeypatch):
    target = tmp_path / "synthetic.db-shm"
    target.write_bytes(b"")
    target.chmod(0o644)
    real_chmod = os.chmod
    removed = False

    def disappearing_chmod(path, mode, **kwargs):
        nonlocal removed
        if Path(path) == target and not removed:
            target.unlink()
            removed = True
        real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(os, "chmod", disappearing_chmod)
    private.secure_sqlite_file(target)
    assert removed
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_sqlite_preparation_handles_a_concurrent_creator(tmp_path, monkeypatch):
    target = tmp_path / "synthetic.db-wal"
    real_link = os.link

    def competing_link(source, destination, **kwargs):
        target.write_bytes(b"concurrent synthetic writer")
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", competing_link)
    private.secure_sqlite_file(target)
    assert target.read_bytes() == b"concurrent synthetic writer"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".lyra-sqlite-*")) == []


def test_sqlite_post_open_validation_tolerates_absence(tmp_path):
    path = tmp_path / "synthetic.db-shm"
    private.secure_sqlite_file(path, create=False)
    assert not path.exists()
