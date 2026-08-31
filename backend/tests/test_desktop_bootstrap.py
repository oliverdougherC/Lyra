"""Packaged bootstrap parsing, auth middleware, and readiness helpers."""

from __future__ import annotations

import asyncio
import io
import os
import socket

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.desktop_bootstrap import (
    PACKAGED_ENV,
    SESSION_HEADER,
    apply_bootstrap_environment,
    read_bootstrap,
)
from backend.desktop_entry import (
    _emit_readiness_when_started,
    _monitor_parent,
    _readiness_line,
    adopt_inherited_socket,
)
from backend.main import create_app

TEST_SESSION = "not-a-real-session-secret"  # noqa: S105


def test_read_bootstrap_requires_socket_fd_and_secret() -> None:
    with pytest.raises(ValueError, match="socket_fd"):
        read_bootstrap('{"parent_pid":1,"session_secret":"secret"}')
    with pytest.raises(ValueError, match="parent_pid"):
        read_bootstrap('{"socket_fd":4,"session_secret":"secret"}')
    with pytest.raises(ValueError, match="session_secret"):
        read_bootstrap('{"socket_fd":4,"parent_pid":1,"session_secret":"   "}')


def test_apply_bootstrap_environment_sets_packaged_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap = read_bootstrap('{"socket_fd":4,"parent_pid":1,"session_secret":"desktop-token"}')
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
        expected = _readiness_line(sock)
        task = asyncio.create_task(_emit_readiness_when_started(server, sock, stream))
        await asyncio.sleep(0)
        server.started = True
        await task
    finally:
        sock.close()

    payload = stream.getvalue().strip()
    assert payload == expected
    assert len(payload.encode("utf-8")) <= 256


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
