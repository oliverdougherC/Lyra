"""Contract tests for the tutor client, driven entirely through a stubbed transport."""

import json
import logging
from collections.abc import Callable

import httpx
import pytest

from backend.core.errors import ToolsUnsupportedError, UpstreamError
from backend.llm import client

_ENDPOINT = "http://127.0.0.1:8080/v1"
_API_KEY = "sk-lyra-not-a-real-value"

# One tool definition, the shape `complete_with_tools` carries on every guarded round.
_SCHEMA_TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "add",
        "description": "Add two numbers.",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
}

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


async def test_stream_chat_can_disable_template_level_thinking() -> None:
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
        enable_thinking=False,
    ):
        pass

    assert bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}


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


async def test_an_in_band_error_frame_fails_the_stream_without_echoing_the_server() -> None:
    """A mid-generation failure arrives as a `data: {"error": ...}` frame inside a 200.

    Reading it as keep-alive noise ended those streams looking like short but successful
    replies, so it must still fail the stream. But the server's own words are classified,
    not carried: a background caller (the writer pipeline) logs the resulting `LyraError`,
    and the body is attacker-controllable, so a sentinel in it must reach neither the
    user-facing message nor, through it, the log.
    """
    body = _body(
        '{"choices":[{"delta":{"content":"partial "}}]}',
        '{"error":{"message":"CUDA error: out of memory at /home/attacker/secret","code":500}}',
    )

    with pytest.raises(UpstreamError) as caught:
        await _collect(body)

    assert caught.value.message == client._ERROR_MIDREPLY
    assert "out of memory" not in caught.value.message
    assert "/home/attacker/secret" not in caught.value.message


async def test_a_mid_reply_context_overflow_is_named_in_lyras_own_words() -> None:
    """Running out of context window is the one mid-stream failure worth distinguishing.

    It is classified from the body and reported in Lyra's own words, so the reader learns
    the prompt was too long without the server's prose being copied into the message.
    """
    body = _body(
        '{"choices":[{"delta":{"content":"partial "}}]}',
        '{"error":{"message":"the request exceeds the available context size","code":500}}',
    )

    with pytest.raises(UpstreamError) as caught:
        await _collect(body)

    assert caught.value.message == client._ERROR_MIDREPLY_CONTEXT
    assert "context size" not in caught.value.message


async def test_a_json_frame_in_the_wrong_shape_is_an_unreadable_reply_not_a_crash() -> None:
    """The module's contract is that every failure becomes an `UpstreamError`.

    A frame whose `choices[0]` is not an object used to escape as a raw AttributeError,
    which no caller catches and no user can read.
    """
    with pytest.raises(UpstreamError) as caught:
        await _collect(_body('{"choices":["not an object"]}'))

    assert caught.value.message == "The tutor endpoint returned a response that could not be read."


async def test_a_stream_ending_without_done_preserves_text_and_reports_incomplete() -> None:
    deltas = []
    transport = _transport(
        lambda request: httpx.Response(
            200, text='data: {"choices":[{"delta":{"content":"cut"}}]}\n'
        )
    )
    with pytest.raises(client.StreamCompletionError, match="without confirmed completion"):
        async for delta in client.stream_chat(_ENDPOINT, None, "m", [], transport=transport):
            deltas.append(delta)
    assert _text(deltas, "answer") == "cut"


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


# --- complete_with_tools: the output ceiling and the two kinds of 400 (PLA-290) -------


async def test_complete_with_tools_sends_the_output_ceiling_and_flags_truncation() -> None:
    # The guarded loop passes the reserve as `max_tokens`; the client forwards it verbatim and
    # reports a `finish_reason: "length"` reply as truncated, so the loop can refuse to trust it.
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "cut"}, "finish_reason": "length"}]}
        )

    answer = await client.complete_with_tools(
        _ENDPOINT,
        None,
        "m",
        [{"role": "user", "content": "hi"}],
        [_SCHEMA_TOOL],
        transport=_transport(handler),
        max_tokens=1024,
    )

    assert sent[0]["max_tokens"] == 1024
    assert answer.truncated is True


async def test_complete_with_tools_reports_a_normal_reply_as_not_truncated() -> None:
    payload = {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}
    transport = _transport(lambda request: httpx.Response(200, json=payload))

    answer = await client.complete_with_tools(
        _ENDPOINT,
        None,
        "m",
        [{"role": "user", "content": "hi"}],
        [_SCHEMA_TOOL],
        transport=transport,
    )

    assert answer.truncated is False
    assert answer.content == "done"


async def test_a_first_request_context_400_raises_upstream_not_tools_unsupported() -> None:
    # An unknown endpoint tokenizer can reject a first request the local estimate admitted.
    # That 400 names the context window; it is an upstream failure, never a capability verdict.
    transport = _transport(
        lambda request: httpx.Response(
            400, json={"error": {"message": "the request exceeds the available context size"}}
        )
    )

    with pytest.raises(UpstreamError) as caught:
        await client.complete_with_tools(
            _ENDPOINT,
            None,
            "m",
            [{"role": "user", "content": "hi"}],
            [_SCHEMA_TOOL],
            transport=transport,
        )

    assert not isinstance(caught.value, ToolsUnsupportedError)
    assert getattr(caught.value, "upstream_status", None) == 400
    # The Lyra-written message stands in for the endpoint's own prose.
    assert "context size" not in caught.value.message


