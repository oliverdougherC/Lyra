"""Document endpoints: upload, listing, ingestion status, reingest, and deletion.

Upload answers `202` and does no work beyond storing the file, because parsing and
embedding take seconds to minutes. The interface polls `/status` from there.

Handlers are sync `def`: `sqlite3` and file writes block, and FastAPI runs sync handlers
in a threadpool, which is exactly where blocking work belongs.
"""

import logging
import re
import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.config import settings
from backend.core import figures, ownership, recognition, storage_intents
from backend.core.classes import get_class, touch_class
from backend.core.errors import ConflictError, LyraError, NotFoundError
from backend.core.ingestion import PENDING, delete_chunks, enqueue
from backend.core.profiles import forget_document_evidence
from backend.rag import render, structure
from backend.rag.parse import PDF_MIME, UNSUPPORTED_MESSAGE
from backend.storage import private
from backend.storage.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["documents"])

DbConn = Annotated[sqlite3.Connection, Depends(get_db)]

# 50 MB. Generous for a lecture deck or a scanned problem set, and small enough that
# reading one upload into memory to check it stays comfortable.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
TOO_LARGE_MESSAGE = "That file is larger than 50 MB. Upload a smaller file."

# How much of a text source the reading pane serves. Well past a problem set, and short of
# anything that would make the pane slow to render.
MAX_TEXT_CHARS = 200_000

