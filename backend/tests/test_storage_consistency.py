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

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pymupdf
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_documents
from backend.config import settings
from backend.core import classes, ingestion, ownership, storage_intents
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


def _created_at(db: sqlite3.Connection, document_id: int) -> str:
    row = db.execute("select created_at from documents where id = ?", (document_id,)).fetchone()
    return str(row["created_at"])


def _intent_rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        "select id, kind, document_id, class_id, payload, blocked_reason from storage_intents "
        "order by id"
    ).fetchall()


def _renderable_pdf(path: Path) -> Path:
    """A real one-page PDF, for tests where the late writer genuinely rasterizes."""
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), "late writer")
    document.save(path)
    document.close()
    return path


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
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = _document(db, class_id)
    created_at = _created_at(db, document_id)
    storage_intents.text_path(document_id).parent.mkdir(parents=True, exist_ok=True)
    storage_intents.text_path(document_id).write_text("previous extraction")
    monkeypatch.setattr(os, "replace", _raise_os_error)

    with pytest.raises(OSError, match="injected"):
        ingestion._write_extracted_text(document_id, "interrupted extraction", created_at)

    assert storage_intents.text_path(document_id).read_text() == "previous extraction"


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


# --- Concurrent lifecycle operations: every mutation proves it owns what it observed ---
#
# The lifecycle mutex serializes these in production. These tests deliberately weaken it
# to a reentrant lock so a competing operation can run *inside* the first one's critical
# section - the exact interleaving the mutex forbids - and prove the second line of
# defence: conditional (compare-and-swap) writes and under-write-lock re-reads mean a
# request acting on stale observations gets a clean refusal, never a split-brain commit.


@pytest.fixture
def overlapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model a second request that slipped past serialization.

    Replaces the process-wide lifecycle mutex with a reentrant lock, so a test seam can
    run a whole competing operation on its own connection from inside the first
    operation's critical section, deterministically and on one thread.
    """
    monkeypatch.setattr(ownership, "_lifecycle_mutex", threading.RLock())


def _second_request(fn: Callable[[sqlite3.Connection], object]) -> object:
    """Run a competing route handler the way a second request would: its own connection."""
    conn = connect()
    try:
        return fn(conn)
    finally:
        conn.close()


def test_two_concurrent_moves_of_one_document_cannot_split_brain(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    other_class_id: int,
    no_worker: list[int],
    overlapped: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Move A->B races move A->C; exactly one wins and DB, file, and intents agree.

    The interleaving is the reviewer's: both moves read the document `ready` at its
    source, the competitor commits first, and the loser must observe that its
    assumptions no longer hold instead of committing its own transition, renaming a file
    that is not where it believed, or compensating over the winner's state.
    """
    cursor = db.execute("insert into classes (name) values ('Statistics')")
    db.commit()
    third_class_id = int(cursor.lastrowid or 0)
    document_id = _document(db, class_id)
    real_get_class = classes.get_class
    ran = False

    def interleave(conn: sqlite3.Connection, target_class_id: int) -> dict[str, object]:
        nonlocal ran
        if not ran:
            ran = True
            _second_request(
                lambda inner: routes_documents.move_document(
                    document_id,
                    routes_documents.DocumentMove(class_id=third_class_id),
                    inner,
                )
            )
        return real_get_class(conn, target_class_id)

    monkeypatch.setattr(routes_documents, "get_class", interleave)

    response = client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})

    # The competitor won; this request's compare-and-swap missed and nothing it staged
    # survived its rollback.
    assert response.status_code == 409
    row = _row(db, document_id)
    assert int(row["class_id"]) == third_class_id
    destination = Path(str(row["stored_path"]))
    assert destination.parent == settings.uploads_dir / str(third_class_id)
    assert destination.read_bytes() == b"pdf!"
    assert list((settings.uploads_dir / str(class_id)).iterdir()) == []
    assert not (settings.uploads_dir / str(other_class_id)).exists()
    assert _intents(db) == []
    # Only the winning move queued a re-ingest.
    assert no_worker == [document_id]


def test_a_move_racing_a_delete_gets_a_clean_refusal(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    other_class_id: int,
    no_worker: list[int],
    overlapped: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The document is deleted between the move's reads and its commit: 404, no revival."""
    document_id = _document(db, class_id)
    _derived_files(db, document_id)
    real_get_class = classes.get_class
    ran = False

    def interleave(conn: sqlite3.Connection, target_class_id: int) -> dict[str, object]:
        nonlocal ran
        if not ran:
            ran = True
            _second_request(lambda inner: routes_documents.delete_document(document_id, inner))
        return real_get_class(conn, target_class_id)

    monkeypatch.setattr(routes_documents, "get_class", interleave)

    response = client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})

    assert response.status_code == 404
    assert db.execute("select count(*) from documents").fetchone()[0] == 0
    assert list((settings.uploads_dir / str(class_id)).iterdir()) == []
    assert not storage_intents.text_path(document_id).exists()
    assert _intents(db) == []
    assert no_worker == []


