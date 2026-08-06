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

# The same argument, for the same reason, for every other pass that runs in a worker rather
# than under someone's cursor: extraction, consolidation, segmentation, solving, and page
# transcription. All five were on `CHAT_TIMEOUT`, which is a number chosen for a person
# watching an answer appear.
#
# What that costs shows up the moment a **reasoning** model is configured, and reasoning
# models are now the norm for local deployment. Measured against Gemma4-12B on a labelled
# corpus: one answer key of three problems produced 21,661 characters of reasoning before
# its first character of JSON, taking 94 seconds; two other documents of the same size had
# not finished at 240. Nothing was wrong with any of them. They were thinking, and the
# client hung up. A document that times out yields no facts at all and, because ingestion
# takes one document at a time, holds every later upload behind it - which is the failure
# `EXTRACTION_MAX_TOKENS` was already written to avoid, arriving by a different road.
BACKGROUND_TIMEOUT = httpx.Timeout(600.0, connect=10.0)

# Sampling for the passes whose reply is parsed rather than read.
#
# Every structured pass in Lyra - extraction, consolidation, segmentation, solving,
# transcription - asks for one exact shape and then parses it. A server's own default is
# routinely 0.8, which is a setting for prose, and it is the difference between a small
# model that emits the schema it was given and one that improvises around it. Chat is
# deliberately left on the model's default: that reply is read by a person, not a parser.
DETERMINISTIC_TEMPERATURE = 0.0

# How a server is asked to constrain a reply to JSON, strongest first.
#
# `json_schema` is the one worth having: llama.cpp compiles it to a GBNF grammar and vLLM
# to a logit mask, so the reply *cannot* leave the shape, and the six-shapes-per-key
# tolerance in `core/profiles.py` stops being what holds extraction together. `json_object`
# guarantees only that the reply parses. `unconstrained` is a sentence in the prompt and
# nothing else, which is where this module was before.
#
# There is no header advertising which of these a server implements, the same problem
# `probe_tool_support` has, so support is discovered by being refused. A 400 carrying a
# `response_format` demotes the endpoint one rung for the life of the process.
JSON_SCHEMA = "json_schema"
JSON_OBJECT = "json_object"
JSON_UNCONSTRAINED = "unconstrained"
_JSON_LADDER = (JSON_SCHEMA, JSON_OBJECT, JSON_UNCONSTRAINED)

# The lowest rung an endpoint is known to need, keyed by endpoint and model. Only
# demotions are recorded: a success says nothing about whether a *stronger* form would
# also have worked, and caching it as a ceiling would cap a schema request at
# `json_object` because some earlier schemaless call happened to succeed.
_json_floor: dict[tuple[str, str | None], str] = {}

# Statuses that may mean "I do not implement that `response_format`". A 400 says so as
# clearly as anything does. A 500 is included because llama.cpp - the runtime this is
# most likely to be pointed at - answers 500 rather than 400 when it cannot compile a
# schema into a grammar, and failing the whole pass there would make constrained decoding
# a liability on the one endpoint it matters most for.
_REFUSED = 400
_REFUSAL_STATUSES = frozenset({_REFUSED, 500})

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
class JsonSchema:
    """A named JSON Schema a reply must conform to.

    Attributes:
        name: Identifier the API requires alongside the schema. Never shown to anyone.
        schema: The schema itself. Written for `strict` mode, which means every property
            appears in `required` and `additionalProperties` is false, so a field the
            caller treats as optional is expressed as an empty array or a null rather
            than as an absent key.
    """

    name: str
    schema: dict[str, object]


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
    max_tokens: int | None = None,
    temperature: float | None = None,
    response_format: dict[str, object] | None = None,
) -> dict[str, object]:
    """Assemble a chat-completions body, omitting `model` when the user has not picked one.

    `tools` is omitted entirely when absent rather than sent empty. A server that does not
    implement tool calling should never see the field on an ordinary chat turn, which is
    what keeps the Phase 1 conversation working against endpoints that would reject it.

    `max_tokens` is omitted the same way, and for a related reason: a ceiling belongs to the
    caller that has one. Only text recognition does, where a dense page can send the model
    into a repetition loop and the ceiling is what turns that into a failed page rather than
    a request that never ends.

    `temperature` and `response_format` follow the same rule: a caller that has no opinion
    sends nothing and gets the server's own behaviour. `temperature` is written out even
    when it is 0, which is why the test is against None rather than falsiness.
    """
    body: dict[str, object] = {"messages": messages, "stream": stream}
    if model is not None:
        body["model"] = model
    if tools:
        body["tools"] = tools
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    if response_format is not None:
        body["response_format"] = response_format
    return body


def reset_json_support() -> None:
    """Forget what every endpoint was found to support. For tests, and for a settings change."""
    _json_floor.clear()


def _json_levels(endpoint: str, model: str | None, schema: JsonSchema | None) -> list[str]:
    """The constraint forms to try for one call, strongest first.

    A caller with no schema constrains nothing. It would be easy to reach for `json_object`
    on the grounds that it is weaker and therefore safe, and it is not: `complete` also
    carries the vision probe and any caller that wants prose, and forcing those to answer
    in JSON would break them against an endpoint that honoured it.

    The endpoint's known floor applies on top: one that has already refused a schema is
    never asked for one again.
    """
    if schema is None:
        return [JSON_UNCONSTRAINED]
    ladder = list(_JSON_LADDER)
    floor = _json_floor.get((endpoint, model))
    start = ladder.index(floor) if floor is not None else 0
    return ladder[start:]