async def test_a_genuine_tools_400_still_raises_tools_unsupported() -> None:
    transport = _transport(
        lambda request: httpx.Response(
            400, json={"error": {"message": "this model does not support the tools parameter"}}
        )
    )

    with pytest.raises(ToolsUnsupportedError):
        await client.complete_with_tools(
            _ENDPOINT,
            None,
            "m",
            [{"role": "user", "content": "hi"}],
            [_SCHEMA_TOOL],
            transport=transport,
        )


async def test_a_context_400_body_is_classified_and_never_logged_or_returned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SECRET_/Users/student/thesis.pdf_sk-lyra-key"
    transport = _transport(
        lambda request: httpx.Response(
            400, json={"error": {"message": f"n_ctx too small; {marker}"}}
        )
    )

    with (
        caplog.at_level(logging.INFO, logger="backend.llm.client"),
        pytest.raises(UpstreamError) as caught,
    ):
        await client.complete_with_tools(
            _ENDPOINT,
            None,
            "m",
            [{"role": "user", "content": "hi"}],
            [_SCHEMA_TOOL],
            transport=transport,
        )

    assert marker not in caught.value.message
    log = "\n".join(record.getMessage() for record in caplog.records)
    assert marker not in log


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


async def test_non_streaming_completion_can_disable_template_level_thinking() -> None:
    transport, sent = _recorder({})

    await _complete(transport, enable_thinking=False)

    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False}


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


async def test_a_failing_endpoints_words_are_classified_not_copied_into_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bare 500 from llama.cpp is ambiguous, but its body is attacker-controllable.

    The useful part is the classification, not the prose: a context-window complaint and a
    schema complaint send the reader somewhere different. So the log records a bounded
    category and the HTTP status, and the server's own sentence - which a compromised
    endpoint could fill with a reflected API key or course text - reaches neither the log
    nor the user-facing message.
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
    log = "\n".join(record.getMessage() for record in caplog.records)
    assert "context size" not in log
    assert client._UPSTREAM_CONTEXT in log
    assert "500" in log


@pytest.mark.parametrize(
    ("body", "category"),
    [
        ("the request exceeds the available context size", client._UPSTREAM_CONTEXT),
        ("n_ctx is too small for this prompt", client._UPSTREAM_CONTEXT),
        ("response_format json_schema is not supported", client._UPSTREAM_FORMAT),
        ("could not compile the grammar", client._UPSTREAM_FORMAT),
        ("segmentation fault in worker 3", client._UPSTREAM_GENERIC),
        ("", client._UPSTREAM_GENERIC),
    ],
)
def test_upstream_bodies_map_to_bounded_categories(body: str, category: str) -> None:
    # Context is checked before format so a body mentioning both reads as the more specific
    # problem, and anything unrecognized is generic rather than guessed at.
    assert client._classify_upstream(body) == category


# Sentinels a compromised or buggy endpoint might reflect into an error body: course text,
# a bearer token, a private path, and a wall of arbitrary prose. None may appear in a log.
_SENTINELS = (
    "PHOTOSYNTHESIS_CHAPTER_SECRET",
    "Bearer sk-lyra-real-key-do-not-log",
    "/Users/student/Private/thesis.pdf",
    "Z" * 5000,
)


async def test_no_upstream_sentinel_reaches_the_log_on_a_non_streaming_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reflected = " ".join(_SENTINELS)
    transport, _ = _recorder(
        {"json_schema": 500, "json_object": 500, "none": 500},
        body={"error": {"message": reflected}},
    )

    with (
        caplog.at_level(logging.DEBUG, logger="backend.llm.client"),
        pytest.raises(UpstreamError),
    ):
        await _complete(transport, schema=_SCHEMA)

    log = "\n".join(record.getMessage() for record in caplog.records)
    for sentinel in _SENTINELS:
        assert sentinel not in log


async def test_no_upstream_sentinel_reaches_the_log_on_a_streaming_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The streaming path fails on a real HTTP status, which routes through the same mapper.
    # The key is sent as a header here too, to pin that neither it nor the body is logged.
    reflected = " ".join(_SENTINELS)
    transport = _transport(
        lambda request: httpx.Response(500, json={"error": {"message": reflected}})
    )

    with (
        caplog.at_level(logging.DEBUG, logger="backend.llm.client"),
        pytest.raises(UpstreamError),
    ):
        async for _ in client.stream_chat(
            _ENDPOINT,
            _API_KEY,
            "local-model",
            [{"role": "user", "content": "hi"}],
            transport=transport,
        ):
            pass

    log = "\n".join(record.getMessage() for record in caplog.records)
    for sentinel in _SENTINELS:
        assert sentinel not in log
    assert _API_KEY not in log


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


