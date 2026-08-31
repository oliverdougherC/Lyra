"""Bounded Exa client for Lyra web search and source retrieval.

The client keeps Lyra's existing public-web safety guarantees:

- only HTTP(S) public URLs are accepted for source retrieval;
- every request is bounded by retries, timeouts, response bytes, and field sizes;
- every returned URL is revalidated before Lyra stores or exposes it.
"""

from __future__ import annotations

import json
import random
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from backend.core.errors import UpstreamError
from backend.core.web_policy import Resolver, URLPolicyError, validate_public_source_url

DEFAULT_BASE_URL = "https://api.exa.ai"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 15.0
DEFAULT_SEARCH_RESULTS = 5
DEFAULT_SEARCH_TYPE = "auto"
DEFAULT_MAX_QUERY_CHARS = 500
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_MAX_TEXT_CHARS = 10_000
DEFAULT_MAX_HIGHLIGHT_CHARS = 1_500
DEFAULT_RETRIES = 2
DEFAULT_RETRY_BASE_SECONDS = 0.25
DEFAULT_RETRY_MAX_SECONDS = 1.0
DEFAULT_REQUEST_ID_CHARS = 128
DEFAULT_TITLE_CHARS = 300
DEFAULT_URL_CHARS = 2_000
DEFAULT_ID_CHARS = 2_000
DEFAULT_AUTHOR_CHARS = 500
DEFAULT_DATE_CHARS = 64
DEFAULT_ERROR_CHARS = 500
SEARCH_PATH = "/search"
CONTENTS_PATH = "/contents"
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class ExaError(UpstreamError):
    """A safe, user-displayable Exa failure."""


class ExaTransientError(ExaError):
    """A temporary failure that may succeed on a bounded retry path."""


class ExaMisconfiguredError(ExaError):
    """The API key or request shape is invalid for the configured Exa endpoint."""


class ExaSchemaError(ExaMisconfiguredError):
    """Exa returned a payload that did not match the expected schema."""


class ExaAuthError(ExaMisconfiguredError):
    """The Exa API key is missing, invalid, or lacks access."""


class ExaPermissionError(ExaMisconfiguredError):
    """The Exa key is valid but the requested feature or source is not allowed."""


class ExaQuotaExceededError(ExaError):
    """The account or API key has exhausted its available credits or budget."""


class ExaRateLimitError(ExaTransientError):
    """The request hit a rate limit or usage throttle."""


class ExaTimeoutError(ExaTransientError):
    """Exa did not answer within the bounded timeout."""


class ExaOfflineError(ExaTransientError):
    """Exa could not be reached at all."""


class ExaConnectionInterruptedError(ExaTransientError):
    """The Exa connection broke mid-request."""


@dataclass(frozen=True)
class ExaSearchResult:
    title: str
    url: str
    id: str | None
    author: str | None
    published_date: str | None
    highlights: tuple[str, ...]
    request_id: str | None
    accessed_at: str
    provider: str
    truncated: bool


@dataclass(frozen=True)
class ExaContentResult:
    id: str
    url: str | None
    title: str | None
    text: str | None
    highlights: tuple[str, ...]
    published_date: str | None
    author: str | None
    request_id: str | None
    status: str
    source: str | None
    error_tag: str | None
    error_message: str | None
    accessed_at: str
    provider: str
    truncated: bool