def _demote_json(endpoint: str, model: str | None, level: str) -> None:
    """Record that this endpoint refused `level`, so nothing asks for it again."""
    _json_floor[(endpoint, model)] = _JSON_LADDER[_JSON_LADDER.index(level) + 1]
    logger.info("Tutor endpoint refused %s replies; using the next weaker form", level)


def _response_format(level: str, schema: JsonSchema | None) -> dict[str, object] | None:
    """The `response_format` field for one rung of the ladder, or None for the last one."""
    if level == JSON_SCHEMA and schema is not None:
        return {
            "type": "json_schema",
            # `strict` is what turns the schema into a grammar rather than a suggestion.
            # It is why every schema in `llm/prompts.py` lists all of its properties as
            # required and closes itself to additional ones.
            "json_schema": {"name": schema.name, "strict": True, "schema": schema.schema},
        }
    if level == JSON_OBJECT:
        return {"type": "json_object"}
    return None


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
    max_tokens: int | None = None,
    temperature: float | None = None,
    schema: JsonSchema | None = None,
    request_timeout: httpx.Timeout | None = None,
) -> str:
    """Run a single non-streaming completion and return the assistant message content.

    Profile extraction uses this: it wants one whole JSON document, not a token stream. Any
    `<think>` block is stripped before the content is returned, because a reasoning model
    left unparsed by its server prefixes its JSON with paragraphs of deliberation.

    Args:
        endpoint: Endpoint base URL including its version suffix.
        api_key: Bearer token, or None when the endpoint needs no auth.
        model: Model identifier, omitted from the request when None.
        messages: OpenAI-shaped messages.
        transport: Test seam. Leave unset in production code.
        max_tokens: Ceiling on the reply, omitted when the caller has none.
        temperature: Sampling temperature. `DETERMINISTIC_TEMPERATURE` for any pass whose
            reply is parsed rather than read.
        schema: The shape the reply must take. Sent as `response_format` where the
            endpoint accepts one, and silently dropped where it does not: a schema is an
            enforcement of what the prompt already asks for, never the only place it is
            asked, so a server that cannot take one still gets a usable instruction.
        request_timeout: The httpx timeouts for this call, which separate connect from
            read. Defaults to `CHAT_TIMEOUT`, the right number only when someone is
            watching a reply arrive; every worker-side caller passes `BACKGROUND_TIMEOUT`.

    Raises:
        UpstreamError: The endpoint failed, or its reply had no readable message content.
    """
    url = f"{_base_url(endpoint)}/chat/completions"
    async with _client(request_timeout or CHAT_TIMEOUT, api_key, transport) as client:
        payload = await _post_constrained(
            client, url, endpoint, model, messages, max_tokens, temperature, schema
        )

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        raise UpstreamError(_ERROR_UNREADABLE)
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str):
        raise UpstreamError(_ERROR_UNREADABLE)
    return strip_reasoning(content)


async def _post_constrained(
    client: httpx.AsyncClient,
    url: str,
    endpoint: str,
    model: str | None,
    messages: list[dict[str, object]],
    max_tokens: int | None,
    temperature: float | None,
    schema: JsonSchema | None,
) -> dict[str, object]:
    """Post one completion, stepping down the constraint ladder if the endpoint refuses.

    A 400 is read as "this server does not implement that `response_format`", the same
    reading `complete_with_tools` gives a 400 carrying tool definitions, and for the same
    reason: an OpenAI-compatible server advertises nothing, so the only way to learn what
    it takes is to be refused. The cost of reading it wrong is bounded and one-directional
    - the request is retried in a weaker form, and the weakest form is what this module
    sent before any of this existed.

    Returns:
        The decoded response body.

    Raises:
        UpstreamError: Every form was refused, or the failure was not a refusal.
    """
    levels = _json_levels(endpoint, model, schema)
    for level in levels:
        body = _chat_body(
            model,
            messages,
            stream=False,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=_response_format(level, schema),
        )
        try:
            response = await client.post(url, json=body)
            if response.status_code in _REFUSAL_STATUSES and level != JSON_UNCONSTRAINED:
                # Remembered only for a 400. A 500 is retried in the weaker form but not
                # recorded, because the two things that produce one are not distinguishable
                # from here: llama.cpp answers 500 when it cannot compile a schema into a
                # grammar, and it also answers 500 when the model simply failed to load.
                # Retrying costs two extra requests in the second case and rescues the
                # first; caching it would let one bad model load quietly downgrade every
                # request for the rest of the process.
                if response.status_code == _REFUSED:
                    _demote_json(endpoint, model, level)
                continue
            response.raise_for_status()
            decoded = response.json()
        except httpx.HTTPError as exc:
            raise _mapped_error(exc) from exc
        except ValueError as exc:
            raise UpstreamError(_ERROR_UNREADABLE) from exc
        return decoded if isinstance(decoded, dict) else {}
    # Only reachable if the ladder were empty, which `_json_levels` never returns.
    raise UpstreamError(_ERROR_UPSTREAM)


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
    temperature: float | None = None,
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
    body = _chat_body(model, messages, stream=False, tools=tools, temperature=temperature)
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