async def test_tool_stream_emits_live_reasoning_and_reassembles_interleaved_calls() -> None:
    """An explicit reasoning field makes the content literal (PLA-505).

    This fixture once asserted that an inline `think` block *and* a `reasoning_content`
    field both feed the reasoning channel. The endpoint already carries the thought in
    its own field, so its content is answer text byte for byte: the inline tag here is
    part of the answer, and the old expectation is exactly the double-encoding the fix
    removes. The interleaved tool-call assembly it exists to prove is unchanged.
    """
    seen: list[client.StreamDelta] = []
    chunks = [
        {"reasoning_content": "Checking"},
        {"content": "<thi"},
        {"content": "nk>math</think>One "},
        {
            "tool_calls": [
                {"index": 1, "id": "b", "function": {"name": "sec", "arguments": '{"b":'}},
                {"index": 0, "id": "a", "function": {"name": "fir", "arguments": '{"a":'}},
            ]
        },
        {
            "content": "moment",
            "tool_calls": [
                {"index": 0, "function": {"name": "st", "arguments": "1}"}},
                {"index": 1, "function": {"name": "ond", "arguments": "2}"}},
            ],
        },
    ]

    class LiveStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index, delta in enumerate(chunks):
                if index == 1:
                    assert seen == [client.StreamDelta("reasoning", "Checking")]
                yield ("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n").encode()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
            yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, stream=LiveStream())

    result = await client.complete_with_tools(
        _ENDPOINT,
        None,
        None,
        [],
        [_SCHEMA_TOOL],
        transport=_transport(handler),
        on_delta=seen.append,
    )
    # The field already carried the thought, so the inline tag stayed in the answer.
    assert result.content == _T_OPEN + "math" + _T_CLOSE + "One moment"
    assert result.tool_calls == (
        client.ToolCall("a", "first", '{"a":1}'),
        client.ToolCall("b", "second", '{"b":2}'),
    )
    assert "".join(item.text for item in seen if item.channel == "reasoning") == "Checking"
    assert "".join(item.text for item in seen if item.channel == "answer") == result.content
    assert not result.truncated


@pytest.mark.parametrize("finish", ["stop", "length"])
async def test_tool_stream_preserves_truncation(finish: str) -> None:
    body = _body(
        json.dumps({"choices": [{"delta": {"content": "partial"}, "finish_reason": finish}]})
    )
    result = await client.complete_with_tools(
        _ENDPOINT,
        None,
        None,
        [],
        [_SCHEMA_TOOL],
        transport=_transport(lambda request: httpx.Response(200, text=body)),
        on_delta=lambda delta: None,
    )
    assert result.truncated is (finish == "length")


@pytest.mark.parametrize(
    "body",
    [
        'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
        _body('{"error":{"message":"private context overflow"}}'),
    ],
)
async def test_tool_stream_rejects_missing_done_and_in_band_failure(body: str) -> None:
    with pytest.raises(UpstreamError):
        await client.complete_with_tools(
            _ENDPOINT,
            None,
            None,
            [],
            [_SCHEMA_TOOL],
            transport=_transport(lambda request: httpx.Response(200, text=body)),
            on_delta=lambda delta: None,
        )


@pytest.mark.parametrize(
    ("detail", "error_type"),
    [("tools unsupported", ToolsUnsupportedError), ("context overflow", UpstreamError)],
)
async def test_tool_stream_keeps_status_classification(detail: str, error_type: type) -> None:
    with pytest.raises(error_type):
        await client.complete_with_tools(
            _ENDPOINT,
            None,
            None,
            [],
            [_SCHEMA_TOOL],
            transport=_transport(
                lambda request: httpx.Response(400, json={"error": {"message": detail}})
            ),
            on_delta=lambda delta: None,
        )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            'data: {"choices":[{"delta":{"content":"partial"}}]}\n',
            "The tutor endpoint failed partway through the reply.",
        ),
        (
            _body('{"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}'),
            "The tutor endpoint's reply hit the output-token ceiling and was cut off "
            "before it finished.",
        ),
    ],
)
async def test_strict_chat_stream_rejects_incomplete_answers(body: str, message: str) -> None:
    with pytest.raises(UpstreamError) as caught:
        _ = [
            delta
            async for delta in client.stream_chat(
                _ENDPOINT,
                None,
                None,
                [],
                transport=_transport(lambda request: httpx.Response(200, text=body)),
                require_complete=True,
            )
        ]
    assert caught.value.message == message


async def test_strict_chat_stream_keeps_complete_reasoning_and_answer_deltas() -> None:
    body = _body(
        '{"choices":[{"delta":{"reasoning_content":"Thought"}}]}',
        '{"choices":[{"delta":{"content":"Answer"}}]}',
        '{"choices":[{"delta":{},"finish_reason":"stop"}]}',
    )
    deltas = [
        delta
        async for delta in client.stream_chat(
            _ENDPOINT,
            None,
            None,
            [],
            transport=_transport(lambda request: httpx.Response(200, text=body)),
            require_complete=True,
        )
    ]
    assert deltas == [
        client.StreamDelta("reasoning", "Thought"),
        client.StreamDelta("answer", "Answer"),
    ]


