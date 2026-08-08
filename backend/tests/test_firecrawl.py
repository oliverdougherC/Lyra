"""Firecrawl client tests use mock transport and fake DNS only."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable

import httpx
import pytest

from backend.core.firecrawl import (
    DEFAULT_MAX_MARKDOWN_CHARS,
    FirecrawlClient,
    FirecrawlError,
    FirecrawlMisconfiguredError,
    FirecrawlSchemaError,
    FirecrawlSearchResult,
)

RESOLUTIONS = {
    "127.0.0.1": ["127.0.0.1"],
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


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> FirecrawlClient:
    return FirecrawlClient(
        base_url="http://127.0.0.1:3002",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=socket.getaddrinfo,
        sleep=lambda _seconds: None,
        random_float=lambda: 0.0,
    )


def test_readiness_uses_loopback_health_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:3002/v0/health/readiness"
        return httpx.Response(200, request=request, json={"status": "ok"})

    assert _client(handler).check_readiness() == {"status": "ok"}


def test_search_sends_bounded_request_and_filters_invalid_results() -> None:
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            request=request,
            json={
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "First result",
                            "description": "Useful detail",
                            "url": "https://public.example.test/a",
                        },
                        {
                            "title": "Private result",
                            "description": "skip me",
                            "url": "http://private.example.test/secret",
                        },
                        {"title": "First result", "url": "https://public.example.test/a"},
                    ]
                },
            },
        )

    results = _client(handler).search("phase four research")
    assert sent == {
        "query": "phase four research",
        "limit": 5,
        "sources": ["web"],
        "timeout": 15000,
        "ignoreInvalidURLs": True,
    }
    assert results == [
        FirecrawlSearchResult(
            title="First result",
            url="https://public.example.test/a",
            description="Useful detail",
        )
    ]


def test_scrape_validates_target_and_final_url_and_truncates_markdown() -> None:
    markdown = "x" * (DEFAULT_MAX_MARKDOWN_CHARS + 10)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["skipTlsVerification"] is False
        assert payload["storeInCache"] is False
        assert "lockdown" not in payload
        assert "headers" not in payload
        assert "actions" not in payload
        return httpx.Response(
            200,
            request=request,
            json={
                "success": True,
                "data": {
                    "markdown": markdown,
                    "warning": "trimmed upstream",
                    "metadata": {
                        "title": "Study page",
                        "sourceURL": "https://public.example.test/final",
                        "contentType": "text/html",
                    },
                },
            },
        )

    result = _client(handler).scrape("https://public.example.test/start")
    assert result.url == "https://public.example.test/start"
    assert result.final_url == "https://public.example.test/final"
    assert result.title == "Study page"
    assert result.content_type == "text/html"
    assert result.warning == "trimmed upstream"
    assert result.truncated is True
    assert len(result.markdown) == DEFAULT_MAX_MARKDOWN_CHARS


def test_scrape_rejects_private_final_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "success": True,
                "data": {
                    "markdown": "ok",
                    "metadata": {"title": "Bad", "sourceURL": "http://private.example.test/final"},
                },
            },
        )

    with pytest.raises(FirecrawlError):
        _client(handler).scrape("https://public.example.test/start")


def test_schema_size_and_timeout_failures_are_mapped() -> None:
    malformed = _client(
        lambda request: httpx.Response(200, request=request, json={"success": True})
    )
    with pytest.raises(FirecrawlSchemaError):
        malformed.scrape("https://public.example.test/start")

    def huge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-length": str(2_000_000)},
            content=b"{}",
        )

    with pytest.raises(FirecrawlError):
        _client(huge).check_readiness()

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(FirecrawlError):
        _client(timeout).search("phase four research")


def test_transient_failures_retry_before_succeeding() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("slow", request=request)
        if attempts == 2:
            return httpx.Response(429, request=request, json={"error": "busy"})
        return httpx.Response(200, request=request, json={"status": "ok"})

    assert _client(handler).check_readiness() == {"status": "ok"}
    assert attempts == 3


def test_transient_transport_read_failure_retries_before_succeeding() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("connection reset", request=request)
        return httpx.Response(200, request=request, json={"status": "ok"})

    assert _client(handler).check_readiness() == {"status": "ok"}
    assert attempts == 2


def test_misconfigured_failures_do_not_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, request=request)

    with pytest.raises(FirecrawlMisconfiguredError):
        _client(handler).check_readiness()
    assert attempts == 1


def test_search_and_scrape_enforce_documented_phase_four_bounds() -> None:
    client = _client(lambda request: httpx.Response(500, request=request))

    with pytest.raises(ValueError, match="500"):
        client.search("x" * 501)
    with pytest.raises(ValueError, match="15000"):
        client.search("bounded", timeout_ms=15_001)
    with pytest.raises(ValueError, match="1000"):
        client.scrape("https://public.example.test/start", timeout_ms=999)
    with pytest.raises(ValueError, match="100000"):
        client.scrape(
            "https://public.example.test/start",
            max_markdown_chars=DEFAULT_MAX_MARKDOWN_CHARS + 1,
        )
