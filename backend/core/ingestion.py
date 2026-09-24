"""Background ingestion: parse, chunk, embed, store, and propose profile facts.

Upload returns `202` and hands the document id to a queue drained by one worker thread.
Everything slow happens there, and progress is visible only through `documents.state`,
so each transition is committed as it happens rather than at the end.

The pipeline has three outcomes. `ready` means searchable. `unsupported` means the file
had no text to read, in practice a scanned PDF that nobody has asked Lyra to read yet:
nothing went wrong, and the file is kept so recognition can be run over it in place.
`failed` means something broke, and carries a message written for the user.

Recognition sits inside the parsing stage rather than beside it, and that is deliberate on
both sides. In the interface, ui-phase-3.md keeps four steps and renders recognition under
Reading, because there is text in the file or there is not and either way what Lyra is
doing is reading the document. In the schema, it means the state machine below is
unchanged: a two-hour transcription is a long `parsing`, and `reconcile_interrupted`
already knows what to do with a document caught mid-parse.
"""

import logging
import queue
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import sqlite_vec

from backend.config import settings
from backend.core import ownership, recognition, storage_intents
from backend.core.consolidation import consolidate_class
from backend.core.errors import LyraError, UpstreamError
from backend.core.figures import store_figures
from backend.core.profiles import (
    ENDPOINT_FAILED,
    EXTRACTION_FAILED,
    EXTRACTION_MAX_TOKENS,
    extract_facts,
)
from backend.rag.chunk import Chunk, chunk_document, detect_doc_type
from backend.rag.embed import BATCH_SIZE as EMBED_BATCH_SIZE
from backend.rag.embed import EMBEDDING_DIM, EMBEDDING_MODEL, embed_documents
from backend.rag.figures import extract_figures
from backend.rag.parse import ParsedDocument, parse_document
from backend.rag.tokens import CHARS_PER_TOKEN
from backend.storage import private
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
  section_path, section_number, problem_number, part_index, doc_type,
  embedding_model, embedding_dim
) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_queue: queue.Queue[int] = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False
PROFILE_QUEUE_LIMIT = 32
_profile_queue: queue.Queue[tuple[Callable[..., None], tuple[object, ...]]] = queue.Queue(
    maxsize=PROFILE_QUEUE_LIMIT
)
_profile_lock = threading.Lock()
_profile_started = False


class _ProfileExecutor:
    """One daemon reader for optional work; it never owns ingestion readiness."""

    def submit(self, callback: Callable[..., None], *args: object) -> bool:
        global _profile_started
        try:
            _profile_queue.put_nowait((callback, args))
        except queue.Full:
            return False
        with _profile_lock:
            if not _profile_started:
                threading.Thread(
                    target=_drain_profile_queue, name="lyra-profile", daemon=True
                ).start()
                _profile_started = True
        return True


_profile_executor = _ProfileExecutor()


def _drain_profile_queue() -> None:
    while True:
        callback, args = _profile_queue.get()
        try:
            callback(*args)
        except Exception:
            logger.exception("Optional profile enrichment failed")
        finally:
            _profile_queue.task_done()


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
            run_ingestion(document_id, defer_profile=True)
        except Exception:
            # The thread is the whole ingestion capability of the process. One bad
            # document must never be able to take it down.
            logger.exception("Ingestion crashed for document %s", document_id)
        finally:
            _queue.task_done()


def run_ingestion(document_id: int, *, defer_profile: bool = False) -> None:
    """Take one document from `pending` to a terminal state.

    Takes only an id and opens its own connection, so the worker thread never touches a
    request-scoped `sqlite3` connection and tests can call it directly without the queue.

    Args:
        document_id: Row id in `documents`. A document deleted since it was queued is
            skipped rather than treated as an error.
    """
    conn = connect()
    try:
        _ingest(conn, document_id, defer_profile=defer_profile)
    except Exception as exc:
        # First, discard whatever the failed stage had half-written but not committed.
        # The connection is reused for the failure write below, and without the rollback
        # those uncommitted rows would ride along silently on its commit.
        conn.rollback()
        # The state column is written at the start of every stage and committed, so it
        # names the stage that just failed.
        stage = _current_state(conn, document_id) or PENDING
        logger.exception("Ingestion failed for document %s during %s", document_id, stage)
        _mark_failed(conn, document_id, stage, _failure_message(stage, exc))
    finally:
        conn.close()


