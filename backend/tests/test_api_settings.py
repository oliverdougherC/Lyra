"""Settings API contract, with a fake keychain and no DNS.

The API key is the sensitive part: it is accepted, stored, and never echoed back.
"""

import socket
import sqlite3
from collections.abc import Iterator

import keyring.errors
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_settings
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


def test_get_settings_never_exposes_the_key(client: TestClient) -> None:
    body = client.get("/api/settings").json()

    assert "api_key" not in body
    assert body["api_key_set"] is False
    assert body["api_key_storage"] == "keychain"


def test_put_stores_the_key_without_returning_it(
    client: TestClient, fake_keyring: FakeKeyring
) -> None:
    body = client.put("/api/settings", json={"api_key": "sk-secret"}).json()

    assert fake_keyring.store[(secrets.SERVICE, secrets.USERNAME)] == "sk-secret"
    assert body["api_key_set"] is True
    assert "api_key" not in body
    assert "sk-secret" not in client.get("/api/settings").text


def test_empty_api_key_deletes_the_stored_one(client: TestClient) -> None:
    client.put("/api/settings", json={"api_key": "sk-secret"})

    body = client.put("/api/settings", json={"api_key": ""}).json()

    assert body["api_key_set"] is False


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
