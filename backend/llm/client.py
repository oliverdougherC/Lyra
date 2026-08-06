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

Tool calling lives here too, and is used only by the verification loop in `llm/tools.py`.
It is non-streaming: the caller wants whole tool calls rather than fragments, and nobody
reads a verification pass live. Tool definitions are never sent on an ordinary chat turn,
so an endpoint that cannot accept them still carries the whole Phase 1 conversation.

Every failure becomes an `UpstreamError` with a message written for the user. Those messages,
and any log line this module writes, never contain the endpoint URL or the API key.
"""

import base64
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

import httpx
import pymupdf

from backend.core.errors import ToolsUnsupportedError, UpstreamError

logger = logging.getLogger(__name__)

# A local model can spend minutes on the first token, so reads are patient while connects
# are not. The probe timeouts are short so a dead host cannot hold the settings screen.
CHAT_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
PROBE_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# A tool turn is patient where a chat turn cannot be. Nobody is waiting on a verification
# pass, and its later turns carry the whole tool transcript, so generation gets slower as
# the loop goes on: checking one real problem hit the chat timeout eight rounds in and
# threw away every check it had run. The loop's own wall clock is the ceiling that should
# bound a run, so this is set to match it rather than to cut in front of it.
TOOL_TIMEOUT = httpx.Timeout(600.0, connect=10.0)

_ERROR_UNREACHABLE = "The tutor endpoint is not reachable. Check that the server is running."
_ERROR_TIMEOUT = "The tutor endpoint did not respond in time."
_ERROR_UNAUTHORIZED = "The tutor endpoint rejected the API key."
_ERROR_NOT_FOUND = "The tutor endpoint path looks wrong. The URL should end in /v1."
_ERROR_UPSTREAM = "The tutor endpoint returned an error."
_ERROR_UNREADABLE = "The tutor endpoint returned a response that could not be read."
_ERROR_NO_TOOLS = "The tutor endpoint does not accept tool calls."

# The smallest tool that cannot be answered from prose. Addition is chosen so a model
# that ignores the tool and answers anyway is still obviously not calling it.
_PROBE_TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "add",
        "description": "Add two numbers and return the sum.",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
}
_PROBE_OK = "This endpoint can run the checks Lyra verifies solutions with."
_PROBE_REFUSED = (
    "This endpoint does not support tool calls, so Lyra cannot check solutions against a "
    "computer algebra system. Solving still works."
)
_PROBE_IGNORED = (
    "This endpoint accepted the request but answered without calling the tool, so Lyra "
    "cannot rely on it to check solutions. Solving still works."
)

# The vision probe renders this into a small image and asks the endpoint to read it back.
# Digits rather than a word: they are the glyphs a vision model reads most reliably, and a
# five-digit number is not something a model that cannot see could land on by guessing.
# A word would risk both mistakes at once, being harder to read and easier to guess.
_PROBE_CODE = "48213"
_PROBE_IMAGE_SIZE = (260, 96)
_PROBE_IMAGE_DPI = 110

_VISION_OK = "This endpoint can read images, so Lyra can transcribe pages with it."
_VISION_REFUSED = (
    "This endpoint does not accept images, so Lyra cannot read scanned pages with it. "
    "Everything else works."
)
_VISION_IGNORED = (
    "This endpoint accepted an image but could not read what it said, so Lyra cannot rely "
    "on it to transcribe pages. Everything else works."
)


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
class ToolCall:
    """One tool the model asked to run.

    `arguments` is the raw JSON text the model produced, not a parsed object. Models emit
    malformed argument JSON often enough that parsing here would turn a recoverable
    round trip into a failed one; the tool loop parses it and hands a parse error back to
    the model as a result it can act on.
    """

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class AssistantMessage:
    """One complete assistant turn: what it said, and what it wants run.

    Both may be present. A model commonly narrates a sentence and calls a tool in the
    same turn, and dropping either half would lose part of the transcript.
    """

    content: str
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolSupport:
    """Whether this endpoint can run tool calls, and how we know.

    Three outcomes, not two. An endpoint that rejects the request cannot do it; one that
    calls the tool can; and one that accepts the request and answers in prose anyway is
    reported as cannot, with a message saying that is what happened. Guessing in that
    third case would either disable verification on a working endpoint or claim
    verification on one that silently never runs it.
    """

    ok: bool
    message: str


@dataclass(frozen=True)
class VisionSupport:
    """Whether this endpoint can read an image, and how we know.

    Three outcomes for the same reason `ToolSupport` has three. An endpoint that rejects
    the request cannot do it; one that reads the code back can; and one that accepts an
    image and answers without having read it is reported as cannot, with a message saying
    so. That third case is the common one, because an OpenAI-compatible server will
    happily accept a content-part array it has no way to look at.
    """

    ok: bool
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
    model: str | None,
    messages: list[dict[str, object]],
    stream: bool,
    tools: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Assemble a chat-completions body, omitting `model` when the user has not picked one.

    `tools` is omitted entirely when absent rather than sent empty. A server that does not
    implement tool calling should never see the field on an ordinary chat turn, which is
    what keeps the Phase 1 conversation working against endpoints that would reject it.
    """
    body: dict[str, object] = {"messages": messages, "stream": stream}
    if model is not None:
        body["model"] = model
    if tools:
        body["tools"] = tools
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
    messages: list[dict[str, object]],
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