def _ingest(conn: sqlite3.Connection, document_id: int, *, defer_profile: bool = False) -> None:
    """The state machine itself, committing at every transition."""
    document = conn.execute(
        "select id, class_id, filename, stored_path, mime, recognize, created_at "
        "from documents where id = ?",
        (document_id,),
    ).fetchone()
    if document is None:
        logger.warning("Ingestion skipped: document %s no longer exists", document_id)
        return
    # The row's identity, held for the whole run. Deleting a document mid-ingestion is
    # allowed - it is the de facto cancel for a long recognition run - and a re-upload can
    # put a different file behind this id, so every stage below re-checks the row before
    # writing and aborts quietly when it is gone or replaced.
    started_at = str(document["created_at"])

    _set_state(conn, document_id, PARSING)
    parsed = parse_document(Path(document["stored_path"]), document["mime"])
    _record_page_counts(conn, document_id, parsed)
    # Always, not only when recognition runs. The rows are what `pages_done` is counted from
    # and what a later `Try those pages` reads, so a document has to have them before anyone
    # asks for one to be read.
    recognition.sync_pages(conn, document_id, parsed)

    skipped = None
    if document["recognize"]:
        skipped = recognition.recognize_pages(conn, document, parsed)
        # Recognition is the stage long enough for a delete to be aimed at, so the run is
        # re-checked the moment it hands control back.
        if _vanished(conn, document_id, started_at):
            return
        parsed = recognition.merge_recognized(conn, document_id, parsed)
        _record_page_counts(conn, document_id, parsed)

    if not parsed.pages:
        # Nothing readable in the whole file. The file stays on disk either way, so the
        # student can ask for it to be read once whatever stopped it is fixed.
        _settle_unreadable(conn, document_id, skipped, document["mime"])
        return

    text = parsed.full_text
    if not _write_extracted_text(document_id, text, started_at):
        logger.warning(
            "Ingestion of document %s abandoned: deleted or replaced mid-run", document_id
        )
        return

    _record_figures(conn, document_id, Path(document["stored_path"]), document["mime"])

    _set_state(conn, document_id, CHUNKING)
    doc_type = detect_doc_type(document["filename"], text, parsed)
    chunks = chunk_document(parsed, doc_type)
    if not chunks:
        # Pages carried text but none of it survived chunking, so there is still nothing
        # to search. Same outcome and same message as a scan.
        _mark_unsupported(conn, document_id, SCANNED_MESSAGE)
        return

    if _vanished(conn, document_id, started_at):
        return
    _set_state(conn, document_id, EMBEDDING, doc_type)
    stored = _store_chunks(
        conn,
        document_id,
        int(document["class_id"]),
        doc_type,
        chunks,
        started_at,
        [page.page_number for page in parsed.pages],
    )
    if not stored:
        return

    if _vanished(conn, document_id, started_at):
        return
    # A requested recognition that could not run must not vanish just because the rest of
    # the document was readable. The reason lands in `error_message`, where the row's
    # "pages skipped" popover already looks for it, so a mixed document that quietly read
    # only its text pages says why the rest were not attempted.
    _mark_ready(conn, document_id, parsed, None, recognition.skip_message(skipped))
    # The optional queue only needs the maximum slice that extraction can ever show a
    # model. A large book must not leave its full transcript queued in memory.
    profile_text = text[: EXTRACTION_MAX_TOKENS * CHARS_PER_TOKEN]
    if defer_profile:
        submitted = _profile_executor.submit(
            _enrich_profile,
            document_id,
            int(document["class_id"]),
            started_at,
            profile_text,
            doc_type,
        )
        if submitted is False:
            conn.execute(
                "update documents set stage_detail = ? where id = ? and state = ?",
                ("Optional profile enrichment skipped while busy.", document_id, READY),
            )
            conn.commit()
    else:
        _enrich_profile(document_id, int(document["class_id"]), started_at, profile_text, doc_type)


