"""Bounded Firecrawl v2 client for Phase 4 web search and scrape.

This client assumes a self-hosted Firecrawl instance on loopback. It never accepts a
remote base URL, and it validates both target URLs and Firecrawl-returned final URLs with
the shared public-web policy.
"""

from __future__ import annotations

import json
import random
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

from backend.core.errors import UpstreamError
from backend.core.web_policy import (
    Resolver,
    URLPolicyError,
    validate_firecrawl_base_url,
    validate_firecrawl_final_url,
    validate_firecrawl_target_url,
)

DEFAULT_BASE_URL = "http://127.0.0.1:3002"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 60.0
DEFAULT_READINESS_TIMEOUT_SECONDS = 5.0
DEFAULT_SEARCH_TIMEOUT_MS = 15_000
DEFAULT_SCRAPE_TIMEOUT_MS = 60_000
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_MAX_MARKDOWN_CHARS = 100_000
DEFAULT_SEARCH_RESULTS = 5
MAX_QUERY_CHARS = 500
DEFAULT_TRANSIENT_RETRIES = 2
DEFAULT_RETRY_BASE_SECONDS = 0.25
DEFAULT_RETRY_MAX_SECONDS = 1.0
_SEARCH_ENDPOINT = "/v2/search"
_SCRAPE_ENDPOINT = "/v2/scrape"
_READINESS_ENDPOINT = "/v0/health/readiness"
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class FirecrawlError(UpstreamError):
    """A safe, user-displayable Firecrawl failure."""


class FirecrawlTransientError(FirecrawlError):
    """A temporary Firecrawl failure that may succeed on one bounded retry path."""


class FirecrawlMisconfiguredError(FirecrawlError):
    """Firecrawl is reachable but not exposing the expected local API contract."""


class FirecrawlSchemaError(FirecrawlMisconfiguredError):
    """Firecrawl returned a response that did not match the expected schema."""


@dataclass(frozen=True)
class FirecrawlSearchResult:
    """Bounded metadata for one search result."""

    title: str
    url: str
    description: str = ""


@dataclass(frozen=True)
class FirecrawlScrapeResult:
    """Bounded markdown snapshot metadata from one scrape."""

    url: str
    final_url: str
    title: str
    markdown: str
    content_type: str | None
    warning: str | None
    truncated: bool


