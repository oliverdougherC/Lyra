"""Packaged bootstrap parsing, auth middleware, and readiness helpers."""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import sys

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.desktop_bootstrap import (
    PACKAGED_ENV,
    PROTOCOL_VERSION,
    SESSION_HEADER,
    PackagedBootstrap,
    apply_bootstrap_environment,
    read_bootstrap,
)
from backend.desktop_entry import (
    _emit_readiness_when_started,
    _monitor_parent,
    _monitor_shutdown_request,
    _readiness_line,
    _validate_bootstrap_socket,
    adopt_inherited_socket,
    main,
)
from backend.main import create_app

TEST_SESSION = "a" * 64  # noqa: S105
TEST_LISTENER = "127.0.0.1:43123"


def _bootstrap_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "socket_fd": 4,
        "parent_pid": 1,
        "listener_addr": TEST_LISTENER,
        "session_header_name": SESSION_HEADER,
        "session_secret": TEST_SESSION,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_read_bootstrap_requires_socket_fd_and_secret() -> None:
    blank_secret = " " * 3
    with pytest.raises(ValueError, match="socket_fd"):
        read_bootstrap(_bootstrap_json(socket_fd=None))
    with pytest.raises(ValueError, match="parent_pid"):
        read_bootstrap(_bootstrap_json(parent_pid=0))
    with pytest.raises(ValueError, match="session_secret"):
        read_bootstrap(_bootstrap_json(session_secret=blank_secret))


def test_apply_bootstrap_environment_sets_packaged_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap = read_bootstrap(_bootstrap_json())
    original_packaged = os.environ.get(PACKAGED_ENV)
    original_session = os.environ.get("LYRA_SESSION_SECRET")

    try:
        apply_bootstrap_environment(bootstrap)
        assert os.environ[PACKAGED_ENV] == "1"
        assert os.environ.get("LYRA_SESSION_SECRET") == original_session
    finally:
        if original_packaged is None:
            os.environ.pop(PACKAGED_ENV, None)
        else:
            os.environ[PACKAGED_ENV] = original_packaged


@pytest.mark.parametrize(
    ("payload", "pattern"),
    [
        ({"protocol_version": 2}, "protocol_version"),
        ({"listener_addr": "0.0.0.0:43123"}, "listener_addr"),
        ({"listener_addr": "127.0.0.1:notaport"}, "listener_addr"),
        ({"session_header_name": "X-Other-Header"}, "session_header_name"),
        ({"session_secret": "A" * 64}, "session_secret"),
        ({"unexpected": True}, "keys are invalid"),
    ],
)
def test_read_bootstrap_rejects_malformed_contract(
    payload: dict[str, object], pattern: str
) -> None:
    with pytest.raises(ValueError, match=pattern):
        read_bootstrap(_bootstrap_json(**payload))


def test_read_bootstrap_returns_the_strict_contract() -> None:
    bootstrap = read_bootstrap(_bootstrap_json())

    assert bootstrap == PackagedBootstrap(
        protocol_version=PROTOCOL_VERSION,
        socket_fd=4,
        parent_pid=1,
        listener_addr=TEST_LISTENER,
        session_header_name=SESSION_HEADER,
        session_secret=TEST_SESSION,
    )


def test_packaged_mode_requires_the_session_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "packaged_mode", True)
    client = TestClient(create_app(session_secret=TEST_SESSION))

    rejected = client.get("/api/health/live", headers={"host": "127.0.0.1:8000"})
    accepted = client.get(
        "/api/health/live",
        headers={"host": "127.0.0.1:8000", SESSION_HEADER: TEST_SESSION},
    )

    assert rejected.status_code == 403
    assert accepted.status_code == 200


def test_packaged_mode_keeps_the_host_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "packaged_mode", True)
    client = TestClient(create_app(session_secret=TEST_SESSION))

    response = client.get(
        "/api/health/live",
        headers={"host": "evil.example:8000", SESSION_HEADER: TEST_SESSION},
    )

    assert response.status_code == 400


def test_packaged_cors_preflight_uses_origin_instead_of_session_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "packaged_mode", True)
    client = TestClient(create_app(session_secret=TEST_SESSION))

    response = client.options(
        "/api/settings",
        headers={
            "host": "127.0.0.1:8000",
            "origin": "tauri://localhost",
            "access-control-request-method": "PUT",
            "access-control-request-headers": "x-lyra-session,content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "tauri://localhost"


def test_adopt_inherited_socket_refuses_non_loopback_bind() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", 0))  # noqa: S104 - this explicitly exercises the rejection path
        with pytest.raises(ValueError, match="loopback"):
            adopt_inherited_socket(sock.fileno())
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_readiness_helper_emits_one_bounded_json_line() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    stream = io.StringIO()

    class Server:
        started = False

    server = Server()
    try:
        sock.bind(("127.0.0.1", 0))
        bootstrap = PackagedBootstrap(
            protocol_version=PROTOCOL_VERSION,
            socket_fd=sock.fileno(),
            parent_pid=1,
            listener_addr=f"127.0.0.1:{sock.getsockname()[1]}",
            session_header_name=SESSION_HEADER,
            session_secret=TEST_SESSION,
        )
        expected = _readiness_line(bootstrap, sock)
        task = asyncio.create_task(_emit_readiness_when_started(server, bootstrap, sock, stream))
        await asyncio.sleep(0)
        server.started = True
        await task
    finally:
        sock.close()

    payload = stream.getvalue().strip()
    assert payload == expected
    body = json.loads(payload)
    assert body == {
        "address_family": "ipv4",
        "api_base": f"http://{bootstrap.listener_addr}",
        "inherited_socket": True,
        "listener_addr": bootstrap.listener_addr,
        "protocol_version": PROTOCOL_VERSION,
        "session_header_name": SESSION_HEADER,
        "session_secret": TEST_SESSION,
        "status": "ready",
    }
    assert len(payload.encode("utf-8")) <= 512


def test_adopted_socket_must_match_bootstrap_listener() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        bootstrap = PackagedBootstrap(
            protocol_version=PROTOCOL_VERSION,
            socket_fd=sock.fileno(),
            parent_pid=1,
            listener_addr="127.0.0.1:9",
            session_header_name=SESSION_HEADER,
            session_secret=TEST_SESSION,
        )
        with pytest.raises(ValueError, match="listener_addr"):
            _validate_bootstrap_socket(bootstrap, sock)
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_parent_monitor_requests_shutdown_when_shell_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Server:
        should_exit = False

    server = Server()
    monkeypatch.setattr(os, "getppid", lambda: 99)

    await _monitor_parent(server, 42, interval_seconds=0)

    assert server.should_exit is True


@pytest.mark.asyncio
async def test_shutdown_monitor_requests_server_exit() -> None:
    class Server:
        should_exit = False

    server = Server()
    requested = asyncio.Event()
    task = asyncio.create_task(_monitor_shutdown_request(server, requested))
    requested.set()
    await task

    assert server.should_exit is True


def test_reclaim_mode_delegates_to_helper_reclaim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.llm import helper_reclaim

    called: list[tuple[list[str] | None, bool]] = []

    def fake_main(argv: list[str] | None = None, *, stream: io.StringIO | None = None) -> int:
        called.append((argv, stream is not None))
        return 0

    monkeypatch.setattr(sys, "argv", ["desktop_entry.py", "--reclaim-helpers"])
    monkeypatch.setattr(helper_reclaim, "main", fake_main)

    assert main(stream=io.StringIO()) == 0
    assert called == [([], True)]
