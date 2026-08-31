"""Packaged-runtime bootstrap parsing and per-launch session-header constants."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

SESSION_HEADER = "X-Lyra-Session"
PACKAGED_ENV = "LYRA_PACKAGED"


@dataclass(frozen=True)
class PackagedBootstrap:
    socket_fd: int
    session_secret: str


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
    socket_fd = payload.get("socket_fd")
    if isinstance(socket_fd, bool) or not isinstance(socket_fd, int) or socket_fd < 0:
        raise ValueError("packaged bootstrap must provide a non-negative integer socket_fd")
    session_secret = payload.get("session_secret")
    if not isinstance(session_secret, str) or not session_secret.strip():
        raise ValueError("packaged bootstrap must provide a non-empty session_secret")
    return PackagedBootstrap(socket_fd=socket_fd, session_secret=session_secret.strip())


def apply_bootstrap_environment(bootstrap: PackagedBootstrap) -> None:
    del bootstrap
    os.environ[PACKAGED_ENV] = "1"
