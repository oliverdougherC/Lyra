"""Regression tests for the ASGI byte guard on the document upload route (PLA-503).

FastAPI spools the multipart body to temp files before the route's copy guard runs, so
these tests drive the real app (`create_app`) against a small synthetic contract
(monkeypatched `MAX_UPLOAD_BYTES`) and assert what the ASGI edge actually does: how many
bytes it pulled from the client, the exact ASGI response sequence it sends, and what is
left on disk and in the database afterward.
"""

import asyncio
import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_documents, upload_body_guard
from backend.config import settings
from backend.core.origins import LOOPBACK_CLIENT_HEADER
from backend.main import create_app
from backend.rag.parse import UNSUPPORTED_MESSAGE
from backend.storage.database import connect, get_db

# The synthetic contract: a 4 KiB accepted-file ceiling, and the whole-request ceiling
# it implies (file contract + the guard's explicit multipart overhead allowance).
MAX = 4096
CAP = upload_body_guard.upload_request_cap(MAX)
# The synthetic client delivers the body in 512-byte messages, like a real transport.
CHUNK = 512

BOUNDARY = "lyra-regression-boundary"


def _part_header(name: str, filename: str | None) -> bytes:
    disposition = f'name="{name}"'
    if filename is not None:
        disposition += f'; filename="{filename}"'
    return (f"--{BOUNDARY}\r\nContent-Disposition: form-data; {disposition}\r\n\r\n").encode()


def _multipart_body(parts: list[tuple[str, str | None, bytes]]) -> bytes:
    body = b"".join(_part_header(name, filename) + data + b"\r\n" for name, filename, data in parts)
    return body + f"--{BOUNDARY}--\r\n".encode()


def _single_file_body(filename: str, content: bytes) -> bytes:
    return _multipart_body([("file", filename, content)])


def _body_padded_to(target: int, filename: str, content: bytes) -> bytes:
    """A body whose total length is exactly `target`, padded with an unexpected field."""
    closing = f"--{BOUNDARY}--\r\n".encode()
    file_part = _part_header("file", filename) + content + b"\r\n"
    pad_header = _part_header("pad", None)
    pad = target - (len(file_part) + len(pad_header) + 2 + len(closing))
    assert pad >= 0, "the file part alone is already past the target"
    return file_part + pad_header + b"p" * pad + b"\r\n" + closing


def _request_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def small_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scale the 50 MiB contract down to 4 KiB so tests move bytes, not gigabytes.

    The guard's ceiling resolves the same constant per request, so one patch scales the
    file contract and the request ceiling together.
    """
    monkeypatch.setattr(routes_documents, "MAX_UPLOAD_BYTES", MAX)


@pytest.fixture(autouse=True)
def spools(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Record every spooled file the pinned parser creates, on real temp files.

    `spool_max_size` is forced small so even tiny parts roll to a real temporary file,
    which is what makes the cleanup assertions (closed, and gone from disk) meaningful.
    """
    import starlette.formparsers as formparsers

    state = SimpleNamespace(files=[], names=[])
    real_spool = formparsers.SpooledTemporaryFile

    class TrackingSpooled(real_spool):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            state.files.append(self)

        def rollover(self) -> None:
            # The rolled backing file is a `TemporaryFile` whose `.name` is a raw fd,
            # so resolve the real temp path through /dev/fd at the moment it appears.
            super().rollover()
            try:
                path = os.readlink(f"/dev/fd/{self._file.fileno()}")
            except OSError:
                return
            if path not in state.names:
                state.names.append(path)

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", TrackingSpooled)
    monkeypatch.setattr(formparsers.MultiPartParser, "spool_max_size", 16)
    return state


