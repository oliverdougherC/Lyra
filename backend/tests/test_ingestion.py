"""Ingestion state transitions, including the paths that must not become failures.

`run_ingestion` is called directly rather than through the queue, so the tests stay
synchronous and deterministic. The embedding server and the tutor model are faked at the
seam `backend.core.ingestion` imports them through, so nothing here starts a subprocess
or touches the network. PDFs are built with PyMuPDF at test time rather than committed
as binary fixtures, so what a page contains is readable in the test that needs it.
"""

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pymupdf
import pytest

from backend.config import settings
from backend.core import ingestion
from backend.core.ingestion import (
    INTERRUPTED_MESSAGE,
    SCANNED_MESSAGE,
    reconcile_interrupted,
    run_ingestion,
)
from backend.rag.embed import EMBEDDING_DIM, EMBEDDING_MODEL
from backend.rag.parse import PDF_MIME, UNREADABLE_PDF_MESSAGE
from backend.storage.database import connect

MARKDOWN_MIME = "text/markdown"


def _vectors(texts: list[str]) -> list[list[float]]:
    """One deterministic 768-float vector per text, standing in for the local model."""
    return [[round(0.001 * (len(text) % 97), 3)] * EMBEDDING_DIM for text in texts]


@pytest.fixture(autouse=True)
def fake_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the embedding server and profile extraction where ingestion looks them up."""
    monkeypatch.setattr(ingestion, "embed_documents", _vectors)
    monkeypatch.setattr(ingestion, "extract_facts", lambda conn, document_id, text: None)


def _prose(words: int, seed: int = 0) -> str:
    """Deterministic filler that clears the scanned-page threshold comfortably."""
    return " ".join(f"w{(index + seed) % 89:02d}" for index in range(words))


def _write_pdf(path: Path, pages: Sequence[str]) -> Path:
    """Build a PDF with one page per string. An empty string leaves the page blank."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        if text:
            remaining = page.insert_textbox(pymupdf.Rect(36, 36, 559, 770), text, fontsize=8)
            assert remaining >= 0, "fixture text does not fit on one page"
    document.save(path)
    document.close()
    return path


