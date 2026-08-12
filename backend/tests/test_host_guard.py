"""The loopback API refuses requests whose `Host` is not a Lyra loopback host.

Lyra is unauthenticated and relies on loopback binding as its access boundary. CORS does
not close DNS rebinding, so a `Host` allowlist runs before every route. These tests pin
both the parser that decides which hosts are Lyra's and the middleware that enforces it.
"""

import pytest
from fastapi.testclient import TestClient

from backend.core.origins import host_is_allowed
from backend.main import create_app


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:8000",
        "127.0.0.1",
        "localhost:8000",
        "localhost",
        "localhost:3000",
        "LocalHost:8000",  # The name check is case-insensitive, as hostnames are.
        "[::1]:8000",
        "[::1]",
        "::1",  # A bare IPv6 literal a client sent unbracketed is still loopback.
    ],
)
def test_loopback_hosts_are_allowed(host: str) -> None:
    assert host_is_allowed(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "evil.example:8000",
        "evil.example",
        "attacker.localhost:8000",  # A subdomain of localhost is not localhost.
        "127.0.0.1.evil.example:8000",  # Loopback as a label, not the whole name.
        "notlocalhost",
        "",
        "   ",
        None,
    ],
)
def test_non_loopback_hosts_are_refused(host: str | None) -> None:
    # The rebinding vector is a hostname that resolves to 127.0.0.1 while a page stays
    # same-origin to it. None of these is a Lyra loopback host, so none may pass.
    assert host_is_allowed(host) is False


def _client() -> TestClient:
    """A client against the full application, without running the lifespan.

    Constructed without a `with` block on purpose: the middleware under test is part of the
    request path and runs regardless, while the lifespan would connect the database and
    start every background worker for a test that only sends headers.
    """
    return TestClient(create_app())


def test_a_read_route_is_refused_under_an_untrusted_host() -> None:
    # `testserver` is the TestClient default and is itself an untrusted host, so the guard
    # has to be told the real loopback name on every allowed request below.
    response = _client().get("/api/health/live", headers={"host": "evil.example:8000"})

    assert response.status_code == 400
    assert "Host header" in response.json()["detail"]


def test_a_state_changing_route_is_refused_under_an_untrusted_host() -> None:
    # The POST never reaches the handler: the guard wraps routing, so a create call under a
    # rebinding host fails before it can touch the database.
    response = _client().post(
        "/api/classes",
        json={"name": "Calculus II", "code": "MATH 201"},
        headers={"host": "evil.example:8000"},
    )

    assert response.status_code == 400
    assert "Host header" in response.json()["detail"]


def test_the_default_testserver_host_is_also_refused() -> None:
    # No `Origin` is sent here at all, which is the case CORS cannot see: the request is
    # refused on its `Host` alone.
    response = _client().get("/api/health/live")

    assert response.status_code == 400


@pytest.mark.parametrize(
    "host", ["127.0.0.1:8000", "localhost:8000", "LocalHost:8000", "[::1]:8000"]
)
def test_a_read_route_answers_under_a_loopback_host(host: str) -> None:
    response = _client().get("/api/health/live", headers={"host": host})

    assert response.status_code == 200
