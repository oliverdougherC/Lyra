"""Settings API contract, with a fake keychain and no DNS.

The tutor key and Exa key are the sensitive parts: they are accepted, stored, and never
echoed back.
"""

import socket
import sqlite3
from collections.abc import Iterator

import keyring.errors
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_settings
from backend.core import exa
from backend.core.app_settings import get_settings_row, update_settings_row
from backend.llm import client as client_module
from backend.storage import secrets
from backend.storage.database import connect, get_db

RESOLUTIONS = {
    "127.0.0.1": ["127.0.0.1"],
    "tutor.example.com": ["203.0.113.10"],
}


class FakeKeyring:
    """Stands in for the `keyring` module, so no test reaches the login keychain."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError("no such password")
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    return fake


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, port: object, *args: object, **kwargs: object) -> list[tuple]:
        try:
            addresses = RESOLUTIONS[host]
        except KeyError:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known") from None
        return [
            (socket.AF_UNSPEC, socket.SOCK_STREAM, 0, "", (address, 0)) for address in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    """An app carrying only the settings router, over the migrated temporary database.

    The override opens its own connection per request rather than handing over the `db`
    fixture's. Sync handlers run in a threadpool, and a sqlite3 connection may only be
    used on the thread that created it.
    """

    def override_get_db() -> Iterator[sqlite3.Connection]:
        conn = connect()
        try:
            yield conn
        finally:
            conn.close()

    app = FastAPI()
    app.include_router(routes_settings.router)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client


def test_get_settings_never_exposes_the_keys(client: TestClient) -> None:
    body = client.get("/api/settings").json()

    assert "api_key" not in body
    assert "exa_api_key" not in body
    assert body["api_key_set"] is False
    assert body["api_key_storage"] == "keychain"
    assert body["exa_api_key_set"] is False
    assert body["exa_api_key_storage"] == "keychain"


def test_put_stores_the_key_without_returning_it(
    client: TestClient, fake_keyring: FakeKeyring
) -> None:
    body = client.put("/api/settings", json={"api_key": "sk-secret"}).json()

    assert fake_keyring.store[(secrets.SERVICE, secrets.USERNAME)] == "sk-secret"
    assert body["api_key_set"] is True
    assert "api_key" not in body
    assert "sk-secret" not in client.get("/api/settings").text


def test_put_stores_the_exa_key_without_returning_it(
    client: TestClient, fake_keyring: FakeKeyring
) -> None:
    body = client.put("/api/settings", json={"exa_api_key": "exa-secret"}).json()

    assert fake_keyring.store[(secrets.EXA_SERVICE, secrets.EXA_USERNAME)] == "exa-secret"
    assert body["exa_api_key_set"] is True
    assert "exa_api_key" not in body
    assert "exa-secret" not in client.get("/api/settings").text


def test_empty_api_key_deletes_the_stored_one(client: TestClient) -> None:
    client.put("/api/settings", json={"api_key": "sk-secret"})

    body = client.put("/api/settings", json={"api_key": ""}).json()

    assert body["api_key_set"] is False


def test_empty_exa_api_key_deletes_the_stored_one(client: TestClient) -> None:
    client.put("/api/settings", json={"exa_api_key": "exa-secret"})

    body = client.put("/api/settings", json={"exa_api_key": ""}).json()

    assert body["exa_api_key_set"] is False


def test_changing_the_endpoint_withdraws_the_acknowledgement(client: TestClient) -> None:
    client.put(
        "/api/settings",
        json={"endpoint_url": "https://tutor.example.com/v1", "remote_ack": True},
    )
    assert client.get("/api/settings").json()["remote_ack"] is True

    body = client.put("/api/settings", json={"endpoint_url": "http://127.0.0.1:8080/v1"}).json()

    assert body["remote_ack"] is False


def test_unchanged_endpoint_keeps_the_acknowledgement(client: TestClient) -> None:
    client.put(
        "/api/settings",
        json={"endpoint_url": "https://tutor.example.com/v1", "remote_ack": True},
    )

    body = client.put(
        "/api/settings",
        json={"endpoint_url": "https://tutor.example.com/v1", "model": "qwen"},
    ).json()

    assert body["remote_ack"] is True
    assert body["model"] == "qwen"


def test_locality_is_reported_per_endpoint(client: TestClient) -> None:
    unset = client.get("/api/settings").json()
    assert unset["endpoint_is_local"] is None
    assert unset["endpoint_host"] is None

    local = client.put("/api/settings", json={"endpoint_url": "http://127.0.0.1:8080/v1"}).json()
    assert local["endpoint_is_local"] is True
    assert local["endpoint_host"] == "127.0.0.1"

    remote = client.put(
        "/api/settings", json={"endpoint_url": "https://tutor.example.com/v1"}
    ).json()
    assert remote["endpoint_is_local"] is False


def test_context_window_below_the_floor_is_rejected(client: TestClient) -> None:
    assert client.put("/api/settings", json={"context_window": 128}).status_code == 422


def test_writer_capability_settings_roundtrip(client: TestClient) -> None:
    defaults = client.get("/api/settings").json()
    assert defaults["allow_web_research"] is False
    assert defaults["parallel_requests"] is False
    assert defaults["parallel_concurrency"] == 1

    updated = client.put(
        "/api/settings",
        json={
            "allow_web_research": True,
            "parallel_requests": True,
            "parallel_concurrency": 3,
        },
    ).json()

    assert updated["allow_web_research"] is True
    assert updated["parallel_requests"] is True
    assert updated["parallel_concurrency"] == 3


def test_exa_settings_probe_reports_availability(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    defaults = client.get("/api/settings").json()
    assert defaults["allow_web_research"] is False
    assert defaults["exa_api_key_set"] is False

    monkeypatch.setattr(routes_settings.secrets, "get_exa_api_key", lambda: "exa-secret")

    class ReadyExa:
        def __init__(self, *, api_key: str, **kwargs: object) -> None:
            assert api_key == "exa-secret"
            del kwargs

        def check_readiness(self) -> dict[str, object]:
            return {"status": "ok"}

    monkeypatch.setattr(routes_settings.exa, "ExaClient", ReadyExa)
    result = client.post("/api/settings/test-exa")
    assert result.status_code == 200
    assert result.json() == {
        "ok": True,
        "status": "available",
        "message": "Exa is available.",
    }
    assert client.put("/api/settings", json={"parallel_concurrency": 0}).status_code == 422


def test_exa_settings_probe_reports_missing_key(client: TestClient) -> None:
    result = client.post("/api/settings/test-exa")

    assert result.status_code == 200
    assert result.json() == {
        "ok": False,
        "status": "missing_key",
        "message": "No Exa API key is configured.",
    }


def test_exa_settings_probe_reports_temporary_unavailability(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_settings.secrets, "get_exa_api_key", lambda: "exa-secret")

    class UnavailableExa:
        def __init__(self, *, api_key: str, **kwargs: object) -> None:
            assert api_key == "exa-secret"
            del kwargs

        def check_readiness(self) -> dict[str, object]:
            raise routes_settings.exa.ExaTransientError("temporarily down")

    monkeypatch.setattr(routes_settings.exa, "ExaClient", UnavailableExa)

    result = client.post("/api/settings/test-exa")

    assert result.status_code == 200
    assert result.json() == {
        "ok": False,
        "status": "temporarily_unavailable",
        "message": "Exa is temporarily unavailable; web research is disabled.",
    }


def test_exa_settings_probe_reports_invalid_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_settings.secrets, "get_exa_api_key", lambda: "exa-secret")

    class MisconfiguredExa:
        def __init__(self, *, api_key: str, **kwargs: object) -> None:
            assert api_key == "exa-secret"
            del kwargs

        def check_readiness(self) -> dict[str, object]:
            raise routes_settings.exa.ExaAuthError("wrong endpoint")

    monkeypatch.setattr(routes_settings.exa, "ExaClient", MisconfiguredExa)

    result = client.post("/api/settings/test-exa")

    assert result.status_code == 200
    assert result.json() == {
        "ok": False,
        "status": "invalid_key",
        "message": "The Exa API key is invalid or not authorized.",
    }


def test_exa_settings_probe_reports_other_distinct_failures(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_settings.secrets, "get_exa_api_key", lambda: "exa-secret")

    class QuotaExa:
        def __init__(self, *, api_key: str, **kwargs: object) -> None:
            assert api_key == "exa-secret"
            del kwargs

        def check_readiness(self) -> dict[str, object]:
            raise exa.ExaQuotaExceededError("no credits")

    monkeypatch.setattr(routes_settings.exa, "ExaClient", QuotaExa)
    assert client.post("/api/settings/test-exa").json()["status"] == "quota_exhausted"

    class RateLimitedExa:
        def __init__(self, *, api_key: str, **kwargs: object) -> None:
            assert api_key == "exa-secret"
            del kwargs

        def check_readiness(self) -> dict[str, object]:
            raise exa.ExaRateLimitError("slow down")

    monkeypatch.setattr(routes_settings.exa, "ExaClient", RateLimitedExa)
    assert client.post("/api/settings/test-exa").json()["status"] == "rate_limited"

    class TimedOutExa:
        def __init__(self, *, api_key: str, **kwargs: object) -> None:
            assert api_key == "exa-secret"
            del kwargs

        def check_readiness(self) -> dict[str, object]:
            raise exa.ExaTimeoutError("slow")

    monkeypatch.setattr(routes_settings.exa, "ExaClient", TimedOutExa)
    assert client.post("/api/settings/test-exa").json()["status"] == "timeout"

    class OfflineExa:
        def __init__(self, *, api_key: str, **kwargs: object) -> None:
            assert api_key == "exa-secret"
            del kwargs

        def check_readiness(self) -> dict[str, object]:
            raise exa.ExaOfflineError("offline")

    monkeypatch.setattr(routes_settings.exa, "ExaClient", OfflineExa)
    assert client.post("/api/settings/test-exa").json()["status"] == "offline"

    class MalformedExa:
        def __init__(self, *, api_key: str, **kwargs: object) -> None:
            assert api_key == "exa-secret"
            del kwargs

        def check_readiness(self) -> dict[str, object]:
            raise exa.ExaSchemaError("bad response")

    monkeypatch.setattr(routes_settings.exa, "ExaClient", MalformedExa)
    assert client.post("/api/settings/test-exa").json()["status"] == "malformed_response"

    class PermissionDeniedExa:
        def __init__(self, *, api_key: str, **kwargs: object) -> None:
            assert api_key == "exa-secret"
            del kwargs

        def check_readiness(self) -> dict[str, object]:
            raise exa.ExaPermissionError("feature disabled")

    monkeypatch.setattr(routes_settings.exa, "ExaClient", PermissionDeniedExa)
    assert client.post("/api/settings/test-exa").json()["status"] == "permission_denied"


def test_repointing_the_endpoint_forgets_what_was_measured_about_tool_support(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """Tool support is a property of the server and model together, not of Lyra."""
    client.put("/api/settings", json={"endpoint_url": "http://127.0.0.1:8080/v1"})
    update_settings_row(db, {"tools_supported": 1, "tools_message": "It can."})

    body = client.put("/api/settings", json={"endpoint_url": "http://127.0.0.1:9090/v1"}).json()

    # Null, not false: nobody has asked the new endpoint, and "not asked" and "asked and
    # no" cost the student different things.
    assert body["tools_supported"] is None
    assert body["tools_message"] is None


def test_changing_only_the_model_also_forgets_it(
    client: TestClient, db: sqlite3.Connection
) -> None:
    client.put("/api/settings", json={"endpoint_url": "http://127.0.0.1:8080/v1"})
    update_settings_row(db, {"tools_supported": 1, "tools_message": "It can."})

    body = client.put("/api/settings", json={"model": "some-other-model"}).json()

    assert body["tools_supported"] is None


def test_an_unrelated_setting_leaves_tool_support_alone(
    client: TestClient, db: sqlite3.Connection
) -> None:
    client.put("/api/settings", json={"endpoint_url": "http://127.0.0.1:8080/v1"})
    update_settings_row(db, {"tools_supported": 1, "tools_message": "It can."})

    body = client.put("/api/settings", json={"context_window": 16384}).json()

    assert body["tools_supported"] is True
    assert body["tools_message"] == "It can."


def test_the_vision_probe_is_recorded_so_recognition_need_not_ask_again(
    client: TestClient, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stored, so a document row can offer or withhold recognition without a round trip."""
    client.put("/api/settings", json={"endpoint_url": "http://127.0.0.1:8080/v1"})

    async def probe(
        endpoint: str, api_key: str | None, model: str | None
    ) -> client_module.VisionSupport:
        return client_module.VisionSupport(ok=False, message="It answered 00000.")

    monkeypatch.setattr(client_module, "probe_vision_support", probe)
    body = client.post("/api/settings/test-vision").json()

    assert body == {"ok": False, "message": "It answered 00000."}
    row = get_settings_row(db)
    # False, not null: this endpoint was asked and cannot see, which is an ordinary
    # configuration rather than an error, and the interface says so plainly.
    assert (row["vision_supported"], row["vision_message"]) == (0, "It answered 00000.")