def test_a_delete_racing_a_move_removes_the_file_where_it_actually_is(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    other_class_id: int,
    overlapped: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A move commits between the delete's read and its transaction.

    The delete re-reads the stored path under SQLite's write lock, so its intent records
    the file's real location - the move's destination - and the terminal state is: no
    row, no file anywhere, no intent. The stale first read must not leave the moved file
    alive behind a settled delete.
    """
    document_id = _document(db, class_id)
    source = _stored_path(db, document_id)
    real_delete_chunks = ingestion.delete_chunks
    ran = False

    def interleave(conn: sqlite3.Connection, target_id: int) -> None:
        nonlocal ran
        if not ran:
            ran = True
            _second_request(
                lambda inner: routes_documents.move_document(
                    target_id,
                    routes_documents.DocumentMove(class_id=other_class_id),
                    inner,
                )
            )
        real_delete_chunks(conn, target_id)

    monkeypatch.setattr(routes_documents, "delete_chunks", interleave)

    response = client.delete(f"/api/documents/{document_id}")

    assert response.status_code == 204
    assert db.execute("select count(*) from documents").fetchone()[0] == 0
    assert not source.exists()
    moved = settings.uploads_dir / str(other_class_id) / f"{document_id}-lecture-2.pdf"
    assert not moved.exists()
    assert _intents(db) == []


def test_stale_compensation_cannot_undo_a_newer_lifecycle_mutation(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    other_class_id: int,
    no_worker: list[int],
    overlapped: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The move's rename fails after a delete has already legitimately won.

    Compensation restores the row to the source location - but only if the row is still
    exactly what this move committed. Here the competing delete removed the document
    while the rename was failing, so compensation must withdraw its intent and stop:
    resurrecting the row would undo a delete the student was told succeeded.
    """
    document_id = _document(db, class_id)
    source = _stored_path(db, document_id)
    ran = False

    def failing_rename(src: Path, dst: Path) -> None:
        nonlocal ran
        if not ran:
            ran = True
            _second_request(lambda inner: routes_documents.delete_document(document_id, inner))
        raise OSError("injected rename failure")

    monkeypatch.setattr(storage_intents, "perform_move", failing_rename)

    response = client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})

    assert response.status_code == 400
    # The delete's outcome stands: no row was restored, and nothing was queued.
    assert db.execute("select count(*) from documents").fetchone()[0] == 0
    assert _intents(db) == []
    assert no_worker == []
    # The one remnant is the source file the failed rename never touched and the delete's
    # cleanup (aimed at the committed destination) could not know about. It has no row
    # and no intent, which is exactly the orphan the startup sweep exists for.
    assert source.exists()
    _, swept = storage_intents.reconcile_storage(db)
    assert swept == 1
    assert not source.exists()


# --- Late writers: derived state cannot reappear after a completed delete --------------


