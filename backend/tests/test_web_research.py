"""Web research is opt-in, bounded, mockable, and cannot cross the public boundary."""

import socket

import httpx
import pytest

from backend.core import web_research


def _public_resolver(
    host: str, port: int, *args: object, **kwargs: object
) -> list[tuple[object, ...]]:
    del host, args, kwargs
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def test_explicit_gate_prevents_any_network_call() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, request=request, text="unexpected")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(web_research.WebResearchDisabledError):
        web_research.fetch_source(
            "https://example.com/", allowed=False, client=client, resolver=_public_resolver
        )
    assert called is False
    client.close()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/admin",
        "http://127.0.0.1/admin",
        "http://10.0.0.2/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/admin",
    ],
)
def test_non_http_and_non_public_destinations_are_rejected(url: str) -> None:
    with pytest.raises(web_research.UnsafeURLError):
        web_research.validate_public_url(url)


def test_fetch_snapshots_visible_html_with_a_bounded_mocked_request() -> None:
    page = (
        "<html><head><title>  Useful Study  </title><script>steal()</script></head>"
        "<body><h1>Finding</h1><p>The evidence supports the claim.</p></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html; charset=utf-8"},
            text=page,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = web_research.fetch_source(
            "https://example.com/study",
            allowed=True,
            client=client,
            resolver=_public_resolver,
        )

    assert result["title"] == "Useful Study"
    assert "The evidence supports the claim." in result["snapshot"]
    assert "steal()" not in result["snapshot"]


def test_redirects_are_revalidated_before_the_next_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            302, request=request, headers={"location": "http://127.0.0.1/private"}
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(web_research.UnsafeURLError),
    ):
        web_research.fetch_source(
            "https://example.com/redirect",
            allowed=True,
            client=client,
            resolver=_public_resolver,
        )
    assert calls == 1


def test_response_size_and_content_type_are_bounded() -> None:
    def large(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/plain"},
            content=b"x" * 11,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(large)) as client,
        pytest.raises(web_research.ResponseTooLargeError),
    ):
        web_research.fetch_source(
            "https://example.com/large",
            allowed=True,
            client=client,
            resolver=_public_resolver,
            max_bytes=10,
        )

    def binary(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "application/octet-stream"},
            content=b"binary",
        )

    with (
        httpx.Client(transport=httpx.MockTransport(binary)) as client,
        pytest.raises(web_research.UnsupportedContentTypeError),
    ):
        web_research.fetch_source(
            "https://example.com/file",
            allowed=True,
            client=client,
            resolver=_public_resolver,
        )


def test_search_parses_public_html_results_and_unwraps_redirect_links() -> None:
    search_page = """
    <html><body>
      <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">
        First result
      </a>
      <a class="result__a" href="https://example.org/b">Second result</a>
      <a class="result__a" href="https://example.org/b">Duplicate</a>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert "q=archival+evidence" in str(request.url)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            text=search_page,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = web_research.search_web(
            "archival evidence",
            allowed=True,
            client=client,
            resolver=_public_resolver,
            max_results=5,
        )

    assert results == [
        {"title": "First result", "url": "https://example.com/a"},
        {"title": "Second result", "url": "https://example.org/b"},
    ]
