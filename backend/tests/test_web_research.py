"""Web research is opt-in, bounded, mockable, and cannot cross the public boundary."""

import socket

import pytest

from backend.core import exa, web_research


def _public_resolver(
    host: str, port: int, *args: object, **kwargs: object
) -> list[tuple[object, ...]]:
    del host, args, kwargs
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def test_explicit_gate_prevents_any_exa_call() -> None:
    called = False

    def fake_factory(**kwargs: object) -> object:
        nonlocal called
        called = True
        return object()

    with pytest.raises(web_research.WebResearchDisabledError):
        web_research.fetch_source(
            "https://example.com/",
            allowed=False,
            resolver=_public_resolver,
            exa_client_factory=fake_factory,
        )
    assert called is False


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


def test_fetch_source_uses_exa_contents_and_surfaces_provider_metadata() -> None:
    class FakeExaClient:
        def contents(
            self, urls: list[str], **kwargs: object
        ) -> tuple[str, list[exa.ExaContentResult]]:
            assert urls == ["https://example.com/study"]
            return (
                "req-2",
                [
                    exa.ExaContentResult(
                        id="doc-1",
                        url="https://example.com/study",
                        title="Useful Study",
                        text="The evidence supports the claim.",
                        highlights=(),
                        published_date="2026-08-30",
                        author="Ada",
                        request_id="req-2",
                        status="success",
                        source="livecrawl",
                        error_tag=None,
                        error_message=None,
                        accessed_at="2026-08-30T12:00:00+00:00",
                        provider="exa",
                        truncated=False,
                    )
                ],
            )

    result = web_research.fetch_source(
        "https://example.com/study",
        allowed=True,
        resolver=_public_resolver,
        exa_api_key="test-exa-key",
        exa_client_factory=lambda **kwargs: FakeExaClient(),
    )

    assert result["title"] == "Useful Study"
    assert result["snapshot"] == "The evidence supports the claim."
    assert result["provider"] == "exa"
    assert result["source_status"] == "success"


def test_fetch_source_requires_source_content_enabled_before_exa() -> None:
    called = False

    def fake_factory(**kwargs: object) -> object:
        nonlocal called
        called = True
        return object()

    with pytest.raises(web_research.WebResearchError, match="disabled"):
        web_research.fetch_source(
            "https://example.com/study",
            allowed=True,
            resolver=_public_resolver,
            source_content_enabled=False,
            exa_client_factory=fake_factory,
        )
    assert called is False


def test_search_uses_exa_and_preserves_public_metadata() -> None:
    class FakeExaClient:
        def search(self, query: str, **kwargs: object) -> tuple[str, list[exa.ExaSearchResult]]:
            assert query == "archival evidence"
            return (
                "req-1",
                [
                    exa.ExaSearchResult(
                        title="First result",
                        url="https://example.com/a",
                        id="doc-1",
                        author="Ada",
                        published_date="2026-08-30",
                        highlights=("Useful detail",),
                        request_id="req-1",
                        accessed_at="2026-08-30T12:00:00+00:00",
                        provider="exa",
                        truncated=False,
                    )
                ],
            )

    results = web_research.search_web(
        "archival evidence",
        allowed=True,
        resolver=_public_resolver,
        exa_api_key="test-exa-key",
        exa_client_factory=lambda **kwargs: FakeExaClient(),
    )

    assert results == [
        {
            "title": "First result",
            "url": "https://example.com/a",
            "description": "Useful detail",
            "id": "doc-1",
            "author": "Ada",
            "published_date": "2026-08-30",
            "request_id": "req-1",
            "accessed_at": "2026-08-30T12:00:00+00:00",
            "provider": "exa",
            "truncated": False,
            "highlights": ["Useful detail"],
        }
    ]


def test_fetch_source_surfaces_per_url_crawl_failure() -> None:
    class FakeExaClient:
        def contents(
            self, urls: list[str], **kwargs: object
        ) -> tuple[str, list[exa.ExaContentResult]]:
            return (
                "req-3",
                [
                    exa.ExaContentResult(
                        id=urls[0],
                        url=urls[0],
                        title=None,
                        text=None,
                        highlights=(),
                        published_date=None,
                        author=None,
                        request_id="req-3",
                        status="error",
                        source="livecrawl",
                        error_tag="CRAWL_TIMEOUT",
                        error_message=None,
                        accessed_at="2026-08-30T12:00:00+00:00",
                        provider="exa",
                        truncated=False,
                    )
                ],
            )

    with pytest.raises(web_research.WebResearchError, match="timed out"):
        web_research.fetch_source(
            "https://example.com/study",
            allowed=True,
            resolver=_public_resolver,
            exa_api_key="test-exa-key",
            exa_client_factory=lambda **kwargs: FakeExaClient(),
        )


def test_missing_exa_key_is_distinct_from_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The key lookup is pinned to "none stored" so the test is hermetic: on a developer
    # machine the keyring holds a real Exa key, and the autouse data-dir fixture cannot
    # isolate it.
    monkeypatch.setattr(web_research.secrets, "get_exa_api_key", lambda: None)

    with pytest.raises(web_research.WebResearchDisabledError, match="disabled for this class"):
        web_research.search_web("evidence", allowed=False, resolver=_public_resolver)

    with pytest.raises(web_research.WebResearchDisabledError, match="Exa key"):
        web_research.search_web("evidence", allowed=True, resolver=_public_resolver)