def _enrich_profile(
    document_id: int, class_id: int, started_at: str, text: str, doc_type: str
) -> None:
    """Optional, serialized model work runs after the document becomes searchable."""
    conn = connect()
    try:
        row = conn.execute(
            "select class_id, created_at, state from documents where id = ?", (document_id,)
        ).fetchone()
        if row is None or (int(row["class_id"]), str(row["created_at"]), str(row["state"])) != (
            class_id,
            started_at,
            READY,
        ):
            return
        detail = _extract_profile_facts(conn, document_id, text, doc_type)
        if detail is None:
            _consolidate_profile(conn, class_id)
        if not recognition.document_replaced(conn, document_id, started_at):
            conn.execute(
                "update documents set stage_detail = ? where id = ? and class_id = ? "
                "and state = ? and refresh_state is null",
                (detail, document_id, class_id, READY),
            )
            conn.commit()
    finally:
        conn.close()


def _vanished(conn: sqlite3.Connection, document_id: int, started_at: str) -> bool:
    """Whether the document this run started from is gone or replaced. Logged once here."""
    if recognition.document_replaced(conn, document_id, started_at):
        logger.warning(
            "Ingestion of document %s abandoned: deleted or replaced mid-run", document_id
        )
        return True
    return False


def _record_figures(conn: sqlite3.Connection, document_id: int, source: Path, mime: str) -> None:
    """Find and store the document's figures, never letting that decide its fate.

    Guarded for the same reason profile extraction is: the document is searchable either
    way, and a diagram Lyra failed to notice is not worth failing an upload over. It runs
    before chunking so that a document is never briefly `ready` with its text indexed and
    its figures missing.
    """
    try:
        store_figures(conn, document_id, extract_figures(source, mime))
        conn.commit()
    except Exception:
        # `store_figures` deletes the previous run's figures before inserting the new
        # ones, all uncommitted. A failure in between must not leave that delete waiting
        # to ride along on the next commit, which would silently take figures the
        # document still legitimately has.
        conn.rollback()
        logger.exception("Figure extraction failed for document %s", document_id)


def _extract_profile_facts(
    conn: sqlite3.Connection, document_id: int, text: str, doc_type: str
) -> str | None:
    """Propose profile facts, never letting that stage decide the document's fate.

    Returns:
        A skip reason to record in `stage_detail`, or None when extraction ran.
    """
    try:
        return extract_facts(conn, document_id, text, doc_type)
    except UpstreamError:
        # Separated from everything below because this one has an address. The endpoint
        # answered, and what it answered was a refusal, which on a local runtime almost
        # always means the server is holding a different model than the settings name -
        # smaller context above all, since extraction sends the largest prompt Lyra
        # builds and is therefore the first thing to fall over. The profile panel turns
        # this reason into a sentence with a link to Settings; `llm.client` has already
        # logged the server's own words for it, which is the part worth reading.
        conn.rollback()
        logger.warning(
            "Profile extraction for document %s was refused by the tutor endpoint", document_id
        )
        return ENDPOINT_FAILED
    except Exception:
        # Whatever the failed pass half-wrote is discarded rather than left uncommitted
        # on the shared connection, where the next commit would silently keep it.
        conn.rollback()
        # The chunks are already stored, so the document is searchable and lands `ready`
        # either way. A missed fact proposal is not worth failing an upload over.
        logger.exception("Profile extraction failed for document %s", document_id)
        return EXTRACTION_FAILED


def _consolidate_profile(conn: sqlite3.Connection, class_id: int) -> None:
    """Tidy the class profile now this document has added to it.

    Guarded separately from extraction and deliberately without a `stage_detail` of its own.
    Extraction succeeded, so the facts are stored and the profile is at worst the
    deterministically merged one; a document that says so would be reporting a failure the
    student cannot act on and that the next upload retries anyway.
    """
    try:
        consolidate_class(conn, class_id)
    except Exception:
        # Same discipline as extraction: a merge half-applied when the endpoint died must
        # not sit uncommitted waiting for an unrelated commit to make it real.
        conn.rollback()
        logger.exception("Profile consolidation failed for class %s", class_id)


