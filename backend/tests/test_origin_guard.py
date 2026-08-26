"""The loopback API rejects untrusted browser origins on every state-changing request.

Lyra's CORS middleware withholds *response-read* permission from hostile pages, but a
simple cross-origin POST (form or no-cors fetch) is still dispatched.  The origin guard
closes this by requiring every unsafe-method request to carry either a trusted browser
Origin or a non-browser client header before the body is parsed.

See also: `test_host_guard.py` for the complementary Host check and
`test_confirmations.py` for the workspace/command confirmation-token contract.
"""

import pytest
from fastapi.testclient import TestClient

from backend.core.origins import (
    LOOPBACK_CLIENT_HEADER,
    mutation_origin_is_acceptable,
)
from backend.main import create_app

TRUSTED_HOST = "127.0.0.1:8000"
TRUSTED_ORIGIN = "http://localhost:3000"
ALT_TRUSTED_ORIGIN = "http://127.0.0.1:3000"

# -- Unit: mutation_origin_is_acceptable ------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get", "options"])
def test_safe_methods_are_exempt(method: str) -> None:
    assert mutation_origin_is_acceptable(method, "https://evil.example", False) is None


@pytest.mark.parametrize("origin", [TRUSTED_ORIGIN, ALT_TRUSTED_ORIGIN])
def test_trusted_browser_origin_is_accepted(origin: str) -> None:
    assert mutation_origin_is_acceptable("POST", origin, False) is True


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://localhost:9999",
        "http://evil.localhost:3000",
        "null",
        "",
        "   ",
    ],
)
def test_untrusted_origins_are_rejected(origin: str) -> None:
    assert mutation_origin_is_acceptable("POST", origin, False) is False


def test_missing_origin_without_client_header_is_rejected() -> None:
    assert mutation_origin_is_acceptable("POST", None, False) is False


def test_missing_origin_with_client_header_is_accepted() -> None:
    assert mutation_origin_is_acceptable("POST", None, True) is True


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_all_unsafe_methods_are_enforced(method: str) -> None:
    assert mutation_origin_is_acceptable(method, "https://evil.example", False) is False
    assert mutation_origin_is_acceptable(method, TRUSTED_ORIGIN, False) is True


# -- Integration: middleware against the full app ---------------------------------


def _client() -> TestClient:
    """Full-app client without lifespan (no DB or workers needed for middleware tests).

    ``raise_server_exceptions=False`` because tests that prove a request *passed*
    the middleware only need to see a non-403 status code, and the handler behind
    the middleware will raise an unrelated DB error when there is no database.
    """
    return TestClient(create_app(), raise_server_exceptions=False)


def _headers(
    *,
    host: str = TRUSTED_HOST,
    origin: str | None = None,
    client_header: str | None = None,
) -> dict[str, str]:
    h: dict[str, str] = {"host": host}
    if origin is not None:
        h["Origin"] = origin
    if client_header is not None:
        h[LOOPBACK_CLIENT_HEADER] = client_header
    return h


# Hostile origin against a representative state-changing route.
class TestHostileOriginIsBlocked:
    def test_post_with_hostile_origin(self) -> None:
        r = _client().post(
            "/api/classes",
            json={"name": "Evil", "code": "EVIL 101"},
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code == 403
        assert "origin" in r.json()["detail"].lower()

    def test_put_with_hostile_origin(self) -> None:
        r = _client().put(
            "/api/settings",
            json={},
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code == 403

    def test_patch_with_hostile_origin(self) -> None:
        r = _client().patch(
            "/api/solutions/999",
            json={"name": "renamed"},
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code == 403

    def test_delete_with_hostile_origin(self) -> None:
        r = _client().delete(
            "/api/solutions/999",
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code == 403


class TestNullAndMalformedOrigins:
    def test_null_origin_is_rejected(self) -> None:
        r = _client().post(
            "/api/classes",
            json={"name": "Test", "code": "T 101"},
            headers=_headers(origin="null"),
        )
        assert r.status_code == 403

    def test_empty_origin_is_rejected(self) -> None:
        r = _client().post(
            "/api/classes",
            json={"name": "Test", "code": "T 101"},
            headers=_headers(origin=""),
        )
        assert r.status_code == 403

    def test_whitespace_origin_is_rejected(self) -> None:
        r = _client().post(
            "/api/classes",
            json={"name": "Test", "code": "T 101"},
            headers=_headers(origin="   "),
        )
        assert r.status_code == 403

    def test_wrong_port_origin_is_rejected(self) -> None:
        r = _client().post(
            "/api/classes",
            json={"name": "Test", "code": "T 101"},
            headers=_headers(origin="http://localhost:9999"),
        )
        assert r.status_code == 403


class TestMissingOriginPolicy:
    def test_missing_origin_without_client_header_is_rejected(self) -> None:
        r = _client().post(
            "/api/classes",
            json={"name": "Test", "code": "T 101"},
            headers=_headers(),
        )
        assert r.status_code == 403

    def test_missing_origin_with_client_header_is_accepted(self) -> None:
        r = _client().post(
            "/api/classes",
            json={"name": "Test", "code": "T 101"},
            headers=_headers(client_header="test"),
        )
        # Should pass the origin guard; a 500/422/etc. from the handler is fine --
        # the point is it was NOT 403 from the origin guard.
        assert r.status_code != 403


class TestTrustedOriginsPass:
    @pytest.mark.parametrize("origin", [TRUSTED_ORIGIN, ALT_TRUSTED_ORIGIN])
    def test_trusted_origin_reaches_handler(self, origin: str) -> None:
        r = _client().post(
            "/api/classes",
            json={"name": "Origin Test", "code": "OT 100"},
            headers=_headers(origin=origin),
        )
        # Not 403 -- the origin guard let it through. Could be 500 if no DB.
        assert r.status_code != 403


class TestSafeMethodsAreExempt:
    def test_get_with_hostile_origin_is_allowed(self) -> None:
        r = _client().get(
            "/api/health/live",
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code == 200

    def test_get_without_any_origin_is_allowed(self) -> None:
        r = _client().get(
            "/api/health/live",
            headers=_headers(),
        )
        assert r.status_code == 200


class TestTrustedHostPlusHostileOrigin:
    """A trusted Host does not rescue a hostile Origin on unsafe methods."""

    def test_trusted_host_hostile_origin(self) -> None:
        r = _client().post(
            "/api/classes",
            json={"name": "Test", "code": "T 101"},
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code == 403


class TestHostileHostPlusTrustedOrigin:
    """A trusted Origin does not rescue a hostile Host."""

    def test_hostile_host_trusted_origin(self) -> None:
        r = _client().post(
            "/api/classes",
            json={"name": "Test", "code": "T 101"},
            headers={"host": "evil.example:8000", "Origin": TRUSTED_ORIGIN},
        )
        assert r.status_code == 400
        assert "Host header" in r.json()["detail"]


class TestErrorResponseDoesNotReflectOrigin:
    """The rejection must not echo the attacker-controlled Origin value."""

    def test_hostile_origin_is_not_reflected(self) -> None:
        injected = "https://evil.example/<script>alert(1)</script>"
        r = _client().post(
            "/api/classes",
            json={"name": "XSS", "code": "X 101"},
            headers=_headers(origin=injected),
        )
        assert r.status_code == 403
        body = r.text
        assert "evil.example" not in body
        assert "<script>" not in body