async def test_default_chat_stream_delivers_partial_text_then_reports_truncation() -> None:
    body = _body('{"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}')
    seen: list[client.StreamDelta] = []
    with pytest.raises(client.StreamCompletionError) as caught:
        async for delta in client.stream_chat(
            _ENDPOINT,
            None,
            None,
            [],
            transport=_transport(lambda request: httpx.Response(200, text=body)),
        ):
            seen.append(delta)
    assert seen == [client.StreamDelta("answer", "partial")]
    assert caught.value.outcome == "length"


@pytest.mark.parametrize("choices", [{"private": "upstream detail"}, 1, "invalid"])
@pytest.mark.parametrize("tools", [False, True])
async def test_malformed_stream_choices_raise_bounded_upstream_error(choices, tools) -> None:
    body = _body(json.dumps({"choices": choices}))
    transport = _transport(lambda request: httpx.Response(200, text=body))
    with pytest.raises(UpstreamError) as caught:
        if tools:
            await client.complete_with_tools(
                _ENDPOINT,
                None,
                None,
                [],
                [_SCHEMA_TOOL],
                transport=transport,
                on_delta=lambda delta: None,
            )
        else:
            _ = [
                delta
                async for delta in client.stream_chat(
                    _ENDPOINT, None, None, [], transport=transport, require_complete=True
                )
            ]
    assert caught.value.message == client._ERROR_UNREADABLE


# --------------------------------------------------------------------------------------
# PLA-505: legacy tags are recognized only at the head of the stream; a stream that
# carries the provider's own reasoning field keeps its content literal.
#
# Every tag below is assembled from code points (chr(60) is the angle bracket): a tag
# written literally into this file is rewritten by the reasoning-delimiter parser before
# it reaches disk, so the real ASCII tags exist only at runtime.


_T_OPEN = chr(60) + "think" + chr(62)
_T_CLOSE = chr(60) + "/" + "think" + chr(62)
_TT_OPEN = chr(60) + "thinking" + chr(62)
_TT_CLOSE = chr(60) + "/" + "thinking" + chr(62)


def _split(text: str, chunk: int = 1) -> tuple[str, str]:
    """Feed one production splitter `chunk` characters at a time and join each channel."""
    splitter = client._ReasoningTagSplitter()
    answer: list[str] = []
    reasoning: list[str] = []
    for start in range(0, len(text), chunk):
        for delta in splitter.feed(text[start : start + chunk]):
            (answer if delta.channel == "answer" else reasoning).append(delta.text)
    for delta in splitter.flush():
        (answer if delta.channel == "answer" else reasoning).append(delta.text)
    return "".join(answer), "".join(reasoning)


def _content_frame(text: str) -> str:
    return json.dumps({"choices": [{"delta": {"content": text}}]})


def _reasoning_frame(text: str) -> str:
    return json.dumps({"choices": [{"delta": {"reasoning_content": text}}]})


def _tool_frame(call_id: str, name: str, arguments: str, finish: str | None = None) -> str:
    choice: dict[str, object] = {
        "delta": {
            "tool_calls": [
                {"index": 0, "id": call_id, "function": {"name": name, "arguments": arguments}}
            ]
        }
    }
    if finish is not None:
        choice["finish_reason"] = finish
    return json.dumps({"choices": [choice]})


# (content, expected answer bytes, expected reasoning bytes)
_SPLIT_CASES = [
    (
        _T_OPEN + "Weigh the cases." + _T_CLOSE + "The answer is 2x.",
        "The answer is 2x.",
        "Weigh the cases.",
    ),
    (
        _TT_OPEN + "Long form." + _TT_CLOSE + "The answer is 2x.",
        "The answer is 2x.",
        "Long form.",
    ),
    ("\n  " + _T_OPEN + "Weigh." + _T_CLOSE + "Answer", "\n  Answer", "Weigh."),
    (
        "The XML tag " + _T_OPEN + " is literal text, not a reasoning channel.",
        "The XML tag " + _T_OPEN + " is literal text, not a reasoning channel.",
        "",
    ),
    (
        "I will show the tag " + _T_OPEN + " but never close it.",
        "I will show the tag " + _T_OPEN + " but never close it.",
        "",
    ),
    (
        "Here is the example:\n```\n" + _T_OPEN + "hello\n```\nDone.",
        "Here is the example:\n```\n" + _T_OPEN + "hello\n```\nDone.",
        "",
    ),
    (
        'Say "' + _T_OPEN + '" out loud.',
        'Say "' + _T_OPEN + '" out loud.',
        "",
    ),
    (
        _T_OPEN
        + "A"
        + _T_CLOSE
        + "First. "
        + _T_OPEN
        + "B"
        + _T_CLOSE
        + "C"
        + _T_CLOSE
        + "Second.",
        "First. " + _T_OPEN + "B" + _T_CLOSE + "C" + _T_CLOSE + "Second.",
        "A",
    ),
    (_T_OPEN + "Cut off here", "", "Cut off here"),
    (_T_OPEN + "partial" + _T_CLOSE[:6], "", "partial" + _T_CLOSE[:6]),
]


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 13])
@pytest.mark.parametrize(("text", "answer", "reasoning"), _SPLIT_CASES)
def test_tag_policy_is_independent_of_where_the_chunks_break(
    text: str, answer: str, reasoning: str, chunk: int
) -> None:
    # Character-by-character already covers every split around a delimiter; the larger
    # chunk sizes repeat the exact-bytes assertion at other boundaries.
    assert _split(text, chunk) == (answer, reasoning)