def _read_tool_calls(message: dict[str, object]) -> tuple[ToolCall, ...]:
    """Pull the tool calls out of an assistant message, skipping anything malformed.

    A frame that claims a tool call but carries no name is dropped rather than passed on
    as a call to the empty string: the loop would refuse it a moment later anyway, and
    the refusal would read as the model's mistake rather than the server's.
    """
    raw = message.get("tool_calls")
    if not isinstance(raw, list):
        return ()
    calls: list[ToolCall] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        function = entry.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments")
        calls.append(
            ToolCall(
                id=str(entry.get("id") or f"call_{len(calls)}"),
                name=name,
                arguments=arguments if isinstance(arguments, str) else "{}",
            )
        )
    return tuple(calls)


async def complete_with_tools(
    endpoint: str,
    api_key: str | None,
    model: str | None,
    messages: list[dict[str, object]],
    tools: list[dict[str, object]],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AssistantMessage:
    """Run one non-streaming turn with tool definitions attached.

    Not streamed, deliberately. The caller is the verification loop, which wants whole
    tool calls rather than fragments and which nobody is reading live, so streaming would
    add reassembly for no benefit.

    Args:
        endpoint: Endpoint base URL including its version suffix.
        api_key: Bearer token, or None when the endpoint needs no auth.
        model: Model identifier, omitted from the request when None.
        messages: OpenAI-shaped messages, including any prior `assistant` turns carrying
            `tool_calls` and the `tool` messages answering them.
        tools: OpenAI-shaped tool definitions.

    Returns:
        The assistant's content and the tool calls it asked for, either of which may be
        empty.

    Raises:
        ToolsUnsupportedError: The endpoint refused the request with a 400. That status
            is the strongest available signal that it does not implement tool calling.
            It can also mean something else was wrong with the request, and the cost of
            reading it the wrong way is bounded: verification degrades and reports itself
            as not run, while solving is unaffected.
        UpstreamError: Any other transport or status failure.
    """
    url = f"{_base_url(endpoint)}/chat/completions"
    body = _chat_body(model, messages, stream=False, tools=tools)
    async with _client(TOOL_TIMEOUT, api_key, transport) as client:
        try:
            response = await client.post(url, json=body)
            if response.status_code == 400:
                logger.info("Tutor endpoint rejected a request carrying tool definitions")
                raise ToolsUnsupportedError(_ERROR_NO_TOOLS)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise _mapped_error(exc) from exc
        except ValueError as exc:
            raise UpstreamError(_ERROR_UNREADABLE) from exc

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        raise UpstreamError(_ERROR_UNREADABLE)
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise UpstreamError(_ERROR_UNREADABLE)

    content = message.get("content")
    # A turn that only calls tools carries no content at all, which is not an error.
    return AssistantMessage(
        content=strip_reasoning(content) if isinstance(content, str) else "",
        tool_calls=_read_tool_calls(message),
    )


def image_message(text: str, image: bytes, mime: str = "image/png") -> dict[str, object]:
    """A user turn carrying one image beside its instruction.

    The OpenAI content-part array, which is how every compatible server that can see takes
    an image. It is built here rather than at call sites so there is one place that knows
    the shape, and one place to change if a server needs a different one.

    The image travels as a `data:` URL rather than a link. Lyra is loopback-only and the
    endpoint may be a different machine, so a URL pointing at this process would be a URL
    the model cannot fetch.

    Args:
        text: The instruction that goes with the image.
        image: Encoded image bytes, normally a rendered page.
        mime: Media type of `image`.

    Returns:
        One OpenAI-shaped `user` message.
    """
    encoded = base64.b64encode(image).decode("ascii")
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
        ],
    }


