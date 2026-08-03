"""Client for the user's OpenAI-compatible tutor endpoint.

The configured endpoint is assumed to already carry its API version suffix, for example
`http://127.0.0.1:8080/v1`. This module appends only `/chat/completions` and `/models` and
never probes alternative paths: a wrong path surfaces as a 404 the user can act on, which
beats silently guessing at another one.

Reasoning models reach this client two different ways, and both are handled. A server run
with a reasoning parser (llama.cpp, vLLM, Ollama, and the hosted DeepSeek and OpenRouter
APIs) puts the thought in its own delta field, under one of `reasoning_content`,
`reasoning`, or `thinking`. A server without one leaves the model's raw `<think>...</think>`
markers inline in the content stream, so `_ReasoningTagSplitter` pulls them back out. A
model that does not think at all trips neither path and streams exactly as before.

Every failure becomes an `UpstreamError` with a message written for the user. Those messages,
and any log line this module writes, never contain the endpoint URL or the API key.
"""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

import httpx

from backend.core.errors import UpstreamError

logger = logging.getLogger(__name__)

# A local model can spend minutes on the first token, so reads are patient while connects
# are not. The probe timeouts are short so a dead host cannot hold the settings screen.
CHAT_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
PROBE_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

_ERROR_UNREACHABLE = "The tutor endpoint is not reachable. Check that the server is running."
_ERROR_TIMEOUT = "The tutor endpoint did not respond in time."
_ERROR_UNAUTHORIZED = "The tutor endpoint rejected the API key."
_ERROR_NOT_FOUND = "The tutor endpoint path looks wrong. The URL should end in /v1."
_ERROR_UPSTREAM = "The tutor endpoint returned an error."
_ERROR_UNREADABLE = "The tutor endpoint returned a response that could not be read."


# Delta fields carrying reasoning, in the order they are trusted. `reasoning_content` is
# the DeepSeek field that llama.cpp and vLLM adopted, `reasoning` is OpenRouter's, and
# `thinking` is Ollama's. A server that sets none of them has its reasoning, if any, left
# inline in `content` for the tag splitter below.
_REASONING_FIELDS = ("reasoning_content", "reasoning", "thinking")

_THINK_OPEN = ("<think>", "<thinking>")
_THINK_CLOSE = ("</think>", "</thinking>")
_LONGEST_TAG = max(len(tag) for tag in _THINK_OPEN + _THINK_CLOSE)

Channel = Literal["answer", "reasoning"]


@dataclass(frozen=True)
class ConnectionResult:
    """Outcome of a connection test, shaped for the four states the settings screen renders."""

    ok: bool
    model_count: int
    message: str


@dataclass(frozen=True)
class StreamDelta:
    """One fragment of a streamed reply, tagged with the channel it belongs to.

    `answer` is the reply the student reads. `reasoning` is the model thinking out loud,
    which the interface shows separately and never mixes into the answer.
    """

    channel: Channel
    text: str