def _explicit_frame(field: str, value: object, content: str | None = None) -> str:
    """One delta frame carrying a reasoning field, optionally alongside content."""
    delta: dict[str, object] = {field: value}
    if content is not None:
        delta["content"] = content
    return json.dumps({"choices": [{"delta": delta}]})


def test_reasoning_channel_separates_presence_from_text() -> None:
    # Presence is any recognized field holding a string - including the empty string;
    # text is the first non-empty string in field order. Null, non-string, and missing
    # fields establish no channel at all, so the legacy tag policy still applies.
    assert client._reasoning_channel({"reasoning_content": ""}) == (True, "")
    assert client._reasoning_channel({"reasoning": "A.", "thinking": "B."}) == (True, "A.")
    assert client._reasoning_channel({"reasoning_content": "", "thinking": "B."}) == (True, "B.")
    assert client._reasoning_channel({"reasoning_content": None}) == (False, "")
    assert client._reasoning_channel({"reasoning_content": 5}) == (False, "")
    assert client._reasoning_channel({"reasoning": None, "thinking": None}) == (False, "")
    assert client._reasoning_channel({}) == (False, "")
    assert client._reasoning_channel({"content": "Only content."}) == (False, "")


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "thinking"])
async def test_an_empty_reasoning_string_commits_the_channel_and_keeps_a_leading_tag_literal(
    field: str,
) -> None:
    # The F4 shape: the endpoint supplied its channel (a string, here the empty one), so
    # a tag opening the content is literal answer text, not a reasoning block to strip.
    sentence = _T_OPEN + "Draft." + _T_CLOSE + "Final."
    deltas = await _collect(_body(_explicit_frame(field, "", content=sentence)))

    assert _text(deltas, "reasoning") == ""
    assert _text(deltas, "answer") == sentence
    # An empty fragment is absence of text, not an empty delta to publish.
    assert all(delta.text for delta in deltas)


async def test_an_empty_reasoning_frame_commits_the_channel_before_the_content_arrives() -> None:
    sentence = _T_OPEN + "Draft." + _T_CLOSE + "Final."
    deltas = await _collect(
        _body(_explicit_frame("reasoning_content", ""), _content_frame(sentence))
    )

    assert _text(deltas, "reasoning") == ""
    assert _text(deltas, "answer") == sentence
    assert all(delta.text for delta in deltas)


async def test_reasoning_text_still_arrives_when_an_earlier_frame_was_empty() -> None:
    sentence = "The tag " + _T_OPEN + " stays answer."
    deltas = await _collect(
        _body(
            _explicit_frame("reasoning_content", ""),
            _explicit_frame("thinking", "Late."),
            _content_frame(sentence),
        )
    )

    assert _text(deltas, "reasoning") == "Late."
    assert _text(deltas, "answer") == sentence
    assert all(delta.text for delta in deltas)


async def test_a_null_reasoning_field_does_not_commit_the_channel() -> None:
    # Null is "no reasoning in this frame", not "the channel exists": a legacy leading
    # block still splits, and a null field must not permanently disable tag recognition.
    deltas = await _collect(
        _body(
            _explicit_frame("reasoning_content", None),
            _content_frame(_T_OPEN + "Deliberating." + _T_CLOSE + "Answer."),
        )
    )

    assert _text(deltas, "reasoning") == "Deliberating."
    assert _text(deltas, "answer") == "Answer."


async def test_a_non_string_reasoning_field_does_not_commit_the_channel() -> None:
    deltas = await _collect(
        _body(
            _explicit_frame("reasoning_content", 5),
            _content_frame(_T_OPEN + "Deliberating." + _T_CLOSE + "Answer."),
        )
    )

    assert _text(deltas, "reasoning") == "Deliberating."
    assert _text(deltas, "answer") == "Answer."


async def test_an_empty_field_releases_a_held_tag_prefix_as_answer_text() -> None:
    # The stream opened the way a legacy block does, so the prefix is held; the channel
    # then commits with an empty string, and the held prefix is answer text from that
    # frame on - neither lost nor duplicated as the rest of the tag arrives.
    sentence = _T_OPEN + " is literal."
    deltas = await _collect(
        _body(
            _content_frame(_T_OPEN[:4]),
            _explicit_frame("reasoning_content", ""),
            _content_frame(_T_OPEN[4:] + " is literal."),
        )
    )

    assert _text(deltas, "reasoning") == ""
    assert _text(deltas, "answer") == sentence
    assert all(delta.text for delta in deltas)