def _store_chunks(
    conn: sqlite3.Connection,
    document_id: int,
    class_id: int,
    doc_type: str,
    chunks: list[Chunk],
    started_at: str,
    indexed_pages: list[int],
) -> bool:
    """Build embeddings off-index, then replace the published index atomically.

    A failed helper or interrupted refresh leaves every old citation and vector in
    place. The private connection's temporary staging table is never visible to
    retrieval and disappears if the process stops before publication.

    Returns:
        False when the run was abandoned because the document was deleted or replaced
        while a batch was embedding - the long call of this stage - so nothing of this
        file's index can land on whatever the id points at now. True otherwise.
    """
    conn.execute("drop table if exists temp.ingest_replacement")
    conn.execute(
        "create temp table ingest_replacement (seq integer primary key, "
        "content text, token_count integer, page_number integer, section_title text, "
        "section_path text, section_number text, problem_number text, part_index integer, "
        "embedding blob)"
    )

    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start : start + EMBED_BATCH_SIZE]
        vectors = embed_documents([chunk.content for chunk in batch])
        if recognition.document_replaced(conn, document_id, started_at):
            conn.rollback()
            logger.warning(
                "Embedding of document %s abandoned: deleted or replaced mid-run", document_id
            )
            return False
        for offset, (chunk, vector) in enumerate(zip(batch, vectors, strict=True)):
            conn.execute(
                "insert into temp.ingest_replacement values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    start + offset,
                    chunk.content,
                    chunk.token_count,
                    chunk.page_number,
                    chunk.section_title,
                    chunk.section_path,
                    chunk.section_number,
                    chunk.problem_number,
                    chunk.part_index,
                    sqlite_vec.serialize_float32(vector),
                ),
            )
        conn.commit()
    if recognition.document_replaced(conn, document_id, started_at):
        return False
    # The old rows remain live until every new vector exists. This one transaction is
    # the only point at which retrieval can switch generations.
    delete_chunks(conn, document_id)
    for row in conn.execute("select * from temp.ingest_replacement order by seq"):
        chunk_id = conn.execute(
            _INSERT_CHUNK_SQL,
            (
                document_id,
                # class_id is denormalized onto the chunk so retrieval can partition
                # by class without joining back through documents.
                class_id,
                row["content"],
                row["token_count"],
                row["page_number"],
                row["section_title"],
                row["section_path"],
                row["section_number"],
                row["problem_number"],
                row["part_index"],
                doc_type,
                EMBEDDING_MODEL,
                EMBEDDING_DIM,
            ),
        ).lastrowid
        conn.execute(
            "insert into chunk_embeddings (chunk_id, class_id, embedding) values (?, ?, ?)",
            (chunk_id, class_id, row["embedding"]),
        )
    conn.executemany(
        "insert into document_index_pages (document_id, page_number) values (?, ?)",
        [(document_id, page_number) for page_number in indexed_pages],
    )
    conn.execute("drop table temp.ingest_replacement")
    return True


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
    conn.execute("delete from document_index_pages where document_id = ?", (document_id,))
    conn.execute("delete from chunks where document_id = ?", (document_id,))


