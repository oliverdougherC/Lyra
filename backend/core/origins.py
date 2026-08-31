"""Loopback browser origins and hosts trusted by Lyra's local HTTP boundary."""

import os
from urllib.parse import urlsplit

_BASE_BROWSER_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
    "http://tauri.localhost",
)


def configured_browser_origins(value: str | None) -> tuple[str, ...]:
    """Parse explicit contributor/test origins, accepting loopback HTTP only."""
    origins: list[str] = []
    for raw in (value or "").split(","):
        origin = raw.strip()
        if not origin:
            continue
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
        ):
            raise RuntimeError(
                "LYRA_BROWSER_ORIGINS accepts loopback HTTP origins with ports only."
            )
        origins.append(origin.rstrip("/"))
    return tuple(dict.fromkeys(origins))


ALLOWED_BROWSER_ORIGINS = _BASE_BROWSER_ORIGINS + configured_browser_origins(
    os.environ.get("LYRA_BROWSER_ORIGINS")
)

ALLOWED_BROWSER_ORIGIN_SET = frozenset(ALLOWED_BROWSER_ORIGINS)

# The hostnames a request's `Host` header may name. This is a security boundary, not a
# convenience list. Source mode treats loopback as an access boundary and packaged mode
# adds session authentication; CORS still does not close the DNS-rebinding case: a page can
# stay same-origin to a hostname it controls while that hostname is rebound to 127.0.0.1,
# so the browser's own same-origin rules stop protecting the loopback API. What the page
# cannot forge from JavaScript is the `Host` value the browser puts on the request, so a
# `Host` that is not one of these is refused before any route runs.
#
# Only loopback *literals* and `localhost` belong here. A literal IP address cannot be
# rebound because it does not resolve, so `127.0.0.1` and `::1` are safe by construction;
# `localhost` is the single name allowed, because the launcher and frontend use it and
# every OS pins it to loopback. Any other registered name is precisely the rebinding
# vector this guards against. `::1` is included so a dual-stack or IPv6-preferring dev
# setup is not broken even though the backend binds IPv4 loopback today; allowing a
# loopback literal costs nothing because it can never be an attacker's rebind target.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Non-browser loopback clients (the launcher health probe, CLI scripts, test harnesses)
# cannot send a browser Origin header. Rather than silently allowing a missing Origin on
# unsafe methods -- which would leave the CSRF boundary open to any HTTP client on the
# machine -- we require these callers to send a custom header that a browser simple
# request cannot carry. Any value in `X-Lyra-Client` is accepted; the header's presence
# is the signal, because a cross-origin browser request carrying a non-safelisted header
# triggers a CORS preflight that the CORS middleware will reject for untrusted origins.
LOOPBACK_CLIENT_HEADER = "X-Lyra-Client"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def host_is_allowed(host_header: str | None) -> bool:
    """Whether an incoming `Host` header names a Lyra loopback host.

    The port is ignored on purpose: which port a request arrived on is not something an
    attacker's page controls, so it carries no security signal, and pinning it would only
    break the launcher health probe and the dev server, which reach the backend on their
    own ports. The hostname is the whole question.

    A missing or empty `Host` is refused. Every browser and proxy sends one, so its absence
    is either a malformed client or a deliberate attempt to slip past a name check.
    """
    if not host_header:
        return False
    host = host_header.strip()
    if not host:
        return False
    if host.startswith("["):
        # An IPv6 host arrives bracketed, with or without a port: `[::1]` or `[::1]:8000`.
        closing = host.find("]")
        if closing == -1:
            return False
        hostname = host[1:closing]
    elif host.count(":") == 1:
        # Exactly one colon is a `hostname:port` split. Two or more, unbracketed, is a bare
        # IPv6 literal a sloppy client sent without brackets; leave it whole rather than
        # truncating it at the first group.
        hostname = host.rsplit(":", 1)[0]
    else:
        hostname = host
    return hostname.lower() in ALLOWED_HOSTS


def mutation_origin_is_acceptable(
    method: str, origin: str | None, has_client_header: bool
) -> bool | None:
    """Whether a request's origin credentials satisfy the mutation boundary.

    Returns ``True`` for acceptable requests, ``False`` for rejected ones, and
    ``None`` for safe methods that are not subject to origin enforcement.
    """
    if method.upper() in _SAFE_METHODS:
        return None

    if origin is not None:
        return origin.strip() in ALLOWED_BROWSER_ORIGIN_SET

    # No Origin header: the request did not come from a browser (browsers always
    # send Origin on cross-origin requests and on same-origin POSTs). Accept only
    # if the caller proved non-browser intent with the client header.
    return has_client_header
