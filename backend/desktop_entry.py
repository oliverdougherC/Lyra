"""Packaged backend entrypoint driven by stdin bootstrap and an inherited socket."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import socket
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

import uvicorn

from backend.desktop_bootstrap import PackagedBootstrap, apply_bootstrap_environment, read_bootstrap

_READINESS_MAX_BYTES = 256
_LOG_MAX_BYTES = 1_048_576
_LOG_BACKUPS = 3


class _PrivacyFilter(logging.Filter):
    """Bound path disclosure in packaged logs without touching exception semantics."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        home = str(Path.home())
        if home:
            message = message.replace(home, "<home>")
        record.msg = message
        record.args = ()
        return True


def configure_packaged_logging() -> Path:
    from backend.desktop_paths import platform_logs_dir

    logs_dir = Path(os.environ.get("LYRA_LOGS_DIR") or platform_logs_dir())
    logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(logs_dir, 0o700)
    path = logs_dir / "backend.log"
    handler = RotatingFileHandler(
        path,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUPS,
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    handler.addFilter(_PrivacyFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return path


def adopt_inherited_socket(socket_fd: int) -> socket.socket:
    duplicate = os.dup(socket_fd)
    adopted = socket.socket(fileno=duplicate)
    _validate_loopback_socket(adopted)
    adopted.setblocking(False)
    return adopted


def _validate_loopback_socket(sock: socket.socket) -> None:
    if sock.family not in {socket.AF_INET, socket.AF_INET6}:
        raise ValueError("packaged bootstrap socket must be AF_INET or AF_INET6")
    address = sock.getsockname()
    if not isinstance(address, tuple) or not address:
        raise ValueError("packaged bootstrap socket must expose an IP address")
    host = address[0]
    if not isinstance(host, str):
        raise ValueError("packaged bootstrap socket host is invalid")
    try:
        resolved = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("packaged bootstrap socket must be bound to a loopback IP") from exc
    if not resolved.is_loopback:
        raise ValueError("packaged bootstrap socket must be bound to loopback")


def _readiness_line(sock: socket.socket) -> str:
    address = sock.getsockname()
    payload = json.dumps(
        {
            "status": "ready",
            "api_base": f"http://{address[0]}:{address[1]}",
            "host": address[0],
            "port": address[1],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(payload.encode("utf-8")) > _READINESS_MAX_BYTES:
        raise RuntimeError("packaged readiness payload exceeded the byte budget")
    return payload


async def _emit_readiness_when_started(
    server: object, sock: socket.socket, stream: TextIO, *, interval_seconds: float = 0.01
) -> None:
    for _ in range(30_000):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(interval_seconds)
    else:
        raise RuntimeError("packaged backend never reported startup readiness")
    stream.write(_readiness_line(sock) + "\n")
    stream.flush()


async def _monitor_parent(
    server: uvicorn.Server,
    parent_pid: int,
    *,
    interval_seconds: float = 0.25,
) -> None:
    """Stop the sidecar if its native-shell parent disappears unexpectedly."""
    while not server.should_exit:
        if os.getppid() != parent_pid:
            logging.getLogger(__name__).warning("desktop shell parent exited; stopping backend")
            server.should_exit = True
            return
        await asyncio.sleep(interval_seconds)


async def run_packaged_backend(
    bootstrap: PackagedBootstrap,
    *,
    stream: TextIO | None = None,
    server_factory: type[uvicorn.Server] = uvicorn.Server,
) -> int:
    apply_bootstrap_environment(bootstrap)
    configure_packaged_logging()
    from backend.main import create_app

    sock = adopt_inherited_socket(bootstrap.socket_fd)
    output = stream or sys.stdout
    config = uvicorn.Config(
        create_app(session_secret=bootstrap.session_secret),
        host="127.0.0.1",
        port=0,
        log_config=None,
        access_log=False,
    )
    server = server_factory(config)
    reporter = asyncio.create_task(_emit_readiness_when_started(server, sock, output))
    parent_monitor = asyncio.create_task(_monitor_parent(server, bootstrap.parent_pid))
    try:
        await server.serve(sockets=[sock])
    finally:
        reporter.cancel()
        parent_monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reporter
        with contextlib.suppress(asyncio.CancelledError):
            await parent_monitor
        sock.close()
    return 0


def main(stdin_text: str | None = None, *, stream: TextIO | None = None) -> int:
    bootstrap = read_bootstrap(stdin_text)
    return asyncio.run(run_packaged_backend(bootstrap, stream=stream))


if __name__ == "__main__":
    raise SystemExit(main())
