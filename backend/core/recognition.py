"""Per-page state, and reading the pages that carry no text of their own.

Stage 2b of docs/rag-pipeline.md. Two things live here because they are one idea.

`documents.pages_done` was a number written once, at the end of a run, and that was honest
while every page of a document shared a single outcome: either the file parsed or it did
not. Recognition breaks that. A page costs seconds of model time, can fail on its own, and
is worth retrying on its own, which makes its state a row. `document_pages` is that row,
and the counts on `documents` become a tally over it while a run is in flight.

**Recognition is opt-in per document and never happens on upload.** Two reasons, and the
second is the one that matters. It is 13.4 seconds a page measured against the reference
book, which is minutes for a problem set and hours for a textbook. And against a configured
remote endpoint it sends a picture of the student's own material somewhere. A capability
arriving is not consent to use it on everything already on disk, so `documents.recognize` is
set by an explicit action and by nothing else.

What is deliberately not here is re-reading pages that already have a text layer. The
Step 3 measurement found transcription beats PyMuPDF on mathematics even where nothing was
scanned, so that is worth doing, but it is a different affordance with a different price -
every page of the document rather than the unreadable ones - and offering it as a side
effect of "this page could not be read" would be a bait and switch. It is recorded in
docs/feature-roadmap.md as its own item.
"""

import asyncio
import logging
import sqlite3
from pathlib import Path

from backend.core.app_settings import (
    NO_ENDPOINT,
    REMOTE_UNACKNOWLEDGED,
    TutorConfig,
    document_text_allowed,
    get_settings_row,
    resolve_tutor_config,
)
from backend.rag import render, transcribe
from backend.rag.parse import ParsedDocument, ParsedPage

logger = logging.getLogger(__name__)

# What a page can be.
#
# `text` and `recognized` are the two ways a page ends up readable, and they are kept
# distinct because only one of them cost inference. `scanned` is not a failure: it is a page
# with no text layer that nothing has been asked to read yet, which is where every page of a
# scanned upload sits until the student asks. `failed` is a page recognition tried and could
# not transcribe, which is the fact `PageFailureNotice` reports.
TEXT = "text"
SCANNED = "scanned"
RECOGNIZED = "recognized"
FAILED = "failed"

READABLE_STATES = (TEXT, RECOGNIZED)
# States a recognition run picks up. A retry is the same call as a first run, so `Try those
# pages` needs no endpoint of its own: pages that already worked are simply not in this set.
PENDING_STATES = (SCANNED, FAILED)

# Written into `documents.stage_detail` while pages are being read. Recognition is not a
# fifth ingestion step and does not get a state of its own: ui-phase-3.md renders it under
# Reading, because a student does not have two concepts here. There is text in the file or
# there is not, and either way what Lyra is doing is reading the document. This detail is
# what tells the interface the page counter is allowed to appear.
RECOGNIZING_DETAIL = "recognizing"

PAGE_FAILED_MESSAGE = "This page could not be read."
ALL_PAGES_FAILED_MESSAGE = "None of the pages in this document could be read."
NO_ENDPOINT_MESSAGE = (
    "Reading pages needs a model that can see images. Add an endpoint in Settings."
)

# Why a requested run did not read anything, in the student's words. A skip is not a page
# failure: nothing was attempted, so no page is marked `failed` and the document lands
# exactly where it would have without the request.
_SKIP_MESSAGES = {
    NO_ENDPOINT: NO_ENDPOINT_MESSAGE,
    REMOTE_UNACKNOWLEDGED: transcribe.REMOTE_MESSAGE,
}

# How many pages in a row may fail before the run gives up on the rest.
#
# A page failing is ordinary. Every page failing in sequence is not a document problem, it
# is the endpoint being down or unable to see, and grinding through six hundred pages to
# discover that costs the student the timeout on each one. The pages never reached stay
# `scanned`, which is the truth - nothing was attempted - and `Try those pages` picks them
# up once the endpoint is back.
MAX_CONSECUTIVE_FAILURES = 3

