"""Contract tests for the tutor client, driven entirely through a stubbed transport."""

import json
import logging
from collections.abc import Callable

import httpx
import pytest

from backend.core.errors import UpstreamError
from backend.llm import client

_ENDPOINT = "http://127.0.0.1:8080/v1"
_API_KEY = "sk-lyra-not-a-real-value"

# Blank lines, a non-data line, and a half-written frame all sit between valid frames,
# because real servers emit all three and none of them may break the stream.
_STREAM_BODY = "\n".join(
    [
        'data: {"choices":[{"delta":{"content":"The "}}]}',
        "",
        ": keep-alive",
        'data: {"choices":[{"delta":{"content":"limit "}}]}',
        'data: {"choices":[{"delta":',
        'data: {"choices":[{"delta":{"content":"is 2."}}]}',
        "data: [DONE]",
        'data: {"choices":[{"delta":{"content":"after done"}}]}',
        "",
    ]
)


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    """Wrap a request handler as a transport, so no test can reach the network."""
    return httpx.MockTransport(handler)


def _body(*frames: str) -> str:
    """An SSE body from raw `data:` payloads, terminated the way a server terminates one."""
    return "\n".join([*(f"data: {frame}" for frame in frames), "data: [DONE]", ""])


async def _collect(body: str) -> list[client.StreamDelta]:
    """Run one stubbed stream to completion and return every delta it yielded."""
    transport = _transport(lambda request: httpx.Response(200, text=body))
    return [
        delta
        async for delta in client.stream_chat(
            _ENDPOINT, None, "local-model", [{"role": "user", "content": "hi"}], transport=transport
        )
    ]


def _text(deltas: list[client.StreamDelta], channel: str) -> str:
    """Everything one channel carried, rejoined."""
    return "".join(delta.text for delta in deltas if delta.channel == channel)


async def test_stream_chat_yields_only_deltas_and_stops_at_done() -> None:
    deltas = await _collect(_STREAM_BODY)

    assert deltas == [
        client.StreamDelta("answer", "The "),
        client.StreamDelta("answer", "limit "),
        client.StreamDelta("answer", "is 2."),
    ]


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "thinking"])
async def test_a_server_side_reasoning_field_arrives_on_its_own_channel(field: str) -> None:
    deltas = await _collect(
        _body(
            f'{{"choices":[{{"delta":{{"{field}":"Chain rule first."}}}}]}}',
            '{"choices":[{"delta":{"content":"The derivative is 2x."}}]}',
        )
    )

    assert _text(deltas, "reasoning") == "Chain rule first."
    assert _text(deltas, "answer") == "The derivative is 2x."


async def test_inline_think_tags_are_split_out_of_the_content_stream() -> None:
    deltas = await _collect(
        _body(
            '{"choices":[{"delta":{"content":"<think>Recall the "}}]}',
            '{"choices":[{"delta":{"content":"power rule.</think>The answer "}}]}',
            '{"choices":[{"delta":{"content":"is 2x."}}]}',
        )
    )

    assert _text(deltas, "reasoning") == "Recall the power rule."
    assert _text(deltas, "answer") == "The answer is 2x."


async def test_a_think_tag_split_across_chunks_is_still_recognized() -> None:
    deltas = await _collect(
        _body(
            '{"choices":[{"delta":{"content":"<thi"}}]}',
            '{"choices":[{"delta":{"content":"nk>hmm</thin"}}]}',
            '{"choices":[{"delta":{"content":"k>Answer."}}]}',
        )
    )

    assert _text(deltas, "reasoning") == "hmm"
    assert _text(deltas, "answer") == "Answer."


async def test_an_unclosed_think_block_is_flushed_as_reasoning_not_swallowed() -> None:
    deltas = await _collect(_body('{"choices":[{"delta":{"content":"<think>cut off here"}}]}'))

    assert _text(deltas, "reasoning") == "cut off here"
    assert _text(deltas, "answer") == ""


