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
from backend.rag import parse, render
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


def test_recognize_marks_the_document_and_queues_it(
    client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list[int]
) -> None:
    """The one action behind both `Read this document` and `Try those pages`."""
    document_id = _document(db, class_id, state="unsupported")

    response = client.post(f"/api/documents/{document_id}/recognize")

    assert response.status_code == 202
    assert response.json()["recognize"] is True
    assert no_worker == [document_id]
    row = db.execute(
        "select state, recognize from documents where id = ?", (document_id,)
    ).fetchone()
    assert (row["state"], row["recognize"]) == ("pending", 1)


def test_recognize_keeps_the_rendered_pages_it_is_about_to_read(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """Unlike a re-ingest, which discards them.

    The rendered pages are recognition's own input, they came from bytes that have not
    changed, and throwing them away would make every retry pay to rasterize the document
    again at 300 dpi.
    """
    document_id = _document(db, class_id, state="ready")
    rendered = render.page_path(document_id, 1, render.RECOGNITION_DPI)
    rendered.parent.mkdir(parents=True, exist_ok=True)
    rendered.write_bytes(b"\x89PNG\r\n\x1a\n rendered")

    client.post(f"/api/documents/{document_id}/recognize")

    assert rendered.exists()


def test_recognize_refuses_a_document_that_is_still_processing(
    client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list[int]
) -> None:
    # The worker is reading this row and about to write its state, which would overwrite
    # the `pending` this would set.
    document_id = _document(db, class_id, state="embedding")

    response = client.post(f"/api/documents/{document_id}/recognize")

    assert response.status_code == 409
    assert no_worker == []
    assert db.execute("select recognize from documents").fetchone()[0] == 0


def test_pages_that_could_not_be_read_are_counted_for_the_row(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """`PageFailureNotice` needs a number, and it is a different number from `pages_skipped`.

    Both can be true of the same document: pages that had no text to find, and pages
    recognition tried and could not transcribe.
    """
    document_id = _document(db, class_id, state="ready")
    db.executemany(
        "insert into document_pages (document_id, page_number, state) values (?, ?, ?)",
        [(document_id, 1, "text"), (document_id, 2, "failed"), (document_id, 3, "failed")],
    )
    db.commit()

    body = client.get(f"/api/classes/{class_id}/documents").json()[0]

    assert body["pages_failed"] == 2


def test_an_image_upload_is_accepted(
    client: TestClient, class_id: int, no_worker: list[int]
) -> None:
    """A scan photographed rather than scanned is still a scan."""
    response = client.post(
        f"/api/classes/{class_id}/documents",
        files={"file": ("whiteboard.PNG", b"\x89PNG\r\n\x1a\n", "application/octet-stream")},
    )

    assert response.status_code == 202
    # The extension decides the mime, not the header the browser guessed.
    assert response.json()["mime"] == "image/png"
    assert no_worker == [response.json()["id"]]


def test_a_folder_upload_is_named_after_the_file_rather_than_the_folder(
    client: TestClient, class_id: int, no_worker: list[int]
) -> None:
    """A folder upload sends each file's path relative to the folder as its name.

    Kept whole, that made every row in the list read as the folder it came from, all of
    them truncated to the same few characters. The class is the folder here.
    """
    response = client.post(
        f"/api/classes/{class_id}/documents",
        files={"file": ("course_files/week 3/CE203_Lab3.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 202
    assert response.json()["filename"] == "CE203_Lab3.pdf"
    assert no_worker == [response.json()["id"]]


def test_a_webp_upload_is_refused_naming_the_types_that_work(
    client: TestClient, class_id: int
) -> None:
    """WebP is absent because this PyMuPDF build will not decode one, which is checked
    rather than assumed. The error names what actually works."""
    response = client.post(
        f"/api/classes/{class_id}/documents",
        files={"file": ("scan.webp", b"RIFF....WEBP", "image/webp")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == parse.UNSUPPORTED_MESSAGE
    assert "PNG" in response.json()["detail"]


def _sectioned_chunk(
    db: sqlite3.Connection,
    document_id: int,
    class_id: int,
    path: str | None,
    number: str | None = None,
    page: int | None = None,
) -> None:
    db.execute(
        "insert into chunks (document_id, class_id, content, token_count, page_number, "
        "section_path, section_number, doc_type, embedding_model, embedding_dim) "
        "values (?, ?, 'text', 2, ?, ?, ?, 'textbook', 'm', 768)",
        (document_id, class_id, page, path, number),
    )
    db.commit()


def test_the_outline_reports_the_structure_the_chunks_were_indexed_under(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """Read from the chunks, not from the PDF's own table of contents.

    The outline in the file is what the publisher wrote. This is what actually partitions
    retrieval, and the two differ exactly when something went wrong.
    """
    document_id = _document(db, class_id)
    _sectioned_chunk(db, document_id, class_id, "Vector Spaces", "4", page=90)
    _sectioned_chunk(db, document_id, class_id, "Vector Spaces / Subspaces", "4.1", page=92)
    _sectioned_chunk(db, document_id, class_id, "Vector Spaces / Subspaces", "4.1", page=95)

    body = client.get(f"/api/documents/{document_id}/outline").json()

    assert [(s["path"], s["depth"]) for s in body["sections"]] == [
        ("Vector Spaces", 1),
        ("Vector Spaces / Subspaces", 2),
    ]
    subsection = body["sections"][1]
    assert (subsection["number"], subsection["first_page"], subsection["last_page"]) == (
        "4.1",
        92,
        95,
    )
    assert subsection["chunk_count"] == 2
    assert (body["chunk_count"], body["sectioned_count"]) == (3, 3)


def test_a_document_read_as_one_flat_blob_says_so_rather_than_returning_nothing(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """The whole point of the disclosure, per pillar 3.

    An empty section list next to a chunk count is the difference between "this document
    has no structure" and "this screen has not loaded", and a student whose book was
    flattened has no other way to discover it except by noticing the answers got worse.
    """
    document_id = _document(db, class_id)
    _sectioned_chunk(db, document_id, class_id, None)
    _sectioned_chunk(db, document_id, class_id, None)

    body = client.get(f"/api/documents/{document_id}/outline").json()

    assert body["sections"] == []
    assert (body["chunk_count"], body["sectioned_count"]) == (2, 0)


def test_the_outline_of_an_unknown_document_is_a_404(client: TestClient) -> None:
    assert client.get("/api/documents/9999/outline").status_code == 404