class _ReasoningTagSplitter:
    """Pulls inline `<think>...</think>` blocks out of a content stream.

    Tags arrive split across network chunks as readily as anything else, so a partial tail
    that could still become a tag is held back rather than emitted as answer text. The
    held-back tail is at most one tag long, which is why a stream never visibly stalls on it.

    One case is deliberately not handled: a chat template that pre-fills the opening
    `<think>` server-side, so the stream carries only the closing marker. Reclassifying
    text already sent to the reader is not possible, and buffering every answer until a
    close marker might arrive would delay the first word of every non-thinking model. Those
    servers ship a reasoning parser that fills `reasoning_content` instead, which is the
    path above.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False

    def _held_tail(self, targets: tuple[str, ...]) -> int:
        """Length of the buffer's suffix that could still grow into one of `targets`."""
        for length in range(min(len(self._buffer), _LONGEST_TAG - 1), 0, -1):
            if any(tag.startswith(self._buffer[-length:]) for tag in targets):
                return length
        return 0

    def feed(self, text: str) -> list[StreamDelta]:
        """Split one content fragment into the deltas that are safe to emit now."""
        self._buffer += text
        deltas: list[StreamDelta] = []

        while True:
            targets = _THINK_CLOSE if self._inside else _THINK_OPEN
            channel: Channel = "reasoning" if self._inside else "answer"
            found = [(index, tag) for tag in targets if (index := self._buffer.find(tag)) != -1]
            if found:
                index, tag = min(found)
                if index:
                    deltas.append(StreamDelta(channel, self._buffer[:index]))
                self._buffer = self._buffer[index + len(tag) :]
                self._inside = not self._inside
                continue

            held = self._held_tail(targets)
            safe = self._buffer[: len(self._buffer) - held]
            if safe:
                deltas.append(StreamDelta(channel, safe))
            self._buffer = self._buffer[len(self._buffer) - held :]
            return deltas

    def flush(self) -> list[StreamDelta]:
        """Emit whatever is still held once the stream ends, so no text is swallowed."""
        if not self._buffer:
            return []
        channel: Channel = "reasoning" if self._inside else "answer"
        deltas = [StreamDelta(channel, self._buffer)]
        self._buffer = ""
        return deltas


def _base_url(endpoint: str) -> str:
    """Drop one trailing slash so path joining does not double it."""
    return endpoint[:-1] if endpoint.endswith("/") else endpoint


def _client(
    timeout: httpx.Timeout,
    api_key: str | None,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.AsyncClient:
    """Build the HTTP client. `transport` is the seam tests inject a stub through."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return httpx.AsyncClient(timeout=timeout, headers=headers, transport=transport)


def _mapped_error(exc: Exception) -> UpstreamError:
    """Turn a transport or status failure into a distinct user-facing error.

    The endpoint URL and the API key are deliberately absent from every branch: these
    messages reach the browser, and `httpx` puts the full request URL in its own strings.
    """
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamError(_ERROR_TIMEOUT)
    if isinstance(exc, httpx.ConnectError):
        return UpstreamError(_ERROR_UNREACHABLE)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return UpstreamError(_ERROR_UNAUTHORIZED)
        if status == 404:
            return UpstreamError(_ERROR_NOT_FOUND)
        logger.warning("Tutor endpoint returned status %s", status)
        return UpstreamError(_ERROR_UPSTREAM)
    return UpstreamError(_ERROR_UNREACHABLE)


def _chat_body(
    model: str | None, messages: list[dict[str, str]], stream: bool
) -> dict[str, object]:
    """Assemble a chat-completions body, omitting `model` when the user has not picked one."""
    body: dict[str, object] = {"messages": messages, "stream": stream}
    if model is not None:
        body["model"] = model
    return body


def _delta_fields(payload: str) -> tuple[str, str]:
    """Pull one SSE data payload apart into its `(content, reasoning)` text.

    Either half is an empty string when the frame does not carry it, which is the common
    case: a frame holds one or the other, not both.
    """
    try:
        frame = json.loads(payload)
    except ValueError:
        # Keep-alive noise and half-written frames are normal on some servers, not fatal.
        return "", ""
    if not isinstance(frame, dict):
        return "", ""
    choices = frame.get("choices") or []
    if not choices:
        return "", ""
    delta = choices[0].get("delta") or {}
    if not isinstance(delta, dict):
        return "", ""

    content = delta.get("content")
    reasoning = next(
        (
            value
            for field in _REASONING_FIELDS
            if isinstance(value := delta.get(field), str) and value
        ),
        "",
    )
    return (content if isinstance(content, str) else ""), reasoning


async def stream_chat(
    endpoint: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict[str, str]],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[StreamDelta]:
    """Stream assistant deltas from the tutor endpoint, split by channel.

    Args:
        endpoint: Endpoint base URL including its version suffix.
        api_key: Bearer token, or None when the endpoint needs no auth.
        model: Model identifier, omitted from the request when None.
        messages: OpenAI-shaped chat messages.
        transport: Test seam. Leave unset in production code.

    Yields:
        Non-empty `StreamDelta` fragments in arrival order, each tagged `answer` or
        `reasoning`, ending at the upstream `[DONE]` frame.

    Raises:
        UpstreamError: The endpoint was unreachable, slow, or returned a non-2xx status.
    """
    url = f"{_base_url(endpoint)}/chat/completions"
    body = _chat_body(model, messages, stream=True)
    splitter = _ReasoningTagSplitter()
    async with _client(CHAT_TIMEOUT, api_key, transport) as client:
        try:
            async with client.stream("POST", url, json=body) as response:
                if response.status_code >= 400:
                    await response.aread()
                    response.raise_for_status()
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        break
                    content, reasoning = _delta_fields(payload)
                    if reasoning:
                        yield StreamDelta("reasoning", reasoning)
                    for delta in splitter.feed(content) if content else ():
                        yield delta
                for delta in splitter.flush():
                    yield delta
        except httpx.HTTPError as exc:
            raise _mapped_error(exc) from exc


async def complete(
    endpoint: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict[str, str]],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Run a single non-streaming completion and return the assistant message content.

    Profile extraction uses this: it wants one whole JSON document, not a token stream. Any
    `<think>` block is stripped before the content is returned, because a reasoning model
    left unparsed by its server prefixes its JSON with paragraphs of deliberation.

    Raises:
        UpstreamError: The endpoint failed, or its reply had no readable message content.
    """
    url = f"{_base_url(endpoint)}/chat/completions"
    body = _chat_body(model, messages, stream=False)
    async with _client(CHAT_TIMEOUT, api_key, transport) as client:
        try:
            response = await client.post(url, json=body)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise _mapped_error(exc) from exc
        except ValueError as exc:
            raise UpstreamError(_ERROR_UNREADABLE) from exc

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        raise UpstreamError(_ERROR_UNREADABLE)
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str):
        raise UpstreamError(_ERROR_UNREADABLE)
    return strip_reasoning(content)