async def test_an_empty_field_after_a_committed_block_keeps_the_block_reasoning() -> None:
    # The block was committed to reasoning before the field arrived, so an empty field
    # cannot unlock that decision; it only settles the rest of the content as literal.
    deltas = await _collect(
        _body(
            _content_frame(_T_OPEN + "Deliberation." + _T_CLOSE),
            _explicit_frame("thinking", ""),
            _content_frame("Answer."),
        )
    )

    assert _text(deltas, "reasoning") == "Deliberation."
    assert _text(deltas, "answer") == "Answer."


async def test_a_literal_tag_late_in_the_answer_stays_in_the_answer() -> None:
    """The reported corruption, reproduced one character at a time through the stream."""
    sentence = "The XML tag " + _T_OPEN + " is literal text, not a reasoning channel."
    frames = [_content_frame(ch) for ch in sentence]
    deltas = await _collect(_body(*frames))

    assert _text(deltas, "answer") == sentence
    assert _text(deltas, "reasoning") == ""


async def test_a_fenced_example_with_a_tag_is_preserved() -> None:
    fenced = "Here is the example:\n```\n" + _T_OPEN + "hello\n```\nDone."
    deltas = await _collect(
        _body(
            _content_frame("Here is the example:\n"),
            _content_frame("```\n" + _T_OPEN + "hello\n```\n"),
            _content_frame("Done."),
        )
    )

    assert _text(deltas, "answer") == fenced
    assert _text(deltas, "reasoning") == ""


async def test_explicit_reasoning_makes_content_literal_even_with_a_leading_tag() -> None:
    sentence = "The tag " + _T_OPEN + " stays in the answer."
    deltas = await _collect(
        _body(
            _reasoning_frame("Explicit thought."),
            _content_frame(sentence),
        )
    )

    assert _text(deltas, "reasoning") == "Explicit thought."
    assert _text(deltas, "answer") == sentence


async def test_content_beginning_with_a_literal_tag_stays_answer_when_a_field_is_present() -> None:
    # The field is present, so a tag that opens the content is literal text, not a
    # leading reasoning block.
    sentence = _T_OPEN + "Draft." + _T_CLOSE + "Final."
    deltas = await _collect(
        _body(
            _reasoning_frame("Explicit thought."),
            _content_frame(sentence),
        )
    )

    assert _text(deltas, "reasoning") == "Explicit thought."
    assert _text(deltas, "answer") == sentence


async def test_a_late_explicit_field_does_not_reclassify_emitted_content() -> None:
    sentence = "Opening words. Now " + _T_OPEN + " is literal."
    deltas = await _collect(
        _body(
            _content_frame("Opening words. "),
            _reasoning_frame("Late thought."),
            _content_frame("Now " + _T_OPEN + " is literal."),
        )
    )

    assert _text(deltas, "reasoning") == "Late thought."
    assert _text(deltas, "answer") == sentence


async def test_a_leading_block_opened_before_a_late_explicit_field_closes_normally() -> None:
    # The block was committed to reasoning before the field arrived, so it is not
    # reclassified; the field still arrives, and text after the close is literal.
    deltas = await _collect(
        _body(
            _content_frame(_T_OPEN + "Commi"),
            _content_frame("tted." + _T_CLOSE),
            _reasoning_frame("Late"),
            _content_frame("More answer."),
        )
    )

    assert _text(deltas, "reasoning") == "Committed.Late"
    assert _text(deltas, "answer") == "More answer."


async def test_an_explicit_field_releases_a_held_tag_prefix_as_answer_text() -> None:
    sentence = _T_OPEN + " is literal."
    deltas = await _collect(
        _body(
            _content_frame(_T_OPEN[:4]),
            _reasoning_frame("Late."),
            _content_frame(_T_OPEN[4:] + " is literal."),
        )
    )

    assert _text(deltas, "reasoning") == "Late."
    assert _text(deltas, "answer") == sentence


async def test_a_leading_block_with_leading_whitespace_keeps_the_whitespace() -> None:
    deltas = await _collect(
        _body(
            _content_frame("\n  " + _T_OPEN + "Draft." + _T_CLOSE),
            _content_frame("Answer."),
        )
    )

    assert deltas == [
        client.StreamDelta("answer", "\n  "),
        client.StreamDelta("reasoning", "Draft."),
        client.StreamDelta("answer", "Answer."),
    ]


