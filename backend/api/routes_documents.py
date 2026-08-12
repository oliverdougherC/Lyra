"""Document endpoints: upload, listing, ingestion status, reingest, and deletion.

Upload answers `202` and does no work beyond storing the file, because parsing and
embedding take seconds to minutes. The interface polls `/status` from there.

Handlers are sync `def`: `sqlite3` and file writes block, and FastAPI runs sync handlers
in a threadpool, which is exactly where blocking work belongs.
"""

import re
import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.config import settings
from backend.core import figures, recognition
from backend.core.classes import get_class, touch_class
from backend.core.errors import ConflictError, LyraError, NotFoundError
from backend.core.ingestion import PENDING, delete_chunks, enqueue
from backend.core.profiles import forget_document_evidence
from backend.rag import render, structure
from backend.rag.parse import PDF_MIME, UNSUPPORTED_MESSAGE
from backend.storage import private
from backend.storage.database import get_db

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
    # until both have happened, so a failed write leaves no orphan row behind.
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
    private.secure_mkdir(stored_path.parent)
    private.write_private_bytes(stored_path, payload)
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
        "select id, stored_path, mime from documents where id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("That document does not exist.")

    path = render.render_page(
        document_id, Path(str(row["stored_path"])), str(row["mime"]), page_number
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
    document = _document_row(conn, document_id)
    # The same guard move and recognize have always had, for the same reason: the worker
    # is reading this row and about to write its state, so the `pending` written below
    # would be overwritten by whatever the in-flight run lands in, and the chunk delete
    # below would race the batches that run is still committing.
    if document["state"] not in TERMINAL_STATES:
        raise ConflictError(STILL_PROCESSING_MESSAGE.format(filename=document["filename"]))

    if document["state"] == "failed":
        # Retrying a failed document is an explicit retry, so pages that failed go back in
        # the pending set. A re-index of a `ready` document deliberately does not do this:
        # that is a chunking pass, and it must not re-pay model time on pages that failed
        # for good. See `recognition.reset_failed_pages`.
        recognition.reset_failed_pages(conn, document_id)
    # Clearing the previous run's chunks here as well as in the job keeps the document
    # from serving stale results while it waits in the queue.
    delete_chunks(conn, document_id)
    # Same reason for the rendered pages: a stale image would show a page from the file
    # that used to be there.
    render.discard_pages(document_id)
    conn.execute(
        "update documents set state = ?, stage_detail = null, error_message = null, "
        "pages_done = 0 where id = ?",
        (PENDING, document_id),
    )
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
    document = _document_row(conn, document_id)
    # The worker is reading this row and about to write its state, and the `pending` set
    # below would be overwritten by whatever it lands in.
    if document["state"] not in TERMINAL_STATES:
        raise ConflictError(STILL_PROCESSING_MESSAGE.format(filename=document["filename"]))

    # This is the explicit "attempt them again", so failed pages rejoin the pending set.
    # An in-flight run never re-attempts `failed` pages on its own - that would make
    # every plain re-index re-pay for pages that failed for good.
    recognition.reset_failed_pages(conn, document_id)
    delete_chunks(conn, document_id)
    # Deliberately no `render.discard_pages` here, unlike the re-ingest below. The rendered
    # pages are what recognition reads, they were rendered from bytes that have not changed,
    # and throwing them away would make every retry pay to rasterize the document again.
    conn.execute(
        "update documents set recognize = 1, state = ?, stage_detail = null, "
        "error_message = null, pages_done = 0 where id = ?",
        (PENDING, document_id),
    )
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
    document = _document_row(conn, document_id)
    source_class_id = int(document["class_id"])
    # Before any work, so an unknown target is a 404 rather than a half-moved file.
    get_class(conn, payload.class_id)
    if payload.class_id == source_class_id:
        return document

    # A document mid-ingestion is being read by the worker from the path this would move,
    # and the state it lands in would overwrite the `pending` written here.
    if document["state"] not in TERMINAL_STATES:
        raise ConflictError(STILL_PROCESSING_MESSAGE.format(filename=document["filename"]))

    used_by = int(
        conn.execute(
            "select count(*) from artifact_sources where document_id = ?", (document_id,)
        ).fetchone()[0]
    )
    if used_by:
        raise ConflictError(IN_USE_MESSAGE.format(filename=document["filename"], count=used_by))

    # Read separately: `_DOCUMENT_COLUMNS` deliberately omits the stored path, because it
    # is the shape the interface receives and the interface never sees a filesystem path.
    stored = conn.execute(
        "select stored_path from documents where id = ?", (document_id,)
    ).fetchone()["stored_path"]
    stored_path = Path(str(stored)) if stored else None
    moved_path = (
        settings.uploads_dir
        / str(payload.class_id)
        / f"{document_id}-{_safe_filename(str(document['filename']))}"
    )
    if stored_path is not None and stored_path.exists():
        moved_path.parent.mkdir(parents=True, exist_ok=True)
        stored_path.replace(moved_path)

    delete_chunks(conn, document_id)
    # The old class stops asserting what only this file ever said, exactly as it would
    # have had the student deleted it. The new class learns it from the re-ingest below.
    forget_document_evidence(conn, document_id)
    conn.execute(
        "update documents set class_id = ?, stored_path = ?, state = ?, stage_detail = null, "
        "error_message = null, pages_done = 0 where id = ?",
        (payload.class_id, str(moved_path), PENDING, document_id),
    )
    conn.commit()

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
    # this row's existence and `created_at` between pages and between stages, and aborts
    # quietly rather than writing onto a row that is gone or has been re-created by a
    # newer upload.
    row = conn.execute(
        "select id, stored_path from documents where id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("That document does not exist.")

    delete_chunks(conn, document_id)
    # Before the delete, while the evidence rows are still there to be counted. Their
    # cascade would otherwise leave the class asserting what only this upload ever said.
    forget_document_evidence(conn, document_id)
    conn.execute("delete from documents where id = ?", (document_id,))
    conn.commit()

    # A file already gone is the state we wanted, not an error.
    if row["stored_path"]:
        Path(row["stored_path"]).unlink(missing_ok=True)
    _text_path(document_id).unlink(missing_ok=True)
    render.discard_pages(document_id)


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
    return settings.text_dir / f"{document_id}.txt"
