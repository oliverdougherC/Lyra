"""Exa client tests use mock transport and fake DNS only by default."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Callable

import httpx
import pytest

from backend.core.exa import (
    CONTENTS_PATH,
    DEFAULT_MAX_HIGHLIGHT_CHARS,
    DEFAULT_MAX_TEXT_CHARS,
    DEFAULT_SEARCH_RESULTS,
    SEARCH_PATH,
    ExaAuthError,
    ExaClient,
    ExaConnectionInterruptedError,
    ExaOfflineError,
    ExaPermissionError,
    ExaQuotaExceededError,
    ExaRateLimitError,
    ExaSchemaError,
    ExaTimeoutError,
    ExaTransientError,
)

RESOLUTIONS = {
    "public.example.test": ["93.184.216.34"],
    "private.example.test": ["10.0.0.12"],
}


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, port: object, *args: object, **kwargs: object) -> list[tuple]:
        try:
            addresses = RESOLUTIONS[host]
        except KeyError:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known") from None
        return [
            (socket.AF_UNSPEC, socket.SOCK_STREAM, 0, "", (address, int(port or 0)))
            for address in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> ExaClient:
    return ExaClient(
        api_key="exa-secret",
        client=httpx.Client(
            base_url="https://api.exa.ai",
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ),
        resolver=socket.getaddrinfo,
        sleep=lambda _seconds: None,
        random_float=lambda: 0.0,
    )


def test_search_sends_bounded_request_and_filters_invalid_results() -> None:
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == SEARCH_PATH
        assert request.headers["x-api-key"] == "exa-secret"
        sent.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            request=request,
            json={
                "requestId": "req-1",
                "results": [
                    {
                        "title": "First result",
                        "url": "https://public.example.test/a",
                        "id": "doc-1",
                        "author": "Ada",
                        "publishedDate": "2026-08-30",
                        "highlights": ["Useful detail"],
                    },
                    {
                        "title": "Private result",
                        "url": "http://private.example.test/secret",
                        "highlights": ["skip me"],
                    },
                    {
                        "title": "Duplicate",
                        "url": "https://public.example.test/a",
                    },
                ],
            },
        )

    request_id, results = _client(handler).search(
        "phase four research",
        contents={"highlights": {"query": "phase four research"}},
    )

    assert request_id == "req-1"
    assert sent == {
        "query": "phase four research",
        "type": "auto",
        "numResults": DEFAULT_SEARCH_RESULTS,
        "contents": {
            "highlights": {
                "query": "phase four research",
                "maxCharacters": DEFAULT_MAX_HIGHLIGHT_CHARS,
            }
        },
    }
    assert len(results) == 1
    assert results[0].url == "https://public.example.test/a"
    assert results[0].highlights == ("Useful detail",)
    assert results[0].provider == "exa"


def test_contents_sends_bounded_request_and_surfaces_per_url_statuses() -> None:
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == CONTENTS_PATH
        sent.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            request=request,
            json={
                "requestId": "req-2",
                "results": [
                    {
                        "id": "https://public.example.test/a",
                        "url": "https://public.example.test/a",
                        "title": "Study page",
                        "text": "Important finding",
                        "publishedDate": "2026-08-30",
                        "author": "Ada",
                    }
                ],
                "statuses": [
                    {
                        "id": "https://public.example.test/a",
                        "status": "success",
                        "source": "livecrawl",
                    },
                    {
                        "id": "https://public.example.test/b",
                        "status": "error",
                        "source": "livecrawl",
                        "error": {
                            "tag": "CRAWL_NOT_FOUND",
                            "message": "page missing",
                        },
                    },
                ],
            },
        )

    request_id, results = _client(handler).contents(
        ["https://public.example.test/a", "https://public.example.test/b"],
        text=True,
        highlights=False,
        max_text_chars=4000,
    )

    assert request_id == "req-2"
    assert sent == {
        "urls": [
            "https://public.example.test/a",
            "https://public.example.test/b",
        ],
        "text": {"maxCharacters": 4000},
        "highlights": False,
    }
    assert [result.status for result in results] == ["success", "error"]
    assert results[0].text == "Important finding"
    assert results[0].source == "livecrawl"
    assert results[1].error_tag == "CRAWL_NOT_FOUND"
    assert results[1].error_message == "page missing"


def test_schema_and_oversized_response_failures_are_mapped() -> None:
    malformed = _client(lambda request: httpx.Response(200, request=request, content=b"{"))
    with pytest.raises(ExaSchemaError):
        malformed.search("phase four research", contents={"highlights": True})

    wrong_shape = _client(
        lambda request: httpx.Response(
            200,
            request=request,
            json={"requestId": "req", "results": {}},
        )
    )
    with pytest.raises(ExaSchemaError):
        wrong_shape.search("phase four research", contents={"highlights": True})

    def huge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-length": str(2_000_000)},
            content=b"{}",
        )

    with pytest.raises(ExaTransientError):
        _client(huge).check_readiness()


def test_auth_quota_permission_and_rate_limit_failures_are_mapped() -> None:
    with pytest.raises(ExaAuthError):
        _client(
            lambda request: httpx.Response(401, request=request, json={"error": "bad key"})
        ).check_readiness()

    with pytest.raises(ExaQuotaExceededError):
        _client(
            lambda request: httpx.Response(402, request=request, json={"error": "no credits"})
        ).check_readiness()

    with pytest.raises(ExaPermissionError):
        _client(
            lambda request: httpx.Response(403, request=request, json={"error": "feature disabled"})
        ).check_readiness()

    with pytest.raises(ExaRateLimitError):
        _client(
            lambda request: httpx.Response(429, request=request, json={"error": "slow down"})
        ).check_readiness()


def test_retryable_failures_retry_before_succeeding() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, request=request, json={"error": "busy"})
        return httpx.Response(200, request=request, json={"requestId": "req", "results": []})

    assert _client(handler).check_readiness() == {"status": "ok", "result_count": 0}
    assert attempts == 2


def test_timeout_offline_and_interruption_are_distinct() -> None:
    def raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def raise_offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    def raise_interrupted(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("reset", request=request)

    with pytest.raises(ExaTimeoutError):
        _client(raise_timeout).check_readiness()

    with pytest.raises(ExaOfflineError):
        _client(raise_offline).check_readiness()

    with pytest.raises(ExaConnectionInterruptedError):
        _client(raise_interrupted).check_readiness()


def test_search_and_contents_enforce_bounds() -> None:
    client = _client(lambda request: httpx.Response(200, request=request, json={"results": []}))

    with pytest.raises(ValueError, match="500"):
        client.search("x" * 501, contents={"highlights": True})
    with pytest.raises(ValueError, match="between 1 and 5"):
        client.search("bounded", limit=6, contents={"highlights": True})
    with pytest.raises(ValueError, match="required"):
        client.contents([], text=True)
    with pytest.raises(ValueError, match=str(DEFAULT_MAX_TEXT_CHARS)):
        client.contents(
            ["https://public.example.test/a"],
            text=True,
            max_text_chars=DEFAULT_MAX_TEXT_CHARS + 1,
        )


@pytest.mark.skipif(
    os.getenv("LYRA_RUN_EXA_SMOKE") != "1" or not os.getenv("LYRA_EXA_SMOKE_API_KEY"),
    reason="requires LYRA_RUN_EXA_SMOKE=1 and LYRA_EXA_SMOKE_API_KEY",
)
def test_live_exa_smoke() -> None:
    client = ExaClient(api_key=os.environ["LYRA_EXA_SMOKE_API_KEY"], retries=0)
    request_id, results = client.search(
        "Lyra study tool",
        limit=1,
        contents={"highlights": True},
        timeout_seconds=10.0,
    )

    assert request_id is None or isinstance(request_id, str)
    assert len(results) <= 1