def _probe_image() -> bytes:
    """A small PNG carrying `_PROBE_CODE`, drawn rather than shipped as a blob.

    Generated so the probe is readable as code: a base64 constant would say nothing about
    what is being asked. PyMuPDF is already a dependency and its base-14 fonts need no
    files on disk.
    """
    document = pymupdf.open()
    page = document.new_page(width=_PROBE_IMAGE_SIZE[0], height=_PROBE_IMAGE_SIZE[1])
    page.insert_text((20, 62), _PROBE_CODE, fontname="helv", fontsize=44)
    return page.get_pixmap(dpi=_PROBE_IMAGE_DPI).tobytes("png")


async def probe_vision_support(
    endpoint: str,
    api_key: str | None,
    model: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> VisionSupport:
    """Show the endpoint a number and ask it to read it back.

    A real inference call for the same reason the tool probe is one: an OpenAI-compatible
    server advertises nothing about vision, and a server that cannot see will still accept
    a message whose content is an array and answer from the text half of it. Asking it to
    report something only visible in the image is the only way to tell those apart.

    Returns the outcome as data rather than raising, so a settings screen can render all
    three states.
    """
    prompt = (
        "This image contains a number. Reply with that number and nothing else. "
        "If you cannot see an image, say NO IMAGE."
    )
    try:
        answer = await complete(
            endpoint,
            api_key,
            model,
            [image_message(prompt, _probe_image())],
            transport=transport,
        )
    except UpstreamError as exc:
        # A server with no vision path commonly rejects the content-part array outright,
        # which arrives here as a 400 and is a refusal rather than an outage.
        return VisionSupport(ok=False, message=_VISION_REFUSED if _is_refusal(exc) else exc.message)

    return (
        VisionSupport(ok=True, message=_VISION_OK)
        if _PROBE_CODE in answer
        else VisionSupport(ok=False, message=_VISION_IGNORED)
    )


def _is_refusal(exc: UpstreamError) -> bool:
    """Whether an upstream failure reads as "this request shape is not supported"."""
    return exc.message in (_ERROR_UPSTREAM, _ERROR_UNREADABLE)


async def probe_tool_support(
    endpoint: str,
    api_key: str | None,
    model: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ToolSupport:
    """Ask the endpoint to make one trivial tool call, and report what happened.

    A real inference call rather than a capability header, because there is no header to
    read: an OpenAI-compatible server advertises nothing about tool calling, and several
    accept the field and then ignore it. The only way to know is to ask for a call and
    see whether one comes back.

    Returns the outcome as data rather than raising, so the settings screen can render
    all three states.
    """
    messages: list[dict[str, object]] = [
        {
            "role": "user",
            "content": "Call the add tool with a = 2 and b = 3. Do not answer in prose.",
        }
    ]
    try:
        answer = await complete_with_tools(
            endpoint, api_key, model, messages, [_PROBE_TOOL], transport=transport
        )
    except ToolsUnsupportedError:
        return ToolSupport(ok=False, message=_PROBE_REFUSED)
    except UpstreamError as exc:
        return ToolSupport(ok=False, message=exc.message)

    if answer.tool_calls:
        return ToolSupport(ok=True, message=_PROBE_OK)
    return ToolSupport(ok=False, message=_PROBE_IGNORED)


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
