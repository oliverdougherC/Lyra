"""Explicitly gated, SSRF-resistant web search and bounded source fetching.

No network call in this module can happen accidentally: both public entry points require
the caller to pass ``allowed=True`` after resolving global/per-class policy. The HTTP
client and DNS resolver are injectable, keeping every security branch unit-testable.
"""

import html
import ipaddress
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlsplit

import httpx

from backend.core import firecrawl, query_guard

SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/?q={query}"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_BYTES = 1024 * 1024
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_USER_AGENT = "Lyra/0.1 writer-research"

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_TEXT_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/markdown",
        "application/json",
        "application/xml",
        "text/xml",
    }
)

Resolver = Callable[..., list[tuple[object, ...]]]


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
    """Validate an HTTP(S) URL and every currently resolved address as public.

    Rejecting all non-global addresses is intentionally stronger than checking only the
    three named private ranges: it also closes unspecified, reserved, and multicast
    destinations that have no place in writer research.
    """
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise UnsafeURLError("Only public http and https URLs may be fetched.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("URLs containing credentials may not be fetched.")
    hostname = parsed.hostname.rstrip(".")
    if hostname.lower() == "localhost":
        raise UnsafeURLError("Local and private addresses may not be fetched.")
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = [literal]
    except ValueError:
        try:
            records = resolver(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        except (OSError, socket.gaierror) as exc:
            raise WebResearchError(f"Could not resolve source host: {hostname}") from exc
        addresses = []
        for record in records:
            try:
                addresses.append(ipaddress.ip_address(record[4][0]))
            except (IndexError, TypeError, ValueError):
                continue
        if not addresses:
            raise WebResearchError(f"Could not resolve source host: {hostname}") from None
    if any(not address.is_global for address in addresses):
        raise UnsafeURLError("Local and private addresses may not be fetched.")
    return value


def _bounded_body(response: httpx.Response, max_bytes: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise ResponseTooLargeError("The source is too large to snapshot.")
        except ValueError:
            pass
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > max_bytes:
            raise ResponseTooLargeError("The source is too large to snapshot.")
        body.extend(chunk)
    return bytes(body)


def _request_text(
    url: str,
    *,
    client: httpx.Client,
    resolver: Resolver,
    timeout_seconds: float,
    max_bytes: int,
    max_redirects: int,
) -> tuple[str, str, str]:
    current = url
    for redirect_count in range(max_redirects + 1):
        current = validate_public_url(current, resolver=resolver)
        try:
            headers = {
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1",
            }
            with client.stream(
                "GET", current, headers=headers, timeout=timeout_seconds
            ) as response:
                if response.status_code in _REDIRECTS:
                    location = response.headers.get("location")
                    if not location:
                        raise WebResearchError("The source returned an empty redirect.")
                    if redirect_count == max_redirects:
                        raise WebResearchError("The source redirected too many times.")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type not in _TEXT_TYPES:
                    raise UnsupportedContentTypeError(
                        f"The source content type is not supported: {content_type or 'unknown'}"
                    )
                body = _bounded_body(response, max_bytes)
                encoding = response.encoding or "utf-8"
                return current, content_type, body.decode(encoding, errors="replace")
        except httpx.HTTPError as exc:
            raise WebResearchError(f"Could not fetch source: {exc}") from exc
    raise WebResearchError("The source redirected too many times.")


class _ReadableHTML(HTMLParser):
    """Tiny snapshot normalizer: title plus visible text, without scripts or styles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self.hidden_depth += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg") and self.hidden_depth:
            self.hidden_depth -= 1
        if tag == "title":
            self.in_title = False
        if tag in ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr"):
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
        self.text_parts.append(data)

    def result(self) -> tuple[str, str]:
        title = " ".join(" ".join(self.title_parts).split())
        lines = [" ".join(line.split()) for line in "".join(self.text_parts).splitlines()]
        return title, "\n".join(line for line in lines if line)


def fetch_source(
    url: str,
    *,
    allowed: bool,
    client: httpx.Client | None = None,
    resolver: Resolver = socket.getaddrinfo,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    firecrawl_base_url: str = firecrawl.DEFAULT_BASE_URL,
    scrape_enabled: bool = True,
) -> dict[str, object]:
    """Fetch and normalize one public textual page into a bounded ledger snapshot."""
    _require_allowed(allowed)
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if max_redirects < 0:
        raise ValueError("max_redirects cannot be negative")
    if client is None:
        if not scrape_enabled:
            raise WebResearchError(
                "Firecrawl scraping is disabled until its redirect safety check passes."
            )
        try:
            fetched = firecrawl.FirecrawlClient(
                base_url=firecrawl_base_url,
                resolver=resolver,
                read_timeout_seconds=timeout_seconds,
                max_response_bytes=max_bytes,
            ).scrape(url, timeout_ms=min(int(timeout_seconds * 1_000), 60_000))
        except (firecrawl.FirecrawlError, ValueError) as exc:
            raise WebResearchError(str(exc)) from exc
        return {
            "url": fetched.url,
            "final_url": fetched.final_url,
            "title": fetched.title,
            "accessed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "content_type": fetched.content_type,
            "snapshot": fetched.markdown,
            "truncated": fetched.truncated,
            "warning": fetched.warning,
        }
    owned = client is None
    active_client = client or httpx.Client(
        timeout=httpx.Timeout(timeout_seconds), follow_redirects=False
    )
    try:
        final_url, content_type, raw_text = _request_text(
            url,
            client=active_client,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            max_redirects=max_redirects,
        )
    finally:
        if owned:
            active_client.close()
    title = ""
    snapshot = raw_text
    if content_type in ("text/html", "application/xhtml+xml"):
        parser = _ReadableHTML()
        parser.feed(raw_text)
        title, snapshot = parser.result()
    if not title:
        title = urlsplit(final_url).hostname or final_url
    return {
        "url": final_url,
        "final_url": final_url,
        "title": title,
        "accessed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "content_type": content_type,
        "snapshot": snapshot,
        "truncated": False,
        "warning": None,
    }


class _SearchHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current_href: str | None = None
        self.current_text: list[str] = []
        self.results: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if "result__a" in classes and values.get("href"):
            self.current_href = values["href"]
            self.current_text = []

    def handle_data(self, data: str) -> None:
        if self.current_href is not None:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self.current_href is None:
            return
        href = html.unescape(self.current_href)
        parsed = urlsplit(href)
        if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
            target = parse_qs(parsed.query).get("uddg", [href])[0]
            href = unquote(target)
        title = " ".join(" ".join(self.current_text).split())
        if title and urlsplit(href).scheme in ("http", "https"):
            self.results.append({"title": title, "url": href})
        self.current_href = None
        self.current_text = []


def search_web(
    query: str,
    *,
    allowed: bool,
    max_results: int = 5,
    client: httpx.Client | None = None,
    resolver: Resolver = socket.getaddrinfo,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = 512 * 1024,
    firecrawl_base_url: str = firecrawl.DEFAULT_BASE_URL,
    private_context: tuple[str, ...] = (),
) -> list[dict[str, str]]:
    """Search through loopback Firecrawl; ``client`` retains the legacy test seam."""
    _require_allowed(allowed)
    guarded = query_guard.guard_web_query(query, private_context=private_context)
    if isinstance(guarded, query_guard.QueryRefusal):
        raise ValueError(guarded.message)
    clean_query = guarded.query
    if max_results < 1 or max_results > firecrawl.DEFAULT_SEARCH_RESULTS:
        raise ValueError(f"max_results must be between 1 and {firecrawl.DEFAULT_SEARCH_RESULTS}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if client is None:
        try:
            results = firecrawl.FirecrawlClient(
                base_url=firecrawl_base_url,
                resolver=resolver,
                read_timeout_seconds=timeout_seconds,
                max_response_bytes=max_bytes,
            ).search(
                clean_query,
                limit=max_results,
                timeout_ms=min(int(timeout_seconds * 1_000), 15_000),
            )
        except (firecrawl.FirecrawlError, ValueError) as exc:
            raise WebResearchError(str(exc)) from exc
        return [
            {"title": result.title, "url": result.url, "description": result.description}
            for result in results
        ]
    endpoint = SEARCH_ENDPOINT.format(query=quote_plus(clean_query))
    owned = client is None
    active_client = client or httpx.Client(
        timeout=httpx.Timeout(timeout_seconds), follow_redirects=False
    )
    try:
        _, content_type, page = _request_text(
            endpoint,
            client=active_client,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            max_redirects=1,
        )
    finally:
        if owned:
            active_client.close()
    if content_type not in ("text/html", "application/xhtml+xml"):
        raise UnsupportedContentTypeError("The search endpoint did not return HTML.")
    parser = _SearchHTMLParser()
    parser.feed(page)
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for result in parser.results:
        if result["url"] in seen:
            continue
        seen.add(result["url"])
        unique.append(result)
        if len(unique) == max_results:
            break
    return unique