async def test_a_lone_angle_bracket_is_answer_text_once_the_stream_ends() -> None:
    deltas = await _collect(_body('{"choices":[{"delta":{"content":"a < b"}}]}'))

    assert _text(deltas, "answer") == "a < b"
    assert _text(deltas, "reasoning") == ""


def test_strip_reasoning_leaves_only_the_answer_for_a_whole_message() -> None:
    assert client.strip_reasoning('<think>weighing it</think>\n{"topics": []}') == '{"topics": []}'
    assert client.strip_reasoning('{"topics": []}') == '{"topics": []}'


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1/"])
async def test_stream_chat_appends_only_the_completions_path(endpoint: str) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="data: [DONE]\n")

    async for _ in client.stream_chat(
        endpoint, None, None, [{"role": "user", "content": "hi"}], transport=_transport(handler)
    ):
        pass

    assert seen == ["http://127.0.0.1:8080/v1/chat/completions"]


async def test_stream_chat_omits_model_when_unset() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, text="data: [DONE]\n")

    async for _ in client.stream_chat(
        _ENDPOINT, None, None, [{"role": "user", "content": "hi"}], transport=_transport(handler)
    ):
        pass

    assert b'"model"' not in bodies[0]


async def test_stream_chat_accepts_a_bounded_background_generation_budget() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text="data: [DONE]\n")

    async for _ in client.stream_chat(
        _ENDPOINT,
        None,
        "local-model",
        [{"role": "user", "content": "write one paragraph"}],
        transport=_transport(handler),
        max_tokens=320,
        request_timeout=client.BACKGROUND_TIMEOUT,
    ):
        pass

    assert bodies[0]["max_tokens"] == 320


async def test_not_found_reports_a_wrong_path_without_leaking_endpoint_or_key() -> None:
    transport = _transport(lambda request: httpx.Response(404, json={"error": "no such route"}))

    with pytest.raises(UpstreamError) as caught:
        await client.complete(
            _ENDPOINT,
            _API_KEY,
            "local-model",
            [{"role": "user", "content": "hi"}],
            transport=transport,
        )

    message = caught.value.message
    assert "path looks wrong" in message
    assert "/v1" in message
    assert _ENDPOINT not in message
    assert "127.0.0.1" not in message
    assert _API_KEY not in message


async def test_unauthorized_reports_a_rejected_key_without_leaking_it() -> None:
    transport = _transport(lambda request: httpx.Response(401, json={"error": "bad key"}))

    with pytest.raises(UpstreamError) as caught:
        await client.complete(
            _ENDPOINT,
            _API_KEY,
            "local-model",
            [{"role": "user", "content": "hi"}],
            transport=transport,
        )

    message = caught.value.message
    assert "rejected the API key" in message
    assert _API_KEY not in message
    assert _ENDPOINT not in message


async def test_stream_chat_maps_status_failures_too() -> None:
    transport = _transport(lambda request: httpx.Response(500, text="boom"))

    with pytest.raises(UpstreamError) as caught:
        async for _ in client.stream_chat(
            _ENDPOINT, _API_KEY, None, [{"role": "user", "content": "hi"}], transport=transport
        ):
            pass

    assert caught.value.message == "The tutor endpoint returned an error."


async def test_an_in_band_error_frame_fails_the_stream_with_the_servers_words() -> None:
    """llama.cpp cannot change the HTTP status once streaming has begun.

    A mid-generation failure arrives as a `data: {"error": ...}` frame inside a 200, and
    reading it as keep-alive noise ended those streams looking like short but successful
    replies. The server's message is kept because it is the only diagnosis there is.
    """
    body = _body(
        '{"choices":[{"delta":{"content":"partial "}}]}',
        '{"error":{"message":"CUDA error: out of memory","code":500}}',
    )

    with pytest.raises(UpstreamError) as caught:
        await _collect(body)

    assert "out of memory" in caught.value.message


async def test_a_json_frame_in_the_wrong_shape_is_an_unreadable_reply_not_a_crash() -> None:
    """The module's contract is that every failure becomes an `UpstreamError`.

    A frame whose `choices[0]` is not an object used to escape as a raw AttributeError,
    which no caller catches and no user can read.
    """
    with pytest.raises(UpstreamError) as caught:
        await _collect(_body('{"choices":["not an object"]}'))

    assert caught.value.message == "The tutor endpoint returned a response that could not be read."


