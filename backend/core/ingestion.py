"""Background ingestion: parse, chunk, embed, store, and propose profile facts.

Upload returns `202` and hands the document id to a queue drained by one worker thread.
Everything slow happens there, and progress is visible only through `documents.state`,
so each transition is committed as it happens rather than at the end.

The pipeline has three outcomes. `ready` means searchable. `unsupported` means the file
is a kind Lyra cannot read yet, in practice a scanned PDF: nothing went wrong, the
feature is not built, and the file is kept so Phase 2 can re-ingest it in place.
`failed` means something broke, and carries a message written for the user.
"""

import logging
import queue
import sqlite3
import threading
from pathlib import Path

import sqlite_vec

from backend.config import settings
from backend.core.errors import LyraError
from backend.core.profiles import extract_facts
from backend.rag.chunk import Chunk, chunk_document, detect_doc_type
from backend.rag.embed import BATCH_SIZE as EMBED_BATCH_SIZE
from backend.rag.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_documents
from backend.rag.parse import ParsedDocument, parse_document
from backend.storage.database import connect

logger = logging.getLogger("lyra.ingestion")

PENDING = "pending"
PARSING = "parsing"
CHUNKING = "chunking"
EMBEDDING = "embedding"
EXTRACTING = "extracting"
READY = "ready"
FAILED = "failed"
UNSUPPORTED = "unsupported"

NON_TERMINAL_STATES = (PENDING, PARSING, CHUNKING, EMBEDDING, EXTRACTING)

SCANNED_MESSAGE = "This looks like a scanned document, so there is no text to read yet."
INTERRUPTED_MESSAGE = "Interrupted, please retry"
EXTRACTION_FAILED_DETAIL = "extraction_failed"

_STAGE_FAILURE_MESSAGES = {
    PARSING: "This document could not be read.",
    CHUNKING: "This document could not be prepared for search.",
    EMBEDDING: (
        "This document could not be indexed for search. "
        "The local embedding model may not be running."
    ),
}
_DEFAULT_FAILURE_MESSAGE = "Something went wrong while processing this document."