@dataclass
class ExaClient:
    """Synchronous Exa client with injectable transport for deterministic tests."""

    api_key: str | None
    base_url: str = DEFAULT_BASE_URL
    client: httpx.Client | None = None
    resolver: Resolver = socket.getaddrinfo
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    retries: int = DEFAULT_RETRIES
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS
    retry_max_seconds: float = DEFAULT_RETRY_MAX_SECONDS
    sleep: Callable[[float], None] = time.sleep
    random_float: Callable[[], float] = random.random

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_key.strip():
            raise ExaAuthError("No Exa API key is configured.")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("Timeouts must be positive.")
        if self.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive.")
        if self.retries < 0:
            raise ValueError("retries must be non-negative.")
        if self.retry_base_seconds <= 0 or self.retry_max_seconds <= 0:
            raise ValueError("Retry delays must be positive.")
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError("retry_base_seconds cannot exceed retry_max_seconds.")
        self.api_key = self.api_key.strip()

    def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_SEARCH_RESULTS,
        contents: Mapping[str, object] | None = None,
        search_type: str = DEFAULT_SEARCH_TYPE,
        timeout_seconds: float | None = None,
    ) -> tuple[str | None, list[ExaSearchResult]]:
        clean_query = " ".join(query.split())
        if not clean_query:
            raise ValueError("A search query is required.")
        if len(clean_query) > DEFAULT_MAX_QUERY_CHARS:
            raise ValueError(f"A search query cannot exceed {DEFAULT_MAX_QUERY_CHARS} characters.")
        if limit < 1 or limit > DEFAULT_SEARCH_RESULTS:
            raise ValueError(f"limit must be between 1 and {DEFAULT_SEARCH_RESULTS}.")
        if search_type not in {"auto", "fast", "instant"}:
            raise ValueError("Unsupported Exa search type.")

        payload: dict[str, object] = {
            "query": clean_query,
            "type": search_type,
            "numResults": limit,
        }
        if contents:
            payload["contents"] = _normalized_search_contents(contents, clean_query=clean_query)

        data = self._post_json(SEARCH_PATH, payload, timeout_seconds=timeout_seconds)
        results = data.get("results")
        if not isinstance(results, list):
            raise ExaSchemaError("Exa search returned an invalid response.")

        request_id, _ = _bounded_text(data.get("requestId"), DEFAULT_REQUEST_ID_CHARS)
        seen: set[str] = set()
        bounded: list[ExaSearchResult] = []
        accessed_at = datetime.now(UTC).isoformat(timespec="seconds")
        for item in results:
            if len(bounded) == limit:
                break
            result = _parse_search_result(
                item,
                request_id=request_id,
                resolver=self.resolver,
                accessed_at=accessed_at,
            )
            if result is None or result.url in seen:
                continue
            seen.add(result.url)
            bounded.append(result)
        return request_id, bounded

    def contents(
        self,
        urls: Sequence[str],
        *,
        text: bool = True,
        highlights: bool | Mapping[str, object] = False,
        timeout_seconds: float | None = None,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    ) -> tuple[str | None, list[ExaContentResult]]:
        if not urls:
            raise ValueError("At least one URL is required.")
        if max_text_chars < 1 or max_text_chars > DEFAULT_MAX_TEXT_CHARS:
            raise ValueError(f"max_text_chars must be between 1 and {DEFAULT_MAX_TEXT_CHARS}.")
        normalized_urls = [
            validate_public_source_url(url, resolver=self.resolver).normalized_url for url in urls
        ]
        payload: dict[str, object] = {"urls": normalized_urls}
        payload["text"] = {"maxCharacters": max_text_chars} if text else False
        if highlights is True:
            payload["highlights"] = {"maxCharacters": DEFAULT_MAX_HIGHLIGHT_CHARS}
        elif isinstance(highlights, Mapping):
            payload["highlights"] = _normalized_highlights(highlights)
        else:
            payload["highlights"] = False

        data = self._post_json(CONTENTS_PATH, payload, timeout_seconds=timeout_seconds)
        raw_results = data.get("results")
        raw_statuses = data.get("statuses")
        if not isinstance(raw_results, list) or not isinstance(raw_statuses, list):
            raise ExaSchemaError("Exa contents returned an invalid response.")

        request_id, _ = _bounded_text(data.get("requestId"), DEFAULT_REQUEST_ID_CHARS)
        results_by_id: dict[str, ExaContentResult] = {}
        results_by_url: dict[str, ExaContentResult] = {}
        parsed_statuses: list[tuple[str, Mapping[str, object]]] = []
        for item in raw_statuses:
            if not isinstance(item, Mapping):
                raise ExaSchemaError("Exa contents returned an invalid status row.")
            status_id, _ = _bounded_text(item.get("id"), DEFAULT_ID_CHARS)
            if not status_id:
                raise ExaSchemaError("Exa contents returned a status row without an id.")
            parsed_statuses.append((status_id, item))

        for item in raw_results:
            result = _parse_content_result(
                item,
                request_id=request_id,
                resolver=self.resolver,
                max_text_chars=max_text_chars,
            )
            if result is None:
                continue
            results_by_id[result.id] = result
            if result.url:
                results_by_url[result.url] = result

        ordered: list[ExaContentResult] = []
        seen_ids: set[str] = set()
        for status_id, status_row in parsed_statuses:
            result = results_by_id.get(status_id) or results_by_url.get(status_id)
            if result is None:
                result = _status_only_content_result(
                    status_id,
                    status_row=status_row,
                    request_id=request_id,
                    resolver=self.resolver,
                )
            else:
                result = _apply_status(result, status_row=status_row)
            ordered.append(result)
            seen_ids.add(result.id)

        for result in results_by_id.values():
            if result.id not in seen_ids:
                ordered.append(result)
        return request_id, ordered

    def check_readiness(self) -> dict[str, object]:
        """Probe the Exa API explicitly, without enabling it during startup."""
        data = self._post_json(
            SEARCH_PATH,
            {"query": "Lyra readiness probe", "type": DEFAULT_SEARCH_TYPE, "numResults": 1},
        )
        results = data.get("results")
        if not isinstance(results, list):
            raise ExaSchemaError("Exa readiness returned an invalid response.")
        return {"status": "ok", "result_count": len(results)}

    def _post_json(
        self,
        path: str,
        body: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        timeout = timeout_seconds or self.read_timeout_seconds
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive.")
        if self.client is not None:
            request_client = self.client
            should_close = False
        else:
            request_client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(timeout, connect=self.connect_timeout_seconds),
                headers={"Content-Type": "application/json", "x-api-key": self.api_key},
                follow_redirects=False,
            )
            should_close = True
        try:
            response = self._request_with_retries(request_client, path, body, timeout=timeout)
        finally:
            if should_close:
                request_client.close()
        return response

    def _request_with_retries(
        self,
        client: httpx.Client,
        path: str,
        body: Mapping[str, object],
        *,
        timeout: float,
    ) -> dict[str, object]:
        last_error: ExaTransientError | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._request_once(client, path, body, timeout=timeout)
            except (
                ExaRateLimitError,
                ExaTimeoutError,
                ExaOfflineError,
                ExaConnectionInterruptedError,
            ) as exc:
                last_error = exc
            except ExaTransientError as exc:
                last_error = exc
            if attempt == self.retries:
                break
            self.sleep(
                _retry_delay(
                    self.retry_base_seconds,
                    self.retry_max_seconds,
                    attempt,
                    self.random_float,
                )
            )
        if last_error is not None:
            raise last_error
        raise ExaTransientError("Exa is temporarily unavailable.")

    def _request_once(
        self,
        client: httpx.Client,
        path: str,
        body: Mapping[str, object],
        *,
        timeout: float,
    ) -> dict[str, object]:
        try:
            with client.stream(
                "POST",
                path,
                json=dict(body),
                headers={"x-api-key": self.api_key},
                timeout=timeout,
            ) as response:
                payload_bytes = _bounded_response_bytes(response, self.max_response_bytes)
                if response.status_code >= 400:
                    raise _response_error(response, payload_bytes)
        except httpx.TimeoutException as exc:
            raise ExaTimeoutError("Exa timed out.") from exc
        except httpx.ConnectError as exc:
            raise ExaOfflineError("Exa is offline or unreachable.") from exc
        except httpx.TransportError as exc:
            raise ExaConnectionInterruptedError("The Exa connection was interrupted.") from exc
        try:
            payload = json.loads(
                payload_bytes.decode(response.encoding or "utf-8", errors="replace")
            )
        except ValueError as exc:
            raise ExaSchemaError("Exa returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ExaSchemaError("Exa returned an invalid response.")
        return payload


def _normalized_search_contents(
    contents: Mapping[str, object],
    *,
    clean_query: str,
) -> dict[str, object]:
    unsupported = set(contents) - {"highlights"}
    if unsupported:
        raise ValueError("Search contents supports bounded highlights only.")
    normalized: dict[str, object] = {}
    highlights = contents.get("highlights")
    if highlights is True:
        normalized["highlights"] = {"maxCharacters": DEFAULT_MAX_HIGHLIGHT_CHARS}
    elif isinstance(highlights, Mapping):
        options = dict(highlights)
        options.setdefault("query", clean_query)
        options.setdefault("maxCharacters", DEFAULT_MAX_HIGHLIGHT_CHARS)
        normalized["highlights"] = options
    elif highlights not in (None, False):
        raise ValueError("Search highlights must be a boolean or object.")
    return normalized


def _normalized_highlights(highlights: Mapping[str, object]) -> dict[str, object]:
    normalized = dict(highlights)
    normalized.setdefault("maxCharacters", DEFAULT_MAX_HIGHLIGHT_CHARS)
    return normalized


def _parse_search_result(
    item: object,
    *,
    request_id: str | None,
    resolver: Resolver,
    accessed_at: str,
) -> ExaSearchResult | None:
    if not isinstance(item, Mapping):
        raise ExaSchemaError("Exa search returned an invalid result row.")
    title, title_truncated = _bounded_text(item.get("title"), DEFAULT_TITLE_CHARS)
    url, url_truncated = _bounded_text(item.get("url"), DEFAULT_URL_CHARS)
    if not title or not url:
        return None
    try:
        normalized_url = validate_public_source_url(url, resolver=resolver).normalized_url
    except URLPolicyError:
        return None
    highlights, highlights_truncated = _bounded_text_list(
        item.get("highlights"),
        max_items=5,
        max_chars=DEFAULT_MAX_HIGHLIGHT_CHARS,
    )
    result_id, id_truncated = _bounded_text(item.get("id"), DEFAULT_ID_CHARS)
    author, author_truncated = _bounded_text(item.get("author"), DEFAULT_AUTHOR_CHARS)
    published_date, date_truncated = _bounded_text(item.get("publishedDate"), DEFAULT_DATE_CHARS)
    return ExaSearchResult(
        title=title,
        url=normalized_url,
        id=result_id,
        author=author,
        published_date=published_date,
        highlights=highlights,
        request_id=request_id,
        accessed_at=accessed_at,
        provider="exa",
        truncated=any(
            (
                title_truncated,
                url_truncated,
                id_truncated,
                author_truncated,
                date_truncated,
                highlights_truncated,
            )
        ),
    )


def _parse_content_result(
    item: object,
    *,
    request_id: str | None,
    resolver: Resolver,
    max_text_chars: int,
) -> ExaContentResult | None:
    if not isinstance(item, Mapping):
        raise ExaSchemaError("Exa contents returned an invalid result row.")
    result_id, id_truncated = _bounded_text(item.get("id"), DEFAULT_ID_CHARS)
    if not result_id:
        return None
    raw_url, url_truncated = _bounded_text(item.get("url"), DEFAULT_URL_CHARS)
    normalized_url: str | None = None
    if raw_url:
        try:
            normalized_url = validate_public_source_url(
                raw_url,
                resolver=resolver,
            ).normalized_url
        except URLPolicyError:
            normalized_url = None
    title, title_truncated = _bounded_text(item.get("title"), DEFAULT_TITLE_CHARS)
    text, text_truncated = _bounded_text(item.get("text"), max_text_chars)
    highlights, highlights_truncated = _bounded_text_list(
        item.get("highlights"),
        max_items=5,
        max_chars=DEFAULT_MAX_HIGHLIGHT_CHARS,
    )
    published_date, date_truncated = _bounded_text(item.get("publishedDate"), DEFAULT_DATE_CHARS)
    author, author_truncated = _bounded_text(item.get("author"), DEFAULT_AUTHOR_CHARS)
    return ExaContentResult(
        id=result_id,
        url=normalized_url,
        title=title,
        text=text,
        highlights=highlights,
        published_date=published_date,
        author=author,
        request_id=request_id,
        status="success",
        source=None,
        error_tag=None,
        error_message=None,
        accessed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        provider="exa",
        truncated=any(
            (
                id_truncated,
                url_truncated,
                title_truncated,
                text_truncated,
                highlights_truncated,
                date_truncated,
                author_truncated,
            )
        ),
    )


def _status_only_content_result(
    status_id: str,
    *,
    status_row: Mapping[str, object],
    request_id: str | None,
    resolver: Resolver,
) -> ExaContentResult:
    normalized_url: str | None = None
    try:
        normalized_url = validate_public_source_url(status_id, resolver=resolver).normalized_url
    except URLPolicyError:
        normalized_url = None
    status, _ = _bounded_text(status_row.get("status"), 32)
    source, _ = _bounded_text(status_row.get("source"), 32)
    error_tag, error_message = _status_error_fields(status_row)
    return ExaContentResult(
        id=status_id,
        url=normalized_url,
        title=None,
        text=None,
        highlights=(),
        published_date=None,
        author=None,
        request_id=request_id,
        status=status or "error",
        source=source,
        error_tag=error_tag,
        error_message=error_message,
        accessed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        provider="exa",
        truncated=False,
    )


def _apply_status(
    result: ExaContentResult,
    *,
    status_row: Mapping[str, object],
) -> ExaContentResult:
    status, _ = _bounded_text(status_row.get("status"), 32)
    source, source_truncated = _bounded_text(status_row.get("source"), 32)
    error_tag, error_message = _status_error_fields(status_row)
    return ExaContentResult(
        id=result.id,
        url=result.url,
        title=result.title,
        text=result.text,
        highlights=result.highlights,
        published_date=result.published_date,
        author=result.author,
        request_id=result.request_id,
        status=status or result.status,
        source=source,
        error_tag=error_tag,
        error_message=error_message,
        accessed_at=result.accessed_at,
        provider=result.provider,
        truncated=result.truncated or source_truncated,
    )


def _status_error_fields(status_row: Mapping[str, object]) -> tuple[str | None, str | None]:
    error = status_row.get("error")
    if not isinstance(error, Mapping):
        return None, None
    error_tag, _ = _bounded_text(error.get("tag"), 64)
    message = error.get("message") or error.get("error")
    error_message, _ = _bounded_text(message, DEFAULT_ERROR_CHARS)
    return error_tag, error_message


def _bounded_response_bytes(response: httpx.Response, max_bytes: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise ExaTransientError("Exa returned a response that was too large.")
        except ValueError:
            pass
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > max_bytes:
            raise ExaTransientError("Exa returned a response that was too large.")
        body.extend(chunk)
    return bytes(body)


def _response_error(response: httpx.Response, body: bytes) -> ExaError:
    message, tag = _error_fields(body)
    if response.status_code == 401:
        return ExaAuthError(message or "The Exa API key was rejected.")
    if response.status_code == 402:
        return ExaQuotaExceededError(message or "Exa credits or budget are exhausted.")
    if response.status_code == 403:
        return ExaPermissionError(message or "Exa denied the requested operation.")
    if response.status_code == 429:
        return ExaRateLimitError(message or "Exa rate limited the request.")
    if response.status_code in RETRYABLE_STATUS_CODES:
        return ExaTransientError(message or "Exa is temporarily unavailable.")
    if response.status_code == 400:
        return ExaSchemaError(message or "Exa rejected the request shape.")
    if response.status_code == 422:
        return ExaError(message or "Exa could not fetch the requested content.")
    if tag == "INVALID_API_KEY":
        return ExaAuthError(message or "The Exa API key was rejected.")
    return ExaError(message or "Exa returned an error.")


def _error_fields(body: bytes) -> tuple[str | None, str | None]:
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return None, None
    if not isinstance(payload, Mapping):
        return None, None
    message, _ = _bounded_text(payload.get("error"), DEFAULT_ERROR_CHARS)
    tag, _ = _bounded_text(payload.get("tag"), 64)
    return message, tag


def _bounded_text(value: object, max_chars: int) -> tuple[str | None, bool]:
    if not isinstance(value, str):
        return None, False
    text = " ".join(value.split())
    if not text:
        return None, False
    if len(text) <= max_chars:
        return text, False
    return text[: max_chars - 1] + "…", True


def _bounded_text_list(
    value: object,
    *,
    max_items: int,
    max_chars: int,
) -> tuple[tuple[str, ...], bool]:
    if not isinstance(value, list):
        return (), False
    bounded: list[str] = []
    truncated = len(value) > max_items
    for entry in value[:max_items]:
        text, item_truncated = _bounded_text(entry, max_chars)
        truncated = truncated or item_truncated
        if text:
            bounded.append(text)
    return tuple(bounded), truncated


def _retry_delay(
    base_seconds: float,
    max_seconds: float,
    attempt: int,
    random_float: Callable[[], float],
) -> float:
    capped = min(max_seconds, base_seconds * (2**attempt))
    return capped * (1.0 + random_float() * 0.25)