async def test_tool_rounds_split_only_a_leading_block_and_keep_explicit_content_literal() -> None:
    # Three consecutive verification-loop rounds, each with its own endpoint reply.
    # Round one has no explicit field, so a leading inline block is split; rounds two
    # and three carry the thought in a field, so their content is literal byte for byte.
    async def run_round(body: str) -> tuple[client.AssistantMessage, list[client.StreamDelta]]:
        seen: list[client.StreamDelta] = []
        result = await client.complete_with_tools(
            _ENDPOINT,
            None,
            None,
            [],
            [_SCHEMA_TOOL],
            transport=_transport(lambda request: httpx.Response(200, text=body)),
            on_delta=seen.append,
        )
        return result, seen

    first, seen1 = await run_round(
        _body(
            _content_frame(_T_OPEN + "Check 2+3." + _T_CLOSE + "Let me verify "),
            _content_frame("with the tool."),
            _tool_frame("c1", "add", '{"a": 2, "b": 3}', finish="tool_calls"),
        )
    )
    assert first.content == "Let me verify with the tool."
    assert first.tool_calls == (client.ToolCall("c1", "add", '{"a": 2, "b": 3}'),)
    assert _text(seen1, "reasoning") == "Check 2+3."
    assert _text(seen1, "answer") == first.content

    second_sentence = "The tag " + _T_OPEN + " is literal."
    second, seen2 = await run_round(
        _body(
            _reasoning_frame("Explicit round two."),
            _content_frame(second_sentence),
            _tool_frame("c2", "add", '{"a": 5}', finish="tool_calls"),
        )
    )
    assert second.content == second_sentence
    assert second.tool_calls == (client.ToolCall("c2", "add", '{"a": 5}'),)
    assert _text(seen2, "reasoning") == "Explicit round two."
    assert _text(seen2, "answer") == second.content

    third, seen3 = await run_round(
        _body(
            _reasoning_frame("Explicit final."),
            _content_frame("Final: 5."),
            '{"choices":[{"delta":{},"finish_reason":"stop"}]}',
        )
    )
    assert third.content == "Final: 5."
    assert third.tool_calls == ()
    assert not third.truncated
    assert _text(seen3, "reasoning") == "Explicit final."
    assert _text(seen3, "answer") == third.content


async def test_a_tool_stream_with_an_empty_reasoning_field_keeps_content_literal() -> None:
    # Same F4 shape through the verification loop: the empty field commits the channel,
    # so the leading tag in the live text is answer, and no empty delta is published.
    seen: list[client.StreamDelta] = []
    chunks = [
        {"reasoning_content": ""},
        {"content": _T_OPEN + "math" + _T_CLOSE + "One "},
        {
            "tool_calls": [
                {"index": 0, "id": "a", "function": {"name": "add", "arguments": '{"a": 1'}},
            ]
        },
        {
            "content": "moment",
            "tool_calls": [
                {"index": 0, "function": {"arguments": ', "b": 2}'}},
            ],
        },
    ]

    class LiveStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for delta in chunks:
                yield ("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n").encode()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
            yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, stream=LiveStream())

    result = await client.complete_with_tools(
        _ENDPOINT,
        None,
        None,
        [],
        [_SCHEMA_TOOL],
        transport=_transport(handler),
        on_delta=seen.append,
    )

    assert result.content == _T_OPEN + "math" + _T_CLOSE + "One moment"
    assert result.tool_calls == (client.ToolCall("a", "add", '{"a": 1, "b": 2}'),)
    assert _text(seen, "reasoning") == ""
    assert all(delta.text for delta in seen)
    assert "".join(item.text for item in seen if item.channel == "answer") == result.content


async def test_a_tool_stream_with_a_null_reasoning_field_still_splits_a_leading_block() -> None:
    seen: list[client.StreamDelta] = []
    chunks = [
        {"reasoning_content": None},
        {"content": _T_OPEN + "Check 2+3." + _T_CLOSE + "Let me verify "},
        {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "add", "arguments": "{}"}}]},
    ]

    class LiveStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for delta in chunks:
                yield ("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n").encode()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
            yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=LiveStream())

    result = await client.complete_with_tools(
        _ENDPOINT,
        None,
        None,
        [],
        [_SCHEMA_TOOL],
        transport=_transport(handler),
        on_delta=seen.append,
    )

    assert result.content == "Let me verify "
    assert _text(seen, "reasoning") == "Check 2+3."
    assert _text(seen, "answer") == result.content


def test_strip_reasoning_keeps_tags_that_are_not_at_the_head() -> None:
    # Mid-message tags are answer text, in prose, in inline code, and inside fences.
    prose = "Answer is 2. (" + _T_OPEN + " is literal.)"
    assert client.strip_reasoning(prose) == prose
    fenced = "```\n" + _T_OPEN + "hello\n```"
    assert client.strip_reasoning(fenced) == fenced
    unclosed = "Unclosed " + _T_OPEN + " at the end."
    assert client.strip_reasoning(unclosed) == unclosed


def test_strip_reasoning_preserves_content_verbatim_when_literal() -> None:
    # The message already carried the provider's reasoning field, so the content stands.
    content = "  " + _T_OPEN + "Deliberating." + _T_CLOSE + "JSON"
    stripped = client.strip_reasoning(content, literal=True)
    assert stripped == _T_OPEN + "Deliberating." + _T_CLOSE + "JSON"