_PAGE_COLUMNS = "page_number, state, text, error_message"

_INSERT_PAGE_SQL = (
    "insert into document_pages (document_id, page_number, state, text, error_message) "
    "values (?, ?, ?, ?, ?)"
)


def sync_pages(conn: sqlite3.Connection, document_id: int, parsed: ParsedDocument) -> None:
    """Record what every page of this document is, keeping transcriptions that exist.

    Called on every ingestion, so the rows always describe the file as it parses now. A
    page already `recognized` or `failed` keeps that state and its text: the stored bytes a
    document id points at never change, so a re-index is re-reading the same page, and
    re-running recognition on it would spend the model time again to reach the same answer.

    Args:
        conn: Open database connection. Committed here.
        document_id: Document these pages belong to.
        parsed: The parse that just ran. `pages` carries only pages with a text layer, and
            `pages_total` is how many the file has.
    """
    prior = {
        int(row["page_number"]): row
        for row in conn.execute(
            f"select {_PAGE_COLUMNS} from document_pages where document_id = ?",  # noqa: S608
            (document_id,),
        )
    }
    with_text = {page.page_number for page in parsed.pages}

    rows = []
    for number in range(1, parsed.pages_total + 1):
        if number in with_text:
            rows.append((document_id, number, TEXT, None, None))
            continue
        previous = prior.get(number)
        if previous is not None and previous["state"] in (RECOGNIZED, FAILED):
            rows.append(
                (
                    document_id,
                    number,
                    previous["state"],
                    previous["text"],
                    previous["error_message"],
                )
            )
            continue
        rows.append((document_id, number, SCANNED, None, None))

    conn.execute("delete from document_pages where document_id = ?", (document_id,))
    conn.executemany(_INSERT_PAGE_SQL, rows)
    conn.commit()


def merge_recognized(
    conn: sqlite3.Connection, document_id: int, parsed: ParsedDocument
) -> ParsedDocument:
    """Splice transcribed pages back into the parse, in page order.

    Everything downstream - chunking, sections, embedding, citation - is written against
    `ParsedDocument` and has no reason to care which pages came from a text layer and which
    from a model. So recognition ends by handing back the same shape with more pages in it.

    A page transcribed as nothing stays out of `pages`. A blank page is correctly a page
    with nothing to find, and putting it in would embed an empty chunk; its row remains
    `recognized`, so it is not read a second time.
    """
    recognized = {
        int(row["page_number"]): str(row["text"] or "")
        for row in conn.execute(
            "select page_number, text from document_pages where document_id = ? and state = ?",
            (document_id, RECOGNIZED),
        )
    }
    extra = [
        ParsedPage(page_number=number, text=text)
        for number, text in recognized.items()
        if text.strip() and number not in {page.page_number for page in parsed.pages}
    ]
    if not extra:
        return parsed

    pages = sorted([*parsed.pages, *extra], key=lambda page: page.page_number)
    return ParsedDocument(
        pages=pages,
        pages_total=parsed.pages_total,
        pages_skipped=parsed.pages_total - len(pages),
        outline=parsed.outline,
    )