_INSERT_CHUNK_SQL = """
insert into chunks (
  document_id, class_id, content, token_count, page_number, section_title,
  problem_number, part_index, doc_type, embedding_model, embedding_dim
) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_queue: queue.Queue[int] = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False


def enqueue(document_id: int) -> None:
    """Hand a document to the ingestion worker. Returns immediately."""
    _queue.put(document_id)


def start_worker() -> None:
    """Start the single ingestion worker, once per process.

    Called from the app lifespan, and idempotent so a reload or a second call cannot
    end up with two workers. There is deliberately only one: the local embedding server
    holds a single model on a single port, so parallel ingestion would queue inside
    llama.cpp anyway while multiplying memory and interleaving progress updates.
    """
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        thread = threading.Thread(target=_drain_queue, name="lyra-ingestion", daemon=True)
        thread.start()
        _worker_started = True


def _drain_queue() -> None:
    """Run queued ingestions forever, surviving anything one document does."""
    while True:
        document_id = _queue.get()
        try:
            run_ingestion(document_id)
        except Exception:
            # The thread is the whole ingestion capability of the process. One bad
            # document must never be able to take it down.
            logger.exception("Ingestion crashed for document %s", document_id)
        finally:
            _queue.task_done()


def run_ingestion(document_id: int) -> None:
    """Take one document from `pending` to a terminal state.

    Takes only an id and opens its own connection, so the worker thread never touches a
    request-scoped `sqlite3` connection and tests can call it directly without the queue.

    Args:
        document_id: Row id in `documents`. A document deleted since it was queued is
            skipped rather than treated as an error.
    """
    conn = connect()
    try:
        _ingest(conn, document_id)
    except Exception as exc:
        # The state column is written at the start of every stage and committed, so it
        # names the stage that just failed.
        stage = _current_state(conn, document_id) or PENDING
        logger.exception("Ingestion failed for document %s during %s", document_id, stage)
        _mark_failed(conn, document_id, stage, _failure_message(stage, exc))
    finally:
        conn.close()


def _ingest(conn: sqlite3.Connection, document_id: int) -> None:
    """The state machine itself, committing at every transition."""
    document = conn.execute(
        "select id, class_id, filename, stored_path, mime from documents where id = ?",
        (document_id,),
    ).fetchone()
    if document is None:
        logger.warning("Ingestion skipped: document %s no longer exists", document_id)
        return

    _set_state(conn, document_id, PARSING)
    parsed = parse_document(Path(document["stored_path"]), document["mime"])
    _record_page_counts(conn, document_id, parsed)
    if not parsed.pages:
        # Every page was scanned. Terminal for now, and the file stays on disk so Phase 2
        # can re-ingest it without asking the student to upload it again.
        _mark_unsupported(conn, document_id)
        return

    text = parsed.full_text
    _write_extracted_text(document_id, text)

    _set_state(conn, document_id, CHUNKING)
    doc_type = detect_doc_type(document["filename"], text)
    chunks = chunk_document(parsed, doc_type)
    if not chunks:
        # Pages carried text but none of it survived chunking, so there is still nothing
        # to search. Same outcome and same message as a scan.
        _mark_unsupported(conn, document_id)
        return

    _set_state(conn, document_id, EMBEDDING, doc_type)
    _store_chunks(conn, document_id, int(document["class_id"]), doc_type, chunks)

    _set_state(conn, document_id, EXTRACTING)
    detail = _extract_profile_facts(conn, document_id, text)

    _mark_ready(conn, document_id, parsed, detail)


def _extract_profile_facts(conn: sqlite3.Connection, document_id: int, text: str) -> str | None:
    """Propose profile facts, never letting that stage decide the document's fate.

    Returns:
        A skip reason to record in `stage_detail`, or None when extraction ran.
    """
    try:
        return extract_facts(conn, document_id, text)
    except Exception:
        # The chunks are already stored, so the document is searchable and lands `ready`
        # either way. A missed fact proposal is not worth failing an upload over.
        logger.exception("Profile extraction failed for document %s", document_id)
        return EXTRACTION_FAILED_DETAIL


def _store_chunks(
    conn: sqlite3.Connection,
    document_id: int,
    class_id: int,
    doc_type: str,
    chunks: list[Chunk],
) -> None:
    """Embed and insert every chunk, replacing whatever this document had before."""
    # A reingest must replace, not accumulate, so the old rows go before the first insert.
    delete_chunks(conn, document_id)

    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start : start + EMBED_BATCH_SIZE]
        vectors = embed_documents([chunk.content for chunk in batch])
        for chunk, vector in zip(batch, vectors, strict=True):
            chunk_id = conn.execute(
                _INSERT_CHUNK_SQL,
                (
                    document_id,
                    # class_id is denormalized onto the chunk so retrieval can partition
                    # by class without joining back through documents.
                    class_id,
                    chunk.content,
                    chunk.token_count,
                    chunk.page_number,
                    chunk.section_title,
                    chunk.problem_number,
                    chunk.part_index,
                    doc_type,
                    EMBEDDING_MODEL,
                    EMBEDDING_DIM,
                ),
            ).lastrowid
            conn.execute(
                "insert into chunk_embeddings (chunk_id, class_id, embedding) values (?, ?, ?)",
                (chunk_id, class_id, sqlite_vec.serialize_float32(vector)),
            )
        conn.commit()


def delete_chunks(conn: sqlite3.Connection, document_id: int) -> None:
    """Drop a document's chunks and their vectors. The caller commits.

    `chunk_embeddings` is a `vec0` virtual table, and a virtual table receives no
    foreign-key cascade, so its rows have to go explicitly and before the chunks they
    are keyed on.
    """
    chunk_ids = [
        (int(row[0]),)
        for row in conn.execute("select id from chunks where document_id = ?", (document_id,))
    ]
    conn.executemany("delete from chunk_embeddings where chunk_id = ?", chunk_ids)
    conn.execute("delete from chunks where document_id = ?", (document_id,))


def reconcile_interrupted(conn: sqlite3.Connection) -> int:
    """Fail every document left mid-flight by a shutdown. Returns the row count.

    The queue lives in memory, so a document caught between `pending` and `extracting`
    when the process stopped would otherwise sit there forever claiming to be working.
    """
    placeholders = ", ".join("?" for _ in NON_TERMINAL_STATES)
    cursor = conn.execute(
        # The placeholders are generated from a module constant, and every value is
        # bound. `stage_detail` reads the pre-update row, so it keeps the lost stage.
        f"update documents set stage_detail = state, state = '{FAILED}', "  # noqa: S608
        f"error_message = ? where state in ({placeholders})",
        (INTERRUPTED_MESSAGE, *NON_TERMINAL_STATES),
    )
    conn.commit()
    return cursor.rowcount


def _write_extracted_text(document_id: int, text: str) -> None:
    """Keep the extracted text beside the upload so a re-index never re-parses."""
    settings.text_dir.mkdir(parents=True, exist_ok=True)
    (settings.text_dir / f"{document_id}.txt").write_text(text, encoding="utf-8")


def _set_state(
    conn: sqlite3.Connection,
    document_id: int,
    state: str,
    stage_detail: str | None = None,
) -> None:
    """Move to the next stage and commit, so a poller sees progress as it happens."""
    conn.execute(
        "update documents set state = ?, stage_detail = ? where id = ?",
        (state, stage_detail, document_id),
    )
    conn.commit()


def _record_page_counts(conn: sqlite3.Connection, document_id: int, parsed: ParsedDocument) -> None:
    """Publish the page totals as soon as parsing knows them."""
    conn.execute(
        "update documents set pages_total = ?, pages_skipped = ? where id = ?",
        (parsed.pages_total, parsed.pages_skipped, document_id),
    )
    conn.commit()


def _mark_unsupported(conn: sqlite3.Connection, document_id: int) -> None:
    """Terminal, but not a failure: the file is kept for Phase 2."""
    conn.execute(
        "update documents set state = ?, stage_detail = null, error_message = ? where id = ?",
        (UNSUPPORTED, SCANNED_MESSAGE, document_id),
    )
    conn.commit()


def _mark_ready(
    conn: sqlite3.Connection,
    document_id: int,
    parsed: ParsedDocument,
    stage_detail: str | None,
) -> None:
    """Land the document searchable, with the page tally the UI reports."""
    conn.execute(
        "update documents set state = ?, stage_detail = ?, error_message = null, "
        "pages_total = ?, pages_done = ?, pages_skipped = ? where id = ?",
        (
            READY,
            stage_detail,
            parsed.pages_total,
            len(parsed.pages),
            parsed.pages_skipped,
            document_id,
        ),
    )
    conn.commit()


def _mark_failed(conn: sqlite3.Connection, document_id: int, stage: str, message: str) -> None:
    """Record the failure with the stage it happened in."""
    conn.execute(
        "update documents set state = ?, stage_detail = ?, error_message = ? where id = ?",
        (FAILED, stage, message, document_id),
    )
    conn.commit()


def _current_state(conn: sqlite3.Connection, document_id: int) -> str | None:
    """The document's state right now, or None if it has been deleted."""
    row = conn.execute("select state from documents where id = ?", (document_id,)).fetchone()
    return None if row is None else str(row["state"])


def _failure_message(stage: str, exc: Exception) -> str:
    """A user-facing reason for a failed stage, never carrying a path or a traceback."""
    if isinstance(exc, LyraError):
        return exc.message
    return _STAGE_FAILURE_MESSAGES.get(stage, _DEFAULT_FAILURE_MESSAGE)
