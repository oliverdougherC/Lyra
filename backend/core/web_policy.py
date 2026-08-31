"""Shared URL policy for local service endpoints and public source URLs.

The web boundary has two distinct checks:

- service endpoints must stay on loopback;
- source URLs must stay public HTTP(S) without credentials.

The policy is intentionally conservative. Anything that cannot be proven safe is refused.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

from backend.core.errors import ConfigurationError, LyraError

Resolver = Callable[..., list[tuple[object, ...]]]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_HTTP_SCHEMES = frozenset({"http", "https"})
_PUBLIC_DEFAULT_PORTS = {"http": 80, "https": 443}
_PRIVATE_TARGET_HOST_SUFFIXES = (".local",)


class URLPolicyError(LyraError):
    """A URL failed Lyra's safety policy."""


class LoopbackPolicyError(ConfigurationError):
    """A configured service endpoint is not safely local."""


@dataclass(frozen=True)
class URLDetails:
    """The normalized URL and the addresses it resolved to."""

    normalized_url: str
    hostname: str
    port: int
    addresses: tuple[IPAddress, ...]


def validate_loopback_service_base_url(url: str, *, resolver: Resolver | None = None) -> URLDetails:
    """Accept only an HTTP(S) base URL that resolves entirely to loopback."""
    active_resolver = resolver or socket.getaddrinfo
    parsed = _parse_http_url(url, allow_path=False, error_type=LoopbackPolicyError)
    port = _port_of(parsed)
    hostname = _normalized_hostname(parsed.hostname)
    addresses = _resolve_addresses(
        hostname,
        port,
        resolver=active_resolver,
        error_type=LoopbackPolicyError,
    )
    if not addresses or any(not address.is_loopback for address in addresses):
        raise LoopbackPolicyError("The service endpoint must point to a loopback address.")
    return URLDetails(
        normalized_url=_normalize_base_url(parsed),
        hostname=hostname,
        port=port,
        addresses=tuple(addresses),
    )


def validate_public_source_url(url: str, *, resolver: Resolver | None = None) -> URLDetails:
    """Accept only an initial public HTTP(S) source URL."""
    return _validate_public_http_url(url, resolver=resolver or socket.getaddrinfo)


def validate_public_source_final_url(url: str, *, resolver: Resolver | None = None) -> URLDetails:
    """Accept only a final public HTTP(S) source URL."""
    return _validate_public_http_url(url, resolver=resolver or socket.getaddrinfo)


def _validate_public_http_url(url: str, *, resolver: Resolver) -> URLDetails:
    parsed = _parse_http_url(url, allow_path=True, error_type=URLPolicyError)
    port = _port_of(parsed)
    hostname = _normalized_hostname(parsed.hostname)
    if hostname.lower() == "localhost" or hostname.lower().endswith(_PRIVATE_TARGET_HOST_SUFFIXES):
        raise URLPolicyError("Only public HTTP(S) URLs may be fetched.")
    addresses = _resolve_addresses(hostname, port, resolver=resolver, error_type=URLPolicyError)
    if not addresses or any(not address.is_global for address in addresses):
        raise URLPolicyError("Only public HTTP(S) URLs may be fetched.")
    return URLDetails(
        normalized_url=_normalize_public_url(parsed),
        hostname=hostname,
        port=port,
        addresses=tuple(addresses),
    )


def _parse_http_url(url: str, *, allow_path: bool, error_type: type[Exception]) -> SplitResult:
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in _HTTP_SCHEMES or not parsed.hostname:
        raise error_type("Only HTTP(S) URLs are allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise error_type("URLs containing credentials are not allowed.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise error_type("The URL uses an invalid port.") from exc
    if not allow_path and (parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise error_type("Service endpoints must be configured as a base URL only.")
    return parsed


def _port_of(parsed: SplitResult) -> int:
    return parsed.port or _PUBLIC_DEFAULT_PORTS[parsed.scheme.lower()]


def _normalized_hostname(hostname: str | None) -> str:
    if hostname is None:
        raise URLPolicyError("The URL is missing a hostname.")
    return hostname.rstrip(".")


def _normalize_base_url(parsed: SplitResult) -> str:
    netloc = parsed.netloc.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def _normalize_public_url(parsed: SplitResult) -> str:
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, parsed.query, parsed.fragment))


def _resolve_addresses(
    hostname: str,
    port: int,
    *,
    resolver: Resolver,
    error_type: type[Exception],
) -> list[IPAddress]:
    try:
        return [ipaddress.ip_address(hostname)]
    except ValueError:
        pass

    try:
        records = resolver(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise error_type("The hostname could not be resolved.") from exc

    addresses: list[IPAddress] = []
    for record in records:
        try:
            candidate = ipaddress.ip_address(record[4][0])
        except (IndexError, TypeError, ValueError):
            continue
        if candidate not in addresses:
            addresses.append(candidate)
    if not addresses:
        raise error_type("The hostname could not be resolved.")
    return addresses