def recognize_pages(
    conn: sqlite3.Connection, document: sqlite3.Row, parsed: ParsedDocument
) -> str | None:
    """Read every page of this document that has no text of its own.

    Runs inside the parsing stage, committing after each page so a poller watching a long
    document sees it advance rather than watching one number for two hours.

    Args:
        conn: Open database connection. Committed repeatedly.
        document: The `documents` row, for its id, stored path, and mime.
        parsed: The parse that just ran, only for its page total.

    Returns:
        None when pages were read or there were none to read, otherwise the reason nothing
        was attempted: `no_endpoint` or `remote_unacknowledged`.
    """
    document_id = int(document["id"])
    pending = [
        int(row[0])
        for row in conn.execute(
            # `PENDING_STATES` is two values and the placeholders are written out to match,
            # so this stays ordinary bound SQL rather than SQL assembled from a constant.
            "select page_number from document_pages "
            "where document_id = ? and state in (?, ?) order by page_number",
            (document_id, *PENDING_STATES),
        )
    ]
    if not pending:
        return None

    # Before a single page is rendered, let alone encoded and sent. The rule is about
    # document content leaving the machine, and a picture of a page is the most complete
    # form of it there is.
    reason = document_text_allowed(conn)
    if reason is not None:
        logger.info("Recognition skipped for document %s: %s", document_id, reason)
        return reason

    config = resolve_tutor_config(conn)
    remote_ack = bool(get_settings_row(conn)["remote_ack"])
    source = Path(str(document["stored_path"]))
    mime = str(document["mime"])

    conn.execute(
        "update documents set stage_detail = ? where id = ?", (RECOGNIZING_DETAIL, document_id)
    )
    conn.commit()

    consecutive = 0
    for page_number in pending:
        try:
            text = _read_one_page(config, remote_ack, document_id, source, mime, page_number)
        except Exception as exc:
            consecutive += 1
            _settle_page(conn, document_id, page_number, FAILED, None, _page_error(exc))
            logger.warning(
                "Could not read page %s of document %s", page_number, document_id, exc_info=True
            )
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                logger.warning(
                    "Stopped reading document %s after %s pages failed in a row",
                    document_id,
                    consecutive,
                )
                break
        else:
            consecutive = 0
            _settle_page(conn, document_id, page_number, RECOGNIZED, text, None)
    return None


def failed_page_count(conn: sqlite3.Connection, document_id: int) -> int:
    """How many pages recognition tried and could not read.

    A different fact from `pages_skipped`, which counts pages that had no text to find, and
    both can be true of the same document at once.
    """
    row = conn.execute(
        "select count(*) from document_pages where document_id = ? and state = ?",
        (document_id, FAILED),
    ).fetchone()
    return int(row[0])


def skip_message(reason: str | None) -> str | None:
    """The student-facing reason a requested run read nothing, if there is one."""
    return None if reason is None else _SKIP_MESSAGES.get(reason)


def _read_one_page(
    config: TutorConfig,
    remote_ack: bool,
    document_id: int,
    source: Path,
    mime: str,
    page_number: int,
) -> str:
    """Render one page at recognition resolution and transcribe it."""
    image = render.render_page(
        document_id, source, mime, page_number, render.RECOGNITION_DPI
    ).read_bytes()
    # `client.complete` is async and the ingestion worker is a plain thread with no event
    # loop, the same situation `extract_facts` is in and handled the same way: own a loop
    # for the length of the call rather than colour the worker async.
    return asyncio.run(
        transcribe.transcribe_page(
            config.endpoint_url,
            config.api_key,
            config.model,
            image,
            remote_ack=remote_ack,
        )
    )


def _settle_page(
    conn: sqlite3.Connection,
    document_id: int,
    page_number: int,
    state: str,
    text: str | None,
    error_message: str | None,
) -> None:
    """Land one page and publish the count, committed so the interface can watch it."""
    conn.execute(
        "update document_pages set state = ?, text = ?, error_message = ? "
        "where document_id = ? and page_number = ?",
        (state, text, error_message, document_id, page_number),
    )
    # `pages_done` becomes a tally rather than a number written at the end. This is the
    # whole reason per-page rows exist, and it is what lets the interface say "Reading page
    # 41 of 608" instead of showing a count that will not move for two hours.
    conn.execute(
        "update documents set pages_done = ("
        "  select count(*) from document_pages where document_id = ? and state in (?, ?)"
        ") where id = ?",
        (document_id, *READABLE_STATES, document_id),
    )
    conn.commit()


def _page_error(exc: Exception) -> str:
    """A user-facing reason one page could not be read, never a traceback.

    `LyraError` and its subclasses carry a message written for the student, so an endpoint
    that refused or a page that would not render says so. Anything else is a surprise and
    gets the neutral line rather than whatever a library put in its exception.
    """
    message = getattr(exc, "message", None)
    return str(message) if isinstance(message, str) and message else PAGE_FAILED_MESSAGE
