"""Per-page state, and reading the pages that carry no text of their own.

Stage 2b of docs/rag-pipeline.md. Two things live here because they are one idea.

`documents.pages_done` was a number written once, at the end of a run, and that was honest
while every page of a document shared a single outcome: either the file parsed or it did
not. Recognition breaks that. A page costs seconds of model time, can fail on its own, and
is worth retrying on its own, which makes its state a row. `document_pages` is that row,
and the counts on `documents` become a tally over it while a run is in flight.

Pages go to the configured vision model, and the locality rule below governs. The
specialist runtime in `llm/ocr_server.py` is deliberately **not** wired in here: it is
built, downloadable, and measured, and the measurement says it loses. 18.5 seconds a page
against the general path's 13.8, with repetition loops on five of the same eight pages.
docs/rag-pipeline.md records the numbers and what has to change upstream first.

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
    resolve_tutor_access,
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
# What a run in flight picks up: only pages nothing has attempted yet. `failed` is
# deliberately not here. A page that failed permanently would otherwise be re-paid for on
# every plain re-index - a chunking-tuning pass re-reads no images at all - so re-attempting
# a `failed` page takes the explicit recognize/retry action, which flips it back to
# `scanned` through `reset_failed_pages` before the run starts.
PENDING_STATES = (SCANNED,)

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
# What a tripped breaker means, in the student's words. Distinct from
# `ALL_PAGES_FAILED_MESSAGE` on purpose: when three pages in a row fail and six hundred
# were never attempted, "none of the pages could be read" is false and points the student
# at the document. The endpoint is the fault, and the endpoint is what they can fix.
ENDPOINT_FAILED_MESSAGE = (
    "The endpoint reading this document failed several pages in a row, so Lyra stopped "
    "rather than paying a timeout on every page. Check the endpoint in Settings, then try "
    "again."
)

# Why a requested run did not read anything, in the student's words. A skip is not a page
# failure: nothing was attempted, so no page is marked `failed` and the document lands
# exactly where it would have without the request.
_SKIP_MESSAGES = {
    NO_ENDPOINT: NO_ENDPOINT_MESSAGE,
    REMOTE_UNACKNOWLEDGED: transcribe.REMOTE_MESSAGE,
}

# Returned by `recognize_pages` when the breaker below tripped. Not in `_SKIP_MESSAGES`,
# because it is a different fact: a skip means nothing was attempted, and this means
# attempts were made and the endpoint failed every one of them.
ENDPOINT_FAILED = "endpoint_failed"

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
        None when pages were read or there were none to read. Otherwise the reason the run
        stopped short: `no_endpoint` or `remote_unacknowledged` when nothing was attempted,
        or `ENDPOINT_FAILED` when attempts were made and the breaker below gave up.
    """
    document_id = int(document["id"])
    pending = [
        int(row[0])
        for row in conn.execute(
            # `PENDING_STATES` is one value and the placeholder is written out to match,
            # so this stays ordinary bound SQL rather than SQL assembled from a constant.
            "select page_number from document_pages "
            "where document_id = ? and state = ? order by page_number",
            (document_id, *PENDING_STATES),
        )
    ]
    if not pending:
        return None

    # Before a single page is rendered, let alone encoded and sent. The rule is about
    # document content leaving the machine, and a picture of a page is the most complete
    # form of it there is. One snapshot resolves the endpoint, the permission, and the
    # acknowledgement together, so the whole run reads and transcribes against the exact
    # endpoint this decision authorized - the `remote_ack` passed down to `transcribe` is
    # the one the skip check agreed with, not a value a concurrent settings write could have
    # changed between two reads.
    access = resolve_tutor_access(conn)
    if access.document_block is not None:
        logger.info("Recognition skipped for document %s: %s", document_id, access.document_block)
        return access.document_block

    config = access.config
    remote_ack = access.remote_ack
    source = Path(str(document["stored_path"]))
    mime = str(document["mime"])

    conn.execute(
        "update documents set stage_detail = ? where id = ?", (RECOGNIZING_DETAIL, document_id)
    )
    conn.commit()

    started_at = str(document["created_at"])
    consecutive = 0
    for page_number in pending:
        try:
            text = _read_one_page(config, remote_ack, document_id, source, mime, page_number)
        except Exception as exc:
            state, error = FAILED, _page_error(exc)
            text = None
            logger.warning(
                "Could not read page %s of document %s", page_number, document_id, exc_info=True
            )
        else:
            state, error = RECOGNIZED, None
        # After the model call and before anything is written, because the call is the long
        # part of a run that can take hours and deleting the document is its de facto
        # cancel. A delete followed by a fresh upload can put a different file behind this
        # id, and settling would write this run's page - or its failure - onto that file.
        if document_replaced(conn, document_id, started_at):
            logger.info("Stopped reading document %s: deleted or replaced mid-run", document_id)
            return None
        _settle_page(conn, document_id, page_number, state, text, error)
        if state == FAILED:
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                logger.warning(
                    "Stopped reading document %s after %s pages failed in a row",
                    document_id,
                    consecutive,
                )
                return ENDPOINT_FAILED
        else:
            consecutive = 0
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


def reset_failed_pages(conn: sqlite3.Connection, document_id: int) -> int:
    """Put this document's failed pages back in the pending set. The caller commits.

    This is what makes a retry explicit. `Try those pages` and `Read this document` mean
    "attempt them again", so the pages they cover go back to `scanned` - the truth once a
    new attempt is coming - and the next run picks them up. A plain re-index never calls
    this, which is exactly why it never re-pays model time on a page that failed for good.

    Returns:
        How many pages were reset.
    """
    cursor = conn.execute(
        "update document_pages set state = ?, error_message = null "
        "where document_id = ? and state = ?",
        (SCANNED, document_id, FAILED),
    )
    return cursor.rowcount


def document_replaced(conn: sqlite3.Connection, document_id: int, started_at: str) -> bool:
    """Whether the row a worker started from is gone, or re-created under the same id.

    Deleting a document is the de facto cancel for a run that can take hours, and a delete
    followed by an immediate re-upload can hand this id to a different file. `started_at`
    is the row's `created_at` captured when the run began; a row that no longer matches it
    is not the document the worker was asked to read, and nothing this run produced -
    pages, chunks, or profile facts - may be written onto it.
    """
    row = conn.execute("select created_at from documents where id = ?", (document_id,)).fetchone()
    return row is None or str(row["created_at"]) != str(started_at)


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