def strip_reasoning(content: str) -> str:
    """Drop inline `<think>` blocks from a complete (non-streamed) message.

    Works on the same splitter as the streaming path, so both agree on which markers count.
    """
    splitter = _ReasoningTagSplitter()
    deltas = [*splitter.feed(content), *splitter.flush()]
    return "".join(delta.text for delta in deltas if delta.channel == "answer").strip()


async def list_models(
    endpoint: str,
    api_key: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """List the model identifiers the endpoint advertises.

    Raises:
        UpstreamError: The endpoint failed or returned something that is not a model list.
    """
    url = f"{_base_url(endpoint)}/models"
    async with _client(PROBE_TIMEOUT, api_key, transport) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise _mapped_error(exc) from exc
        except ValueError as exc:
            raise UpstreamError(_ERROR_UNREADABLE) from exc

    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise UpstreamError(_ERROR_UNREADABLE)
    return [str(entry["id"]) for entry in entries if isinstance(entry, dict) and entry.get("id")]


async def test_connection(
    endpoint: str,
    api_key: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ConnectionResult:
    """Probe the endpoint and report the outcome instead of raising.

    The settings screen renders four distinct outcomes, so a failure has to come back as
    data with its own message rather than as an exception the caller has to re-classify.
    A reachable endpoint advertising zero models is still `ok`; the UI reads `model_count`.
    """
    try:
        models = await list_models(endpoint, api_key, transport=transport)
    except UpstreamError as exc:
        return ConnectionResult(ok=False, model_count=0, message=exc.message)
    count = len(models)
    return ConnectionResult(
        ok=True,
        model_count=count,
        message=f"Connected. {count} models available.",
    )