# The extension decides the mime, not `UploadFile.content_type`: browsers send
# `application/octet-stream`, `text/x-markdown`, or nothing at all for `.md`, so trusting
# the header would reject files that parse perfectly well.
#
# An uploaded image is a one-page scan and ingests through the ordinary pipeline: PyMuPDF
# opens it as a one-page document with no text, which is exactly what a scanned page is.
# WebP is not here because the PyMuPDF build refuses to decode one, which is checked rather
# than assumed. See `rag.parse.IMAGE_MIMES`.
MIME_BY_SUFFIX = {
    ".pdf": PDF_MIME,
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")

# States in which nothing is reading the file or about to write the document's row.
TERMINAL_STATES = ("ready", "failed", "unsupported")

STILL_PROCESSING_MESSAGE = "{filename} is still being processed. Move it once it has finished."
IN_USE_MESSAGE = (
    "{filename} is a source for {count} solution set(s) in this class. "
    "Delete those first, or upload the file to the other class instead."
)
MISSING_SOURCE_MESSAGE = (
    "The stored file for {filename} is missing, so it cannot be moved. "
    "Delete the document and upload the file again."
)
MOVE_FAILED_MESSAGE = "{filename} could not be moved. It stays in its current class; try again."
CHANGED_UNDERNEATH_MESSAGE = (
    "{filename} was changed by another request while this one was running. Try again."
)

# Counted rather than stored, so it cannot drift from the page rows it describes. A
# correlated subquery over a table keyed on (document_id, page_number) is an index seek per
# document, which is what the list screen already costs.
_PAGES_FAILED_COLUMN = (
    # The only value reaching the SQL text is a module constant of this codebase's own.
    f"(select count(*) from document_pages p where p.state = '{recognition.FAILED}' "  # noqa: S608
    "and p.document_id = documents.id) as pages_failed"
)

_DOCUMENT_COLUMNS = (
    "id, class_id, filename, mime, byte_size, state, stage_detail, "
    "pages_total, pages_done, pages_skipped, error_message, created_at, recognize, "
    + _PAGES_FAILED_COLUMN
)


class DocumentRead(BaseModel):
    """A document and where its ingestion has got to.

    `stored_path` is deliberately absent: the interface never sees a filesystem path.
    """

    id: int
    class_id: int
    filename: str
    mime: str
    byte_size: int
    state: str
    stage_detail: str | None
    pages_total: int | None
    pages_done: int
    pages_skipped: int
    # Pages recognition tried and could not read. A different fact from `pages_skipped`,
    # which counts pages that had no text to find, and both can be true at once: the
    # document is still `ready` and `PageFailureNotice` reports this quietly beside it.
    pages_failed: int
    # Whether the student has asked for this document to be read as images.
    recognize: bool
    error_message: str | None
    created_at: str


class DocumentDetail(DocumentRead):
    """One document, plus whether its extracted text is on hand for a re-index."""

    has_text: bool


class DocumentMove(BaseModel):
    """Body of `POST /api/documents/{document_id}/move`: where the file belongs."""

    class_id: int


class DocumentText(BaseModel):
    """A text source as the solver's source pane reads it."""

    filename: str
    text: str
    truncated: bool


class OutlineSection(BaseModel):
    """One addressable section of a document, as its indexed chunks record it."""

    path: str
    number: str | None
    depth: int
    first_page: int | None
    last_page: int | None
    chunk_count: int


class FigureRead(BaseModel):
    """One figure found in a document.

    `name` is what to call it on screen: its caption's label where it has one, otherwise
    the page and position it was found at. Most figures have no caption, and inventing a
    number the document does not use would be worse than saying where it came from.
    """

    id: int
    document_id: int
    page_number: int
    figure_index: int
    bbox: list[float]
    label: str | None
    caption: str | None
    name: str


class DocumentOutline(BaseModel):
    """What structure Lyra found in a document, and how much of it is covered.

    `chunk_count` against `sectioned_count` is the honest part. A book read as one flat
    blob has sections for none of its chunks, and the difference is exactly what a student
    would otherwise have no way to discover except by noticing the answers got worse.
    """

    sections: list[OutlineSection]
    chunk_count: int
    sectioned_count: int


class StatusRead(BaseModel):
    """The poll target while a document ingests."""

    state: str
    stage_detail: str | None
    pages_total: int | None
    pages_done: int
    pages_skipped: int
    pages_failed: int
    recognize: bool
    error_message: str | None


@router.post(
    "/classes/{class_id}/documents",
    response_model=DocumentRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def upload_document(
    class_id: int, file: Annotated[UploadFile, File()], conn: DbConn
) -> dict[str, object]:
    get_class(conn, class_id)
    filename = _display_filename(file.filename or "")
    mime = _mime_for(filename)

    # Checked against the bytes actually read. A `content-length` header is whatever the
    # client chose to send, so it cannot be the thing that enforces the limit.
    payload = file.file.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise LyraError(TOO_LARGE_MESSAGE)

    # The stored name needs the row id to be unique within a class, so the row is
    # inserted first and pointed at the file once it is written. Nothing is committed
    # until the file is fully published, so a committed row always points at a whole
    # file: a failed or interrupted write leaves no row behind, and the publication is
    # staged-then-renamed so it can leave no truncated file under the final name either.
    # What a crash can leave is a whole file with no row - its insert rolled back - and
    # the startup orphan sweep removes those (docs/storage-consistency.md).
    document_id = int(
        conn.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
            "values (?, ?, '', ?, ?, ?)",
            (class_id, filename, mime, len(payload), PENDING),
        ).lastrowid
        or 0
    )
    stored_path = settings.uploads_dir / str(class_id) / f"{document_id}-{_safe_filename(filename)}"
    # The class upload directory is `0o700` and the stored file `0o600`: an uploaded source
    # is coursework, private like the rest of the data tree and not left to the umask.
    private.secure_mkdir(stored_path.parent, root=settings.data_dir)
    private.publish_private_bytes(stored_path, payload)
    conn.execute(
        "update documents set stored_path = ? where id = ?", (str(stored_path), document_id)
    )
    conn.commit()

    touch_class(conn, class_id)
    enqueue(document_id)
    return _document_row(conn, document_id)


@router.get("/classes/{class_id}/documents", response_model=list[DocumentRead])
def list_documents(class_id: int, conn: DbConn) -> list[dict[str, object]]:
    # An unknown class is a 404 rather than an empty list, so a stale link is obvious.
    get_class(conn, class_id)
    rows = conn.execute(
        f"select {_DOCUMENT_COLUMNS} from documents where class_id = ? "  # noqa: S608
        "order by created_at desc, id desc",
        (class_id,),
    )
    return [dict(row) for row in rows]


@router.get("/documents/{document_id}", response_model=DocumentDetail)
def read_document(document_id: int, conn: DbConn) -> dict[str, object]:
    document = _document_row(conn, document_id)
    return {**document, "has_text": _text_path(document_id).exists()}


@router.get("/documents/{document_id}/status", response_model=StatusRead)
def read_document_status(document_id: int, conn: DbConn) -> dict[str, object]:
    return _document_row(conn, document_id)


@router.get("/documents/{document_id}/pages/{page_number}")
def read_document_page(document_id: int, page_number: int, conn: DbConn) -> FileResponse:
    """One page of a source document, rendered to PNG and cached.

    The solver's source pane shows the page a problem came from beside its solution. Page
    images rather than an embedded viewer, because that is what buys exact anchoring and
    identical rendering everywhere, and because Phase 3 needs the same rasterization.

    Cached aggressively on the client: a rendered page of a stored file never changes, and
    re-ingesting or deleting the document clears the server-side cache.
    """
    row = conn.execute(
        "select id, stored_path, mime, created_at from documents where id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("That document does not exist.")

    path = render.render_page(
        document_id,
        Path(str(row["stored_path"])),
        str(row["mime"]),
        page_number,
        created_at=str(row["created_at"]),
    )
    return FileResponse(
        path,
        media_type="image/png",
        headers={"cache-control": "private, max-age=86400"},
    )


@router.get("/documents/{document_id}/text", response_model=DocumentText)
def read_document_text(document_id: int, conn: DbConn) -> dict[str, object]:
    """The extracted text of a document that has no pages to draw.

    TXT and MD sources render as their text in the solver's source pane, which is the same
    anchor as a page image with a different surface. Truncated, because this is a reading
    pane rather than a download: a source the student wants in full is a file they already
    have.
    """
    document = _document_row(conn, document_id)
    path = _text_path(document_id)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return {
        "filename": document["filename"],
        "text": text[:MAX_TEXT_CHARS],
        "truncated": len(text) > MAX_TEXT_CHARS,
    }


@router.post(
    "/documents/{document_id}/reingest",
    response_model=DocumentRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def reingest_document(document_id: int, conn: DbConn) -> dict[str, object]:
    # Under the lifecycle mutex: this clears the page cache and rewrites the row's state,
    # and both must happen from the state that was just observed, not from a row a
    # concurrent move or delete has advanced in the meantime.
    with ownership.lifecycle_mutation():
        document = _document_row(conn, document_id)
        # The same guard move and recognize have always had, for the same reason: the
        # worker is reading this row and about to write its state, so the `pending`
        # written below would be overwritten by whatever the in-flight run lands in, and
        # the chunk delete below would race the batches that run is still committing.
        if document["state"] not in TERMINAL_STATES:
            raise ConflictError(STILL_PROCESSING_MESSAGE.format(filename=document["filename"]))

        if document["state"] == "failed":
            # Retrying a failed document is an explicit retry, so pages that failed go
            # back in the pending set. A re-index of a `ready` document deliberately does
            # not do this: that is a chunking pass, and it must not re-pay model time on
            # pages that failed for good. See `recognition.reset_failed_pages`.
            recognition.reset_failed_pages(conn, document_id)
        # Clearing the previous run's chunks here as well as in the job keeps the document
        # from serving stale results while it waits in the queue.
        delete_chunks(conn, document_id)
        # Same reason for the rendered pages: a stale image would show a page from the
        # file that used to be there. Best-effort by explicit choice, unlike the delete
        # paths: there is no durable intent to keep here, the file itself is unchanged,
        # and a page being read this instant must not fail the re-ingest - so an
        # incomplete discard is logged rather than raised.
        if not render.discard_pages(document_id):
            logger.warning(
                "The page cache for document %s could not be fully cleared before "
                "re-ingest; leftover entries will be overwritten as pages re-render",
                document_id,
            )
        changed = conn.execute(
            "update documents set state = ?, stage_detail = null, error_message = null, "
            "pages_done = 0 where id = ? and state = ?",
            (PENDING, document_id, str(document["state"])),
        ).rowcount
        if changed != 1:
            conn.rollback()
            raise ConflictError(CHANGED_UNDERNEATH_MESSAGE.format(filename=document["filename"]))
        conn.commit()

        enqueue(document_id)
        return _document_row(conn, document_id)


@router.get("/documents/{document_id}/figures", response_model=list[FigureRead])
def list_document_figures(document_id: int, conn: DbConn) -> list[dict[str, object]]:
    """Every figure Lyra found in this document, in page then reading order."""
    _document_row(conn, document_id)
    return figures.list_figures(conn, document_id)


@router.get("/figures/{figure_id}")
def read_figure(figure_id: int, conn: DbConn) -> FileResponse:
    """One figure, cropped out of its page and cached.

    Addressed by its own id rather than under its document, so that a solution can render a
    figure knowing only what its `artifact_part` stores. The alternative was to thread the
    source document id through every figure part and every component that draws one, to
    re-derive something the figure row already knows.

    Cached aggressively on the client for the same reason a rendered page is: a crop of a
    stored file never changes, and re-ingesting or deleting the document clears the cache
    behind it.
    """
    figure = figures.get_figure(conn, figure_id)
    if figure is None:
        raise NotFoundError("That figure does not exist.")

    bbox = figure["bbox"]
    path = render.render_figure(
        int(figure["document_id"]),  # type: ignore[arg-type]
        Path(str(figure["stored_path"])),
        str(figure["mime"]),
        int(figure["page_number"]),  # type: ignore[arg-type]
        figure_id,
        (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),  # type: ignore[index]
        created_at=str(figure["document_created_at"]),
    )
    return FileResponse(
        path,
        media_type="image/png",
        headers={"cache-control": "private, max-age=86400"},
    )


@router.get("/documents/{document_id}/outline", response_model=DocumentOutline)
def read_document_outline(document_id: int, conn: DbConn) -> dict[str, object]:
    """The section hierarchy Lyra indexed this document under.

    Read from the chunks rather than from the PDF's own table of contents, deliberately.
    The outline in the file is what the publisher wrote; what this reports is what actually
    partitions retrieval, and those differ exactly when something went wrong. A student
    whose 600-page book was read as one flat blob has no other way to find that out.

    Ordered by the first chunk of each section, which is document order: chunks are
    inserted in the order they were cut.
    """
    _document_row(conn, document_id)
    rows = conn.execute(
        "select section_path, section_number, min(page_number) as first_page, "
        "max(page_number) as last_page, count(*) as chunk_count from chunks "
        "where document_id = ? and section_path is not null "
        "group by section_path, section_number order by min(id)",
        (document_id,),
    ).fetchall()
    totals = conn.execute(
        "select count(*), count(section_path) from chunks where document_id = ?",
        (document_id,),
    ).fetchone()

    return {
        "sections": [
            {
                "path": str(row["section_path"]),
                "number": row["section_number"],
                # Derived here so the separator stays a single constant. A path is titles
                # joined, and how deep it sits is how many of them there are.
                "depth": str(row["section_path"]).count(structure.PATH_SEPARATOR) + 1,
                "first_page": row["first_page"],
                "last_page": row["last_page"],
                "chunk_count": int(row["chunk_count"]),
            }
            for row in rows
        ],
        "chunk_count": int(totals[0]),
        "sectioned_count": int(totals[1]),
    }


@router.post(
    "/documents/{document_id}/recognize",
    response_model=DocumentRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def recognize_document(document_id: int, conn: DbConn) -> dict[str, object]:
    """Read this document's unreadable pages as images, and index what they say.

    One endpoint serves both affordances in ui-phase-3.md, because they are one operation.
    `Read this document` on a scanned upload and `Try those pages` on a document with three
    failed pages both mean "attempt every page that does not currently have text", and the
    page rows already know which those are. Pages that worked are not touched, so a retry
    never spends model time re-reading them.

    Nothing here transcribes on the student's behalf. Recognition is minutes of model time
    per document and, against a configured remote endpoint, it sends page images of the
    student's own material somewhere, so it happens when it is asked for and not before.
    The flag stays set afterwards: it is a property of the document, so a later re-index
    keeps reading it the way the student asked for.
    """
    with ownership.lifecycle_mutation():
        document = _document_row(conn, document_id)
        # The worker is reading this row and about to write its state, and the `pending`
        # set below would be overwritten by whatever it lands in.
        if document["state"] not in TERMINAL_STATES:
            raise ConflictError(STILL_PROCESSING_MESSAGE.format(filename=document["filename"]))

        # This is the explicit "attempt them again", so failed pages rejoin the pending
        # set. An in-flight run never re-attempts `failed` pages on its own - that would
        # make every plain re-index re-pay for pages that failed for good.
        recognition.reset_failed_pages(conn, document_id)
        delete_chunks(conn, document_id)
        # Deliberately no `render.discard_pages` here, unlike the re-ingest below. The
        # rendered pages are what recognition reads, they were rendered from bytes that
        # have not changed, and throwing them away would make every retry pay to
        # rasterize the document again.
        changed = conn.execute(
            "update documents set recognize = 1, state = ?, stage_detail = null, "
            "error_message = null, pages_done = 0 where id = ? and state = ?",
            (PENDING, document_id, str(document["state"])),
        ).rowcount
        if changed != 1:
            conn.rollback()
            raise ConflictError(CHANGED_UNDERNEATH_MESSAGE.format(filename=document["filename"]))
        conn.commit()

        enqueue(document_id)
        return _document_row(conn, document_id)


@router.post(
    "/documents/{document_id}/move",
    response_model=DocumentRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def move_document(document_id: int, payload: DocumentMove, conn: DbConn) -> dict[str, object]:
    """File a document under a different class.

    A misfiled document is worse than a missing one: retrieval is partitioned by class, so
    the lecture notes sitting in the wrong workspace are invisible where they are needed
    and are quietly answering questions where they are not. The alternative on offer was
    delete and upload again, which throws the file away to fix its label.

    The move is a re-ingest, not a relabel. Chunks carry a denormalized `class_id`, their
    vectors live in a table partitioned by it, and the profile facts drawn out of the text
    belong to whichever class asked for them, so the document arrives in its new class the
    same way an upload does: `pending`, and indexed from there. Its rendered pages survive,
    because those depend on the file's bytes rather than on where it is filed.
    """
    # The whole read-decide-commit-rename sequence runs under the lifecycle mutex, so no
    # other lifecycle operation - a second move, a delete, a class delete - can advance
    # this document between the reads below and the rename at the end.
    with ownership.lifecycle_mutation():
        document = _document_row(conn, document_id)
        source_class_id = int(document["class_id"])
        # Before any work, so an unknown target is a 404 rather than a half-moved file.
        get_class(conn, payload.class_id)
        if payload.class_id == source_class_id:
            return document

        # A document mid-ingestion is being read by the worker from the path this would
        # move, and the state it lands in would overwrite the `pending` written here.
        if document["state"] not in TERMINAL_STATES:
            raise ConflictError(STILL_PROCESSING_MESSAGE.format(filename=document["filename"]))

        used_by = int(
            conn.execute(
                "select count(*) from artifact_sources where document_id = ?", (document_id,)
            ).fetchone()[0]
        )
        if used_by:
            raise ConflictError(IN_USE_MESSAGE.format(filename=document["filename"], count=used_by))

        # Read separately: `_DOCUMENT_COLUMNS` deliberately omits the stored path, because
        # it is the shape the interface receives and the interface never sees a filesystem
        # path. Checked for absence again, not assumed from the read above: under the
        # mutex the row cannot vanish in between, and if some future path ever lets it,
        # the answer is a clean 404 rather than a crash.
        stored_row = conn.execute(
            "select stored_path from documents where id = ?", (document_id,)
        ).fetchone()
        if stored_row is None:
            raise NotFoundError("That document does not exist.")
        stored = stored_row["stored_path"]
        stored_path = Path(str(stored)) if stored else None
        # The live move is held to the same owned-path contract as recovery, checked
        # before this request invalidates a single chunk, commits anything, records an
        # intent, or touches the filesystem: a corrupted or legacy row pointing outside
        # the uploads tree, or reachable only through a symlinked intermediate
        # directory, must not let a move relocate an arbitrary file the current user
        # happens to own. Either defect refuses exactly like a missing source - for the
        # student the file is equally unmovable either way.
        if stored_path is not None and not private.is_within(stored_path, settings.uploads_dir):
            raise ConflictError(MISSING_SOURCE_MESSAGE.format(filename=document["filename"]))
        try:
            source_present = stored_path is not None and storage_intents.source_file_present(
                stored_path
            )
        except private.PrivacyContractError:
            source_present = False
        except OSError:
            # Presence could not be determined - EACCES, EIO, a transient filesystem
            # fault. That is not the same statement as "the file is missing", and the 409
            # below would tell the student to re-upload a file that likely still exists.
            # Nothing has been invalidated or committed yet, so refuse as a failed,
            # retryable move instead.
            logger.warning(
                "Move of document %s could not inspect its stored file; refusing without mutating",
                document_id,
                exc_info=True,
            )
            raise LyraError(MOVE_FAILED_MESSAGE.format(filename=document["filename"])) from None
        # A missing source is an honest, recoverable refusal, never a "successful" move
        # whose destination path is fiction: the row must not be pointed at a file that was
        # never going to exist there.
        if not source_present:
            raise ConflictError(MISSING_SOURCE_MESSAGE.format(filename=document["filename"]))
        moved_path = (
            settings.uploads_dir
            / str(payload.class_id)
            / f"{document_id}-{_safe_filename(str(document['filename']))}"
        )

        delete_chunks(conn, document_id)
        # The old class stops asserting what only this file ever said, exactly as it would
        # have had the student deleted it. The new class learns it from the re-ingest
        # below.
        forget_document_evidence(conn, document_id)
        # The move intent becomes durable in the same commit as the row update, so after a
        # crash at any later point the database knows a rename was owed and startup
        # reconciliation converges: it rolls the rename forward if the file still sits at
        # the source, recognizes completion if it sits at the destination, and fails the
        # document honestly if it sits at neither (docs/storage-consistency.md).
        intent_id = storage_intents.record_intent(
            conn,
            storage_intents.MOVE_DOCUMENT,
            document_id=document_id,
            payload={"source": str(stored_path), "destination": str(moved_path)},
        )
        # Compare-and-swap on everything this move decided from. The mutex makes a
        # concurrent change impossible; the conditional write makes one harmless anyway:
        # a transition is committed only from the exact state that was observed, so a
        # request that somehow raced past serialization gets a clean conflict and its
        # whole transaction - chunk invalidation, evidence, intent - rolls back together.
        moved = conn.execute(
            "update documents set class_id = ?, stored_path = ?, state = ?, "
            "stage_detail = null, error_message = null, pages_done = 0 "
            "where id = ? and class_id = ? and stored_path = ? and state = ?",
            (
                payload.class_id,
                str(moved_path),
                PENDING,
                document_id,
                source_class_id,
                str(stored_path),
                str(document["state"]),
            ),
        ).rowcount
        if moved != 1:
            conn.rollback()
            raise ConflictError(CHANGED_UNDERNEATH_MESSAGE.format(filename=document["filename"]))
        conn.commit()

        try:
            # The destination class directory is Lyra-owned like any upload directory, so
            # it is created `0o700` and never through a symlink - the same contract the
            # initial upload (above) is held to.
            storage_intents.perform_move(stored_path, moved_path)
        except (OSError, private.PrivacyContractError):
            logger.warning(
                "Move of document %s could not rename its file; restoring it in class %s",
                document_id,
                source_class_id,
                exc_info=True,
            )
            # Compensate in one commit: the file never left, so the row returns to where
            # the file is and the intent is withdrawn together. The chunks and evidence
            # are already gone, so the document re-indexes in place - state stays
            # `pending` and it is queued below. The restore is itself conditional on the
            # row still being exactly what this move committed: compensation must never
            # overwrite a newer lifecycle mutation with stale state, so if the row has
            # legitimately advanced (or is gone), only the intent is withdrawn - the
            # rename it describes never happened, so no filesystem work is owed.
            restored = conn.execute(
                "update documents set class_id = ?, stored_path = ? "
                "where id = ? and class_id = ? and stored_path = ? and state = ?",
                (
                    source_class_id,
                    str(stored_path),
                    document_id,
                    payload.class_id,
                    str(moved_path),
                    PENDING,
                ),
            ).rowcount
            conn.execute("delete from storage_intents where id = ?", (intent_id,))
            conn.commit()
            if restored:
                touch_class(conn, source_class_id)
                enqueue(document_id)
            raise LyraError(MOVE_FAILED_MESSAGE.format(filename=document["filename"])) from None

        storage_intents.settle_intent(conn, intent_id)
        touch_class(conn, source_class_id)
        touch_class(conn, payload.class_id)
        enqueue(document_id)
        return _document_row(conn, document_id)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: int, conn: DbConn) -> None:
    # Deliberately no still-processing guard, unlike move, recognize, and reingest.
    # Deleting is the de facto cancel for a run in flight - a two-hour recognition pass
    # has no other stop button - so refusing it here would trap the student inside the
    # very run they are trying to abandon. The worker defends itself instead: it re-checks
    # this row's existence and `created_at` between pages and between stages, publishes
    # derived files only through the identity-checked barrier, and aborts quietly rather
    # than writing onto a row that is gone or has been re-created by a newer upload.
    #
    # The mutex spans the commit *and* the cleanup, which is what makes the late-writer
    # barrier airtight: a publication that checks the row after this block starts finds
    # it gone, and one that published before is removed by the cleanup below.
    with ownership.lifecycle_mutation():
        row = conn.execute(
            "select id, stored_path from documents where id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("That document does not exist.")

        delete_chunks(conn, document_id)
        # `delete_chunks` acquired SQLite's write lock, so no other connection can commit
        # until this transaction ends. The stored path is re-read under that lock: the
        # value read above could in principle be stale (a move committing in between
        # would leave this intent unlinking the file's old location while its new one
        # survives deletion), and the intent must record where the file actually is.
        # The mutex already prevents that interleave; the re-read makes the recorded
        # cleanup correct even for a caller that raced past it.
        fresh = conn.execute(
            "select stored_path from documents where id = ?", (document_id,)
        ).fetchone()
        if fresh is None:
            conn.rollback()
            raise NotFoundError("That document does not exist.")
        stored_path = fresh["stored_path"]
        # Before the delete, while the evidence rows are still there to be counted. Their
        # cascade would otherwise leave the class asserting what only this upload ever
        # said.
        forget_document_evidence(conn, document_id)
        # The intent commits with the row delete, so the row is never the only record of
        # the files still to remove: after a crash between this commit and the unlinks
        # below, startup reconciliation re-runs the cleanup from the intent instead of
        # leaving private coursework orphaned behind a UI that says it is gone.
        intent_id = storage_intents.record_intent(
            conn,
            storage_intents.DELETE_DOCUMENT,
            document_id=document_id,
            payload={"stored_path": str(stored_path) if stored_path else None},
        )
        conn.execute("delete from documents where id = ?", (document_id,))
        conn.commit()

        # A file already gone is the state we wanted, not an error; a file that cannot be
        # removed right now keeps its intent and is retried at the next startup rather
        # than failing a delete that has already committed.
        try:
            storage_intents.run_document_cleanup(document_id, stored_path)
        except (
            OSError,
            private.PrivacyContractError,
            storage_intents.IntentBlockedError,
            storage_intents.CleanupIncompleteError,
        ):
            logger.warning(
                "Cleanup for deleted document %s is deferred to the next startup",
                document_id,
                exc_info=True,
            )
            return
        storage_intents.settle_intent(conn, intent_id)


def _document_row(conn: sqlite3.Connection, document_id: int) -> dict[str, object]:
    """One document, or a 404. Every handler routes its lookup through here."""
    row = conn.execute(
        f"select {_DOCUMENT_COLUMNS} from documents where id = ?",  # noqa: S608
        (document_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("That document does not exist.")
    return dict(row)


def _mime_for(filename: str) -> str:
    """The mime implied by the file extension.

    Raises:
        LyraError: The extension is not one Phase 1 reads. Same message the parser
            uses, so the answer does not depend on where the file was refused.
    """
    mime = MIME_BY_SUFFIX.get(Path(filename).suffix.lower())
    if mime is None:
        raise LyraError(UNSUPPORTED_MESSAGE)
    return mime


def _display_filename(filename: str) -> str:
    """The name to show for an upload: its own, without the folders it was found in.

    A folder upload sends each file's path relative to the chosen folder as its multipart
    filename, so a term of notes arrives as `week 3/lecture/slides.pdf` and the document
    list reads as one folder repeated rather than as a list of files. The path is the
    browser's bookkeeping and not something the student typed, and there is nowhere to put
    it back: a Lyra class is the folder. Both separators are cut, because a Windows browser
    is entitled to send either.
    """
    return filename.replace("\\", "/").rsplit("/", 1)[-1].strip()


def _safe_filename(filename: str) -> str:
    """The upload name reduced to something safe to write inside the class directory.

    Directory separators go first, then every character outside `[A-Za-z0-9._-]`, then
    any leading dots, so the result can be neither a traversal nor a hidden file. The
    original name is kept intact in the `filename` column and is what the user sees.
    """
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return _UNSAFE_FILENAME_CHARS.sub("", base).lstrip(".") or "document"


def _text_path(document_id: int) -> Path:
    """Where ingestion stores this document's extracted text."""
    return storage_intents.text_path(document_id)
