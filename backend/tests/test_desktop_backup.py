"""Packaged backup round-trip and deterministic interrupted-rename recovery."""

import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from backend import desktop_backup as backup
from backend.config import settings


@pytest.fixture
def profile(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(settings, "data_dir", root)
    monkeypatch.setattr(settings, "db_path", root / "lyra.db")
    with sqlite3.connect(root / "lyra.db") as conn:
        conn.executescript(
            "create table classes(id); "
            "create table documents(id,class_id,filename,stored_path); "
            "create table drafts(body); insert into drafts values('saved essay');"
        )
    (root / "uploads").mkdir()
    (root / "uploads" / "source.pdf").write_bytes(b"source document bytes")
    (root / "credentials").mkdir(mode=0o700)
    (root / "credentials" / "slot.json").write_text('{"storage":"keychain"}')
    return root


def test_roundtrip_replaces_populated_profile_and_retains_previous(profile, tmp_path):
    target = tmp_path / "backup.tar.gz"
    assert backup.create_backup(target)["status"] == "created"
    assert target.stat().st_mode & 0o777 == 0o600
    (profile / "uploads" / "source.pdf").write_bytes(b"newer source")
    with sqlite3.connect(profile / "lyra.db") as conn:
        conn.execute("update drafts set body='newer essay'")
    result = backup.restore_backup(target)
    assert result["status"] == "restored"
    with sqlite3.connect(profile / "lyra.db") as conn:
        assert conn.execute("select body from drafts").fetchone()[0] == "saved essay"
    assert (profile / "uploads" / "source.pdf").read_bytes() == b"source document bytes"
    assert (profile / "credentials" / "slot.json").is_file()
    previous = profile.parent / result["label"]
    with sqlite3.connect(previous / "lyra.db") as conn:
        assert conn.execute("select body from drafts").fetchone()[0] == "newer essay"
    assert (previous / "uploads" / "source.pdf").read_bytes() == b"newer source"
    assert not backup._journal(profile).exists()


class SimulatedPowerLoss(BaseException):
    pass


@pytest.mark.parametrize("boundary", ["before_old_move", "after_old_move", "after_stage_move"])
def test_restore_recovers_after_interrupted_rename(profile, tmp_path, monkeypatch, boundary):
    target = tmp_path / "backup.tar.gz"
    backup.create_backup(target)
    (profile / "uploads" / "source.pdf").write_bytes(b"newer source")
    original = Path.rename
    fired = False

    def barrier(source, destination):
        nonlocal fired
        is_old = source == profile
        is_stage = source.name.startswith(".profile.restore-")
        if not fired and boundary == "before_old_move" and is_old:
            fired = True
            raise SimulatedPowerLoss()
        result = original(source, destination)
        if not fired and (
            (boundary == "after_old_move" and is_old)
            or (boundary == "after_stage_move" and is_stage)
        ):
            fired = True
            raise SimulatedPowerLoss()
        return result

    monkeypatch.setattr(Path, "rename", barrier)
    with pytest.raises(SimulatedPowerLoss):
        backup.restore_backup(target)
    assert fired
    monkeypatch.setattr(Path, "rename", original)
    backup.recover_restore()
    assert (profile / "uploads" / "source.pdf").read_bytes() == b"source document bytes"
    previous = next(profile.parent.glob("profile.before-restore-*"))
    assert (previous / "uploads" / "source.pdf").read_bytes() == b"newer source"
    backup.recover_restore()  # idempotent


def test_newer_schema_is_rejected_before_live_profile_changes(profile, tmp_path):
    target = tmp_path / "future.tar.gz"
    with sqlite3.connect(profile / "lyra.db") as conn:
        conn.execute("pragma user_version=99999")
    # Assemble unsupported archive without using the correctly guarded creator.
    stage = tmp_path / "stage"
    backup.archive.stage_backup_tree(stage, profile, profile / "lyra.db")
    with tarfile.open(target, "w:gz") as bundle:
        bundle.add(stage / "manifest.json", arcname="manifest.json")
        bundle.add(stage / "data", arcname="data")
    with sqlite3.connect(profile / "lyra.db") as conn:
        conn.execute("pragma user_version=0")
    before = (profile / "lyra.db").read_bytes()
    with pytest.raises(Exception, match="newer"):
        backup.restore_backup(target)
    assert (profile / "lyra.db").read_bytes() == before
    assert not list(tmp_path.glob("profile.before-restore-*"))


def test_existing_backup_is_never_overwritten(profile, tmp_path):
    target = tmp_path / "existing.tar.gz"
    target.write_bytes(b"existing")
    with pytest.raises(backup.BackupError):
        backup.create_backup(target)
    assert target.read_bytes() == b"existing"


def test_link_archive_is_rejected_without_touching_live_profile(profile, tmp_path):
    target = tmp_path / "unsafe.tar.gz"
    manifest = json.dumps(
        backup.archive.staged_backup_manifest(profile, profile / "lyra.db")
    ).encode()
    with tarfile.open(target, "w:gz") as bundle:
        item = tarfile.TarInfo("manifest.json")
        item.size = len(manifest)
        bundle.addfile(item, io.BytesIO(manifest))
        link = tarfile.TarInfo("data/lyra.db")
        link.type = tarfile.SYMTYPE
        link.linkname = "/outside"
        bundle.addfile(link)
    before = (profile / "lyra.db").read_bytes()
    with pytest.raises(backup.archive.LauncherError, match="link"):
        backup.restore_backup(target)
    assert (profile / "lyra.db").read_bytes() == before


def test_future_live_schema_blocks_pending_restore_before_renames(profile, tmp_path):
    token = "a" * 32
    stage, previous = backup._paths(profile, token)
    backup.archive.copy_tree_without_symlinks(profile, stage, excluded=set())
    backup._save_journal(profile, token, "prepared")
    with sqlite3.connect(profile / "lyra.db") as conn:
        conn.execute("pragma user_version=99999")
    before = (profile / "lyra.db").read_bytes()
    with pytest.raises(Exception, match="newer"):
        backup.recover_restore()
    assert (profile / "lyra.db").read_bytes() == before
    assert stage.exists()
    assert backup._journal(profile).exists()
    assert not previous.exists()


def test_backup_parent_rename_does_not_redirect_publication(profile, tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    selected.mkdir()
    moved = tmp_path / "moved"
    original = backup.archive.stage_backup_tree

    def barrier(*args, **kwargs):
        result = original(*args, **kwargs)
        selected.rename(moved)
        selected.mkdir()
        return result

    monkeypatch.setattr(backup.archive, "stage_backup_tree", barrier)
    assert backup.create_backup(selected / "backup.tar.gz")["status"] == "created"
    assert (moved / "backup.tar.gz").is_file()
    assert not (selected / "backup.tar.gz").exists()


def test_recovery_without_journal_preserves_external_db_support(profile, tmp_path, monkeypatch):
    external = tmp_path / "external.db"
    profile.joinpath("lyra.db").rename(external)
    monkeypatch.setattr(settings, "db_path", external)
    backup.recover_restore()
    assert external.is_file()


def test_restore_preserves_forgotten_credential_authority(profile, tmp_path, monkeypatch):
    from backend.storage import secrets

    monkeypatch.setattr(secrets, "_keyring_ok", False)
    (profile / ".exa_api_key").write_text("old-exa")
    (profile / ".exa_api_key.authority").write_text("file")
    (profile / ".tutor_credential_generation").write_text("old-generation")
    target = tmp_path / "old-credentials.tar.gz"
    backup.create_backup(target)
    (profile / ".exa_api_key").unlink()
    (profile / ".exa_api_key.authority").write_text("deleted")
    (profile / ".tutor_credential_generation").write_text("forgotten-generation")
    (profile / "credentials" / "slot.json").write_text('{"storage":"none"}')
    backup.restore_backup(target)
    assert secrets.get_exa_api_key() is None
    assert (profile / ".tutor_credential_generation").read_text() != "old-generation"
    assert "keychain" not in (profile / "credentials" / "slot.json").read_text()


def test_restore_recovers_power_loss_after_rollback_rename(profile, tmp_path, monkeypatch):
    target = tmp_path / "archive.tar.gz"
    backup.create_backup(target)
    (profile / "uploads" / "source.pdf").write_bytes(b"current source")
    verify = backup._verify_database
    rename = Path.rename
    verification_failed = False

    def fail_published(path):
        nonlocal verification_failed
        if (
            path == profile / "lyra.db"
            and list(tmp_path.glob("profile.before-restore-*"))
            and not verification_failed
        ):
            verification_failed = True
            raise RuntimeError("injected publication verification failure")
        return verify(path)

    def crash_after_rollback(source, destination):
        result = rename(source, destination)
        if source.name.startswith("profile.before-restore-"):
            raise SimulatedPowerLoss()
        return result

    monkeypatch.setattr(backup, "_verify_database", fail_published)
    monkeypatch.setattr(Path, "rename", crash_after_rollback)
    with pytest.raises(SimulatedPowerLoss):
        backup.restore_backup(target)
    assert verification_failed
    monkeypatch.setattr(backup, "_verify_database", verify)
    monkeypatch.setattr(Path, "rename", rename)
    backup.recover_restore()
    backup.recover_restore()
    assert (profile / "uploads" / "source.pdf").read_bytes() == b"current source"
    assert not backup._journal(profile).exists()


def test_restore_keeps_current_endpoint_and_credential_together(profile, tmp_path, monkeypatch):
    import keyring.errors

    from backend.storage import database, secrets

    (profile / "lyra.db").unlink()
    conn = database.connect()
    database.migrate(conn)

    def unavailable(*args, **kwargs):
        raise keyring.errors.KeyringError("injected unavailable keychain")

    monkeypatch.setattr(secrets, "_keyring_call", unavailable)
    old = secrets.stage_tutor_credential("https://old.example/v1", "old-value")
    conn.execute(
        "update settings set endpoint_url=?, tutor_credential_id=? where id=1",
        ("https://old.example/v1", old),
    )
    conn.commit()
    conn.close()
    target = tmp_path / "old-endpoint.tar.gz"
    backup.create_backup(target)
    current = secrets.stage_tutor_credential("https://current.example/v1", "current-value")
    conn = database.connect()
    conn.execute(
        "update settings set endpoint_url=?, tutor_credential_id=? where id=1",
        ("https://current.example/v1", current),
    )
    conn.commit()
    conn.close()
    backup.restore_backup(target)
    row = backup._settings_snapshot(profile / "lyra.db")
    assert row["endpoint_url"] == "https://current.example/v1"
    assert row["tutor_credential_id"] == current
    assert secrets.get_tutor_credential(current, row["endpoint_url"]) == "current-value"
    assert secrets.get_tutor_credential(current, "https://old.example/v1") is None


def test_sqlite_backup_includes_commit_between_checkpoint_and_writer_lock(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    with sqlite3.connect(source) as connection:
        connection.execute("pragma journal_mode=wal")
        connection.execute("create table entries(value)")
        connection.execute("insert into entries values(1)")
    original = sqlite3.connect
    fired = False

    class BarrierConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            nonlocal fired
            if sql == "begin immediate" and not fired:
                fired = True
                with original(source) as writer:
                    writer.execute("insert into entries values(2)")
            return super().execute(sql, *args, **kwargs)

    def connect(path, *args, **kwargs):
        if str(path) == str(source) and kwargs.get("isolation_level", "") is None:
            kwargs["factory"] = BarrierConnection
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    backup.archive.snapshot_sqlite_database(source, destination)
    assert fired
    with original(destination) as snapshot:
        assert snapshot.execute("select count(*) from entries").fetchone()[0] == 2


def test_cross_profile_restore_rebases_document_path_to_copied_original(
    profile, tmp_path, monkeypatch
):
    import shutil

    source_directory = profile / "uploads" / "1"
    source_directory.mkdir()
    source = source_directory / "1-original.txt"
    source.write_bytes(b"exact original bytes")
    with sqlite3.connect(profile / "lyra.db") as conn:
        conn.execute("insert into documents values(1,1,'original.txt',?)", (str(source),))
    target = tmp_path / "cross-profile.tar.gz"
    backup.create_backup(target)
    destination = tmp_path / "different-profile"
    shutil.copytree(profile, destination)
    monkeypatch.setattr(settings, "data_dir", destination)
    monkeypatch.setattr(settings, "db_path", destination / "lyra.db")
    backup.restore_backup(target)
    with sqlite3.connect(destination / "lyra.db") as conn:
        stored = Path(conn.execute("select stored_path from documents where id=1").fetchone()[0])
    assert stored == destination / "uploads" / "1" / "1-original.txt"
    assert stored.read_bytes() == source.read_bytes() == b"exact original bytes"


def test_snapshot_closes_sqlite_handles_before_archiving(profile, tmp_path, monkeypatch):
    import gc

    original = sqlite3.connect
    connections = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def connect(*args, **kwargs):
        kwargs["factory"] = TrackedConnection
        result = original(*args, **kwargs)
        connections.append(result)  # prevent GC from masking missing explicit closes
        return result

    monkeypatch.setattr(sqlite3, "connect", connect)
    backup.archive.snapshot_sqlite_database(profile / "lyra.db", tmp_path / "snapshot.db")
    assert len(connections) == 3
    assert all(connection.closed for connection in connections)
    gc.collect()
