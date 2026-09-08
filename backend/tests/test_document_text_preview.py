"""Contract tests for the source pane's bounded extraction preview (PLA-506).

The production route is exercised through a `TestClient` over the documents router on
the shared isolated data directory, with extractions published where ingestion puts
them. The boundary and instrumentation tests monkeypatch `MAX_TEXT_CHARS` down so the
exact cut is observed cheaply; the ceiling's own value is exercised separately at its
real size, and the instrumentation tests prove the route asks the stream for a bounded
prefix rather than running the extraction to EOF.
"""

import io
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_documents
from backend.config import settings
from backend.core import storage_intents
from backend.core.errors import LyraError
from backend.storage import private
from backend.storage.database import connect, get_db


def _request_db() -> Iterator[sqlite3.Connection]:
    """A connection to the temporary database, opened inside the calling thread."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


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


def _document(db: sqlite3.Connection, class_id: int, filename: str = "week-3-notes.md") -> int:
    """A ready text document with a real original stored under its class directory."""
    cursor = db.execute(
        "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
        "values (?, ?, '', 'text/markdown', 4, 'ready')",
        (class_id, filename),
    )
    document_id = int(cursor.lastrowid or 0)
    stored = settings.uploads_dir / str(class_id) / f"{document_id}-{filename}"
    stored.parent.mkdir(parents=True, exist_ok=True)
    private.write_private_bytes(stored, b"# the stored original bytes")
    db.execute("update documents set stored_path = ? where id = ?", (str(stored), document_id))
    db.commit()
    return document_id


def _write_extraction(document_id: int, content: str | bytes) -> Path:
    """Publish an extraction under the document's text path the way ingestion does."""
    path = storage_intents.text_path(document_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        private.publish_private_bytes(path, content)
    else:
        private.publish_private_text(path, content)
    return path


def _get_text(client: TestClient, document_id: int) -> dict[str, object]:
    response = client.get(f"/api/documents/{document_id}/text")
    assert response.status_code == 200
    result: dict[str, object] = response.json()
    return result


class _CountingBuffer(io.BufferedReader):
    """A buffered reader that counts the bytes its chunked reads pull through."""

    def __init__(self, fileno: int) -> None:
        # The buffered reader takes ownership of the raw file and closes it with itself.
        super().__init__(open(fileno, "rb", buffering=0))  # noqa: SIM115
        self.bytes_read = 0

    def read1(self, size: int = -1) -> bytes:
        data = super().read1(size)
        self.bytes_read += len(data)
        return data


class _RecordedStream(io.TextIOWrapper):
    """A text stream that records the character caps each read asked it to decode."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.requested_sizes: list[int] = []

    def read(self, size: int | None = -1) -> str:  # type: ignore[override]
        self.requested_sizes.append(-1 if size is None else size)
        return super().read(-1 if size is None else size)


@pytest.fixture
def stream_spy(monkeypatch: pytest.MonkeyPatch) -> list[_RecordedStream]:
    """Wrap every text-mode `os.fdopen` so a test can see what a preview read.

    The wrapper records each requested read size and the bytes the decoder buffer
    pulled, and it is the same object the route closes, so it also proves the stream
    is closed whether the read succeeded or failed.
    """
    streams: list[_RecordedStream] = []
    real_fdopen = os.fdopen

    def spy(fd: int, mode: str = "r", *args: object, **kwargs: object) -> object:
        if mode != "r":
            return real_fdopen(fd, mode, *args, **kwargs)
        buffer = _CountingBuffer(fd)
        stream = _RecordedStream(buffer, **kwargs)
        stream.counting_buffer = buffer
        streams.append(stream)
        return stream

    monkeypatch.setattr(os, "fdopen", spy)
    return streams


def _open_descriptor_count() -> int:
    """How many file descriptors this process holds open right now."""
    return len(os.listdir("/dev/fd"))


# --- the preview's content contract --------------------------------------------------


def test_a_document_without_an_extraction_still_gets_an_empty_preview(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """Missing extraction is the documented empty-preview answer, not a failure."""
    document_id = _document(db, class_id)
    assert _get_text(client, document_id) == {
        "filename": "week-3-notes.md",
        "text": "",
        "truncated": False,
    }


def test_an_empty_extraction_is_an_empty_preview(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    _write_extraction(document_id, "")
    assert _get_text(client, document_id) == {
        "filename": "week-3-notes.md",
        "text": "",
        "truncated": False,
    }


def test_a_short_extraction_is_returned_whole_with_its_filename(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id, filename="cours — semaine.md")
    text = "Substitution takes a few lines.\n\n$$u = x^2$$"
    _write_extraction(document_id, text)
    body = _get_text(client, document_id)
    assert body == {"filename": "cours — semaine.md", "text": text, "truncated": False}


# --- the ceiling, at its real size ----------------------------------------------------


def test_the_real_ceiling_of_exactly_its_own_length_is_not_truncated(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """Exactly `MAX_TEXT_CHARS` characters come back whole and say not truncated."""
    document_id = _document(db, class_id)
    limit = routes_documents.MAX_TEXT_CHARS
    text = "".join(chr(ord("a") + index % 26) for index in range(limit))
    _write_extraction(document_id, text)
    body = _get_text(client, document_id)
    assert body["text"] == text
    assert body["truncated"] is False


def test_one_character_past_the_real_ceiling_is_cut_to_it(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    limit = routes_documents.MAX_TEXT_CHARS
    text = "".join(chr(ord("a") + index % 26) for index in range(limit)) + "z"
    _write_extraction(document_id, text)
    body = _get_text(client, document_id)
    assert body["text"] == text[:limit]
    assert body["truncated"] is True


# --- the boundary, made small enough to inspect ----------------------------------------


def test_exactly_the_limit_is_whole_but_one_more_character_reports_truncation(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extra character is what sets `truncated`; it is never served to the pane."""
    monkeypatch.setattr(routes_documents, "MAX_TEXT_CHARS", 10)
    whole = _document(db, class_id)
    exact = _write_extraction(whole, "0123456789")
    body = _get_text(client, whole)
    assert body["text"] == exact.read_text(encoding="utf-8")
    assert body["truncated"] is False

    cut = _document(db, class_id)
    _write_extraction(cut, "0123456789a")
    body = _get_text(client, cut)
    assert body["text"] == "0123456789"
    assert body["truncated"] is True


def test_a_long_extraction_is_cut_to_its_ceiling(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes_documents, "MAX_TEXT_CHARS", 10)
    document_id = _document(db, class_id)
    _write_extraction(document_id, "x" * 1000)
    body = _get_text(client, document_id)
    assert body["text"] == "x" * 10
    assert body["truncated"] is True


def test_multibyte_text_is_counted_in_characters_across_the_cut(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cut lands on whole characters, not on a partial UTF-8 sequence."""
    monkeypatch.setattr(routes_documents, "MAX_TEXT_CHARS", 10)
    document_id = _document(db, class_id, filename="éclipse — 週.md")
    _write_extraction(document_id, "ab" + "日" * 30)
    body = _get_text(client, document_id)
    assert body["text"] == "ab" + "日" * 8
    assert body["truncated"] is True


def test_multibyte_text_of_exactly_the_limit_is_returned_whole(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes_documents, "MAX_TEXT_CHARS", 10)
    document_id = _document(db, class_id)
    _write_extraction(document_id, "日" * 10)
    body = _get_text(client, document_id)
    assert body["text"] == "日" * 10
    assert body["truncated"] is False


# --- the read is bounded, and proved so -----------------------------------------------


def test_the_preview_asks_the_stream_for_its_ceiling_and_never_the_whole_extraction(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    stream_spy: list[_RecordedStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bounded read of limit+1 characters, a fraction of the file's bytes, closed."""
    monkeypatch.setattr(routes_documents, "MAX_TEXT_CHARS", 10)
    document_id = _document(db, class_id)
    path = _write_extraction(document_id, "x" * 100_000)
    body = _get_text(client, document_id)
    assert body["text"] == "x" * 10
    assert body["truncated"] is True

    assert len(stream_spy) == 1
    stream = stream_spy[0]
    # Exactly one read, capped at the ceiling plus the one observing character.
    assert stream.requested_sizes == [11]
    assert 0 < stream.counting_buffer.bytes_read < path.stat().st_size
    assert stream.closed


def test_the_bound_is_characters_even_when_the_extraction_uses_more_bytes(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    stream_spy: list[_RecordedStream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multibyte extraction is still bounded by characters, not by its byte size."""
    monkeypatch.setattr(routes_documents, "MAX_TEXT_CHARS", 10)
    document_id = _document(db, class_id)
    path = _write_extraction(document_id, "日" * 30_000)
    body = _get_text(client, document_id)
    assert body["text"] == "日" * 10
    assert body["truncated"] is True

    stream = stream_spy[0]
    assert stream.requested_sizes == [11]
    # A bounded decoder buffer may overshoot the character cap; running to EOF may not.
    assert 0 < stream.counting_buffer.bytes_read < path.stat().st_size
    assert stream.closed


def test_the_read_stream_is_closed_when_the_extraction_cannot_be_decoded(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    stream_spy: list[_RecordedStream],
) -> None:
    """A corrupt extraction is a failure, not a blank pane - and it closes its stream."""
    document_id = _document(db, class_id)
    _write_extraction(document_id, b"\xff\xfe not utf-8 \x80")
    with pytest.raises(UnicodeDecodeError):
        client.get(f"/api/documents/{document_id}/text")
    assert len(stream_spy) == 1
    assert stream_spy[0].closed


# --- unsafe and inaccessible sources stay failures -------------------------------------


def test_a_symlinked_extraction_is_refused_rather_than_read_through(
    client: TestClient, db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    document_id = _document(db, class_id)
    outside = tmp_path / "outside.txt"
    outside.write_text("someone else's file", encoding="utf-8")
    path = storage_intents.text_path(document_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside)
    with pytest.raises(private.PrivacyContractError):
        client.get(f"/api/documents/{document_id}/text")
    assert outside.read_text(encoding="utf-8") == "someone else's file"


def test_a_directory_where_the_extraction_belongs_is_refused_without_leaking(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    # Anyio starts the request portal on the first call and holds its event-loop
    # descriptors for the client's life; warm that up on a different document so the
    # measurement covers the route's own window and not the client's boot-up.
    warmup = _document(db, class_id)
    client.get(f"/api/documents/{warmup}/text")
    storage_intents.text_path(document_id).mkdir(parents=True, exist_ok=True)
    before = _open_descriptor_count()
    with pytest.raises(private.PrivacyContractError):
        client.get(f"/api/documents/{document_id}/text")
    assert _open_descriptor_count() == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any mode")
def test_an_unreadable_extraction_is_an_error_not_an_empty_preview(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    path = _write_extraction(document_id, "private extraction")
    path.chmod(0o000)
    try:
        with pytest.raises(private.PrivacyContractError):
            client.get(f"/api/documents/{document_id}/text")
    finally:
        path.chmod(0o600)


def test_the_preview_leaves_the_extraction_and_the_original_untouched(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    extraction = _write_extraction(document_id, "é" * (routes_documents.MAX_TEXT_CHARS + 2))
    stored = Path(
        str(
            db.execute("select stored_path from documents where id = ?", (document_id,)).fetchone()[
                0
            ]
        )
    )
    extraction_bytes = extraction.read_bytes()
    stored_bytes = stored.read_bytes()
    extraction_mtime = extraction.stat().st_mtime_ns
    stored_mtime = stored.stat().st_mtime_ns

    body = _get_text(client, document_id)
    assert body["truncated"] is True

    assert extraction.read_bytes() == extraction_bytes
    assert extraction.stat().st_mtime_ns == extraction_mtime
    assert stored.read_bytes() == stored_bytes
    assert stored.stat().st_mtime_ns == stored_mtime


# --- the shared reader's own bounded contract ------------------------------------------


def test_read_private_text_without_a_bound_still_reads_the_whole_file(
    tmp_path: Path,
) -> None:
    """The existing callers - journal, key files - see exactly the old behavior."""
    path = tmp_path / "state.json"
    private.write_private_text(path, '{"a": 1234567890}')
    assert private.read_private_text(path) == '{"a": 1234567890}'


def test_read_private_text_with_a_bound_returns_only_a_prefix(tmp_path: Path) -> None:
    path = tmp_path / "long.txt"
    private.write_private_text(path, "abcdefg")
    assert private.read_private_text(path, max_chars=3) == "abc"
    assert private.read_private_text(path, max_chars=100) == "abcdefg"


def test_read_private_text_refuses_a_negative_bound_before_opening(
    tmp_path: Path,
) -> None:
    path = tmp_path / "long.txt"
    private.write_private_text(path, "abcdefg")
    with pytest.raises(ValueError, match="max_chars"):
        private.read_private_text(path, max_chars=-1)