def reconcile_interrupted(conn: sqlite3.Connection) -> tuple[int, int]:
    """Settle every document the last shutdown caught, and say what happened to each.

    The queue lives in memory, so a document left non-terminal would otherwise sit there
    forever claiming to be working. What it deserves depends on how far it had got.

    A `pending` document was queued and never touched: nothing was interrupted, the file
    and its row are intact, and the student's instruction was "index this". It goes back
    in the queue. Failing those was punishing: dropping twenty-eight files into a class
    and restarting the server - which in development is any code edit at all - turned the
    whole queue into a wall of red the student had to retry one row at a time.

    A document that was mid-stage is failed instead, deliberately. It had already started
    when the process died, which makes it the most likely reason the process died, and a
    file that crashes the worker would otherwise be requeued into the same crash on every
    restart from now on. Retrying it is one click, and that click is the student's.

    Returns:
        How many were requeued, and how many were failed.
    """
    queued = [
        int(row[0]) for row in conn.execute("select id from documents where state = ?", (PENDING,))
    ]

    mid_flight = tuple(state for state in NON_TERMINAL_STATES if state != PENDING)
    placeholders = ", ".join("?" for _ in mid_flight)
    stalled = [
        int(row[0])
        for row in conn.execute(
            f"select id from documents where state in ({placeholders})",  # noqa: S608
            mid_flight,
        )
    ]
    queued_refreshes = [
        int(row[0])
        for row in conn.execute(
            "select id from documents where state = ? and refresh_state = ?", (READY, PENDING)
        )
    ]
    interrupted_refreshes = [
        int(row[0])
        for row in conn.execute(
            "select id from documents where state = ? and refresh_state in (?, ?, ?, ?) ",
            (READY, PARSING, CHUNKING, EMBEDDING, EXTRACTING),
        )
    ]
    # A document about to be marked failed must not keep serving whatever chunks its
    # interrupted run had already committed - `_store_chunks` lands them a batch at a
    # time. Deleted in the same transaction as the state write, so there is no moment at
    # which a failed document still answers searches.
    recovered_extractions = 0
    for document_id in stalled:
        state = conn.execute("select state from documents where id = ?", (document_id,)).fetchone()[
            0
        ]
        chunks = conn.execute(
            "select count(*) from chunks where document_id = ?", (document_id,)
        ).fetchone()[0]
        if state == EXTRACTING and chunks:
            conn.execute(
                "update documents set state = ?, stage_detail = null, error_message = null "
                "where id = ?",
                (READY, document_id),
            )
            recovered_extractions += 1
        else:
            delete_chunks(conn, document_id)
    cursor = conn.execute(
        # The placeholders are generated from a module constant, and every value is
        # bound. `stage_detail` reads the pre-update row, so it keeps the lost stage.
        f"update documents set stage_detail = state, state = '{FAILED}', "  # noqa: S608
        f"error_message = ? where state in ({placeholders})",
        (INTERRUPTED_MESSAGE, *mid_flight),
    )
    if interrupted_refreshes:
        conn.executemany(
            "update documents set refresh_state = ?, error_message = ? where id = ?",
            [(FAILED, INTERRUPTED_MESSAGE, document_id) for document_id in interrupted_refreshes],
        )
    conn.commit()

    # After the commit, so a queue that starts draining immediately cannot race the write
    # that failed its neighbours.
    for document_id in [*queued, *queued_refreshes]:
        enqueue(document_id)
    return len(queued) + len(queued_refreshes), cursor.rowcount + len(interrupted_refreshes)


def _write_extracted_text(document_id: int, text: str, started_at: str) -> bool:
    """Keep the extracted text beside the upload so a re-index never re-parses.

    Extracted text is the coursework in plain form, so it is written `0o600` inside the
    `0o700` text directory rather than at the mercy of the umask. Published atomically
    (staged, then renamed), because readers trust this file on the strength of its
    existence: a crash mid-write must not leave a truncated transcript that a re-index or
    the source pane would silently treat as the whole document.

    Publication is conditional on the document row still carrying the identity this run
    started from. Parsing can take minutes and deletion is allowed meanwhile; a completed
    delete must not have this worker resurrect the document's text afterwards
    (docs/storage-consistency.md). Returns False, with nothing written, when the document
    is gone or replaced - the caller abandons the run.
    """
    private.secure_mkdir(settings.text_dir, root=settings.data_dir)
    return ownership.publish_current_document(
        document_id, started_at, storage_intents.text_path(document_id), text.encode("utf-8")
    )


def _set_state(
    conn: sqlite3.Connection,
    document_id: int,
    state: str,
    stage_detail: str | None = None,
) -> None:
    """Move to the next stage and commit, so a poller sees progress as it happens."""
    refreshing = conn.execute(
        "select refresh_state from documents where id = ?", (document_id,)
    ).fetchone()
    if refreshing is not None and refreshing[0] is not None:
        conn.execute(
            "update documents set refresh_state = ?, stage_detail = ? where id = ?",
            (state, stage_detail, document_id),
        )
    else:
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