def _write_markdown(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _seed_document(
    db: sqlite3.Connection,
    class_id: int,
    stored_path: Path,
    *,
    filename: str | None = None,
    mime: str = PDF_MIME,
) -> int:
    """A `documents` row in `pending`, as upload would have left it."""
    byte_size = stored_path.stat().st_size if stored_path.exists() else 0
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, ?, ?, ?, ?, 'pending')",
        (class_id, filename or stored_path.name, str(stored_path), mime, byte_size),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def _document(db: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    row = db.execute("select * from documents where id = ?", (document_id,)).fetchone()
    assert row is not None
    return row


def _chunk_count(db: sqlite3.Connection, document_id: int) -> int:
    row = db.execute("select count(*) from chunks where document_id = ?", (document_id,)).fetchone()
    return int(row[0])


def _homework_markdown() -> str:
    """A homework document long enough to need several chunks."""
    problems = "\n\n".join(f"{number}. {_prose(220, number)}" for number in range(1, 6))
    return f"MATH 201 Homework 3\nDue Friday.\n\n{problems}\n"


def test_a_fully_scanned_pdf_ends_unsupported_with_its_file_kept(
    db: sqlite3.Connection, class_id: int
) -> None:
    # Three pages carrying a page number and nothing else, which is what a scan extracts.
    stored = _write_pdf(settings.uploads_dir / "scanned.pdf", ["1", "2", "3"])
    document_id = _seed_document(db, class_id, stored)

    run_ingestion(document_id)

    row = _document(db, document_id)
    assert row["state"] == "unsupported"
    assert row["error_message"] == SCANNED_MESSAGE
    assert (row["pages_total"], row["pages_skipped"]) == (3, 3)
    # `unsupported` is not `failed`: the file is kept so Phase 2 re-ingests it in place
    # rather than asking the student to upload it again.
    assert stored.exists()
    assert _chunk_count(db, document_id) == 0
    assert db.execute("select count(*) from chunk_embeddings").fetchone()[0] == 0


def test_a_mixed_document_ends_ready_with_the_scanned_page_counted(
    db: sqlite3.Connection, class_id: int
) -> None:
    stored = _write_pdf(
        settings.uploads_dir / "mixed.pdf",
        [_prose(180), "4", _prose(180, seed=13)],
    )
    document_id = _seed_document(db, class_id, stored, filename="week3-notes.pdf")

    run_ingestion(document_id)

    row = _document(db, document_id)
    assert row["state"] == "ready"
    assert row["pages_skipped"] == 1
    assert (row["pages_total"], row["pages_done"]) == (3, 2)
    assert row["error_message"] is None
    assert _chunk_count(db, document_id) > 0
    # The readable pages are searchable; only the scanned one was dropped.
    pages = {row[0] for row in db.execute("select distinct page_number from chunks")}
    assert pages == {1, 3}


def test_every_chunk_lands_with_an_embedding_row(db: sqlite3.Connection, class_id: int) -> None:
    stored = _write_markdown(settings.uploads_dir / "hw3.md", _homework_markdown())
    document_id = _seed_document(db, class_id, stored, mime=MARKDOWN_MIME)

    run_ingestion(document_id)

    chunks = db.execute(
        "select id, class_id, doc_type, embedding_model, embedding_dim from chunks "
        "where document_id = ?",
        (document_id,),
    ).fetchall()
    assert len(chunks) > 1
    for chunk in chunks:
        # class_id is denormalized onto the chunk so retrieval can partition without a join.
        assert chunk["class_id"] == class_id
        assert chunk["doc_type"] == "homework"
        assert (chunk["embedding_model"], chunk["embedding_dim"]) == (
            EMBEDDING_MODEL,
            EMBEDDING_DIM,
        )
        vectors = db.execute(
            "select count(*) from chunk_embeddings where chunk_id = ?", (chunk["id"],)
        ).fetchone()[0]
        assert vectors == 1

    total = db.execute("select count(*) from chunk_embeddings").fetchone()[0]
    assert total == len(chunks)


def test_reingesting_replaces_chunks_instead_of_duplicating_them(
    db: sqlite3.Connection, class_id: int
) -> None:
    stored = _write_markdown(settings.uploads_dir / "hw3.md", _homework_markdown())
    document_id = _seed_document(db, class_id, stored, mime=MARKDOWN_MIME)

    run_ingestion(document_id)
    first = _chunk_count(db, document_id)
    run_ingestion(document_id)

    assert _chunk_count(db, document_id) == first
    assert db.execute("select count(*) from chunk_embeddings").fetchone()[0] == first


def test_a_poller_sees_the_embedding_stage_before_the_document_is_ready(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = _write_markdown(settings.uploads_dir / "hw3.md", _homework_markdown())
    document_id = _seed_document(db, class_id, stored, mime=MARKDOWN_MIME)
    observed: list[str] = []

    def spy(texts: list[str]) -> list[list[float]]:
        # A separate connection, as the status endpoint would use: it can only see the
        # stage if the transition was committed rather than held until the end.
        poller = connect()
        try:
            observed.append(
                poller.execute(
                    "select state from documents where id = ?", (document_id,)
                ).fetchone()[0]
            )
        finally:
            poller.close()
        return _vectors(texts)

    monkeypatch.setattr(ingestion, "embed_documents", spy)
    run_ingestion(document_id)

    assert observed and set(observed) == {"embedding"}
    assert _document(db, document_id)["state"] == "ready"


def test_extracted_text_is_kept_so_a_reindex_never_reparses(
    db: sqlite3.Connection, class_id: int
) -> None:
    stored = _write_pdf(settings.uploads_dir / "notes.pdf", [_prose(150)])
    document_id = _seed_document(db, class_id, stored)

    run_ingestion(document_id)

    text_file = settings.text_dir / f"{document_id}.txt"
    assert text_file.exists()
    assert "w00" in text_file.read_text(encoding="utf-8")


def test_a_failed_extraction_still_lands_the_document_ready(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(conn: sqlite3.Connection, document_id: int, text: str) -> str | None:
        raise RuntimeError("the tutor endpoint hung up")

    monkeypatch.setattr(ingestion, "extract_facts", boom)
    stored = _write_markdown(settings.uploads_dir / "hw3.md", _homework_markdown())
    document_id = _seed_document(db, class_id, stored, mime=MARKDOWN_MIME)

    run_ingestion(document_id)

    row = _document(db, document_id)
    # The chunks are already stored, so the document is searchable either way.
    assert row["state"] == "ready"
    assert row["stage_detail"] == "extraction_failed"
    assert _chunk_count(db, document_id) > 0


def test_a_skipped_extraction_records_its_reason(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ingestion, "extract_facts", lambda conn, document_id, text: "remote_unacknowledged"
    )
    stored = _write_markdown(settings.uploads_dir / "hw3.md", _homework_markdown())
    document_id = _seed_document(db, class_id, stored, mime=MARKDOWN_MIME)

    run_ingestion(document_id)

    row = _document(db, document_id)
    assert row["state"] == "ready"
    assert row["stage_detail"] == "remote_unacknowledged"


def test_a_document_with_text_but_no_chunks_is_unsupported_too(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ingestion, "chunk_document", lambda parsed, doc_type: [])
    stored = _write_pdf(settings.uploads_dir / "odd.pdf", [_prose(150)])
    document_id = _seed_document(db, class_id, stored)

    run_ingestion(document_id)

    row = _document(db, document_id)
    assert row["state"] == "unsupported"
    assert row["error_message"] == SCANNED_MESSAGE
    assert stored.exists()
    assert _chunk_count(db, document_id) == 0


def test_an_unreadable_file_fails_with_the_stage_and_no_filesystem_path(
    db: sqlite3.Connection, class_id: int
) -> None:
    # The row points at a file that is not there, as it would be after a manual delete.
    missing = settings.uploads_dir / str(class_id) / "gone.pdf"
    document_id = _seed_document(db, class_id, missing)

    run_ingestion(document_id)

    row = _document(db, document_id)
    assert row["state"] == "failed"
    assert row["stage_detail"] == "parsing"
    assert row["error_message"] == UNREADABLE_PDF_MESSAGE
    # PyMuPDF puts the absolute path in its own message. That must not reach the user.
    assert "/" not in row["error_message"]


def test_an_unsupported_mime_fails_rather_than_raising_out_of_the_worker(
    db: sqlite3.Connection, class_id: int
) -> None:
    stored = _write_markdown(settings.uploads_dir / "slides.pptx", "content")
    document_id = _seed_document(db, class_id, stored, mime="application/vnd.ms-powerpoint")

    run_ingestion(document_id)

    row = _document(db, document_id)
    assert row["state"] == "failed"
    assert row["error_message"] == "Unsupported file type. Upload a PDF, TXT, or MD file."


def test_a_document_deleted_before_its_turn_is_skipped(db: sqlite3.Connection) -> None:
    run_ingestion(4242)

    assert db.execute("select count(*) from documents").fetchone()[0] == 0


def test_reconcile_interrupted_fails_a_document_stuck_in_embedding(
    db: sqlite3.Connection, class_id: int
) -> None:
    stored = _write_markdown(settings.uploads_dir / "hw3.md", _homework_markdown())
    stuck = _seed_document(db, class_id, stored, mime=MARKDOWN_MIME)
    finished = _seed_document(db, class_id, stored, mime=MARKDOWN_MIME)
    db.execute("update documents set state = 'embedding' where id = ?", (stuck,))
    db.execute("update documents set state = 'ready' where id = ?", (finished,))
    db.commit()

    assert reconcile_interrupted(db) == 1

    row = _document(db, stuck)
    assert row["state"] == "failed"
    assert row["error_message"] == INTERRUPTED_MESSAGE
    # The stage it died in is kept, so the interface can say where it stopped.
    assert row["stage_detail"] == "embedding"
    assert _document(db, finished)["state"] == "ready"


def test_reconcile_interrupted_counts_every_non_terminal_state(
    db: sqlite3.Connection, class_id: int
) -> None:
    stored = _write_markdown(settings.uploads_dir / "hw3.md", _homework_markdown())
    for state in ("pending", "parsing", "chunking", "embedding", "extracting"):
        document_id = _seed_document(db, class_id, stored, mime=MARKDOWN_MIME)
        db.execute("update documents set state = ? where id = ?", (state, document_id))
    for state in ("ready", "failed", "unsupported"):
        document_id = _seed_document(db, class_id, stored, mime=MARKDOWN_MIME)
        db.execute("update documents set state = ? where id = ?", (state, document_id))
    db.commit()

    assert reconcile_interrupted(db) == 5
    assert reconcile_interrupted(db) == 0
