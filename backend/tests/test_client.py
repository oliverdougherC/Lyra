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


async def test_stream_chat_yields_only_deltas_and_stops_at_done() -> None:
    transport = _transport(lambda request: httpx.Response(200, text=_STREAM_BODY))

    chunks = [
        chunk
        async for chunk in client.stream_chat(
            _ENDPOINT, None, "local-model", [{"role": "user", "content": "hi"}], transport=transport
        )
    ]

    assert chunks == ["The ", "limit ", "is 2."]


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
