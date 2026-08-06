"""Reading the pages a document has no text for, and the state that makes it retryable.

The transcription call is faked at the seam `backend.core.recognition` reaches it through,
so nothing here touches the network, but everything else is real: real PDFs built with
PyMuPDF, real rendering at 300 dpi, real chunking and storage. What the fake records is
which pages were sent, because most of the rules in this module are about pages that must
not be.
"""

import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

import pymupdf
import pytest

from backend.config import settings
from backend.core import ingestion, recognition
from backend.core.errors import UpstreamError
from backend.core.ingestion import SCANNED_MESSAGE, run_ingestion
from backend.rag import transcribe
from backend.rag.embed import EMBEDDING_DIM
from backend.rag.parse import PDF_MIME
from backend.storage.database import connect

LOCAL_ENDPOINT = "http://127.0.0.1:8080/v1"
REMOTE_ENDPOINT = "https://api.example.com/v1"

# Long enough to survive chunking and to be visibly different from the text layer.
TRANSCRIPT = (
    "Table of Fourier Transforms\n\n"
    "The transform of a rectangular pulse is a sinc, and the transform of a sinc is a "
    "rectangular pulse. Scaling in one domain is the reciprocal scaling in the other, so a "
    "narrow pulse has a wide spectrum. Every pair below follows from that duality together "
    "with the shift and modulation properties stated above it."
)


@pytest.fixture(autouse=True)
def fake_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the embedding server and both profile passes, as test_ingestion does."""
    monkeypatch.setattr(
        ingestion,
        "embed_documents",
        lambda texts: [[round(0.001 * (len(text) % 97), 3)] * EMBEDDING_DIM for text in texts],
    )
    monkeypatch.setattr(ingestion, "extract_facts", lambda conn, document_id, text, doc_type: None)
    monkeypatch.setattr(ingestion, "consolidate_class", lambda conn, class_id: None)


def _write_pdf(path: Path, pages: Sequence[str]) -> Path:
    """Build a PDF with one page per string. An empty string leaves the page scanned."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        if text:
            page.insert_textbox(pymupdf.Rect(36, 36, 559, 770), text, fontsize=8)
    document.save(path)
    document.close()
    return path


def _prose(words: int, seed: int = 0) -> str:
    return " ".join(f"w{(index + seed) % 89:02d}" for index in range(words))


def _seed_document(
    db: sqlite3.Connection,
    class_id: int,
    stored_path: Path,
    *,
    mime: str = PDF_MIME,
    recognize: bool = False,
) -> int:
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state, "
        "recognize) values (?, ?, ?, ?, ?, 'pending', ?)",
        (
            class_id,
            stored_path.name,
            str(stored_path),
            mime,
            stored_path.stat().st_size,
            int(recognize),
        ),
    )
    db.commit()
    return int(cursor.lastrowid or 0)


def _configure_endpoint(db: sqlite3.Connection, url: str = LOCAL_ENDPOINT, ack: int = 0) -> None:
    db.execute("update settings set endpoint_url = ?, remote_ack = ? where id = 1", (url, ack))
    db.commit()


