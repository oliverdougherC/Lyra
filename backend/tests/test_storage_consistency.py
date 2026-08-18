"""Crash-consistency of document storage across SQLite and the filesystem.

Every test here injects a fault at one boundary of the contract in
docs/storage-consistency.md - the staged write, the publication rename, the move rename,
the unlinks after a committed delete - or simulates a crash between a commit and its owed
filesystem work, then proves the state either converged immediately (compensation) or
converges at the next startup (`reconcile_storage`), idempotently, without following
symlinks and without inventing state.

A simulated crash is a raised exception the route deliberately does not catch, so the
real handler runs up to the exact boundary and then stops - the tests exercise the
production code paths, not hand-written approximations of them.
"""

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_documents
from backend.config import settings
from backend.core import classes, ingestion, storage_intents
from backend.core.errors import LyraError
from backend.rag import render
from backend.storage import private
from backend.storage.database import connect, get_db


class SimulatedCrashError(RuntimeError):
    """Stands in for the process dying: nothing after the raise point runs."""


def _request_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def no_worker(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record what would have been queued instead of running it."""
    queued: list[int] = []
    monkeypatch.setattr(routes_documents, "enqueue", queued.append)
    return queued


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(routes_documents.router)
    app.dependency_overrides[get_db] = _request_db
    # A SimulatedCrashError must come back as a 500, not abort the test: the assertion is
    # about the state the crash left behind.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def crash_patch() -> Iterator[pytest.MonkeyPatch]:
    """A patcher for the injected fault alone.

    Kept separate from the test's ordinary `monkeypatch` so undoing the crash - the
    "restart" between the fault and reconciliation - does not also undo the autouse
    temporary data directory.
    """
    patcher = pytest.MonkeyPatch()
    yield patcher
    patcher.undo()


@pytest.fixture
def other_class_id(db: sqlite3.Connection) -> int:
    cursor = db.execute("insert into classes (name) values ('Linear Algebra')")
    db.commit()
    return int(cursor.lastrowid or 0)


def _document(db: sqlite3.Connection, class_id: int, state: str = "ready") -> int:
    """A stored document with a real file on disk under its class directory."""
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, 'lecture-2.pdf', '', 'application/pdf', 4, ?)",
        (class_id, state),
    )
    document_id = int(cursor.lastrowid or 0)
    stored = settings.uploads_dir / str(class_id) / f"{document_id}-lecture-2.pdf"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"pdf!")
    db.execute("update documents set stored_path = ? where id = ?", (str(stored), document_id))
    db.commit()
    return document_id


def _stored_path(db: sqlite3.Connection, document_id: int) -> Path:
    row = db.execute("select stored_path from documents where id = ?", (document_id,)).fetchone()
    return Path(str(row["stored_path"]))


def _row(db: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    return db.execute(
        "select class_id, stored_path, state, error_message from documents where id = ?",
        (document_id,),
    ).fetchone()


def _intents(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("select id, kind, document_id, class_id from storage_intents").fetchall()


def _derived_files(db: sqlite3.Connection, document_id: int) -> None:
    """Extracted text and a rendered page, so deletion has derived state to clear."""
    settings.text_dir.mkdir(parents=True, exist_ok=True)
    storage_intents.text_path(document_id).write_text("extracted text")
    page = render.page_path(document_id, 1)
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"\x89PNG\r\n\x1a\n page")


# --- The publication contract: a final name is whole or absent -----------------------


def test_an_interrupted_publication_leaves_no_file_under_the_final_name(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = settings.text_dir / "published.txt"
    settings.text_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(os, "replace", _raise_os_error)

    with pytest.raises(OSError, match="injected"):
        private.publish_private_bytes(target, b"whole or nothing")

    assert not target.exists()
    assert list(settings.text_dir.iterdir()) == []


def test_an_interrupted_publication_keeps_the_previous_contents(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader-visible file is the old complete version until the new one is whole."""
    target = settings.text_dir / "published.txt"
    settings.text_dir.mkdir(parents=True, exist_ok=True)
    target.write_text("the old complete version")
    monkeypatch.setattr(os, "replace", _raise_os_error)

    with pytest.raises(OSError, match="injected"):
        private.publish_private_text(target, "a torn new version")

    assert target.read_text() == "the old complete version"


def test_extracted_text_is_published_atomically(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_intents.text_path(7).parent.mkdir(parents=True, exist_ok=True)
    storage_intents.text_path(7).write_text("previous extraction")
    monkeypatch.setattr(os, "replace", _raise_os_error)

    with pytest.raises(OSError, match="injected"):
        ingestion._write_extracted_text(7, "interrupted extraction")

    assert storage_intents.text_path(7).read_text() == "previous extraction"


def _raise_os_error(*args: object, **kwargs: object) -> None:
    raise OSError("injected")


# --- Upload: a committed row never points at a missing or torn file ------------------


def test_a_failed_upload_write_leaves_no_row_and_no_file(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(private, "publish_private_bytes", _raise_os_error)

    response = client.post(
        f"/api/classes/{class_id}/documents",
        files={"file": ("notes.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 500
    assert db.execute("select count(*) from documents").fetchone()[0] == 0
    class_dir = settings.uploads_dir / str(class_id)
    assert not class_dir.exists() or list(class_dir.iterdir()) == []


def test_a_successful_upload_row_points_at_a_whole_file(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    response = client.post(
        f"/api/classes/{class_id}/documents",
        files={"file": ("notes.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 202
    stored = _stored_path(db, int(response.json()["id"]))
    assert stored.read_bytes() == b"%PDF-1.4"
    assert _intents(db) == []


def test_an_upload_that_crashed_before_its_commit_is_swept_at_startup(
    db: sqlite3.Connection, class_id: int
) -> None:
    """The one state upload can leak: a whole published file whose insert rolled back."""
    ghost = settings.uploads_dir / str(class_id) / "999-ghost.pdf"
    ghost.parent.mkdir(parents=True, exist_ok=True)
    ghost.write_bytes(b"orphan")

    _, swept = storage_intents.reconcile_storage(db)

    assert swept == 1
    assert not ghost.exists()


# --- Move: both sides at the old location, or both at the new; never split ------------


def test_a_move_with_a_missing_source_is_refused_honestly(
    client: TestClient, db: sqlite3.Connection, class_id: int, other_class_id: int
) -> None:
    document_id = _document(db, class_id)
    before = _stored_path(db, document_id)
    before.unlink()

    response = client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})

    # An honest recoverable refusal, never a "successful" move to a fictional path.
    assert response.status_code == 409
    row = _row(db, document_id)
    assert int(row["class_id"]) == class_id
    assert Path(str(row["stored_path"])) == before
    assert _intents(db) == []


def test_a_move_whose_source_is_a_symlink_is_refused(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    other_class_id: int,
    tmp_path: Path,
) -> None:
    """A planted link where the upload belongs must not be relocated into another class."""
    document_id = _document(db, class_id)
    stored = _stored_path(db, document_id)
    outside = tmp_path / "outside-upload"
    outside.write_bytes(b"outside data")
    stored.unlink()
    stored.symlink_to(outside)

    response = client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})

    assert response.status_code == 409
    assert stored.is_symlink()
    assert outside.read_bytes() == b"outside data"


def test_a_failed_rename_compensates_back_to_the_source_location(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    other_class_id: int,
    no_worker: list[int],
    crash_patch: pytest.MonkeyPatch,
) -> None:
    document_id = _document(db, class_id)
    source = _stored_path(db, document_id)
    crash_patch.setattr(storage_intents, "perform_move", _raise_os_error)

    response = client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})

    assert response.status_code == 400
    row = _row(db, document_id)
    # The file never left, so the row is back where the file is: no split-brain.
    assert int(row["class_id"]) == class_id
    assert Path(str(row["stored_path"])) == source
    assert source.read_bytes() == b"pdf!"
    assert _intents(db) == []
    # The chunks were already invalidated, so the document re-indexes in place.
    assert row["state"] == "pending"
    assert no_worker == [document_id]


def test_a_crash_after_the_move_commit_rolls_the_rename_forward_at_startup(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    other_class_id: int,
    crash_patch: pytest.MonkeyPatch,
) -> None:
    document_id = _document(db, class_id)
    source = _stored_path(db, document_id)
    crash_patch.setattr(storage_intents, "perform_move", _crash)

    response = client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})

    # The crash landed between the commit and the rename: DB points at the destination,
    # the file still sits at the source, and the intent survives to say so.
    assert response.status_code == 500
    assert source.exists()
    assert len(_intents(db)) == 1

    crash_patch.undo()
    settled, swept = storage_intents.reconcile_storage(db)

    assert settled == 1
    row = _row(db, document_id)
    destination = Path(str(row["stored_path"]))
    assert destination.parent == settings.uploads_dir / str(other_class_id)
    assert destination.read_bytes() == b"pdf!"
    assert not source.exists()
    # The wedged source file was rolled forward, never mistaken for an orphan and swept.
    assert swept == 0
    assert _intents(db) == []
    assert row["state"] == "pending"


def test_a_crash_after_the_rename_is_recognized_as_complete_at_startup(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    other_class_id: int,
    crash_patch: pytest.MonkeyPatch,
) -> None:
    document_id = _document(db, class_id)
    source = _stored_path(db, document_id)
    crash_patch.setattr(storage_intents, "settle_intent", _crash)

    response = client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})

    assert response.status_code == 500
    assert not source.exists()
    assert len(_intents(db)) == 1

    crash_patch.undo()
    settled, _ = storage_intents.reconcile_storage(db)

    assert settled == 1
    destination = _stored_path(db, document_id)
    assert destination.read_bytes() == b"pdf!"
    assert _intents(db) == []