def _settle_unreadable(
    conn: sqlite3.Connection, document_id: int, skipped: str | None, mime: str
) -> None:
    """Land a document that yielded no text at all, saying which kind of nothing it was.

    Four different situations end up here and they are not the same fact. A document
    recognition was never asked to read is `unsupported`, which is where it has always
    been. A document where recognition was asked for but could not start - no endpoint,
    or a remote one the student has not acknowledged - is `unsupported` again, but
    carrying the reason, since that reason is the one thing the student can act on. A
    document whose run the breaker stopped is `failed` and blames the endpoint, because
    three consecutive failures out of hundreds of unattempted pages is an endpoint fact,
    not a document fact. And a document where recognition genuinely tried every page and
    read none is `failed` with nothing to keep.

    The order matters. What this run learned outranks what old page rows still say: a
    retry after the endpoint was removed skips before it attempts anything, and reporting
    the previous run's stale "none of the pages could be read" instead of "add an
    endpoint" would hide the one thing the student can act on.
    """
    if mime in ("text/plain", "text/markdown"):
        _mark_unsupported(conn, document_id, "This text file is empty.")
        return
    message = recognition.skip_message(skipped)
    if message is not None:
        _mark_unsupported(conn, document_id, message)
        return
    if skipped == recognition.ENDPOINT_FAILED:
        _mark_failed(conn, document_id, PARSING, recognition.ENDPOINT_FAILED_MESSAGE)
        return
    if recognition.failed_page_count(conn, document_id):
        _mark_failed(conn, document_id, PARSING, recognition.ALL_PAGES_FAILED_MESSAGE)
        return
    _mark_unsupported(conn, document_id, SCANNED_MESSAGE)


def _mark_unsupported(conn: sqlite3.Connection, document_id: int, message: str) -> None:
    """Terminal, but not a failure: the file is kept so it can be read later."""
    refreshing = conn.execute(
        "select refresh_state from documents where id = ?", (document_id,)
    ).fetchone()
    if refreshing is not None and refreshing[0] is not None:
        conn.execute(
            "update documents set refresh_state = ?, stage_detail = null, error_message = ? "
            "where id = ?",
            (FAILED, message, document_id),
        )
    else:
        conn.execute(
            "update documents set state = ?, stage_detail = null, error_message = ? where id = ?",
            (UNSUPPORTED, message, document_id),
        )
    conn.commit()


def _mark_ready(
    conn: sqlite3.Connection,
    document_id: int,
    parsed: ParsedDocument,
    stage_detail: str | None,
    notice: str | None = None,
) -> None:
    """Land the document searchable, with the page tally the UI reports.

    `notice` is the reason a requested recognition read nothing, on the one path where
    the document is otherwise fine: a mixed file whose text pages carried it to `ready`.
    Dropped, the student's explicit "read this document" would have silently done nothing.
    """
    conn.execute(
        "update documents set state = ?, refresh_state = null, stage_detail = ?, "
        "error_message = ?, "
        "pages_total = ?, pages_done = ?, pages_skipped = ? where id = ?",
        (
            READY,
            stage_detail,
            notice,
            parsed.pages_total,
            len(parsed.pages),
            parsed.pages_skipped,
            document_id,
        ),
    )
    conn.commit()


def _mark_failed(conn: sqlite3.Connection, document_id: int, stage: str, message: str) -> None:
    """Record the failure with the stage it happened in.

    The document's chunks go in the same transaction. `_store_chunks` commits a batch at
    a time, so a failure - or a restart reconciled later - can catch a document with part
    of its index already committed, and a `failed` row must not go on answering searches
    with half an index.
    """
    refreshing = conn.execute(
        "select refresh_state from documents where id = ?", (document_id,)
    ).fetchone()
    if refreshing is not None and refreshing[0] is not None:
        conn.execute(
            "update documents set refresh_state = ?, stage_detail = ?, error_message = ? "
            "where id = ?",
            (FAILED, stage, message, document_id),
        )
    else:
        delete_chunks(conn, document_id)
        conn.execute(
            "update documents set state = ?, stage_detail = ?, error_message = ? where id = ?",
            (FAILED, stage, message, document_id),
        )
    conn.commit()


def _current_state(conn: sqlite3.Connection, document_id: int) -> str | None:
    """The document's state right now, or None if it has been deleted."""
    row = conn.execute(
        "select state, refresh_state from documents where id = ?", (document_id,)
    ).fetchone()
    return None if row is None else str(row["refresh_state"] or row["state"])


def _failure_message(stage: str, exc: Exception) -> str:
    """A user-facing reason for a failed stage, never carrying a path or a traceback."""
    if isinstance(exc, LyraError):
        return exc.message
    return _STAGE_FAILURE_MESSAGES.get(stage, _DEFAULT_FAILURE_MESSAGE)