@pytest.fixture(autouse=True)
def no_worker(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    queued: list[int] = []
    monkeypatch.setattr(routes_documents, "enqueue", queued.append)
    return queued


@pytest.fixture
def app(db: sqlite3.Connection) -> FastAPI:
    created = create_app()
    created.dependency_overrides[get_db] = _request_db
    return created


@pytest.fixture
def packaged_app(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(settings, "packaged_mode", True)
    created = create_app(session_secret="s" * 64)
    created.dependency_overrides[get_db] = _request_db
    return created


def _drive(
    app: FastAPI,
    *,
    class_id: int,
    body: bytes,
    content_length: int | None,
    path: str | None = None,
    chunk: int = CHUNK,
    disconnect_after: int | None = None,
    host: str = "127.0.0.1:3000",
    origin: str | None = None,
    client_header: bool = True,
) -> SimpleNamespace:
    """Drive the real ASGI app with a synthetic client.

    Returns `.status`, `.body`, `.read` (bytes the app pulled from the client), and
    `.messages` (every ASGI response message). The client delivers the body in
    `chunk`-sized messages; `disconnect_after` ends the client after that many bytes;
    `content_length` is declared metadata only - the client delivers the whole body
    regardless, which is what makes an understated length a fair test.
    """
    path = path or f"/api/classes/{class_id}/documents"
    # Real ASGI servers deliver header names lowercased, so the scope does the same.
    headers: list[tuple[bytes, bytes]] = [(b"host", host.encode("ascii"))]
    if client_header:
        headers.append((LOOPBACK_CLIENT_HEADER.lower().encode("ascii"), b"regression"))
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    headers.append((b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode("ascii")))
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
        "root_path": "",
    }
    state = {"pos": 0, "read": 0, "final": False}
    messages: list[dict] = []

    async def receive() -> dict:
        if disconnect_after is not None and state["read"] >= disconnect_after:
            return {"type": "http.disconnect"}
        if state["pos"] >= len(body):
            if not state["final"]:
                state["final"] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}
        piece = body[state["pos"] : state["pos"] + chunk]
        state["pos"] += len(piece)
        state["read"] += len(piece)
        return {"type": "http.request", "body": piece, "more_body": state["pos"] < len(body)}

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    starts = [m for m in messages if m["type"] == "http.response.start"]
    assert len(starts) == 1, f"expected exactly one response, got {len(starts)}"
    return SimpleNamespace(
        status=starts[0]["status"],
        body=b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"),
        read=state["read"],
        messages=messages,
    )


def _assert_single_json_response(result: SimpleNamespace, *, status: int, detail: str) -> None:
    """The exact wire shape: one start, then bodies ending in exactly one final body.

    The middleware stack re-streams responses, so the final JSON may arrive as a
    chunked body - what is pinned here is that there is one start, no start after the
    bodies begin, and exactly one terminal message carrying the whole JSON body.
    """
    response_messages = [
        m for m in result.messages if m["type"] in ("http.response.start", "http.response.body")
    ]
    starts = [m for m in response_messages if m["type"] == "http.response.start"]
    bodies = [m for m in response_messages if m["type"] == "http.response.body"]
    assert len(starts) == 1, f"expected exactly one response start, got {len(starts)}"
    assert starts[0] is response_messages[0], "a body message arrived before the start"
    assert starts[0]["status"] == status
    assert bodies, "expected a response body"
    for message in bodies[:-1]:
        assert message.get("more_body", False) is True
    assert bodies[-1].get("more_body", False) is False
    body = b"".join(m.get("body", b"") for m in bodies)
    assert json.loads(body) == {"detail": detail}


def _documents(db: sqlite3.Connection, class_id: int) -> list[sqlite3.Row]:
    return db.execute("select * from documents where class_id = ?", (class_id,)).fetchall()


def _uploaded_files() -> list[Path]:
    return [path for path in settings.uploads_dir.rglob("*") if path.is_file()]


def _assert_spools_closed(spools: SimpleNamespace, *, created: bool = False) -> None:
    if created:
        assert spools.files, "expected the parser to spool at least one part"
    for spooled in spools.files:
        assert spooled.closed
    for name in spools.names:
        assert not os.path.exists(name), f"spooled temp file survived: {name}"