async def test_a_stream_ending_without_done_is_logged_as_possibly_incomplete(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A connection that closes cleanly without `[DONE]` looks exactly like a short reply.

    The text is still delivered - yanking back words already read would cost a plausibly
    complete answer - but the one place that can tell the difference has to say so.
    """
    with caplog.at_level(logging.WARNING, logger="backend.llm.client"):
        deltas = await _collect('data: {"choices":[{"delta":{"content":"cut"}}]}\n')

    assert _text(deltas, "answer") == "cut"
    assert any("[DONE]" in record.getMessage() for record in caplog.records)


async def test_complete_returns_the_message_content() -> None:
    payload = {"choices": [{"message": {"role": "assistant", "content": '{"topics": []}'}}]}
    transport = _transport(lambda request: httpx.Response(200, json=payload))

    result = await client.complete(
        _ENDPOINT, None, "local-model", [{"role": "user", "content": "hi"}], transport=transport
    )

    assert result == '{"topics": []}'


_TRUNCATED = {"choices": [{"message": {"content": "78 Matri"}, "finish_reason": "length"}]}


async def test_a_truncated_reply_raises_when_the_caller_made_truncation_fatal() -> None:
    """`finish_reason: "length"` is the server saying "this is not the whole reply".

    A caller that stores what it gets back - transcription - must hear that, or half a
    page is filed under a whole page's name and nothing downstream can ever tell.
    """
    transport = _transport(lambda request: httpx.Response(200, json=_TRUNCATED))

    with pytest.raises(UpstreamError) as caught:
        await client.complete(
            _ENDPOINT,
            None,
            "local-model",
            [{"role": "user", "content": "hi"}],
            transport=transport,
            fail_on_truncation=True,
        )

    assert "output-token ceiling" in caught.value.message


async def test_a_truncated_reply_still_reaches_callers_that_did_not_opt_in() -> None:
    """Chat and the probes read a partial reply for exactly what it is."""
    transport = _transport(lambda request: httpx.Response(200, json=_TRUNCATED))

    result = await client.complete(
        _ENDPOINT, None, "local-model", [{"role": "user", "content": "hi"}], transport=transport
    )

    assert result == "78 Matri"


async def test_connection_reports_the_model_count_when_healthy() -> None:
    payload = {"data": [{"id": "qwen3-8b"}, {"id": "llama-3.1-8b"}]}
    transport = _transport(lambda request: httpx.Response(200, json=payload))

    result = await client.test_connection(_ENDPOINT, None, transport=transport)

    assert result.ok is True
    assert result.model_count == 2
    assert result.message == "Connected. 2 models available."


async def test_connection_reports_unreachable_instead_of_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = await client.test_connection(_ENDPOINT, _API_KEY, transport=_transport(handler))

    assert result.ok is False
    assert result.model_count == 0
    assert (
        result.message == "The tutor endpoint is not reachable. Check that the server is running."
    )
    assert _API_KEY not in result.message


async def test_connection_reports_a_timeout_distinctly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    result = await client.test_connection(_ENDPOINT, None, transport=_transport(handler))

    assert result.ok is False
    assert result.message == "The tutor endpoint did not respond in time."


async def test_list_models_returns_the_advertised_ids() -> None:
    payload = {"data": [{"id": "qwen3-8b"}, {"object": "model"}, {"id": "nomic"}]}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=payload)

    models = await client.list_models(_ENDPOINT + "/", None, transport=_transport(handler))

    assert models == ["qwen3-8b", "nomic"]
    assert seen == ["http://127.0.0.1:8080/v1/models"]


async def test_auth_header_is_sent_only_when_a_key_is_set() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"data": []})

    await client.list_models(_ENDPOINT, _API_KEY, transport=_transport(handler))
    await client.list_models(_ENDPOINT, None, transport=_transport(handler))

    assert seen == [f"Bearer {_API_KEY}", None]


def _reply(text: str) -> Callable[[httpx.Request], httpx.Response]:
    """A non-streaming completion answering with fixed content."""
    return lambda request: httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def test_an_image_message_carries_a_data_url_beside_its_instruction() -> None:
    """A URL pointing at this process would be one the model cannot fetch.

    Lyra is loopback-only and the endpoint may be another machine entirely, so the bytes
    travel inline.
    """
    message = client.image_message("read this", b"\x89PNG fake")

    assert message["role"] == "user"
    parts = message["content"]
    assert parts[0] == {"type": "text", "text": "read this"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_a_model_that_reads_the_code_back_can_see() -> None:
    support = await client.probe_vision_support(
        _ENDPOINT, None, "vision-model", transport=_transport(_reply("48213"))
    )

    assert support.ok is True


async def test_a_model_that_answers_without_looking_cannot_see() -> None:
    """The common case, and the reason the probe asks for something only visible.

    An OpenAI-compatible server with no vision path still accepts a content-part array and
    answers from the text half of it, so "it did not raise" proves nothing at all.
    """
    support = await client.probe_vision_support(
        _ENDPOINT, None, "text-model", transport=_transport(_reply("I cannot see an image."))
    )

    assert support.ok is False
    assert "could not read" in support.message


async def test_a_server_that_rejects_an_image_is_reported_as_unable_not_broken() -> None:
    """A 400 is the server processing the request and refusing its shape - a capability."""
    transport = _transport(lambda request: httpx.Response(400, json={"error": "no vision"}))

    support = await client.probe_vision_support(_ENDPOINT, None, "text-model", transport=transport)

    assert support.ok is False
    assert "does not accept images" in support.message


async def test_a_server_that_errors_on_the_image_probe_is_reported_as_broken_not_blind() -> None:
    """A 5xx says the endpoint failed, and says nothing about what it can see.

    This used to read as "does not accept images", which told a user with a crashing
    server that their vision model was blind - a diagnosis they could only disprove by
    distrusting the settings screen. An unreadable reply is the same: an outage wearing
    a capability verdict.
    """
    transport = _transport(lambda request: httpx.Response(500, json={"error": "model crashed"}))

    support = await client.probe_vision_support(_ENDPOINT, None, "text-model", transport=transport)

    assert support.ok is False
    assert support.message == "The tutor endpoint returned an error."
    assert "does not accept images" not in support.message


async def test_a_vision_probe_never_raises() -> None:
    """The settings screen renders the outcome, so an unreachable host is data too."""
    transport = _transport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")))

    support = await client.probe_vision_support(_ENDPOINT, None, None, transport=transport)

    assert support.ok is False
    assert support.message


async def test_both_capability_probes_are_bounded_in_time_and_tokens() -> None:
    """The probes run under someone's cursor on the settings screen.

    They used to inherit `CHAT_TIMEOUT` and `TOOL_TIMEOUT` - minutes of patience budgeted
    for work nobody is watching - so a hung endpoint held the screen for exactly that
    long, against the module's own note that probe timeouts are short. Their answers are
    a tool call and a five-digit number, so a token ceiling travels with the deadline.
    """
    seen: list[tuple[float | None, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.extensions.get("timeout", {}).get("read"), body.get("max_tokens")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "48213"}}]})

    await client.probe_vision_support(_ENDPOINT, None, "m", transport=_transport(handler))
    await client.probe_tool_support(_ENDPOINT, None, "m", transport=_transport(handler))

    assert client.CAPABILITY_PROBE_TIMEOUT.read < client.CHAT_TIMEOUT.read
    assert client.CAPABILITY_PROBE_TIMEOUT.read < client.TOOL_TIMEOUT.read
    assert len(seen) == 2
    for timeout_read, max_tokens in seen:
        assert timeout_read == client.CAPABILITY_PROBE_TIMEOUT.read
        assert max_tokens == client._PROBE_MAX_TOKENS


# --------------------------------------------------------------------------------------
# Constrained decoding: temperature, and the response_format ladder.


_SCHEMA = client.JsonSchema(
    name="answer",
    schema={
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    },
)

_OK = {"choices": [{"message": {"content": '{"answer": "yes"}'}}]}


@pytest.fixture(autouse=True)
def forget_endpoint_support() -> None:
    """The support cache lives for the process, so a test must not inherit another's."""
    client.reset_json_support()


def _recorder(
    statuses: dict[str, int],
    body: dict[str, object] | None = None,
) -> tuple[httpx.MockTransport, list[dict[str, object]]]:
    """A transport that refuses the `response_format` types named in `statuses`.

    Args:
        statuses: Status to answer each `response_format` type with.
        body: What a refusal says, for the tests about a server explaining itself.
            Defaults to naming the format, which is what the ladder reads a 400 for.

    Returns:
        The transport and the list every request body is recorded into, which is what
        lets a test assert on what was *sent* rather than only on what came back.
    """
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_body = json.loads(request.content)
        sent.append(request_body)
        declared = request_body.get("response_format") or {}
        status = statuses.get(str(declared.get("type", "none")), 200)
        # A refusing server names the thing it refused, and the ladder only trusts a 400
        # that does: an anonymous 400 is a request problem, not a capability signal.
        refusal = body or {"error": {"message": "response_format is not supported"}}
        return httpx.Response(status, json=_OK if status == 200 else refusal)

    return _transport(handler), sent


async def _complete(transport: httpx.MockTransport, **kwargs: object) -> str:
    return await client.complete(
        _ENDPOINT,
        None,
        "local-model",
        [{"role": "user", "content": "hi"}],
        transport=transport,
        **kwargs,
    )


async def test_a_caller_with_no_opinion_sends_neither_temperature_nor_a_format() -> None:
    """Chat must reach an endpoint exactly as it always did."""
    transport, sent = _recorder({})

    await _complete(transport)

    assert "temperature" not in sent[0]
    assert "response_format" not in sent[0]


async def test_temperature_zero_is_sent_rather_than_dropped_as_falsy() -> None:
    transport, sent = _recorder({})

    await _complete(transport, temperature=client.DETERMINISTIC_TEMPERATURE)

    assert sent[0]["temperature"] == 0.0


async def test_a_schema_is_sent_as_a_strict_json_schema_response_format() -> None:
    transport, sent = _recorder({})

    await _complete(transport, schema=_SCHEMA)

    declared = sent[0]["response_format"]
    assert declared["type"] == "json_schema"
    assert declared["json_schema"]["strict"] is True
    assert declared["json_schema"]["schema"] == _SCHEMA.schema


async def test_an_endpoint_refusing_a_schema_falls_back_to_json_object() -> None:
    transport, sent = _recorder({"json_schema": 400})

    answer = await _complete(transport, schema=_SCHEMA)

    assert answer == '{"answer": "yes"}'
    assert [body["response_format"]["type"] for body in sent] == ["json_schema", "json_object"]


async def test_an_endpoint_refusing_every_format_still_answers_unconstrained() -> None:
    """The last rung is what this module sent before any of this existed."""
    transport, sent = _recorder({"json_schema": 400, "json_object": 400})

    answer = await _complete(transport, schema=_SCHEMA)

    assert answer == '{"answer": "yes"}'
    assert "response_format" not in sent[-1]
    assert len(sent) == 3


async def test_a_refusal_is_remembered_so_the_next_call_does_not_pay_for_it_again() -> None:
    transport, sent = _recorder({"json_schema": 400})

    await _complete(transport, schema=_SCHEMA)
    await _complete(transport, schema=_SCHEMA)

    # Three requests, not four: the second call starts at the rung the first one landed on.
    assert [body["response_format"]["type"] for body in sent] == [
        "json_schema",
        "json_object",
        "json_object",
    ]


async def test_a_400_that_does_not_blame_the_format_is_an_error_not_a_demotion() -> None:
    """A 400 also means "the prompt does not fit the context window".

    That one is about this request, not this endpoint: retrying it weaker resends the
    same oversized prompt, and recording a demotion would permanently switch constrained
    decoding off because one document was long. Only a body that names the format - the
    way every refusing server does - is a capability signal.
    """
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            400, json={"error": {"message": "the request exceeds the context window"}}
        )

    transport = _transport(handler)

    with pytest.raises(UpstreamError):
        await _complete(transport, schema=_SCHEMA)
    with pytest.raises(UpstreamError):
        await _complete(transport, schema=_SCHEMA)

    # One request per call, both at the top rung: the ladder was neither walked nor
    # remembered, so a shorter document afterwards still gets the strict schema.
    assert [body["response_format"]["type"] for body in sent] == ["json_schema", "json_schema"]


