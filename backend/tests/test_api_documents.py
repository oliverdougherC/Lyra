"""Contract tests for moving a document between classes.

The worker is never started: `enqueue` is stubbed, so a move stays a pure write and what
is under test is the state the document is left in rather than what ingestion then does
with it.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlite_vec
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_documents
from backend.config import settings
from backend.core.errors import LyraError
from backend.storage.database import connect, get_db


def _request_db() -> Iterator[sqlite3.Connection]:
    """A connection to the temporary database, opened inside the calling thread."""
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
    """A TestClient over an app carrying only the documents router."""
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(routes_documents.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def other_class_id(db: sqlite3.Connection) -> int:
    """A second class, since a move needs somewhere to go."""
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


def _embedded_chunk(db: sqlite3.Connection, document_id: int, class_id: int) -> int:
    """One chunk and its vector, so the move has an index to invalidate."""
    cursor = db.execute(
        "insert into chunks (document_id, class_id, content, token_count, doc_type, "
        "embedding_model, embedding_dim) values (?, ?, 'text', 2, 'notes', 'm', 768)",
        (document_id, class_id),
    )
    chunk_id = int(cursor.lastrowid or 0)
    db.execute(
        "insert into chunk_embeddings (chunk_id, class_id, embedding) values (?, ?, ?)",
        (chunk_id, class_id, sqlite_vec.serialize_float32([0.0] * 768)),
    )
    db.commit()
    return chunk_id


def test_move_refiles_the_document_and_queues_a_reindex(
    client: TestClient, db: sqlite3.Connection, class_id: int, other_class_id: int, no_worker: list
) -> None:
    document_id = _document(db, class_id)
    _embedded_chunk(db, document_id, class_id)

    response = client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})

    assert response.status_code == 202
    body = response.json()
    assert body["class_id"] == other_class_id
    # Back to the start of ingestion: chunks carry the class they were indexed under, so
    # the document is not searchable in its new class until it has been read again.
    assert body["state"] == "pending"
    assert no_worker == [document_id]
    assert db.execute("select count(*) from chunks").fetchone()[0] == 0
    assert db.execute("select count(*) from chunk_embeddings").fetchone()[0] == 0


def test_move_relocates_the_file_into_the_new_class_directory(
    client: TestClient, db: sqlite3.Connection, class_id: int, other_class_id: int
) -> None:
    document_id = _document(db, class_id)
    original = Path(
        str(
            db.execute("select stored_path from documents where id = ?", (document_id,)).fetchone()[
                0
            ]
        )
    )

    client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})

    stored = Path(
        str(
            db.execute("select stored_path from documents where id = ?", (document_id,)).fetchone()[
                0
            ]
        )
    )
    assert stored.parent == settings.uploads_dir / str(other_class_id)
    assert stored.read_bytes() == b"pdf!"
    assert not original.exists()


def test_move_forgets_facts_only_this_document_supported(
    client: TestClient, db: sqlite3.Connection, class_id: int, other_class_id: int
) -> None:
    document_id = _document(db, class_id)
    fact_id = int(
        db.execute(
            "insert into profile_facts (class_id, kind, label, value, confidence, "
            "source_document_id) values (?, 'deadline', 'Midterm', 'Oct 3', 'high', ?)",
            (class_id, document_id),
        ).lastrowid
        or 0
    )
    db.execute(
        "insert into profile_fact_sources (fact_id, document_id) values (?, ?)",
        (fact_id, document_id),
    )
    db.commit()

    client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})

    # The class it left should not go on asserting what only that file ever said.
    assert db.execute("select count(*) from profile_facts").fetchone()[0] == 0


def test_move_to_the_same_class_is_a_no_op(
    client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list
) -> None:
    document_id = _document(db, class_id)
    _embedded_chunk(db, document_id, class_id)

    response = client.post(f"/api/documents/{document_id}/move", json={"class_id": class_id})

    assert response.status_code == 202
    assert response.json()["state"] == "ready"
    assert no_worker == []
    assert db.execute("select count(*) from chunks").fetchone()[0] == 1


def test_move_refuses_a_document_that_is_still_processing(
    client: TestClient, db: sqlite3.Connection, class_id: int, other_class_id: int
) -> None:
    document_id = _document(db, class_id, state="parsing")

    response = client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})

    assert response.status_code == 409
    assert "lecture-2.pdf" in response.json()["detail"]
    assert db.execute("select class_id from documents").fetchone()[0] == class_id


def test_move_refuses_a_document_a_solution_set_is_built_from(
    client: TestClient, db: sqlite3.Connection, class_id: int, other_class_id: int
) -> None:
    document_id = _document(db, class_id)
    artifact_id = int(
        db.execute(
            "insert into artifacts (class_id, kind, title, state) "
            "values (?, 'solution_set', 'Homework 4', 'ready')",
            (class_id,),
        ).lastrowid
        or 0
    )
    db.execute(
        "insert into artifact_sources (artifact_id, document_id, role, ordinal) "
        "values (?, ?, 'problem_set', 0)",
        (artifact_id, document_id),
    )
    db.commit()

    response = client.post(f"/api/documents/{document_id}/move", json={"class_id": other_class_id})

    assert response.status_code == 409
    assert "solution set" in response.json()["detail"]


def test_move_to_an_unknown_class_is_a_404(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)

    response = client.post(f"/api/documents/{document_id}/move", json={"class_id": 9999})

    assert response.status_code == 404
    assert db.execute("select class_id from documents").fetchone()[0] == class_id
