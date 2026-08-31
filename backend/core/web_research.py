"""Explicitly gated, SSRF-resistant Exa search and bounded source fetching.

No network call in this module can happen accidentally: both public entry points require
the caller to pass ``allowed=True`` after resolving global/per-class policy. The Exa
client factory and DNS resolver are injectable, keeping every security branch unit-testable.
"""

import socket
from collections.abc import Callable
from urllib.parse import urlsplit

from backend.core import exa, query_guard
from backend.core.web_policy import URLPolicyError, validate_public_source_url
from backend.storage import secrets

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_BYTES = 1024 * 1024

Resolver = Callable[..., list[tuple[object, ...]]]
ExaClientFactory = Callable[..., exa.ExaClient]


class WebResearchError(RuntimeError):
    """A safe, user-displayable web research failure."""


class WebResearchDisabledError(WebResearchError):
    """The caller did not grant web access for this run and class."""


class UnsafeURLError(WebResearchError):
    """A URL could reach a local, private, or otherwise non-public address."""


class ResponseTooLargeError(WebResearchError):
    """The source exceeded the configured snapshot ceiling."""


class UnsupportedContentTypeError(WebResearchError):
    """The response was not a textual source the writer can safely snapshot."""


def _require_allowed(allowed: bool) -> None:
    if allowed is not True:
        raise WebResearchDisabledError("Web research is disabled for this class.")


def validate_public_url(url: str, *, resolver: Resolver = socket.getaddrinfo) -> str:
    """Validate an HTTP(S) URL and every currently resolved address as public."""
    try:
        return validate_public_source_url(url, resolver=resolver).normalized_url
    except URLPolicyError as exc:
        raise UnsafeURLError(str(exc)) from exc


def fetch_source(
    url: str,
    *,
    allowed: bool,
    resolver: Resolver = socket.getaddrinfo,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    source_content_enabled: bool = True,
    exa_api_key: str | None = None,
    exa_client_factory: ExaClientFactory = exa.ExaClient,
) -> dict[str, object]:
    """Fetch one public textual page into a bounded ledger snapshot through Exa."""
    _require_allowed(allowed)
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    normalized_url = validate_public_url(url, resolver=resolver)
    if not source_content_enabled:
        raise WebResearchError("Source snapshots are disabled for this workspace.")
    try:
        exa_client = exa_client_factory(
            api_key=exa_api_key or _exa_api_key(),
            resolver=resolver,
            read_timeout_seconds=timeout_seconds,
            max_response_bytes=max_bytes,
        )
        request_id, results = exa_client.contents(
            [normalized_url],
            text=True,
            highlights=False,
            timeout_seconds=timeout_seconds,
            max_text_chars=min(max_bytes, exa.DEFAULT_MAX_TEXT_CHARS),
        )
    except (exa.ExaError, ValueError, URLPolicyError) as exc:
        raise WebResearchError(str(exc)) from exc
    if not results:
        raise WebResearchError("The source returned no usable content.")
    fetched = results[0]
    if fetched.status != "success":
        raise WebResearchError(_content_failure_message(fetched))
    final_url = fetched.url or normalized_url
    title = fetched.title or urlsplit(final_url).hostname or normalized_url
    snapshot = fetched.text or ""
    if not snapshot and fetched.highlights:
        snapshot = "\n".join(fetched.highlights)
    if not snapshot:
        raise WebResearchError("The source returned no usable text.")
    return {
        "url": final_url,
        "final_url": final_url,
        "title": title,
        "accessed_at": fetched.accessed_at,
        "content_type": "text/markdown",
        "snapshot": snapshot,
        "truncated": fetched.truncated,
        "warning": None,
        "provider": fetched.provider,
        "request_id": request_id,
        "source_status": fetched.status,
        "source_source": fetched.source,
        "error_tag": fetched.error_tag,
        "error_message": fetched.error_message,
    }


def search_web(
    query: str,
    *,
    allowed: bool,
    max_results: int = 5,
    resolver: Resolver = socket.getaddrinfo,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = 512 * 1024,
    private_context: tuple[str, ...] = (),
    exa_api_key: str | None = None,
    exa_client_factory: ExaClientFactory = exa.ExaClient,
) -> list[dict[str, str]]:
    """Search public sources through Exa."""
    _require_allowed(allowed)
    guarded = query_guard.guard_web_query(query, private_context=private_context)
    if isinstance(guarded, query_guard.QueryRefusal):
        raise ValueError(guarded.message)
    clean_query = guarded.query
    if max_results < 1 or max_results > exa.DEFAULT_SEARCH_RESULTS:
        raise ValueError(f"max_results must be between 1 and {exa.DEFAULT_SEARCH_RESULTS}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    try:
        request_id, results = exa_client_factory(
            api_key=exa_api_key or _exa_api_key(),
            resolver=resolver,
            read_timeout_seconds=timeout_seconds,
            max_response_bytes=max_bytes,
        ).search(
            clean_query,
            limit=max_results,
            contents={"highlights": {"query": clean_query}},
            timeout_seconds=timeout_seconds,
        )
    except (exa.ExaError, ValueError, URLPolicyError) as exc:
        raise WebResearchError(str(exc)) from exc
    return [
        {
            "title": result.title,
            "url": result.url,
            "description": result.highlights[0] if result.highlights else "",
            "id": result.id,
            "author": result.author,
            "published_date": result.published_date,
            "request_id": request_id,
            "accessed_at": result.accessed_at,
            "provider": result.provider,
            "truncated": result.truncated,
            "highlights": list(result.highlights),
        }
        for result in results
    ]


def _exa_api_key() -> str:
    key = secrets.get_exa_api_key()
    if key is None:
        raise WebResearchDisabledError("Web research is disabled until an Exa key is configured.")
    return key


def _content_failure_message(result: exa.ExaContentResult) -> str:
    if result.error_message:
        return result.error_message
    if result.error_tag == "CRAWL_TIMEOUT":
        return "The source timed out while Exa tried to fetch it."
    if result.error_tag == "CRAWL_LIVECRAWL_TIMEOUT":
        return "The source timed out during Exa live crawling."
    if result.error_tag == "SOURCE_NOT_AVAILABLE":
        return "The source is unavailable or blocked from public access."
    if result.error_tag == "UNSUPPORTED_URL":
        return "The source URL is not supported."
    return "The source could not be fetched."