def _document(db: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    row = db.execute("select * from documents where id = ?", (document_id,)).fetchone()
    assert row is not None
    return row


def _page_states(db: sqlite3.Connection, document_id: int) -> dict[int, str]:
    return {
        int(row["page_number"]): str(row["state"])
        for row in db.execute(
            "select page_number, state from document_pages where document_id = ?", (document_id,)
        )
    }


def _chunk_pages(db: sqlite3.Connection, document_id: int) -> set[int]:
    return {
        int(row[0])
        for row in db.execute(
            "select distinct page_number from chunks where document_id = ?", (document_id,)
        )
    }


def _reader(
    monkeypatch: pytest.MonkeyPatch,
    replies: Callable[[int], str | Exception] | None = None,
    default: str = TRANSCRIPT,
) -> list[bytes]:
    """Stand in for the vision model, returning the image bytes of every call made.

    Pages are read in page order, so `replies` is keyed on the 1-based call number, which
    is what lets a test say "the second page fails" without threading page numbers through
    a signature that does not carry them.
    """
    calls: list[bytes] = []

    async def fake(
        endpoint: str, api_key: str | None, model: str | None, image: bytes, **kwargs: object
    ) -> str:
        calls.append(image)
        reply = replies(len(calls)) if replies is not None else default
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(transcribe, "transcribe_page", fake)
    return calls


def test_a_scanned_document_is_not_read_until_someone_asks(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in, which is the whole posture of this stage.

    Recognition is minutes of model time per document and, against a configured remote
    endpoint, it sends page images of the student's own material somewhere. A capability
    arriving is not consent to use it on everything already on disk, so an ordinary
    ingestion of a scanned file must not call the model at all.
    """
    _configure_endpoint(db)
    stored = _write_pdf(settings.uploads_dir / "scanned.pdf", ["1", "2", "3"])
    document_id = _seed_document(db, class_id, stored)
    calls = _reader(monkeypatch)

    run_ingestion(document_id)

    assert calls == []
    row = _document(db, document_id)
    assert row["state"] == "unsupported"
    assert row["error_message"] == SCANNED_MESSAGE
    # The rows exist anyway, because they are what a later request reads to know which
    # pages to attempt.
    assert _page_states(db, document_id) == {1: "scanned", 2: "scanned", 3: "scanned"}


def test_asking_for_a_scanned_document_makes_it_searchable(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_endpoint(db)
    stored = _write_pdf(settings.uploads_dir / "scanned.pdf", ["1", "2"])
    document_id = _seed_document(db, class_id, stored, recognize=True)
    calls = _reader(monkeypatch)

    run_ingestion(document_id)

    row = _document(db, document_id)
    assert row["state"] == "ready"
    assert (row["pages_total"], row["pages_done"], row["pages_skipped"]) == (2, 2, 0)
    assert _page_states(db, document_id) == {1: "recognized", 2: "recognized"}
    # What was sent is a real rendered page, not a placeholder.
    assert len(calls) == 2
    assert all(image[:8] == b"\x89PNG\r\n\x1a\n" for image in calls)
    # And what came back is what is searchable now.
    assert _chunk_pages(db, document_id) == {1, 2}
    content = db.execute(
        "select content from chunks where document_id = ? order by id", (document_id,)
    ).fetchone()[0]
    assert "Fourier" in content


def test_only_the_pages_without_text_are_sent(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mixed document is handled per page, which is the point of a per-page row.

    The pages that already read perfectly well are not re-read: that would be the entire
    document's worth of model time to recover pages that were never missing.
    """
    _configure_endpoint(db)
    stored = _write_pdf(
        settings.uploads_dir / "mixed.pdf", [_prose(180), "7", _prose(180, seed=13)]
    )
    document_id = _seed_document(db, class_id, stored, recognize=True)
    calls = _reader(monkeypatch)

    run_ingestion(document_id)

    assert len(calls) == 1
    assert _page_states(db, document_id) == {1: "text", 2: "recognized", 3: "text"}
    row = _document(db, document_id)
    assert row["state"] == "ready"
    # The page that was skipped before is now one of the pages that were read.
    assert (row["pages_done"], row["pages_skipped"]) == (3, 0)
    assert _chunk_pages(db, document_id) == {1, 2, 3}


def test_a_page_that_fails_does_not_take_the_document_with_it(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thirty-nine good pages and one bad one is a document that works.

    ui-phase-3.md makes this a quiet caption with a retry rather than a failure, because
    styling it as a failure would tell the student to throw away something mostly fine.
    """
    _configure_endpoint(db)
    stored = _write_pdf(settings.uploads_dir / "scanned.pdf", ["1", "2", "3"])
    document_id = _seed_document(db, class_id, stored, recognize=True)
    _reader(
        monkeypatch,
        replies=lambda n: UpstreamError("The model ran out of context.") if n == 2 else TRANSCRIPT,
    )

    run_ingestion(document_id)

    row = _document(db, document_id)
    assert row["state"] == "ready"
    assert _page_states(db, document_id) == {1: "recognized", 2: "failed", 3: "recognized"}
    assert recognition.failed_page_count(db, document_id) == 1
    # The message is the endpoint's own, written for the student, rather than a traceback.
    failure = db.execute(
        "select error_message from document_pages where document_id = ? and page_number = 2",
        (document_id,),
    ).fetchone()[0]
    assert failure == "The model ran out of context."


def test_a_document_where_every_page_failed_is_a_failure(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was kept, so there is nothing to call ready. Distinct from `unsupported`,
    which means nobody has tried yet."""
    _configure_endpoint(db)
    stored = _write_pdf(settings.uploads_dir / "scanned.pdf", ["1", "2"])
    document_id = _seed_document(db, class_id, stored, recognize=True)
    _reader(monkeypatch, replies=lambda n: UpstreamError("No."))

    run_ingestion(document_id)

    row = _document(db, document_id)
    assert row["state"] == "failed"
    assert row["error_message"] == recognition.ALL_PAGES_FAILED_MESSAGE


def test_a_run_gives_up_rather_than_grinding_through_a_dead_endpoint(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every page failing in sequence is the endpoint, not the document.

    Discovering that six hundred times costs the student the timeout on each one. The pages
    never reached stay `scanned`, which is the truth: nothing was attempted on them.
    """
    _configure_endpoint(db)
    stored = _write_pdf(settings.uploads_dir / "book.pdf", ["1", "2", "3", "4", "5", "6"])
    document_id = _seed_document(db, class_id, stored, recognize=True)
    calls = _reader(monkeypatch, replies=lambda n: UpstreamError("Connection refused."))

    run_ingestion(document_id)

    assert len(calls) == recognition.MAX_CONSECUTIVE_FAILURES
    states = _page_states(db, document_id)
    assert [states[page] for page in (1, 2, 3)] == ["failed", "failed", "failed"]
    assert [states[page] for page in (4, 5, 6)] == ["scanned", "scanned", "scanned"]


def test_a_retry_reads_the_failed_pages_and_leaves_the_rest_alone(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Try those pages` is the same call as `Read this document`.

    Both mean "attempt every page that does not currently have text", and the page rows
    already know which those are, so a retry never spends model time on a page that worked.
    """
    _configure_endpoint(db)
    stored = _write_pdf(settings.uploads_dir / "scanned.pdf", ["1", "2", "3"])
    document_id = _seed_document(db, class_id, stored, recognize=True)
    _reader(monkeypatch, replies=lambda n: UpstreamError("Timed out.") if n == 2 else TRANSCRIPT)
    run_ingestion(document_id)

    second = _reader(monkeypatch)
    run_ingestion(document_id)

    assert len(second) == 1
    assert _page_states(db, document_id) == {
        1: "recognized",
        2: "recognized",
        3: "recognized",
    }


def test_reindexing_never_reruns_recognition(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcription outlives a re-parse, which is why it is stored on the page row.

    The bytes a document id points at do not change, so re-reading the same page would
    spend the model time again to arrive at the same answer.
    """
    _configure_endpoint(db)
    stored = _write_pdf(settings.uploads_dir / "scanned.pdf", ["1", "2"])
    document_id = _seed_document(db, class_id, stored, recognize=True)
    _reader(monkeypatch)
    run_ingestion(document_id)

    again = _reader(monkeypatch, default="something completely different")
    run_ingestion(document_id)

    assert again == []
    row = _document(db, document_id)
    assert row["state"] == "ready"
    content = db.execute(
        "select content from chunks where document_id = ? order by id", (document_id,)
    ).fetchone()[0]
    assert "Fourier" in content


def test_a_remote_endpoint_without_acknowledgement_reads_nothing(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check runs before a single page is rendered, let alone encoded and sent.

    What travels is a picture of the student's document, so this is the same rule
    `extract_facts` follows and it is enforced ahead of the work rather than inside it.
    """
    _configure_endpoint(db, REMOTE_ENDPOINT, ack=0)
    stored = _write_pdf(settings.uploads_dir / "scanned.pdf", ["1", "2"])
    document_id = _seed_document(db, class_id, stored, recognize=True)
    calls = _reader(monkeypatch)

    run_ingestion(document_id)

    assert calls == []
    row = _document(db, document_id)
    # Not a failure and not a page failure: nothing was attempted, so no page is marked
    # failed and the document sits exactly where it would have without the request.
    assert row["state"] == "unsupported"
    assert row["error_message"] == transcribe.REMOTE_MESSAGE
    assert set(_page_states(db, document_id).values()) == {"scanned"}
    assert not list(settings.pages_dir.glob(f"{document_id}/*"))


def test_with_no_endpoint_the_document_says_what_would_fix_it(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = _write_pdf(settings.uploads_dir / "scanned.pdf", ["1"])
    document_id = _seed_document(db, class_id, stored, recognize=True)
    calls = _reader(monkeypatch)

    run_ingestion(document_id)

    assert calls == []
    assert _document(db, document_id)["error_message"] == recognition.NO_ENDPOINT_MESSAGE


def test_the_page_count_advances_while_the_run_is_in_flight(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason per-page rows exist at all.

    `pages_done` used to be written once, at the end, so a two-hour transcription would show
    a number that never moved. A separate connection reads it here, because a value only
    visible inside the writing transaction is not visible to the poller either.
    """
    _configure_endpoint(db)
    stored = _write_pdf(settings.uploads_dir / "scanned.pdf", ["1", "2", "3"])
    document_id = _seed_document(db, class_id, stored, recognize=True)
    seen: list[tuple[str, str | None, int]] = []

    async def watching(
        endpoint: str, api_key: str | None, model: str | None, image: bytes, **kwargs: object
    ) -> str:
        observer = connect()
        try:
            row = observer.execute(
                "select state, stage_detail, pages_done from documents where id = ?",
                (document_id,),
            ).fetchone()
            seen.append((str(row["state"]), row["stage_detail"], int(row["pages_done"])))
        finally:
            observer.close()
        return TRANSCRIPT

    monkeypatch.setattr(transcribe, "transcribe_page", watching)
    run_ingestion(document_id)

    assert [entry[2] for entry in seen] == [0, 1, 2]
    # Recognition is not a fifth step. It renders under Reading, and this detail is what
    # tells the interface the page counter is allowed to appear.
    assert {entry[:2] for entry in seen} == {("parsing", recognition.RECOGNIZING_DETAIL)}


def test_a_page_that_transcribes_to_nothing_is_read_but_not_indexed(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank page is a page with nothing to find, not a page that failed.

    It stays out of the chunker, because an empty chunk answers nothing and dilutes every
    search that touches it, and it stays out of the retry set, because reading it again
    would return the same nothing.
    """
    _configure_endpoint(db)
    stored = _write_pdf(settings.uploads_dir / "scanned.pdf", ["1", "2"])
    document_id = _seed_document(db, class_id, stored, recognize=True)
    _reader(monkeypatch, replies=lambda n: "" if n == 1 else TRANSCRIPT)

    run_ingestion(document_id)

    assert _page_states(db, document_id) == {1: "recognized", 2: "recognized"}
    row = _document(db, document_id)
    assert row["state"] == "ready"
    assert (row["pages_done"], row["pages_skipped"]) == (1, 1)
    assert _chunk_pages(db, document_id) == {2}


def test_an_uploaded_image_is_a_one_page_scan(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PNG and JPG need no parse path of their own.

    PyMuPDF opens one as a one-page document whose page has no text, which is exactly what
    a scanned page is, so it arrives at recognition the same way page 7 of a PDF does.
    """
    _configure_endpoint(db)
    source = _write_pdf(settings.uploads_dir / "seed.pdf", ["only page"])
    image_path = settings.uploads_dir / "photo.png"
    with pymupdf.open(source) as document:
        document[0].get_pixmap(dpi=100).save(image_path, output="png")

    document_id = _seed_document(db, class_id, image_path, mime="image/png", recognize=True)
    calls = _reader(monkeypatch)

    run_ingestion(document_id)

    assert len(calls) == 1
    row = _document(db, document_id)
    assert row["state"] == "ready"
    assert (row["pages_total"], row["pages_done"]) == (1, 1)
    assert _chunk_pages(db, document_id) == {1}
