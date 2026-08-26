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


# -- Integration: content-type and method coverage --------------------------------
#
# The middleware is method-and-path-agnostic (it fires for every request), but the
# browser's CORS simple-request rules mean certain content types and body shapes bypass
# preflight entirely.  The tests below prove the guard catches every browser-simple
# attack vector and representative routes from every module.


class TestMultipartUploadWithHostileOrigin:
    """multipart/form-data is a browser-simple content type that skips CORS preflight.

    An attacker can construct an invisible <form enctype="multipart/form-data"> targeting
    the document upload endpoint. The origin guard must reject it before any file is read.
    """

    def test_multipart_upload_blocked(self) -> None:
        r = _client().post(
            "/api/classes/1/documents",
            files={"file": ("evil.pdf", b"%PDF-fake", "application/pdf")},
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code == 403
        assert "origin" in r.json()["detail"].lower()

    def test_multipart_upload_allowed_with_trusted_origin(self) -> None:
        r = _client().post(
            "/api/classes/1/documents",
            files={"file": ("test.pdf", b"%PDF-fake", "application/pdf")},
            headers=_headers(origin=TRUSTED_ORIGIN),
        )
        assert r.status_code != 403


class TestFormUrlencodedWithHostileOrigin:
    """application/x-www-form-urlencoded is the default <form method=POST> content type.

    Like multipart, it is browser-simple and skips CORS preflight.
    """

    def test_form_urlencoded_blocked(self) -> None:
        r = _client().post(
            "/api/classes",
            data={"name": "Evil", "code": "EVIL 101"},
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code == 403


class TestNoBodyPostWithHostileOrigin:
    """POST with no body is browser-simple. Routes like /cancel and /start accept no body."""

    def test_empty_post_blocked(self) -> None:
        r = _client().post(
            "/api/documents/999/reingest",
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code == 403

    def test_empty_post_allowed_with_client_header(self) -> None:
        r = _client().post(
            "/api/documents/999/reingest",
            headers=_headers(client_header="cli"),
        )
        assert r.status_code != 403


class TestStreamingPostWithHostileOrigin:
    """SSE streaming endpoints (chat, regenerate, write) are still POST mutations.

    The origin guard must reject before any streaming response begins.
    """

    def test_chat_stream_blocked(self) -> None:
        r = _client().post(
            "/api/sessions/999/chat",
            json={"content": "hello"},
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code == 403

    def test_regenerate_stream_blocked(self) -> None:
        r = _client().post(
            "/api/sessions/999/regenerate",
            json={},
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code == 403

    def test_draft_write_stream_blocked(self) -> None:
        r = _client().post(
            "/api/drafts/999/write",
            json={},
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code == 403

    def test_writer_chat_stream_blocked(self) -> None:
        r = _client().post(
            "/api/drafts/999/chat/999",
            json={"content": "hello"},
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code == 403


class TestHeadAndOptionsIntegration:
    """HEAD and OPTIONS must pass through the origin guard regardless of Origin."""

    def test_head_with_hostile_origin_is_allowed(self) -> None:
        r = _client().head(
            "/api/health/live",
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code != 403

    def test_options_preflight_with_hostile_origin_is_allowed(self) -> None:
        r = _client().options(
            "/api/classes",
            headers=_headers(origin="https://evil.example"),
        )
        assert r.status_code != 403


class TestRepresentativeRoutesFromEachModule:
    """At least one state-changing route per module is tested against the guard."""

    _HOSTILE = "https://evil.example"

    @pytest.mark.parametrize(
        "method,path",
        [
            ("POST", "/api/classes/1/sessions/1/agent-chat"),
            ("POST", "/api/classes/1/sessions/1/agent-chat/retry"),
            ("PUT", "/api/classes/1/workspace"),
            ("PATCH", "/api/classes/1/workspace/grants"),
            ("POST", "/api/classes/1/sessions/1/workspace/changes/1/reject"),
        ],
    )
    def test_agent_routes_blocked(self, method: str, path: str) -> None:
        r = _client().request(method, path, json={}, headers=_headers(origin=self._HOSTILE))
        assert r.status_code == 403

    @pytest.mark.parametrize(
        "method,path",
        [
            ("PATCH", "/api/drafts/999"),
            ("DELETE", "/api/drafts/999"),
            ("POST", "/api/drafts/999/cancel"),
            ("POST", "/api/drafts/999/parts/999/restore"),
        ],
    )
    def test_draft_routes_blocked(self, method: str, path: str) -> None:
        r = _client().request(method, path, json={}, headers=_headers(origin=self._HOSTILE))
        assert r.status_code == 403

    @pytest.mark.parametrize(
        "method,path",
        [
            ("POST", "/api/classes/1/decks"),
            ("POST", "/api/classes/1/quizzes"),
            ("POST", "/api/quizzes/999/attempts"),
            ("POST", "/api/attempts/999/answers"),
            ("POST", "/api/attempts/999/finish"),
            ("POST", "/api/cards/999/review"),
        ],
    )
    def test_study_routes_blocked(self, method: str, path: str) -> None:
        r = _client().request(method, path, json={}, headers=_headers(origin=self._HOSTILE))
        assert r.status_code == 403

    @pytest.mark.parametrize(
        "method,path",
        [
            ("PATCH", "/api/classes/1/profile"),
            ("POST", "/api/classes/1/profile/confirm"),
        ],
    )
    def test_profile_routes_blocked(self, method: str, path: str) -> None:
        r = _client().request(method, path, json={}, headers=_headers(origin=self._HOSTILE))
        assert r.status_code == 403

    @pytest.mark.parametrize(
        "method,path",
        [
            ("PUT", "/api/drafts/999/plan"),
            ("PUT", "/api/classes/1/writer-settings"),
        ],
    )
    def test_writer_routes_blocked(self, method: str, path: str) -> None:
        r = _client().request(method, path, json={}, headers=_headers(origin=self._HOSTILE))
        assert r.status_code == 403

    @pytest.mark.parametrize(
        "method,path",
        [
            ("POST", "/api/classes/1/solutions"),
            ("POST", "/api/solutions/999/start"),
            ("POST", "/api/solutions/999/cancel"),
        ],
    )
    def test_solution_routes_blocked(self, method: str, path: str) -> None:
        r = _client().request(method, path, json={}, headers=_headers(origin=self._HOSTILE))
        assert r.status_code == 403


class TestPreflightCorsInteraction:
    """CORS preflight (OPTIONS with Access-Control-Request-Method) behaviour.

    The CORSMiddleware handles preflights and only allows listed origins. This verifies
    the interaction between the CORS middleware and the origin guard: the CORS middleware
    processes OPTIONS first (since it wraps everything), and the origin guard exempts
    OPTIONS as a safe method, so a hostile preflight gets a CORS denial, not a 403.
    """

    def test_preflight_from_trusted_origin_gets_cors_headers(self) -> None:
        r = _client().options(
            "/api/classes",
            headers={
                "host": TRUSTED_HOST,
                "Origin": TRUSTED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == TRUSTED_ORIGIN

    def test_preflight_from_hostile_origin_lacks_allow_header(self) -> None:
        r = _client().options(
            "/api/classes",
            headers={
                "host": TRUSTED_HOST,
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert "access-control-allow-origin" not in r.headers