def test_a_body_at_the_exact_ceiling_is_accepted(
    app: FastAPI, class_id: int, db: sqlite3.Connection, spools: SimpleNamespace, no_worker: list
) -> None:
    file_bytes = b"a" * MAX
    body = _body_padded_to(CAP, "notes.txt", file_bytes)
    assert len(body) == CAP

    result = _drive(app, class_id=class_id, body=body, content_length=len(body))

    assert result.status == 202
    document = json.loads(result.body)
    assert document["filename"] == "notes.txt"
    assert document["byte_size"] == MAX
    assert document["state"] == "pending"
    assert result.read == CAP, "a request at exactly the ceiling must be fully accepted"
    assert no_worker == [document["id"]]
    rows = _documents(db, class_id)
    assert len(rows) == 1
    stored = Path(str(rows[0]["stored_path"]))
    assert stored.exists() and stored.stat().st_size == MAX
    _assert_spools_closed(spools, created=True)


def test_one_byte_past_the_ceiling_is_refused_413_without_committing(
    app: FastAPI, class_id: int, db: sqlite3.Connection, spools: SimpleNamespace
) -> None:
    # The declared length understates the body, so the up-front check cannot save this:
    # the counted receive has to catch it at the ceiling.
    body = _body_padded_to(CAP + 1, "notes.txt", b"a" * MAX)
    assert len(body) == CAP + 1

    result = _drive(app, class_id=class_id, body=body, content_length=len(body) // 2)

    assert result.status == 413
    _assert_single_json_response(result, status=413, detail=routes_documents.TOO_LARGE_MESSAGE)
    assert result.read == CAP + 1, "the app must stop at the ceiling, not after the whole body"
    assert result.read <= CAP + CHUNK
    assert _documents(db, class_id) == []
    assert _uploaded_files() == []
    _assert_spools_closed(spools, created=True)


def test_a_missing_content_length_is_still_bounded(
    app: FastAPI, class_id: int, db: sqlite3.Connection, spools: SimpleNamespace
) -> None:
    body = _body_padded_to(CAP + 1024, "notes.txt", b"a" * MAX)

    result = _drive(app, class_id=class_id, body=body, content_length=None)

    assert result.status == 413
    _assert_single_json_response(result, status=413, detail=routes_documents.TOO_LARGE_MESSAGE)
    assert result.read <= CAP + CHUNK
    assert result.read < len(body)
    assert _documents(db, class_id) == []
    assert _uploaded_files() == []
    _assert_spools_closed(spools, created=True)


def test_a_declared_length_past_the_ceiling_is_refused_before_any_byte_is_read(
    app: FastAPI, class_id: int, db: sqlite3.Connection
) -> None:
    body = _single_file_body("tiny.txt", b"small")

    result = _drive(app, class_id=class_id, body=body, content_length=CAP + 1)

    assert result.status == 413
    _assert_single_json_response(result, status=413, detail=routes_documents.TOO_LARGE_MESSAGE)
    assert result.read == 0, "an honest over-ceiling Content-Length must not cost a single read"
    assert _documents(db, class_id) == []
    assert _uploaded_files() == []


def test_an_unexpected_extra_part_counts_toward_the_ceiling(
    app: FastAPI, class_id: int, db: sqlite3.Connection, spools: SimpleNamespace
) -> None:
    # The file itself is under its contract; the second part is what crosses the
    # ceiling, so a guard that bounded only the `file` part would have accepted this.
    file_bytes = b"b" * (MAX - 2000)
    base = _multipart_body([("file", "notes.txt", file_bytes), ("attachment", "extra.bin", b"")])
    body = _multipart_body(
        [
            ("file", "notes.txt", file_bytes),
            ("attachment", "extra.bin", b"e" * (CAP + 1 - len(base))),
        ]
    )
    assert len(body) == CAP + 1

    result = _drive(app, class_id=class_id, body=body, content_length=None)

    assert result.status == 413
    _assert_single_json_response(result, status=413, detail=routes_documents.TOO_LARGE_MESSAGE)
    # The crossing byte is the last byte of the body: the app read exactly to the
    # ceiling plus that byte, never a lenient whole-body read.
    assert result.read == CAP + 1
    assert _documents(db, class_id) == []
    assert _uploaded_files() == []
    _assert_spools_closed(spools, created=True)


def test_a_small_extra_part_still_allows_the_upload(
    app: FastAPI, class_id: int, db: sqlite3.Connection, spools: SimpleNamespace, no_worker: list
) -> None:
    file_bytes = b"b" * (MAX - 2000)
    body = _multipart_body(
        [("file", "notes.txt", file_bytes), ("attachment", "extra.bin", b"e" * 500)]
    )
    assert len(body) <= CAP

    result = _drive(app, class_id=class_id, body=body, content_length=len(body))

    assert result.status == 202
    document = json.loads(result.body)
    assert document["byte_size"] == MAX - 2000
    assert len(_documents(db, class_id)) == 1
    assert no_worker == [document["id"]]
    _assert_spools_closed(spools, created=True)


def test_a_file_one_byte_past_the_contract_is_refused_413_by_the_route(
    app: FastAPI, class_id: int, db: sqlite3.Connection, spools: SimpleNamespace
) -> None:
    # The whole request is under the ceiling, so the guard lets it through and the
    # route's file-level contract rejects the part: the 50 MiB accepted-file rule
    # (scaled) still has an owner at the top of the ceiling.
    body = _single_file_body("big.txt", b"x" * (MAX + 1))
    assert len(body) < CAP

    result = _drive(app, class_id=class_id, body=body, content_length=len(body))

    assert result.status == 413
    _assert_single_json_response(result, status=413, detail=routes_documents.TOO_LARGE_MESSAGE)
    assert result.read == len(body)
    assert _documents(db, class_id) == []
    assert _uploaded_files() == []
    _assert_spools_closed(spools, created=True)


def test_an_unsupported_extension_is_still_refused_before_staging(
    app: FastAPI, class_id: int, db: sqlite3.Connection, spools: SimpleNamespace
) -> None:
    body = _single_file_body("virus.exe", b"MZ")

    result = _drive(app, class_id=class_id, body=body, content_length=len(body))

    assert result.status == 400
    _assert_single_json_response(result, status=400, detail=UNSUPPORTED_MESSAGE)
    assert _documents(db, class_id) == []
    assert _uploaded_files() == []
    _assert_spools_closed(spools, created=True)


def test_a_disconnect_mid_body_cleans_up_the_spools(
    app: FastAPI, class_id: int, db: sqlite3.Connection, spools: SimpleNamespace
) -> None:
    # The client goes away mid-file, before the route could ever run. The observable
    # answer to the (already dead) client is a 400; what this test pins is that the
    # spooled bytes are actually reclaimed on the way out.
    body = _single_file_body("notes.txt", b"a" * (MAX * 4))

    result = _drive(
        app,
        class_id=class_id,
        body=body,
        content_length=None,
        disconnect_after=8192,
    )

    assert result.status == 400
    assert result.read < len(body)
    assert _documents(db, class_id) == []
    assert _uploaded_files() == []
    _assert_spools_closed(spools, created=True)


def test_a_normal_upload_still_succeeds_after_a_refusal(
    app: FastAPI, class_id: int, db: sqlite3.Connection, spools: SimpleNamespace, no_worker: list
) -> None:
    bad = _body_padded_to(CAP + 1024, "big.pdf", b"p" * MAX)
    result = _drive(app, class_id=class_id, body=bad, content_length=None)
    assert result.status == 413
    assert result.read <= CAP + CHUNK

    good = _single_file_body("tiny.txt", b"hello\n")
    result = _drive(app, class_id=class_id, body=good, content_length=len(good))

    assert result.status == 202
    document = json.loads(result.body)
    assert document["byte_size"] == 6
    assert len(_documents(db, class_id)) == 1
    assert no_worker == [document["id"]]
    _assert_spools_closed(spools, created=True)


def test_a_malicious_host_is_refused_before_the_guard_reads_anything(
    app: FastAPI, class_id: int, db: sqlite3.Connection
) -> None:
    body = _body_padded_to(CAP + 1024, "notes.txt", b"a" * MAX)

    result = _drive(
        app, class_id=class_id, body=body, content_length=len(body), host="evil.example"
    )

    assert result.status == 400
    assert result.read == 0, "the Host guard must refuse before the byte guard touches the body"
    assert _documents(db, class_id) == []


def test_a_hostile_origin_is_refused_before_the_guard_reads_anything(
    app: FastAPI, class_id: int, db: sqlite3.Connection
) -> None:
    body = _body_padded_to(CAP + 1024, "notes.txt", b"a" * MAX)

    result = _drive(
        app,
        class_id=class_id,
        body=body,
        content_length=len(body),
        origin="http://evil.example",
        client_header=False,
    )

    assert result.status == 403
    assert result.read == 0
    assert _documents(db, class_id) == []


def test_a_missing_session_header_is_refused_before_the_guard_reads_anything(
    packaged_app: FastAPI, class_id: int, db: sqlite3.Connection
) -> None:
    body = _body_padded_to(CAP + 1024, "notes.txt", b"a" * MAX)

    result = _drive(packaged_app, class_id=class_id, body=body, content_length=len(body))

    assert result.status == 403
    assert result.read == 0
    assert _documents(db, class_id) == []


def test_a_trailing_slash_is_redirected_without_reading_the_body(
    app: FastAPI, class_id: int, db: sqlite3.Connection
) -> None:
    # The guard's path regex deliberately does not match the trailing-slash variant:
    # the router redirects it, and a redirect must not consume the body.
    body = _body_padded_to(CAP + 1024, "notes.txt", b"a" * MAX)
    path = f"/api/classes/{class_id}/documents/"

    result = _drive(app, class_id=class_id, body=body, content_length=None, path=path)

    assert 300 <= result.status < 400, f"expected a redirect, got {result.status}"
    assert result.read == 0, "a redirect must not read or spool the body"
    assert _documents(db, class_id) == []


def test_a_413_keeps_cors_headers_for_the_trusted_browser(
    app: FastAPI, class_id: int, db: sqlite3.Connection
) -> None:
    # The guard sits inside CORS, so its 413 is the response CORS decorates: the
    # student's own page can read the message that tells it to choose a smaller file.
    body = _body_padded_to(CAP + 1024, "notes.txt", b"a" * MAX)

    result = _drive(
        app,
        class_id=class_id,
        body=body,
        content_length=None,
        origin="http://127.0.0.1:3000",
        client_header=False,
    )

    assert result.status == 413
    starts = [m for m in result.messages if m["type"] == "http.response.start"]
    assert len(starts) == 1
    response_headers = {
        k.decode("latin-1").lower(): v.decode("latin-1") for k, v in starts[0].get("headers", [])
    }
    assert response_headers.get("access-control-allow-origin") == "http://127.0.0.1:3000"
    _assert_single_json_response(result, status=413, detail=routes_documents.TOO_LARGE_MESSAGE)


def test_a_real_client_upload_round_trip_and_file_level_refusal(
    app: FastAPI, class_id: int, db: sqlite3.Connection, spools: SimpleNamespace, no_worker: list
) -> None:
    client = TestClient(app)
    headers = {"host": "127.0.0.1:3000", "x-lyra-client": "test"}
    url = f"/api/classes/{class_id}/documents"

    response = client.post(
        url, headers=headers, files={"file": ("tiny.txt", b"hello\n", "text/plain")}
    )
    assert response.status_code == 202
    assert response.json()["byte_size"] == 6
    assert no_worker == [response.json()["id"]]

    oversize = client.post(
        url, headers=headers, files={"file": ("big.txt", b"x" * (MAX + 1), "text/plain")}
    )
    assert oversize.status_code == 413
    assert oversize.json()["detail"] == routes_documents.TOO_LARGE_MESSAGE
    assert (
        db.execute("select count(*) from documents where filename = 'big.txt'").fetchone()[0] == 0
    )
    # The accepted upload is the only file on disk: the refused one staged nothing.
    assert [p.name for p in _uploaded_files()] == [f"{response.json()['id']}-tiny.txt"]
    _assert_spools_closed(spools)
