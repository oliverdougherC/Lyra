"""Incoming byte guard for the document upload route.

FastAPI resolves the `UploadFile` dependency before the route handler runs, and the
Starlette multipart parser spools every file part to a temporary file while it does so.
The handler-side copy guard (routes_documents) only bounds the spool-to-disk copy, so an
oversized body, or a body with extra parts, lands in the system temp directory in full
before anything refuses it. This middleware closes that gap at the ASGI edge, on the
upload route only: a declared `Content-Length` beyond the ceiling is refused up front
(before a body byte is read), and every byte that actually arrives is counted against
the ceiling, so a missing or understated length cannot smuggle more in. The ceiling
covers the whole request - file part, multipart framing, and any unexpected extra
parts - and is the accepted-file contract (routes_documents.MAX_UPLOAD_BYTES) plus an
explicit, bounded framing allowance.

Two signals cross the boundary, both deliberately `OSError` subclasses. The pinned
Starlette `MultiPartParser` closes its spooled temp files only when parsing fails with a
`MultiPartException` or an `OSError` (starlette/formparsers.py); a plain exception would
leave the spools open until garbage collection. Raised from the receive channel, each
signal travels through that cleanup path first. The overflow is then answered with a
413 - and because the pinned FastAPI converts any exception out of `request.form()`
into a 400, the guard detects the overflow through sticky state on the receive wrapper
and replaces the inner 400 it would have sent with the 413, so the overflow is never
masked as a parse error.
"""

import json
import logging
import re
from collections.abc import Callable

from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.api.routes_documents import TOO_LARGE_MESSAGE

logger = logging.getLogger("lyra")

# The single endpoint that ingests a multipart file body. The trailing-slash variant is
# not matched: the router redirects it without reading the body, and the redirect
# target hits this guard on the way in.
_UPLOAD_PATH = re.compile(r"^/api/classes/[^/]+/documents$")

# How far the whole request may exceed the accepted-file ceiling without being refused:
# the multipart framing (two boundary lines, the part headers - filename included - and
# CRLFs). A well-behaved browser spends a few hundred bytes; this is a generous but
# explicit ceiling for that framing, not a percentage of the file. Every byte of the
# request counts against the ceiling, including unexpected extra parts.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


def upload_request_cap(max_file_bytes: int) -> int:
    """The whole-request ceiling for an upload: the file contract plus framing allowance."""
    return max_file_bytes + MULTIPART_OVERHEAD_BYTES


class UploadBodyTooLargeError(OSError):
    """The request body crossed the upload ceiling while it was still arriving.

    An `OSError` so the pinned Starlette parser runs its spool cleanup before re-raising
    (see module docstring). The guard owns this type end to end: it is raised in the
    receive channel, remembered as sticky state, and answered with a 413.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(f"request body exceeded the {limit}-byte ceiling")
        self.limit = limit


class UploadStreamAbortedError(OSError):
    """The client went away mid-upload, before the body completed.

    A bare disconnect (Starlette's `ClientDisconnect`) is not in the parser's cleanup
    path, so the spools would linger; this signal routes the abort through it.
    """


class _CountingReceive:
    """Wraps the receive channel, counting body bytes against the ceiling."""

    __slots__ = ("_receive", "_limit", "_received", "_finished", "overflowed")

    def __init__(self, receive: Receive, limit: int) -> None:
        self._receive = receive
        self._limit = limit
        self._received = 0
        self._finished = False
        # Sticky, not transient: the overflow surfaces past the guard as FastAPI's
        # 400, not as this exception, and the guard must still recognize it then.
        self.overflowed = False

    async def receive(self) -> Message:
        message = await self._receive()
        mtype = message["type"]
        if mtype == "http.request":
            body = message.get("body")
            if body:
                self._received += len(body)
                if self._received > self._limit:
                    self.overflowed = True
                    raise UploadBodyTooLargeError(self._limit)
            if not message.get("more_body", False):
                self._finished = True
        elif mtype == "http.disconnect" and not self._finished:
            # The body never completed: the parser is mid-stream and would leave its
            # spools open on an uncaught disconnect. Signal the abort through cleanup.
            raise UploadStreamAbortedError()
        return message


def _declared_length(headers: list[tuple[bytes, bytes]]) -> int | None:
    """The declared body length, or None when absent or unusable.

    The header is not the boundary - a chunked request carries none, and a lying client
    can understate it - it only enables the cheap up-front refusal.
    """
    for name, value in headers:
        if name.lower() == b"content-length":
            try:
                length = int(value.decode("ascii").strip())
            except ValueError:
                return None
            return length if length >= 0 else None
    return None


async def _send_too_large(send: Send) -> None:
    body = json.dumps({"detail": TOO_LARGE_MESSAGE}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode("ascii")],
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class UploadBodyGuard:
    """ASGI middleware: bound the incoming bytes on the document upload route.

    Register it *first*, before every other user middleware: middleware runs in reverse
    registration order, so the Host, Origin, and session guards still refuse a request
    first, and CORS wraps it - which is how a 413 from the guard still carries CORS
    headers for the trusted browser that needs to read the student-facing message. It
    wraps receive/send only for the one route that ingests a file body; every other
    request passes through untouched.
    """

    def __init__(self, app: ASGIApp, *, limit: Callable[[], int]) -> None:
        self.app = app
        self._limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not _UPLOAD_PATH.match(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return

        limit = self._limit()
        declared = _declared_length(scope.get("headers", []))
        if declared is not None and declared > limit:
            logger.warning(
                "Refused an upload before reading its body: declared %d bytes exceed "
                "the %d-byte ceiling",
                declared,
                limit,
            )
            await _send_too_large(send)
            return

        counter = _CountingReceive(receive, limit)
        replaced = False
        forwarded = False

        async def guarded_send(message: Message) -> None:
            nonlocal replaced, forwarded
            if replaced:
                # Every remaining original message is dropped: the 413 is already a
                # complete response, and forwarding the 400 it replaced - start or
                # body - would put a second response on the wire.
                return
            if counter.overflowed and message["type"] == "http.response.start":
                # FastAPI converted the overflow into a 400 "error parsing the body".
                # Swap the first response message for the 413; nothing has been
                # forwarded yet, because the overflow happens before a response exists.
                replaced = True
                logger.warning(
                    "Refused an upload at the %d-byte ceiling after receiving %d bytes",
                    limit,
                    counter._received,
                )
                await _send_too_large(send)
                return
            forwarded = True
            await send(message)

        try:
            await self.app(scope, counter.receive, guarded_send)
        except UploadBodyTooLargeError:
            # A path that reads the body without FastAPI's 400 conversion. Answer the
            # 413 only while nothing has been put on the wire; a half-sent response
            # cannot be un-sent, so in that impossible corner the exception is left to
            # the server error handler.
            if not forwarded:
                await _send_too_large(send)
            else:
                raise
        except UploadStreamAbortedError:
            # The client is gone; its spools are already closed. Hand the stack the
            # disconnect it would have seen without the guard.
            raise ClientDisconnect() from None