@dataclass
class FirecrawlClient:
    """Synchronous Firecrawl client with injectable transport for deterministic tests."""

    base_url: str = DEFAULT_BASE_URL
    client: httpx.Client | None = None
    resolver: Resolver = socket.getaddrinfo
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    transient_retries: int = DEFAULT_TRANSIENT_RETRIES
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS
    retry_max_seconds: float = DEFAULT_RETRY_MAX_SECONDS
    sleep: Callable[[float], None] = time.sleep
    random_float: Callable[[], float] = random.random

    def __post_init__(self) -> None:
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("Timeouts must be positive.")
        if self.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive.")
        if self.transient_retries < 0:
            raise ValueError("transient_retries must be non-negative.")
        if self.retry_base_seconds <= 0 or self.retry_max_seconds <= 0:
            raise ValueError("Retry delays must be positive.")
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError("retry_base_seconds cannot exceed retry_max_seconds.")
        base = validate_firecrawl_base_url(self.base_url, resolver=self.resolver)
        object.__setattr__(self, "base_url", base.normalized_url)

    def check_readiness(self) -> dict[str, object]:
        """Probe Firecrawl's loopback readiness endpoint."""
        payload = self._request_json(
            "GET",
            _READINESS_ENDPOINT,
            timeout=httpx.Timeout(
                DEFAULT_READINESS_TIMEOUT_SECONDS,
                connect=self.connect_timeout_seconds,
            ),
        )
        status = payload.get("status")
        if not isinstance(status, str) or not status.strip():
            raise FirecrawlSchemaError("Firecrawl readiness returned an invalid response.")
        return {"status": status.strip()}

    def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_SEARCH_RESULTS,
        timeout_ms: int = DEFAULT_SEARCH_TIMEOUT_MS,
    ) -> list[FirecrawlSearchResult]:
        """Search the web through Firecrawl and keep only public URLs."""
        clean_query = " ".join(query.split())
        if not clean_query:
            raise ValueError("A Firecrawl search needs a query.")
        if len(clean_query) > MAX_QUERY_CHARS:
            raise ValueError(
                f"A Firecrawl search query cannot exceed {MAX_QUERY_CHARS} characters."
            )
        if limit < 1 or limit > DEFAULT_SEARCH_RESULTS:
            raise ValueError(f"limit must be between 1 and {DEFAULT_SEARCH_RESULTS}.")
        if timeout_ms < 1 or timeout_ms > DEFAULT_SEARCH_TIMEOUT_MS:
            raise ValueError(f"timeout_ms must be between 1 and {DEFAULT_SEARCH_TIMEOUT_MS}.")

        payload = self._request_json(
            "POST",
            _SEARCH_ENDPOINT,
            json_body={
                "query": clean_query,
                "limit": limit,
                "sources": ["web"],
                "timeout": timeout_ms,
                # Firecrawl applies its own endpoint-validity filter; Lyra still validates every
                # returned URL because this option is not a security boundary.
                "ignoreInvalidURLs": True,
            },
        )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise FirecrawlSchemaError("Firecrawl search returned an invalid response.")
        web_results = data.get("web")
        if not isinstance(web_results, list):
            raise FirecrawlSchemaError("Firecrawl search returned an invalid response.")

        bounded: list[FirecrawlSearchResult] = []
        seen: set[str] = set()
        for item in web_results:
            if len(bounded) == limit:
                break
            if not isinstance(item, Mapping):
                raise FirecrawlSchemaError("Firecrawl search returned an invalid result row.")
            title = _bounded_text(item.get("title"), field_name="title", max_chars=200)
            url = _required_text(item.get("url"), field_name="url", max_chars=2_000)
            try:
                normalized = validate_firecrawl_target_url(
                    url,
                    resolver=self.resolver,
                ).normalized_url
            except URLPolicyError:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            bounded.append(
                FirecrawlSearchResult(
                    title=title,
                    url=normalized,
                    description=_optional_text(item.get("description"), max_chars=500),
                )
            )
        return bounded

    def scrape(
        self,
        url: str,
        *,
        timeout_ms: int = DEFAULT_SCRAPE_TIMEOUT_MS,
        max_markdown_chars: int = DEFAULT_MAX_MARKDOWN_CHARS,
    ) -> FirecrawlScrapeResult:
        """Fetch one public URL through Firecrawl and return a bounded markdown snapshot."""
        if timeout_ms < 1_000 or timeout_ms > DEFAULT_SCRAPE_TIMEOUT_MS:
            raise ValueError(f"timeout_ms must be between 1000 and {DEFAULT_SCRAPE_TIMEOUT_MS}.")
        if max_markdown_chars < 1 or max_markdown_chars > DEFAULT_MAX_MARKDOWN_CHARS:
            raise ValueError(
                f"max_markdown_chars must be between 1 and {DEFAULT_MAX_MARKDOWN_CHARS}."
            )
        target = validate_firecrawl_target_url(url, resolver=self.resolver)
        payload = self._request_json(
            "POST",
            _SCRAPE_ENDPOINT,
            json_body={
                "url": target.normalized_url,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "timeout": timeout_ms,
                "removeBase64Images": True,
                "blockAds": True,
                # Firecrawl currently documents TLS skipping as the default; Lyra explicitly
                # refuses it. Custom headers, actions, profiles, and proxy selection are omitted.
                "skipTlsVerification": False,
                "storeInCache": False,
            },
        )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise FirecrawlSchemaError("Firecrawl scrape returned an invalid response.")
        markdown = _required_text(data.get("markdown"), field_name="markdown", max_chars=None)
        truncated = len(markdown) > max_markdown_chars
        if truncated:
            markdown = markdown[:max_markdown_chars]
        metadata = data.get("metadata")
        if not isinstance(metadata, Mapping):
            raise FirecrawlSchemaError("Firecrawl scrape returned invalid metadata.")
        final_candidate = _required_text(
            metadata.get("sourceURL") or metadata.get("url"),
            field_name="final_url",
            max_chars=2_000,
        )
        try:
            final_url = validate_firecrawl_final_url(
                final_candidate,
                resolver=self.resolver,
            ).normalized_url
        except URLPolicyError as exc:
            raise FirecrawlError("Firecrawl returned a non-public final URL.") from exc
        title = _optional_text(metadata.get("title"), max_chars=200) or final_url
        content_type = _optional_text(metadata.get("contentType"), max_chars=200) or None
        warning = _optional_text(data.get("warning"), max_chars=500) or None
        return FirecrawlScrapeResult(
            url=target.normalized_url,
            final_url=final_url,
            title=title,
            markdown=markdown,
            content_type=content_type,
            warning=warning,
            truncated=truncated,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> dict[str, object]:
        attempts = self.transient_retries + 1
        for attempt in range(attempts):
            try:
                return self._request_json_once(
                    method,
                    path,
                    json_body=json_body,
                    timeout=timeout,
                )
            except FirecrawlTransientError:
                if attempt + 1 >= attempts:
                    raise
                self.sleep(self._retry_delay_seconds(attempt))
        raise AssertionError("retry loop exited without returning or raising")

    def _request_json_once(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> dict[str, object]:
        request_timeout = timeout or httpx.Timeout(
            self.read_timeout_seconds, connect=self.connect_timeout_seconds
        )
        owned = self.client is None
        client = self.client or httpx.Client(timeout=request_timeout, follow_redirects=False)
        try:
            with client.stream(
                method,
                f"{self.base_url}{path}",
                json=dict(json_body) if json_body is not None else None,
                timeout=request_timeout,
            ) as response:
                body = _bounded_response_bytes(response, self.max_response_bytes)
                if response.status_code >= 400:
                    raise _upstream_error(response, body)
                try:
                    payload = json.loads(body)
                except ValueError as exc:
                    raise FirecrawlSchemaError("Firecrawl returned invalid JSON.") from exc
        except httpx.TimeoutException as exc:
            raise FirecrawlTransientError("Firecrawl timed out.") from exc
        except httpx.ConnectError as exc:
            raise FirecrawlTransientError("Firecrawl is unreachable.") from exc
        except httpx.TransportError as exc:
            raise FirecrawlTransientError("Firecrawl connection was interrupted.") from exc
        except httpx.HTTPError as exc:
            raise FirecrawlError("Firecrawl request failed.") from exc
        finally:
            if owned:
                client.close()
        if not isinstance(payload, dict):
            raise FirecrawlSchemaError("Firecrawl returned an invalid response.")
        if payload.get("success") is False:
            message = _optional_text(payload.get("error"), max_chars=200) or "Firecrawl failed."
            raise FirecrawlError(message)
        return payload

    def _retry_delay_seconds(self, attempt: int) -> float:
        capped = min(self.retry_max_seconds, self.retry_base_seconds * (2**attempt))
        return capped * (0.5 + (self.random_float() * 0.5))


def _bounded_response_bytes(response: httpx.Response, max_bytes: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise FirecrawlError("Firecrawl returned too much data.")
        except ValueError:
            pass
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > max_bytes:
            raise FirecrawlError("Firecrawl returned too much data.")
        body.extend(chunk)
    return bytes(body)


def _upstream_message(response: httpx.Response, body: bytes) -> str:
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        message = _optional_text(payload.get("error"), max_chars=200)
        if message:
            return message
    if response.status_code in (401, 403):
        return "Firecrawl rejected the request."
    if response.status_code == 404:
        return "Firecrawl does not expose the expected API."
    if response.status_code == 429:
        return "Firecrawl is busy. Retry shortly."
    if response.status_code in {408, 425, 500, 502, 503, 504}:
        return "Firecrawl is temporarily unavailable."
    return "Firecrawl request failed."


def _upstream_error(response: httpx.Response, body: bytes) -> FirecrawlError:
    message = _upstream_message(response, body)
    if response.status_code in _TRANSIENT_STATUS_CODES:
        return FirecrawlTransientError(message)
    if response.status_code in {401, 403, 404}:
        return FirecrawlMisconfiguredError(message)
    return FirecrawlError(message)


def _required_text(value: object, *, field_name: str, max_chars: int | None) -> str:
    if not isinstance(value, str):
        raise FirecrawlSchemaError(f"Firecrawl response is missing {field_name}.")
    text = " ".join(value.split()) if field_name != "markdown" else value
    if not text:
        raise FirecrawlSchemaError(f"Firecrawl response is missing {field_name}.")
    if max_chars is not None and len(text) > max_chars:
        raise FirecrawlSchemaError(f"Firecrawl response field {field_name} is too large.")
    return text


def _optional_text(value: object, *, max_chars: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise FirecrawlSchemaError("Firecrawl response contained an invalid text field.")
    text = " ".join(value.split())
    if len(text) > max_chars:
        raise FirecrawlSchemaError("Firecrawl response contained an oversized text field.")
    return text


def _bounded_text(value: object, *, field_name: str, max_chars: int) -> str:
    return _required_text(value, field_name=field_name, max_chars=max_chars)
