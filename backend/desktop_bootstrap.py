"""Packaged-runtime bootstrap parsing and per-launch session-header constants."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
from dataclasses import dataclass

SESSION_HEADER = "X-Lyra-Session"
PACKAGED_ENV = "LYRA_PACKAGED"
PROTOCOL_VERSION = 1
_SESSION_SECRET_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_KEYS = frozenset(
    {
        "protocol_version",
        "socket_fd",
        "parent_pid",
        "listener_addr",
        "session_header_name",
        "session_secret",
    }
)


@dataclass(frozen=True)
class PackagedBootstrap:
    protocol_version: int
    socket_fd: int
    parent_pid: int
    listener_addr: str
    session_header_name: str
    session_secret: str


def _require_exact_keys(payload: dict[object, object]) -> None:
    keys = {key for key in payload if isinstance(key, str)}
    missing = sorted(_EXPECTED_KEYS - keys)
    unknown = sorted(keys - _EXPECTED_KEYS)
    if missing or unknown or len(keys) != len(payload):
        problems: list[str] = []
        if missing:
            problems.append(f"missing {', '.join(missing)}")
        if unknown:
            problems.append(f"unknown {', '.join(unknown)}")
        if len(keys) != len(payload):
            problems.append("non-string key")
        joined = "; ".join(problems)
        raise ValueError(f"packaged bootstrap keys are invalid: {joined}")


def _require_listener_addr(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("packaged bootstrap must provide a non-empty listener_addr")
    host, separator, port_text = value.strip().partition(":")
    if not separator:
        raise ValueError("packaged bootstrap listener_addr must be host:port")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("packaged bootstrap listener_addr must use a loopback IP") from exc
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_loopback:
        raise ValueError("packaged bootstrap listener_addr must use an IPv4 loopback IP")
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ValueError("packaged bootstrap listener_addr must use a valid port")
    return f"{host}:{int(port_text)}"


def read_bootstrap(stdin_text: str | None = None) -> PackagedBootstrap:
    raw = sys.stdin.read() if stdin_text is None else stdin_text
    if not raw.strip():
        raise ValueError("packaged bootstrap is empty")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"packaged bootstrap is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("packaged bootstrap must be a JSON object")
    _require_exact_keys(payload)
    protocol_version = payload.get("protocol_version")
    if (
        isinstance(protocol_version, bool)
        or not isinstance(protocol_version, int)
        or protocol_version != PROTOCOL_VERSION
    ):
        raise ValueError(f"packaged bootstrap must provide protocol_version={PROTOCOL_VERSION}")
    socket_fd = payload.get("socket_fd")
    if isinstance(socket_fd, bool) or not isinstance(socket_fd, int) or socket_fd < 0:
        raise ValueError("packaged bootstrap must provide a non-negative integer socket_fd")
    parent_pid = payload.get("parent_pid")
    if isinstance(parent_pid, bool) or not isinstance(parent_pid, int) or parent_pid <= 0:
        raise ValueError("packaged bootstrap must provide a positive integer parent_pid")
    listener_addr = _require_listener_addr(payload.get("listener_addr"))
    session_header_name = payload.get("session_header_name")
    if session_header_name != SESSION_HEADER:
        raise ValueError(f"packaged bootstrap must provide session_header_name={SESSION_HEADER!r}")
    session_secret = payload.get("session_secret")
    if not isinstance(session_secret, str) or not _SESSION_SECRET_RE.fullmatch(
        session_secret.strip()
    ):
        raise ValueError(
            "packaged bootstrap must provide a 64-character lowercase-hex session_secret"
        )
    return PackagedBootstrap(
        protocol_version=protocol_version,
        socket_fd=socket_fd,
        parent_pid=parent_pid,
        listener_addr=listener_addr,
        session_header_name=session_header_name,
        session_secret=session_secret.strip(),
    )


def apply_bootstrap_environment(bootstrap: PackagedBootstrap) -> None:
    del bootstrap
    os.environ[PACKAGED_ENV] = "1"
