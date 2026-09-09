"""Healthy slow Keychain reads must remain pending, never become missing credentials."""

import asyncio
import threading
from types import SimpleNamespace

import httpx
import keyring.errors
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_settings
from backend.core.errors import LyraError
from backend.storage import secrets
from backend.storage.database import connect, get_db


@pytest.fixture
def delayed_keychain(monkeypatch):
    values = {
        (secrets.SERVICE, secrets.USERNAME): "synthetic-tutor",
        (secrets.SERVICE, secrets.EXA_USERNAME): "synthetic-exa",
    }
    release = threading.Event()
    release.set()
    calls = []
    errors = {}

    def get(service, username):
        calls.append(username)
        assert release.wait(3)
        if username in errors:
            raise errors[username]
        return values.get((service, username))

    fake = SimpleNamespace(
        get_password=get,
        set_password=lambda service, username, value: values.__setitem__(
            (service, username), value
        ),
    )
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    monkeypatch.setattr(secrets, "_keyring_ok", None)
    monkeypatch.setattr(secrets, "_slot_read_cache", {})
    yield SimpleNamespace(values=values, release=release, calls=calls, errors=errors)
    release.set()
    if secrets._operation_thread:
        secrets._operation_thread.join(1)


@pytest.mark.parametrize("slot", [False, True])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("route", ["models", "test-connection", "test-tools", "test-vision"])
def test_delayed_credentials_remain_authenticated_through_production_routes(
    db, delayed_keychain, monkeypatch, slot, warm, route
):
    endpoint = "http://127.0.0.1:8080/v1"
    db.execute(
        "update settings set endpoint_url=?,legacy_credential_endpoint=? where id=1",
        (endpoint, endpoint),
    )
    if slot:
        identity = secrets.stage_tutor_credential(endpoint, "synthetic-tutor")
        db.execute("update settings set tutor_credential_id=? where id=1", (identity,))
    db.commit()
    sent = []

    async def models(destination, key):
        async def capture(request):
            sent.append((str(request.url), request.headers.get("Authorization")))
            return httpx.Response(200)

        async with routes_settings.client._client(
            httpx.Timeout(1), key, httpx.MockTransport(capture)
        ) as http:
            await http.get(destination + "/models")
        return ["synthetic-model"]

    async def probe(destination, key, *args):
        await models(destination, key)
        return SimpleNamespace(ok=True, model_count=1, message="available")

    monkeypatch.setattr(routes_settings.client, "list_models", models)
    for name in ("test_connection", "probe_tool_support", "probe_vision_support"):
        monkeypatch.setattr(routes_settings.client, name, probe)

    def connection():
        conn = connect()
        try:
            yield conn
        finally:
            conn.close()

    app = FastAPI()
    app.include_router(routes_settings.router)
    app.dependency_overrides[get_db] = connection

    @app.exception_handler(LyraError)
    async def error(request: Request, exc: LyraError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    with TestClient(app) as client:
        if warm:
            assert client.get("/api/settings").json()["api_key_set"]
        secrets._slot_read_cache.clear()
        delayed_keychain.release.clear()
        request = client.get if route == "models" else client.post
        first = request("/api/settings/" + route)
        assert first.status_code == 400
        assert "responding" in first.json()["detail"]
        assert sent == []
        assert secrets._keyring_ok is not False
        worker = secrets._operation_thread
        for _ in range(3):
            response = request("/api/settings/" + route)
            assert response.status_code == 400
            assert secrets._operation_thread is worker
        delayed_keychain.release.set()
        worker.join(1)
        for _ in range(5):
            response = request("/api/settings/" + route)
            if response.status_code == 200:
                break
            secrets._operation_thread.join(1)
        assert response.status_code == 200
        assert sent == [(endpoint + "/models", "Bearer synthetic-tutor")]
        state = client.get("/api/settings").json()
        assert state["api_key_set"] and state["exa_api_key_set"]
        assert secrets.get_exa_api_key() == "synthetic-exa"
        # Exa's production test endpoint is synchronous. A normal delayed lookup
        # must still authenticate after the asynchronous tutor request above.
        import time

        backend = secrets._keyring()
        original_get = backend.get_password

        def healthy_delay(*args):
            time.sleep(0.03)
            return original_get(*args)

        exa_keys = []

        class ExaProbe:
            def __init__(self, *, api_key):
                exa_keys.append(api_key)

            def check_readiness(self):
                pass

        monkeypatch.setattr(backend, "get_password", healthy_delay)
        monkeypatch.setattr(routes_settings.exa, "ExaClient", ExaProbe)
        exa_response = client.post("/api/settings/test-exa")
        assert exa_response.status_code == 200
        assert exa_response.json()["status"] == "available"
        assert exa_keys == ["synthetic-exa"]


def test_completed_read_survives_other_key_lookup(delayed_keychain):
    import asyncio

    async def read(getter):
        return getter()

    delayed_keychain.release.clear()
    try:
        with pytest.raises(LyraError, match="responding"):
            asyncio.run(read(secrets.get_api_key))
        first = secrets._operation_thread
        delayed_keychain.release.set()
        first.join(1)
        # Skip a second global availability probe: the prior healthy probe can be
        # consumed separately from the original tutor credential read.
        secrets._keyring_ok = True
        delayed_keychain.release.clear()
        with pytest.raises(LyraError, match="responding"):
            asyncio.run(read(secrets.get_exa_api_key))
        second = secrets._operation_thread
        assert second is not first
        assert asyncio.run(read(secrets.get_api_key)) == "synthetic-tutor"
        assert secrets._operation_thread is second
    finally:
        delayed_keychain.release.set()
        secrets._operation_thread.join(1)
    assert asyncio.run(read(secrets.get_exa_api_key)) == "synthetic-exa"
    assert secrets._keyring_ok is True


def test_pending_denied_read_reports_failure_without_demoting_other_key(delayed_keychain):
    import asyncio

    async def read(getter):
        return getter()

    secrets._keyring_ok = True
    delayed_keychain.errors[secrets.USERNAME] = keyring.errors.KeyringError("synthetic denied")
    delayed_keychain.release.clear()
    with pytest.raises(LyraError, match="responding"):
        asyncio.run(read(secrets.get_api_key))
    delayed_keychain.release.set()
    secrets._operation_thread.join(1)
    with pytest.raises(LyraError, match="Unlock"):
        asyncio.run(read(secrets.get_api_key))
    assert secrets._keyring_ok is True
    assert secrets.get_exa_api_key() == "synthetic-exa"
    delayed_keychain.errors.clear()
    assert secrets.get_api_key() == "synthetic-tutor"


def test_pending_result_cannot_override_acknowledged_deletion(delayed_keychain, monkeypatch):
    import asyncio

    async def read():
        return secrets.get_api_key()

    delayed_keychain.release.clear()
    with pytest.raises(LyraError, match="responding"):
        asyncio.run(read())
    monkeypatch.setattr(secrets, "_keyring_ok", False)
    secrets.delete_api_key()
    delayed_keychain.release.set()
    secrets._operation_thread.join(1)
    secrets.reset_keyring_probe()
    assert secrets.get_api_key() is None


@pytest.mark.parametrize("getter_name", ["get_api_key", "get_exa_api_key"])
@pytest.mark.parametrize("warm", [False, True])
def test_synchronous_read_timeout_remains_pending_and_retry_consumes_result(
    delayed_keychain, monkeypatch, getter_name, warm
):
    import time

    getter = getattr(secrets, getter_name)
    expected = "synthetic-exa" if getter_name == "get_exa_api_key" else "synthetic-tutor"
    if warm:
        assert getter() == expected
    delayed_keychain.release.clear()
    monkeypatch.setattr(secrets, "_PROBE_TIMEOUT_SECONDS", 0.02)
    started = time.monotonic()
    with pytest.raises(LyraError, match="responding"):
        getter()
    assert time.monotonic() - started < 0.5
    assert secrets._keyring_ok is not False
    worker = secrets._operation_thread
    calls = len(delayed_keychain.calls)
    delayed_keychain.release.set()
    worker.join(1)
    assert getter() == expected
    assert len(delayed_keychain.calls) == calls
    assert secrets._operation_thread is worker


def test_synchronous_denied_read_is_failure_and_no_backend_still_falls_back(delayed_keychain):
    from backend.core.errors import ConfigurationError

    secrets._keyring_ok = True
    delayed_keychain.errors[secrets.USERNAME] = keyring.errors.KeyringError("synthetic denied")
    with pytest.raises(ConfigurationError, match="Unlock"):
        secrets.get_api_key()
    assert secrets._keyring_ok is True
    assert secrets.get_exa_api_key() == "synthetic-exa"
    delayed_keychain.errors[secrets.USERNAME] = keyring.errors.NoKeyringError("no backend")
    assert secrets.get_api_key() is None
    assert secrets._keyring_ok is False


@pytest.mark.parametrize("already_pending", [False, True])
def test_wait_for_tutor_credential_consumes_one_delayed_read(delayed_keychain, already_pending):
    async def run():
        delayed_keychain.release.clear()
        if already_pending:
            with pytest.raises(secrets.CredentialPendingError):
                secrets.get_api_key()
        task = asyncio.create_task(secrets.wait_for_credential_read(secrets.get_api_key))
        await asyncio.sleep(0)
        worker = secrets._operation_thread
        assert worker is not None and worker.is_alive()
        assert not task.done()
        # The caller remains suspended while unrelated event-loop work can run.
        await asyncio.sleep(0.03)
        assert not task.done()
        assert secrets._operation_thread is worker
        delayed_keychain.release.set()
        assert await asyncio.wait_for(task, 1) == "synthetic-tutor"
        assert delayed_keychain.calls == [secrets.USERNAME]

    asyncio.run(run())


def test_wait_for_tutor_credential_waits_for_unrelated_exa_read(delayed_keychain):
    async def run():
        secrets._keyring_ok = True
        delayed_keychain.release.clear()
        with pytest.raises(secrets.CredentialPendingError):
            secrets.get_exa_api_key()
        exa_worker = secrets._operation_thread
        task = asyncio.create_task(secrets.wait_for_credential_read(secrets.get_api_key))
        await asyncio.sleep(0.03)
        assert not task.done()
        assert secrets._operation_thread is exa_worker
        delayed_keychain.release.set()
        assert await asyncio.wait_for(task, 1) == "synthetic-tutor"
        assert secrets.get_exa_api_key() == "synthetic-exa"
        assert delayed_keychain.calls == [secrets.EXA_USERNAME, secrets.USERNAME]

    asyncio.run(run())


def test_wait_for_credential_deadline_preserves_pending_read(delayed_keychain, monkeypatch):
    monkeypatch.setattr(secrets, "_PROBE_TIMEOUT_SECONDS", 0.04)

    async def run():
        delayed_keychain.release.clear()
        started = asyncio.get_running_loop().time()
        with pytest.raises(secrets.CredentialPendingError):
            await secrets.wait_for_credential_read(secrets.get_api_key)
        elapsed = asyncio.get_running_loop().time() - started
        assert 0.04 <= elapsed < 0.5
        worker = secrets._operation_thread
        assert worker is not None and worker.is_alive()
        assert delayed_keychain.calls == [secrets.USERNAME]
        delayed_keychain.release.set()
        await asyncio.to_thread(worker.join, 1)
        assert await secrets.wait_for_credential_read(secrets.get_api_key) == "synthetic-tutor"
        assert secrets._operation_thread is worker
        assert delayed_keychain.calls == [secrets.USERNAME]
        assert secrets._keyring_ok is True

    asyncio.run(run())


def test_cancelled_credential_wait_stops_polling(delayed_keychain):
    reads = []

    def read():
        reads.append(None)
        return secrets.get_api_key()

    async def run():
        delayed_keychain.release.clear()
        task = asyncio.create_task(secrets.wait_for_credential_read(read))
        await asyncio.sleep(0)
        worker = secrets._operation_thread
        assert worker is not None and worker.is_alive()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        reads_at_cancel = len(reads)
        delayed_keychain.release.set()
        await asyncio.to_thread(worker.join, 1)
        await asyncio.sleep(0.03)
        assert len(reads) == reads_at_cancel == 1
        assert delayed_keychain.calls == [secrets.USERNAME]
        assert secrets._operation_thread is worker

    asyncio.run(run())


def test_wait_for_credential_does_not_retry_actual_denial(delayed_keychain):
    from backend.core.errors import ConfigurationError

    async def run():
        secrets._keyring_ok = True
        delayed_keychain.errors[secrets.USERNAME] = keyring.errors.KeyringError("synthetic denied")
        delayed_keychain.release.clear()
        with pytest.raises(secrets.CredentialPendingError):
            secrets.get_api_key()
        worker = secrets._operation_thread
        delayed_keychain.release.set()
        await asyncio.to_thread(worker.join, 1)
        reads = []

        def read():
            reads.append(None)
            return secrets.get_api_key()

        with pytest.raises(ConfigurationError, match="Unlock"):
            await secrets.wait_for_credential_read(read)
        assert len(reads) == 1
        assert delayed_keychain.calls == [secrets.USERNAME]
        assert secrets._operation_thread is worker
        assert secrets._keyring_ok is True

    asyncio.run(run())


def test_credential_wait_uses_coherent_settings_after_endpoint_changes(db, delayed_keychain):
    from backend.core import app_settings

    first_endpoint = "http://127.0.0.1:8080/v1"
    second_endpoint = "http://192.0.2.1:8080/v1"
    first_identity = secrets.stage_tutor_credential(first_endpoint, "synthetic-first")
    second_identity = secrets.stage_tutor_credential(second_endpoint, "synthetic-second")
    secrets._slot_read_cache.clear()
    db.execute(
        "update settings set endpoint_url=?, tutor_credential_id=?, model=?, remote_ack=1 "
        "where id=1",
        (first_endpoint, first_identity, "first-model"),
    )
    db.commit()

    async def run():
        delayed_keychain.release.clear()
        task = asyncio.create_task(
            secrets.wait_for_credential_read(lambda: app_settings.resolve_tutor_access(db))
        )
        await asyncio.sleep(0)
        assert not task.done()
        db.execute(
            "update settings set endpoint_url=?, tutor_credential_id=?, model=?, remote_ack=0 "
            "where id=1",
            (second_endpoint, second_identity, "second-model"),
        )
        db.commit()
        delayed_keychain.release.set()
        access = await asyncio.wait_for(task, 1)
        assert access.config.endpoint_url == second_endpoint
        assert access.config.credential_id == second_identity
        assert access.config.api_key == "synthetic-second"
        assert access.config.model == "second-model"
        assert access.document_block == app_settings.REMOTE_UNACKNOWLEDGED
        assert access.remote_ack is False
        assert delayed_keychain.calls == ["tutor:" + first_identity, "tutor:" + second_identity]

    asyncio.run(run())