def test_repointing_the_endpoint_forgets_what_was_measured_about_vision(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """The same rule as tool support: vision is a property of the server and model."""
    client.put("/api/settings", json={"endpoint_url": "http://127.0.0.1:8080/v1"})
    update_settings_row(db, {"vision_supported": 1, "vision_message": "It read it."})

    body = client.put("/api/settings", json={"model": "some-other-model"}).json()

    assert body["vision_supported"] is None
    assert body["vision_message"] is None


@pytest.mark.parametrize("key", ["replacement-key", ""])
def test_key_change_forgets_endpoint_capabilities(
    client: TestClient, db: sqlite3.Connection, key: str
) -> None:
    client.put("/api/settings", json={"api_key": "original-key"})
    update_settings_row(
        db,
        {
            "tools_supported": 1,
            "tools_message": "Works",
            "vision_supported": 1,
            "vision_message": "Works",
        },
    )
    body = client.put("/api/settings", json={"api_key": key}).json()
    assert body["tools_supported"] is None
    assert body["vision_supported"] is None


@pytest.mark.parametrize("kind", ["tools", "vision"])
@pytest.mark.parametrize(
    "change", [{"endpoint_url": None}, {"model": "new-model"}, {"api_key": "new-key"}]
)
def test_delayed_capability_probe_cannot_stamp_a_changed_configuration(
    client: TestClient,
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    change: dict[str, object],
) -> None:
    client.put(
        "/api/settings", json={"endpoint_url": "http://127.0.0.1:8080/v1", "api_key": "old-key"}
    )

    async def probe(endpoint: str, api_key: str | None, model: str | None):
        routes_settings.write_settings(routes_settings.SettingsUpdate(**change), db)
        support = client_module.ToolSupport if kind == "tools" else client_module.VisionSupport
        return support(ok=True, message="Previous setup works")

    monkeypatch.setattr(
        client_module, "probe_tool_support" if kind == "tools" else "probe_vision_support", probe
    )
    response = client.post(f"/api/settings/test-{kind}")
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "changed" in response.json()["message"]
    assert get_settings_row(db)[f"{kind}_supported"] is None
