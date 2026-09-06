"""Packaged backend entrypoint driven by stdin bootstrap and an inherited socket."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

import uvicorn

from backend.desktop_bootstrap import (
    PROTOCOL_VERSION,
    SESSION_HEADER,
    PackagedBootstrap,
    apply_bootstrap_environment,
    read_bootstrap,
)

_READINESS_MAX_BYTES = 512
_LOG_MAX_BYTES = 1_048_576
_LOG_BACKUPS = 3


class _PrivacyFormatter(logging.Formatter):
    """Render safe stack locations without exception values or source-code excerpts."""

    def formatException(self, exc_info: tuple) -> str:  # noqa: N802
        lines = []
        seen = set()
        exc = exc_info[1]
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            lines.append(type(exc).__name__)
            for frame in traceback.extract_tb(exc.__traceback__):
                lines.append(f"  {Path(frame.filename).name}:{frame.lineno} in {frame.name}")
            exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        return "\n".join(lines)

    def format(self, record: logging.LogRecord) -> str:
        safe = copy.copy(record)
        # Other handlers may already have cached the unsafe standard traceback.
        safe.exc_text = (
            None if safe.exc_info else ("[exception details omitted]" if safe.exc_text else None)
        )
        rendered = super().format(safe)
        rendered = rendered.replace(str(Path.home()), "<home>")
        rendered = re.sub(
            r"(?i)(bearer\s+|(?:api[_-]?key|token|secret|password)[\s=:]+)[^\s,;]+",
            r"\1<redacted>",
            rendered,
        )
        return re.sub(
            r"(?<![\w:])/(?:Users|home|private|Volumes|var)/[^\s]+", "<private-path>", rendered
        )


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self) -> TextIO:
        descriptor = os.open(
            self.baseFilename,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, self.mode, encoding=self.encoding, errors=self.errors)


def configure_packaged_logging() -> Path:
    from backend.desktop_paths import platform_logs_dir

    logs_dir = Path(os.environ.get("LYRA_LOGS_DIR") or platform_logs_dir())
    logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(logs_dir, 0o700)
    path = logs_dir / "backend.log"
    handler = _PrivateRotatingFileHandler(
        path,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUPS,
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    handler.setFormatter(_PrivacyFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
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


def _socket_listener_addr(sock: socket.socket) -> str:
    address = sock.getsockname()
    return f"{address[0]}:{address[1]}"


def _socket_address_family(sock: socket.socket) -> str:
    if sock.family == socket.AF_INET:
        return "ipv4"
    if sock.family == socket.AF_INET6:
        return "ipv6"
    raise ValueError("packaged bootstrap socket must be AF_INET or AF_INET6")


def _validate_bootstrap_socket(bootstrap: PackagedBootstrap, sock: socket.socket) -> None:
    actual = _socket_listener_addr(sock)
    if bootstrap.listener_addr != actual:
        raise ValueError("packaged bootstrap listener_addr did not match the inherited socket")
    if bootstrap.protocol_version != PROTOCOL_VERSION:
        raise ValueError("packaged bootstrap protocol_version is unsupported")
    if bootstrap.session_header_name != SESSION_HEADER:
        raise ValueError("packaged bootstrap session_header_name is unsupported")


def _readiness_line(bootstrap: PackagedBootstrap, sock: socket.socket) -> str:
    address = sock.getsockname()
    payload = json.dumps(
        {
            "status": "ready",
            "protocol_version": PROTOCOL_VERSION,
            "api_base": f"http://{address[0]}:{address[1]}",
            "listener_addr": _socket_listener_addr(sock),
            "address_family": _socket_address_family(sock),
            "inherited_socket": True,
            "session_header_name": bootstrap.session_header_name,
            "session_secret": bootstrap.session_secret,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(payload.encode("utf-8")) > _READINESS_MAX_BYTES:
        raise RuntimeError("packaged readiness payload exceeded the byte budget")
    return payload


async def _emit_readiness_when_started(
    server: object,
    bootstrap: PackagedBootstrap,
    sock: socket.socket,
    stream: TextIO,
    *,
    interval_seconds: float = 0.01,
) -> None:
    for _ in range(30_000):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(interval_seconds)
    else:
        raise RuntimeError("packaged backend never reported startup readiness")
    stream.write(_readiness_line(bootstrap, sock) + "\n")
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


async def _monitor_shutdown_request(
    server: uvicorn.Server,
    shutdown_requested: asyncio.Event,
) -> None:
    await shutdown_requested.wait()
    logging.getLogger(__name__).info("desktop shell requested graceful backend shutdown")
    server.should_exit = True


async def run_packaged_backend(
    bootstrap: PackagedBootstrap,
    *,
    stream: TextIO | None = None,
    server_factory: type[uvicorn.Server] = uvicorn.Server,
) -> int:
    apply_bootstrap_environment(bootstrap)
    configure_packaged_logging()
    from backend.desktop_backup import recover_restore

    recover_restore()
    from backend.main import create_app

    sock = adopt_inherited_socket(bootstrap.socket_fd)
    _validate_bootstrap_socket(bootstrap, sock)
    output = stream or sys.stdout
    app = create_app(session_secret=bootstrap.session_secret)
    shutdown_requested = asyncio.Event()
    app.state.request_shutdown = shutdown_requested.set
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_config=None,
        access_log=False,
    )
    server = server_factory(config)
    reporter = asyncio.create_task(_emit_readiness_when_started(server, bootstrap, sock, output))
    parent_monitor = asyncio.create_task(_monitor_parent(server, bootstrap.parent_pid))
    shutdown_monitor = asyncio.create_task(_monitor_shutdown_request(server, shutdown_requested))
    try:
        await server.serve(sockets=[sock])
    finally:
        reporter.cancel()
        parent_monitor.cancel()
        shutdown_monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reporter
        with contextlib.suppress(asyncio.CancelledError):
            await parent_monitor
        with contextlib.suppress(asyncio.CancelledError):
            await shutdown_monitor
        sock.close()
    return 0


def main(stdin_text: str | None = None, *, stream: TextIO | None = None) -> int:
    for operation in ("create", "restore"):
        flag = f"--desktop-backup-{operation}"
        if flag in sys.argv[1:]:
            os.environ.setdefault("LYRA_PACKAGED", "1")
            from backend import desktop_backup

            index = sys.argv.index(flag)
            if index + 1 >= len(sys.argv):
                return 2
            return desktop_backup.main(operation, sys.argv[index + 1], stream=stream)
    if "--reclaim-helpers" in sys.argv[1:]:
        os.environ.setdefault("LYRA_PACKAGED", "1")
        from backend.llm import helper_reclaim

        return helper_reclaim.main([], stream=stream)
    if "--publish-desktop-import" in sys.argv[1:]:
        os.environ.setdefault("LYRA_PACKAGED", "1")
        from backend import desktop_import

        return desktop_import.publish_staged_import(stream=stream)
    bootstrap = read_bootstrap(stdin_text)
    return asyncio.run(run_packaged_backend(bootstrap, stream=stream))


if __name__ == "__main__":
    raise SystemExit(main())