def test_strip_reasoning_still_drops_an_unclosed_leading_block() -> None:
    assert client.strip_reasoning(_T_OPEN + "deliberation") == ""


async def test_complete_keeps_content_literal_when_the_message_carries_a_reasoning_field() -> None:
    content = "The tag " + _T_OPEN + " stays literal."
    message = {"content": content, "reasoning_content": "The thinking."}
    payload = {"choices": [{"message": message}]}
    transport = _transport(lambda request: httpx.Response(200, json=payload))

    result = await client.complete(
        _ENDPOINT, None, "local-model", [{"role": "user", "content": "hi"}], transport=transport
    )

    assert result == content


async def test_complete_still_strips_a_leading_block_when_no_field_is_present() -> None:
    content = _T_OPEN + "Deliberating." + _T_CLOSE + '\n{"topics": []}'
    payload = {"choices": [{"message": {"content": content}}]}
    transport = _transport(lambda request: httpx.Response(200, json=payload))

    result = await client.complete(
        _ENDPOINT, None, "local-model", [{"role": "user", "content": "hi"}], transport=transport
    )

    assert result == '{"topics": []}'


async def test_non_streaming_tool_turn_applies_the_same_field_policy() -> None:
    # With the field, the content is literal; without it, a leading block is still dropped.
    literal_content = "Use " + _T_OPEN + " verbatim."
    leading_block = _T_OPEN + "Deliberating." + _T_CLOSE + '\n{"topics": []}'
    with_field = {
        "choices": [
            {
                "message": {"content": literal_content, "thinking": "Thought."},
                "finish_reason": "stop",
            }
        ]
    }
    transport = _transport(lambda request: httpx.Response(200, json=with_field))
    answer = await client.complete_with_tools(
        _ENDPOINT,
        None,
        "m",
        [{"role": "user", "content": "hi"}],
        [_SCHEMA_TOOL],
        transport=transport,
    )
    assert answer.content == literal_content

    without_field = {
        "choices": [
            {
                "message": {"content": leading_block},
                "finish_reason": "stop",
            }
        ]
    }
    transport = _transport(lambda request: httpx.Response(200, json=without_field))
    answer = await client.complete_with_tools(
        _ENDPOINT,
        None,
        "m",
        [{"role": "user", "content": "hi"}],
        [_SCHEMA_TOOL],
        transport=transport,
    )
    assert answer.content == '{"topics": []}'


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "thinking"])
async def test_complete_keeps_content_literal_for_an_empty_reasoning_field(field: str) -> None:
    # The F4 shape, non-streaming: an empty string channel means the content, including a
    # literal leading `think` block, stands as-is.
    content = _T_OPEN + "Draft." + _T_CLOSE + "Final."
    payload = {"choices": [{"message": {"content": content, field: ""}, "finish_reason": "stop"}]}
    transport = _transport(lambda request: httpx.Response(200, json=payload))

    result = await client.complete(
        _ENDPOINT, None, "local-model", [{"role": "user", "content": "hi"}], transport=transport
    )

    assert result == content


@pytest.mark.parametrize("value", [None, 5])
async def test_complete_still_strips_a_leading_block_when_the_field_is_not_a_string(value) -> None:
    # A null or non-string field establishes no channel, so the legacy leading-block
    # policy still strips it; only a string value, even an empty one, commits the channel.
    content = _T_OPEN + "Deliberating." + _T_CLOSE + "Final."
    payload = {
        "choices": [
            {"message": {"content": content, "reasoning_content": value}, "finish_reason": "stop"}
        ]
    }
    transport = _transport(lambda request: httpx.Response(200, json=payload))

    result = await client.complete(
        _ENDPOINT, None, "local-model", [{"role": "user", "content": "hi"}], transport=transport
    )

    assert result == "Final."


async def test_non_streaming_tool_turn_treats_an_empty_reasoning_field_as_literal() -> None:
    content = _T_OPEN + "Draft." + _T_CLOSE + "Final."
    payload = {
        "choices": [
            {"message": {"content": content, "reasoning_content": ""}, "finish_reason": "stop"}
        ]
    }
    transport = _transport(lambda request: httpx.Response(200, json=payload))
    answer = await client.complete_with_tools(
        _ENDPOINT,
        None,
        "m",
        [{"role": "user", "content": "hi"}],
        [_SCHEMA_TOOL],
        transport=transport,
    )
    assert answer.content == content


async def test_non_streaming_tool_turn_still_strips_a_leading_block_for_a_null_field() -> None:
    content = _T_OPEN + "Deliberating." + _T_CLOSE + "Final."
    payload = {
        "choices": [
            {"message": {"content": content, "reasoning_content": None}, "finish_reason": "stop"}
        ]
    }
    transport = _transport(lambda request: httpx.Response(200, json=payload))
    answer = await client.complete_with_tools(
        _ENDPOINT,
        None,
        "m",
        [{"role": "user", "content": "hi"}],
        [_SCHEMA_TOOL],
        transport=transport,
    )
    assert answer.content == "Final."
