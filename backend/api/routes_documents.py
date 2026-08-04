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
from backend.core.classes import get_class, touch_class
from backend.core.errors import LyraError, NotFoundError
from backend.core.ingestion import PENDING, delete_chunks, enqueue
from backend.rag import render
from backend.rag.parse import PDF_MIME, UNSUPPORTED_MESSAGE
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
MIME_BY_SUFFIX = {
    ".pdf": PDF_MIME,
    ".txt": "text/plain",
    ".md": "text/markdown",
}

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")

_DOCUMENT_COLUMNS = (
    "id, class_id, filename, mime, byte_size, state, stage_detail, "
    "pages_total, pages_done, pages_skipped, error_message, created_at"
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
    error_message: str | None
    created_at: str


class DocumentDetail(DocumentRead):
    """One document, plus whether its extracted text is on hand for a re-index."""

    has_text: bool


class DocumentText(BaseModel):
    """A text source as the solver's source pane reads it."""

    filename: str
    text: str
    truncated: bool


class StatusRead(BaseModel):
    """The poll target while a document ingests."""

    state: str
    stage_detail: str | None
    pages_total: int | None
    pages_done: int
    pages_skipped: int
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
    filename = (file.filename or "").strip()
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
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    stored_path.write_bytes(payload)
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
    _document_row(conn, document_id)
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


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: int, conn: DbConn) -> None:
    row = conn.execute(
        "select id, stored_path from documents where id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("That document does not exist.")

    delete_chunks(conn, document_id)
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