def test_a_move_whose_file_is_gone_from_both_ends_fails_the_document_honestly(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    other_class_id: int,
    crash_patch: pytest.MonkeyPatch,
) -> None:
    """Recovery must not fabricate a file it cannot find, nor leave `pending` to fail
    cryptically inside the parser later."""
    document_id = _document(db, class_id)
    source = _stored_path(db, document_id)
    crash_patch.setattr(storage_intents, "perform_move", _crash)
    client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})
    source.unlink()

    crash_patch.undo()
    settled, _ = storage_intents.reconcile_storage(db)

    assert settled == 1
    row = _row(db, document_id)
    assert row["state"] == "failed"
    assert row["error_message"] == storage_intents.FILE_LOST_MESSAGE
    assert _intents(db) == []


def test_move_recovery_is_idempotent(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    other_class_id: int,
    crash_patch: pytest.MonkeyPatch,
) -> None:
    document_id = _document(db, class_id)
    crash_patch.setattr(storage_intents, "perform_move", _crash)
    client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})

    crash_patch.undo()
    storage_intents.reconcile_storage(db)
    settled_again, swept_again = storage_intents.reconcile_storage(db)

    assert (settled_again, swept_again) == (0, 0)
    assert _stored_path(db, document_id).read_bytes() == b"pdf!"