def test_a_released_text_writer_cannot_resurrect_a_deleted_document(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """The Blocker-2 sequence: worker parses, delete fully commits/cleans/settles, the
    old worker is then released and tries to publish. Nothing may reappear."""
    document_id = _document(db, class_id)
    created_at = _created_at(db, document_id)
    _derived_files(db, document_id)

    response = client.delete(f"/api/documents/{document_id}")
    assert response.status_code == 204
    assert _intents(db) == []
    assert not storage_intents.text_path(document_id).exists()

    published = ingestion._write_extracted_text(document_id, "resurrected text", created_at)

    assert published is False
    assert not storage_intents.text_path(document_id).exists()


def test_a_text_writer_for_a_replaced_document_is_refused(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """Delete plus immediate re-upload hands the id to a different file; the old run's
    text must not land on the new document."""
    document_id = _document(db, class_id)
    stale_identity = _created_at(db, document_id)
    assert client.delete(f"/api/documents/{document_id}").status_code == 204
    db.execute(
        "insert into documents (id, class_id, filename, stored_path, mime, byte_size, "
        "state, created_at) values (?, ?, 'newer.pdf', '', 'application/pdf', 0, 'ready', "
        "'2099-01-01T00:00:00.000Z')",
        (document_id, class_id),
    )
    db.commit()

    published = ingestion._write_extracted_text(document_id, "old file text", stale_identity)

    assert published is False
    assert not storage_intents.text_path(document_id).exists()


def test_a_released_page_renderer_cannot_repopulate_a_deleted_cache(
    client: TestClient, db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """An in-flight page render finishes after the delete: the PNG must not appear."""
    document_id = _document(db, class_id)
    created_at = _created_at(db, document_id)
    _derived_files(db, document_id)
    # The bytes the renderer already holds, surviving the delete the way an open file or
    # a parsed representation would in the worker.
    source = _renderable_pdf(tmp_path / "still-open.pdf")

    assert client.delete(f"/api/documents/{document_id}").status_code == 204
    assert not render.pages_dir(document_id).exists()

    with pytest.raises(Exception, match="does not exist"):
        render.render_page(document_id, source, "application/pdf", 1, created_at=created_at)

    directory = render.pages_dir(document_id)
    assert not directory.exists() or list(directory.iterdir()) == []


def test_a_released_figure_renderer_cannot_repopulate_a_deleted_cache(
    client: TestClient, db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    document_id = _document(db, class_id)
    created_at = _created_at(db, document_id)
    source = _renderable_pdf(tmp_path / "still-open.pdf")

    assert client.delete(f"/api/documents/{document_id}").status_code == 204

    with pytest.raises(Exception, match="does not exist"):
        render.render_figure(
            document_id,
            source,
            "application/pdf",
            1,
            99,
            (0.1, 0.1, 0.5, 0.5),
            created_at=created_at,
        )

    directory = render.pages_dir(document_id)
    assert not directory.exists() or list(directory.iterdir()) == []


# --- Blocked intents: recovery never acts outside the tree, never settles skipped work,
# --- and never lets one bad intent take down startup ----------------------------------


def _insert_intent(
    db: sqlite3.Connection,
    kind: str,
    *,
    document_id: int | None = None,
    class_id: int | None = None,
    payload: str = "{}",
) -> int:
    cursor = db.execute(
        "insert into storage_intents (kind, document_id, class_id, payload) values (?, ?, ?, ?)",
        (kind, document_id, class_id, payload),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def test_an_out_of_root_delete_intent_is_blocked_not_silently_settled(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    outside = tmp_path / "kept-outside.pdf"
    outside.write_bytes(b"must survive")
    _insert_intent(
        db,
        storage_intents.DELETE_DOCUMENT,
        document_id=424242,
        payload=json.dumps({"stored_path": str(outside)}),
    )

    settled, _ = storage_intents.reconcile_storage(db)

    assert settled == 0
    assert outside.read_bytes() == b"must survive"
    rows = _intent_rows(db)
    assert len(rows) == 1
    assert rows[0]["blocked_reason"] == "recorded path is outside the current data directory"
    # Idempotent, and still not crashing or settling on the next startup.
    settled_again, _ = storage_intents.reconcile_storage(db)
    assert settled_again == 0
    assert len(_intent_rows(db)) == 1


def test_a_blocked_intent_settles_by_itself_once_its_path_is_back_inside(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocked means re-validated each startup, not abandoned: a data directory that
    moved back makes the same intent actionable again."""
    outside = tmp_path / "was-outside.pdf"
    outside.write_bytes(b"cleanup owed")
    _insert_intent(
        db,
        storage_intents.DELETE_DOCUMENT,
        document_id=424242,
        payload=json.dumps({"stored_path": str(outside)}),
    )
    storage_intents.reconcile_storage(db)
    assert _intent_rows(db)[0]["blocked_reason"] is not None

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    settled, _ = storage_intents.reconcile_storage(db)

    assert settled == 1
    assert not outside.exists()
    assert _intent_rows(db) == []


def test_an_out_of_root_move_source_is_blocked(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    document_id = _document(db, class_id)
    destination = _stored_path(db, document_id)
    outside_source = tmp_path / "outside-source.pdf"
    outside_source.write_bytes(b"not lyra's to move")
    _insert_intent(
        db,
        storage_intents.MOVE_DOCUMENT,
        document_id=document_id,
        payload=json.dumps({"source": str(outside_source), "destination": str(destination)}),
    )

    settled, _ = storage_intents.reconcile_storage(db)

    assert settled == 0
    assert outside_source.read_bytes() == b"not lyra's to move"
    assert (
        _intent_rows(db)[0]["blocked_reason"]
        == "recorded move source is outside the uploads directory"
    )


def test_an_out_of_root_move_destination_is_blocked(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """A destination outside the tree must neither be renamed to nor crash startup with
    the owned-root helper's ValueError."""
    document_id = _document(db, class_id)
    source = _stored_path(db, document_id)
    outside_destination = tmp_path / "outside-destination.pdf"
    # The row must point at the recorded destination for recovery to consider the rename
    # owed at all; that is exactly the shape a corrupted payload would need.
    db.execute(
        "update documents set stored_path = ? where id = ?",
        (str(outside_destination), document_id),
    )
    db.commit()
    _insert_intent(
        db,
        storage_intents.MOVE_DOCUMENT,
        document_id=document_id,
        payload=json.dumps({"source": str(source), "destination": str(outside_destination)}),
    )

    settled, _ = storage_intents.reconcile_storage(db)

    assert settled == 0
    assert source.exists()
    assert not outside_destination.exists()
    assert (
        _intent_rows(db)[0]["blocked_reason"]
        == "recorded move destination is outside the uploads directory"
    )


def test_a_malformed_intent_payload_is_blocked_with_its_evidence_kept(
    db: sqlite3.Connection,
) -> None:
    _insert_intent(db, storage_intents.DELETE_DOCUMENT, document_id=424242, payload="{not json")

    settled, _ = storage_intents.reconcile_storage(db)

    assert settled == 0
    rows = _intent_rows(db)
    assert rows[0]["blocked_reason"] == "unreadable payload"
    # The payload is the durable evidence and survives verbatim for manual handling.
    assert rows[0]["payload"] == "{not json"


def test_a_malformed_move_payload_with_a_live_document_is_blocked(
    db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    _insert_intent(db, storage_intents.MOVE_DOCUMENT, document_id=document_id, payload="{}")

    settled, _ = storage_intents.reconcile_storage(db)

    assert settled == 0
    assert _intent_rows(db)[0]["blocked_reason"] == "unreadable move payload"
    # The document itself is untouched.
    assert _stored_path(db, document_id).exists()


def test_an_unknown_intent_kind_is_blocked_rather_than_dropped(
    db: sqlite3.Connection,
) -> None:
    """A kind this build does not understand is owed work, not garbage. (Constructed
    directly: the table's check constraint stops SQL from planting one, so this models a
    downgraded install reading a newer schema's row.)"""
    row = {"id": 77, "kind": "shred_uploads", "document_id": None, "class_id": None}

    with pytest.raises(storage_intents.IntentBlockedError, match="unknown intent kind"):
        storage_intents._settle_one(db, row, {})


def test_a_symlinked_move_destination_parent_is_kept_for_retry_not_blocked(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    """Tampering inside the tree is a refusal (PrivacyContractError), and the intent is
    kept unblocked: remove the link and the next startup completes the move."""
    document_id = _document(db, class_id)
    source = _stored_path(db, document_id)
    outside = tmp_path / "outside-class"
    outside.mkdir()
    linked_dir = settings.uploads_dir / "999"
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    linked_dir.symlink_to(outside)
    destination = linked_dir / f"{document_id}-lecture-2.pdf"
    db.execute("update documents set stored_path = ? where id = ?", (str(destination), document_id))
    db.commit()
    _insert_intent(
        db,
        storage_intents.MOVE_DOCUMENT,
        document_id=document_id,
        payload=json.dumps({"source": str(source), "destination": str(destination)}),
    )

    settled, _ = storage_intents.reconcile_storage(db)

    assert settled == 0
    assert source.exists()
    assert list(outside.iterdir()) == []
    row = _intent_rows(db)[0]
    # Kept for retry, not classified unrecoverable: the refusal is environmental.
    assert row["blocked_reason"] is None


def test_one_bad_intent_does_not_stop_later_valid_intents_from_settling(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-bad.pdf"
    outside.write_bytes(b"blocked")
    _insert_intent(
        db,
        storage_intents.DELETE_DOCUMENT,
        document_id=424242,
        payload=json.dumps({"stored_path": str(outside)}),
    )
    # A valid delete intent recorded after the bad one: a document whose row is gone but
    # whose files survived a crash before cleanup.
    orphan = settings.uploads_dir / str(class_id) / "31313-crashed.pdf"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"owed cleanup")
    _insert_intent(
        db,
        storage_intents.DELETE_DOCUMENT,
        document_id=31313,
        payload=json.dumps({"stored_path": str(orphan)}),
    )

    settled, _ = storage_intents.reconcile_storage(db)

    assert settled == 1
    assert not orphan.exists()
    assert outside.exists()
    rows = _intent_rows(db)
    assert len(rows) == 1
    assert rows[0]["blocked_reason"] is not None