async def test_a_400_with_no_format_left_to_drop_is_reported_rather_than_retried() -> None:
    """A 400 on the last rung is a real failure, not a capability signal."""
    transport, sent = _recorder({"json_schema": 400, "json_object": 400, "none": 400})

    with pytest.raises(UpstreamError):
        await _complete(transport, schema=_SCHEMA)

    assert [str(body.get("response_format", {}).get("type", "none")) for body in sent] == [
        "json_schema",
        "json_object",
        "none",
    ]


async def test_a_500_carrying_a_format_is_retried_weaker_but_not_remembered() -> None:
    """llama.cpp answers 500, not 400, when it cannot compile a schema into a grammar.

    It also answers 500 when the model failed to load, and the two are indistinguishable
    from here. So the weaker form is tried, and nothing is cached: one bad model load must
    not quietly downgrade every request for the rest of the process.
    """
    transport, sent = _recorder({"json_schema": 500})

    answer = await _complete(transport, schema=_SCHEMA)
    await _complete(transport, schema=_SCHEMA)

    assert answer == '{"answer": "yes"}'
    assert [body["response_format"]["type"] for body in sent] == [
        "json_schema",
        "json_object",
        # The second call starts at the top again, because a 500 was not evidence.
        "json_schema",
        "json_object",
    ]