def _crash(*args: object, **kwargs: object) -> None:
    raise SimulatedCrashError("the process died here")


# --- Delete: cleanup completes, or its intent survives to be retried -----------------


def test_a_completed_delete_leaves_no_files_and_no_intent(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    stored = _stored_path(db, document_id)
    _derived_files(db, document_id)

    response = client.delete(f"/api/documents/{document_id}")

    assert response.status_code == 204
    assert not stored.exists()
    assert not storage_intents.text_path(document_id).exists()
    assert not render.pages_dir(document_id).exists()
    assert _intents(db) == []


def test_a_crash_after_the_delete_commit_finishes_the_cleanup_at_startup(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    crash_patch: pytest.MonkeyPatch,
) -> None:
    document_id = _document(db, class_id)
    stored = _stored_path(db, document_id)
    _derived_files(db, document_id)
    crash_patch.setattr(storage_intents, "run_document_cleanup", _crash)

    response = client.delete(f"/api/documents/{document_id}")

    # The delete committed; the files and the intent that records them both survive.
    assert response.status_code == 500
    assert db.execute("select count(*) from documents").fetchone()[0] == 0
    assert stored.exists()
    assert len(_intents(db)) == 1

    crash_patch.undo()
    settled, _ = storage_intents.reconcile_storage(db)

    assert settled == 1
    assert not stored.exists()
    assert not storage_intents.text_path(document_id).exists()
    assert not render.pages_dir(document_id).exists()
    assert _intents(db) == []


def test_a_delete_whose_unlink_fails_defers_cleanup_instead_of_erroring(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    crash_patch: pytest.MonkeyPatch,
) -> None:
    """A delete that has already committed must not fail its response over cleanup: the
    intent keeps the pointer, and startup retries the unlinks."""
    document_id = _document(db, class_id)
    stored = _stored_path(db, document_id)
    crash_patch.setattr(storage_intents, "run_document_cleanup", _raise_os_error)

    response = client.delete(f"/api/documents/{document_id}")

    assert response.status_code == 204
    assert len(_intents(db)) == 1

    crash_patch.undo()
    settled, _ = storage_intents.reconcile_storage(db)

    assert settled == 1
    assert not stored.exists()
    assert _intents(db) == []


def test_delete_recovery_is_idempotent(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    crash_patch: pytest.MonkeyPatch,
) -> None:
    document_id = _document(db, class_id)
    crash_patch.setattr(storage_intents, "run_document_cleanup", _crash)
    client.delete(f"/api/documents/{document_id}")

    crash_patch.undo()
    storage_intents.reconcile_storage(db)
    settled_again, swept_again = storage_intents.reconcile_storage(db)

    assert (settled_again, swept_again) == (0, 0)


# --- Class delete: every document's files go, or the intent survives -----------------


def test_a_crash_after_the_class_delete_commit_finishes_the_cleanup_at_startup(
    db: sqlite3.Connection, class_id: int, crash_patch: pytest.MonkeyPatch
) -> None:
    document_id = _document(db, class_id)
    stored = _stored_path(db, document_id)
    _derived_files(db, document_id)
    crash_patch.setattr(storage_intents, "run_class_cleanup", _raise_os_error)

    classes.delete_class(db, class_id)

    # The class is gone from the database; the files and their intent survive the "crash".
    assert db.execute("select count(*) from classes").fetchone()[0] == 0
    assert stored.exists()
    assert len(_intents(db)) == 1

    crash_patch.undo()
    settled, _ = storage_intents.reconcile_storage(db)

    assert settled == 1
    assert not (settings.uploads_dir / str(class_id)).exists()
    assert not storage_intents.text_path(document_id).exists()
    assert not render.pages_dir(document_id).exists()
    assert _intents(db) == []


def test_a_completed_class_delete_leaves_no_intent(db: sqlite3.Connection, class_id: int) -> None:
    document_id = _document(db, class_id)
    _derived_files(db, document_id)

    classes.delete_class(db, class_id)

    assert not (settings.uploads_dir / str(class_id)).exists()
    assert not storage_intents.text_path(document_id).exists()
    assert _intents(db) == []


def test_a_symlinked_class_directory_is_unlinked_not_followed(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """Tampering: cleanup removes the link itself and never reaches its target."""
    outside = tmp_path / "outside-class-tree"
    outside.mkdir()
    (outside / "victim.txt").write_text("must survive")
    link = settings.uploads_dir / str(class_id)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    classes.delete_class(db, class_id)

    assert not link.exists() and not link.is_symlink()
    assert (outside / "victim.txt").read_text() == "must survive"


# --- The startup orphan sweep --------------------------------------------------------


def test_the_sweep_removes_partials_and_ownerless_files_and_keeps_live_ones(
    db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    live = _stored_path(db, document_id)
    _derived_files(db, document_id)
    class_dir = live.parent
    (class_dir / f"55555-gone.pdf.1234.5678{private.PARTIAL_SUFFIX}").write_bytes(b"half")
    (class_dir / "55555-gone.pdf").write_bytes(b"orphan upload")
    (settings.text_dir / "55555.txt").write_text("orphan text")
    (settings.text_dir / f"9.txt.1.2{private.PARTIAL_SUFFIX}").write_text("half")
    orphan_pages = settings.pages_dir / "55555"
    orphan_pages.mkdir(parents=True, exist_ok=True)
    (orphan_pages / "1@144.png").write_bytes(b"png")

    _, swept = storage_intents.reconcile_storage(db)

    assert swept == 5
    assert live.read_bytes() == b"pdf!"
    assert storage_intents.text_path(document_id).exists()
    assert render.page_path(document_id, 1).exists()
    assert not (class_dir / "55555-gone.pdf").exists()
    assert not (settings.text_dir / "55555.txt").exists()
    assert not orphan_pages.exists()
    assert list(class_dir.glob(f"*{private.PARTIAL_SUFFIX}")) == []
    assert list(settings.text_dir.glob(f"*{private.PARTIAL_SUFFIX}")) == []


def test_the_sweep_never_touches_a_symlink(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-sweep"
    outside.write_bytes(b"outside data")
    class_dir = settings.uploads_dir / str(class_id)
    class_dir.mkdir(parents=True, exist_ok=True)
    planted = class_dir / "77777-planted.pdf"
    planted.symlink_to(outside)

    _, swept = storage_intents.reconcile_storage(db)

    assert swept == 0
    assert planted.is_symlink()
    assert outside.read_bytes() == b"outside data"


def test_the_sweep_removes_the_directory_of_a_class_that_no_longer_exists(
    db: sqlite3.Connection,
) -> None:
    dead_class_dir = settings.uploads_dir / "424242"
    dead_class_dir.mkdir(parents=True, exist_ok=True)
    (dead_class_dir / "31313-old.pdf").write_bytes(b"stale")

    _, swept = storage_intents.reconcile_storage(db)

    assert swept == 1
    assert not dead_class_dir.exists()
