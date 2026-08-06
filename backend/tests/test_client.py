"""Contract tests for the tutor client, driven entirely through a stubbed transport."""

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


async def test_complete_returns_the_message_content() -> None:
    payload = {"choices": [{"message": {"role": "assistant", "content": '{"topics": []}'}}]}
    transport = _transport(lambda request: httpx.Response(200, json=payload))

    result = await client.complete(
        _ENDPOINT, None, "local-model", [{"role": "user", "content": "hi"}], transport=transport
    )

    assert result == '{"topics": []}'


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
    transport = _transport(lambda request: httpx.Response(500, json={"error": "no vision"}))

    support = await client.probe_vision_support(_ENDPOINT, None, "text-model", transport=transport)

    assert support.ok is False
    assert "does not accept images" in support.message


async def test_a_vision_probe_never_raises() -> None:
    """The settings screen renders the outcome, so an unreachable host is data too."""
    transport = _transport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")))

    support = await client.probe_vision_support(_ENDPOINT, None, None, transport=transport)

    assert support.ok is False
    assert support.message