async def test_a_failing_endpoints_own_words_reach_the_log_but_not_the_user(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bare 500 from llama.cpp is ambiguous, and its message is what resolves it.

    Without this, someone told to check the logs found a status code and a traceback that
    only said where Lyra was standing when the endpoint said no - never that the server
    was holding a model whose context window is smaller than the settings describe. The
    message still goes nowhere near the browser: it is the one string in the exchange that
    software Lyra does not control wrote.
    """
    transport, _ = _recorder(
        {"json_schema": 500, "json_object": 500, "none": 500},
        body={"error": {"message": "the request exceeds the available context size"}},
    )

    with (
        caplog.at_level(logging.WARNING, logger="backend.llm.client"),
        pytest.raises(UpstreamError) as caught,
    ):
        await _complete(transport, schema=_SCHEMA)

    assert "context size" not in caught.value.message
    assert any(
        "the request exceeds the available context size" in record.getMessage()
        for record in caplog.records
    )


async def test_an_endpoint_that_is_simply_down_still_reports_an_error() -> None:
    """The retry must not turn an outage into a hang or a silent empty answer."""
    transport, sent = _recorder({"json_schema": 500, "json_object": 500, "none": 500})

    with pytest.raises(UpstreamError):
        await _complete(transport, schema=_SCHEMA)

    assert len(sent) == 3


async def test_a_background_caller_gets_a_longer_deadline_than_a_chat_turn() -> None:
    """Extraction, segmentation, solving and transcription run in workers, not under a cursor.

    All four were on `CHAT_TIMEOUT`. Measured against a reasoning model, that number is
    simply wrong for them: one answer key spent 94 seconds thinking before its first
    character of JSON and two documents of the same size had not finished at 240, so the
    client hung up on work that was going to succeed.
    """
    assert client.BACKGROUND_TIMEOUT.read > client.CHAT_TIMEOUT.read

    seen: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout", {}).get("read"))
        return httpx.Response(200, json=_OK)

    await _complete(_transport(handler), request_timeout=client.BACKGROUND_TIMEOUT)
    await _complete(_transport(handler))

    assert seen == [client.BACKGROUND_TIMEOUT.read, client.CHAT_TIMEOUT.read]
