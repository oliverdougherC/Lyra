"""Contract tests for the class endpoints."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlite_vec
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_classes
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


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    """A TestClient over an app carrying only the class router.

    The override pins the app to the `db` fixture's database rather than to its
    connection object: handlers are sync, so they run in a threadpool, and a `sqlite3`
    connection may only be used from the thread that opened it.
    """
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(routes_classes.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app) as test_client:
        yield test_client


def _insert_class(db: sqlite3.Connection, name: str, last_active_at: str) -> int:
    """A class row with a pinned activity timestamp, so ordering is deterministic."""
    cursor = db.execute(
        "insert into classes (name, last_active_at) values (?, ?)",
        (name, last_active_at),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def _seed_embedded_chunk(db: sqlite3.Connection, class_id: int) -> None:
    """One document, one chunk, and its vector, so deletion has something to cascade."""
    document_id = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, 'notes.pdf', 'notes.pdf', 'application/pdf', 1024, 'ready')",
        (class_id,),
    ).lastrowid
    chunk_id = db.execute(
        "insert into chunks (document_id, class_id, content, token_count, doc_type, "
        "embedding_model, embedding_dim) "
        "values (?, ?, 'the derivative of x squared', 6, 'generic', 'nomic', 768)",
        (document_id, class_id),
    ).lastrowid
    db.execute(
        "insert into chunk_embeddings (chunk_id, class_id, embedding) values (?, ?, ?)",
        (chunk_id, class_id, sqlite_vec.serialize_float32([0.01] * 768)),
    )
    db.commit()


def _embedding_count(db: sqlite3.Connection, class_id: int) -> int:
    row = db.execute(
        "select count(*) from chunk_embeddings where class_id = ?", (class_id,)
    ).fetchone()
    return int(row[0])


def test_create_then_list_round_trips(client: TestClient) -> None:
    created = client.post(
        "/api/classes",
        json={"name": "Calculus II", "code": "MATH 201", "semester": "Fall 2026"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Calculus II"
    assert body["code"] == "MATH 201"
    assert body["document_count"] == 0

    listed = client.get("/api/classes")
    assert listed.status_code == 200
    assert listed.json() == [body]


def test_document_count_reflects_uploaded_documents(
    client: TestClient, db: sqlite3.Connection
) -> None:
    class_id = client.post("/api/classes", json={"name": "Linear Algebra"}).json()["id"]
    _seed_embedded_chunk(db, class_id)

    assert client.get(f"/api/classes/{class_id}").json()["document_count"] == 1


def test_list_orders_by_last_active_descending(client: TestClient, db: sqlite3.Connection) -> None:
    _insert_class(db, "Stale", "2026-01-01 00:00:00")
    _insert_class(db, "Recent", "2026-08-01 00:00:00")

    names = [row["name"] for row in client.get("/api/classes").json()]
    assert names == ["Recent", "Stale"]


def test_whitespace_only_name_is_rejected(client: TestClient) -> None:
    assert client.post("/api/classes", json={"name": "   "}).status_code == 422

    class_id = client.post("/api/classes", json={"name": "Statistics"}).json()["id"]
    assert client.patch(f"/api/classes/{class_id}", json={"name": " "}).status_code == 422
    # The column is not nullable, so an explicit null is bad input, not a cleared name.
    assert client.patch(f"/api/classes/{class_id}", json={"name": None}).status_code == 422


def test_created_name_is_stripped(client: TestClient) -> None:
    assert client.post("/api/classes", json={"name": "  Physics I  "}).json()["name"] == "Physics I"


def test_unknown_id_is_not_found(client: TestClient) -> None:
    assert client.get("/api/classes/404").status_code == 404
    assert client.patch("/api/classes/404", json={"name": "Ghost"}).status_code == 404
    assert client.delete("/api/classes/404").status_code == 404


def test_patch_renames_and_persists(client: TestClient) -> None:
    class_id = client.post("/api/classes", json={"name": "Calc II", "code": "MATH 201"}).json()[
        "id"
    ]

    patched = client.patch(f"/api/classes/{class_id}", json={"name": "Calculus II"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "Calculus II"

    fetched = client.get(f"/api/classes/{class_id}").json()
    assert fetched["name"] == "Calculus II"
    # An absent field is left alone rather than nulled.
    assert fetched["code"] == "MATH 201"


def test_archive_moves_class_in_and_out_of_the_active_list(client: TestClient) -> None:
    class_id = client.post("/api/classes", json={"name": "Finished Course"}).json()["id"]
    assert client.get(f"/api/classes/{class_id}").json()["archived"] is False

    archived = client.patch(f"/api/classes/{class_id}", json={"archived": True})
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    listed = client.get("/api/classes").json()
    assert any(item["id"] == class_id and item["archived"] is True for item in listed)

    restored = client.patch(f"/api/classes/{class_id}", json={"archived": False})
    assert restored.json()["archived"] is False
    assert any(item["id"] == class_id for item in client.get("/api/classes").json())


def test_delete_removes_only_that_class_embeddings(
    client: TestClient, db: sqlite3.Connection
) -> None:
    doomed = client.post("/api/classes", json={"name": "Dropped"}).json()["id"]
    kept = client.post("/api/classes", json={"name": "Kept"}).json()["id"]
    _seed_embedded_chunk(db, doomed)
    _seed_embedded_chunk(db, kept)
    assert _embedding_count(db, doomed) == 1
    assert _embedding_count(db, kept) == 1

    assert client.delete(f"/api/classes/{doomed}").status_code == 204

    assert _embedding_count(db, doomed) == 0
    assert _embedding_count(db, kept) == 1
    assert client.get(f"/api/classes/{doomed}").status_code == 404
    assert (
        db.execute("select count(*) from chunks where class_id = ?", (doomed,)).fetchone()[0] == 0
    )


def test_delete_removes_the_upload_directory(client: TestClient) -> None:
    class_id = client.post("/api/classes", json={"name": "Chemistry"}).json()["id"]
    upload_dir: Path = settings.uploads_dir / str(class_id)
    upload_dir.mkdir(parents=True)
    (upload_dir / "1-syllabus.pdf").write_bytes(b"%PDF-1.7")

    assert client.delete(f"/api/classes/{class_id}").status_code == 204
    assert not upload_dir.exists()


def test_delete_succeeds_without_an_upload_directory(client: TestClient) -> None:
    class_id = client.post("/api/classes", json={"name": "Nothing Uploaded"}).json()["id"]
    assert not (settings.uploads_dir / str(class_id)).exists()

    assert client.delete(f"/api/classes/{class_id}").status_code == 204
