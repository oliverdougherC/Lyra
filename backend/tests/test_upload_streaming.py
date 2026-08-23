"""The upload size limit is enforced while the body streams, not after it is buffered.

Two layers are under test. `backend.storage.private.publish_private_stream` is the streaming
publication primitive: it copies a source to a staged file one chunk at a time, aborts the
moment the running total crosses its ceiling, and leaves nothing behind on any exit that
does not publish. `routes_documents.upload_document` is the route that uses it, where a
rejected upload must roll its half-built row back as well as its staged file.

The old shape - `payload = file.file.read()` then `len(payload) > MAX_UPLOAD_BYTES` -
materialized the whole body in memory before it could be refused, so an oversized upload
spiked memory before Lyra reached the code that rejected it. The regression test at the
bottom makes returning to that shape fail.
"""

import inspect
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_documents
from backend.config import settings
from backend.core.errors import LyraError
from backend.storage import private
from backend.storage.database import connect, get_db


def _request_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def no_worker(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record what would have been queued instead of running ingestion."""
    queued: list[int] = []
    monkeypatch.setattr(routes_documents, "enqueue", queued.append)
    return queued


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(routes_documents.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def small_limit(monkeypatch: pytest.MonkeyPatch) -> int:
    """Shrink the ceiling so limit behavior is exercised without megabyte fixtures.

    The chunk size is shrunk with it, so the streaming loop takes several reads to cross
    the limit - the same path a real 50 MB ceiling takes, at a size a test can assert on.
    """
    monkeypatch.setattr(routes_documents, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(routes_documents, "UPLOAD_CHUNK_BYTES", 4)
    return 10


def _partial_files(root: Path) -> list[Path]:
    """Every staging leftover under the data tree - there should never be one at rest."""
    return list(root.rglob(f"*{private.PARTIAL_SUFFIX}"))


def _document_count(db: sqlite3.Connection) -> int:
    return int(db.execute("select count(*) from documents").fetchone()[0])


# --- The streaming primitive ----------------------------------------------------------


class _EndlessSource:
    """A source that never runs out, and remembers how many bytes it was asked for.

    Stands in for a hostile upload: if `publish_private_stream` ever read the whole body
    before checking the limit, this would hand it an unbounded stream and `served` would
    grow without bound. It does not, so `served` stops just past the ceiling.
    """

    def __init__(self) -> None:
        self.served = 0

    def read(self, size: int) -> bytes:
        self.served += size
        return b"x" * size


class _ErroringSource:
    """Serves one good chunk, then fails - a disconnect or disk fault mid-stream."""

    def __init__(self) -> None:
        self.reads = 0

    def read(self, size: int) -> bytes:
        self.reads += 1
        if self.reads == 1:
            return b"abcd"
        raise OSError("stream broke")


def test_stream_publishes_a_complete_upload_and_reports_its_size(tmp_path: Path) -> None:
    final = tmp_path / "stored.bin"
    source = _FakeReader(b"hello world")

    written = private.publish_private_stream(final, source, max_bytes=1024, chunk_size=4)

    assert written == len(b"hello world")
    assert final.read_bytes() == b"hello world"
    assert _partial_files(tmp_path) == []


def test_stream_at_the_exact_limit_is_accepted(tmp_path: Path) -> None:
    final = tmp_path / "stored.bin"
    source = _FakeReader(b"0123456789")

    written = private.publish_private_stream(final, source, max_bytes=10, chunk_size=4)

    assert written == 10
    assert final.read_bytes() == b"0123456789"


def test_stream_one_byte_over_the_limit_is_rejected_and_leaves_nothing(tmp_path: Path) -> None:
    final = tmp_path / "stored.bin"
    source = _FakeReader(b"0123456789X")

    with pytest.raises(private.StreamTooLargeError):
        private.publish_private_stream(final, source, max_bytes=10, chunk_size=4)

    assert not final.exists()
    assert _partial_files(tmp_path) == []


def test_stream_stops_reading_once_the_limit_is_crossed(tmp_path: Path) -> None:
    """An oversized stream costs at most one chunk past the ceiling, never the whole body."""
    final = tmp_path / "stored.bin"
    source = _EndlessSource()

    with pytest.raises(private.StreamTooLargeError):
        private.publish_private_stream(final, source, max_bytes=1000, chunk_size=256)

    # Read stopped as soon as the total crossed 1000; it never pulled anything close to an
    # unbounded body into memory.
    assert source.served <= 1000 + 256
    assert not final.exists()
    assert _partial_files(tmp_path) == []


def test_stream_cleans_up_when_the_source_errors_midway(tmp_path: Path) -> None:
    final = tmp_path / "stored.bin"
    source = _ErroringSource()

    with pytest.raises(OSError, match="stream broke"):
        private.publish_private_stream(final, source, max_bytes=1024, chunk_size=4)

    assert not final.exists()
    assert _partial_files(tmp_path) == []


def test_stream_of_empty_input_publishes_a_zero_byte_file(tmp_path: Path) -> None:
    final = tmp_path / "stored.bin"
    source = _FakeReader(b"")

    written = private.publish_private_stream(final, source, max_bytes=10, chunk_size=4)

    assert written == 0
    assert final.read_bytes() == b""


class _FakeReader:
    """A blocking `read(size)` over a fixed buffer, the shape an upload's spooled file has."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def read(self, size: int) -> bytes:
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


# --- The upload route -----------------------------------------------------------------


def test_upload_at_the_exact_limit_is_stored(
    client: TestClient, db: sqlite3.Connection, class_id: int, small_limit: int, no_worker: list
) -> None:
    response = client.post(
        f"/api/classes/{class_id}/documents",
        files={"file": ("notes.txt", b"0123456789", "text/plain")},
    )

    assert response.status_code == 202
    assert response.json()["byte_size"] == small_limit
    assert no_worker == [response.json()["id"]]
    assert _partial_files(settings.data_dir) == []


def test_upload_one_byte_over_the_limit_is_refused_and_stores_nothing(
    client: TestClient, db: sqlite3.Connection, class_id: int, small_limit: int, no_worker: list
) -> None:
    response = client.post(
        f"/api/classes/{class_id}/documents",
        files={"file": ("notes.txt", b"0123456789X", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == routes_documents.TOO_LARGE_MESSAGE
    # The half-built row was rolled back, the staged file discarded, and nothing queued.
    assert _document_count(db) == 0
    assert no_worker == []
    assert list((settings.uploads_dir / str(class_id)).glob("*")) == []
    assert _partial_files(settings.data_dir) == []


def test_upload_substantially_over_the_limit_is_refused_cleanly(
    client: TestClient, db: sqlite3.Connection, class_id: int, small_limit: int, no_worker: list
) -> None:
    response = client.post(
        f"/api/classes/{class_id}/documents",
        files={"file": ("big.txt", b"x" * 10_000, "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == routes_documents.TOO_LARGE_MESSAGE
    assert _document_count(db) == 0
    assert no_worker == []
    assert _partial_files(settings.data_dir) == []


def test_upload_of_an_empty_file_is_accepted(
    client: TestClient, db: sqlite3.Connection, class_id: int, small_limit: int, no_worker: list
) -> None:
    """A zero-byte upload is stored and queued; the pipeline decides its fate downstream."""
    response = client.post(
        f"/api/classes/{class_id}/documents",
        files={"file": ("empty.txt", b"", "text/plain")},
    )

    assert response.status_code == 202
    assert response.json()["byte_size"] == 0
    assert no_worker == [response.json()["id"]]


def test_a_store_failure_rolls_the_row_back(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    no_worker: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disk error mid-store surfaces as a retryable failure and leaves no document."""

    def boom(*args: object, **kwargs: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(private, "publish_private_stream", boom)
    response = client.post(
        f"/api/classes/{class_id}/documents",
        files={"file": ("notes.txt", b"content", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == routes_documents.UPLOAD_FAILED_MESSAGE
    assert _document_count(db) == 0
    assert no_worker == []


def test_the_upload_route_never_reads_the_whole_body_into_memory() -> None:
    """A guard against regressing to one unbounded `read()` of the request body.

    The old code buffered the entire upload (`file.file.read()`) and only then measured it.
    The size limit must stay a streaming check, so the route must publish through the
    bounded streaming primitive and must not call an argument-less `read()` on the upload.
    """
    source = inspect.getsource(routes_documents.upload_document)
    assert "publish_private_stream" in source
    assert "file.file.read()" not in source
    assert ".read()" not in source
