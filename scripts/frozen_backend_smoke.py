"""Launch a frozen backend through the real inherited-socket/auth handshake."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import select
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

_MAX_FAILURE_DETAIL_CHARS = 2_000


def _safe_failure_detail(text: str, *, profile: Path, secret: str) -> str:
    """Bound and redact contributor-only child diagnostics before CI prints them."""
    detail = text.replace(secret, "<secret>").replace(str(profile), "<profile>")
    home = str(Path.home())
    if home:
        detail = detail.replace(home, "<home>")
    return detail[-_MAX_FAILURE_DETAIL_CHARS:]


def run_smoke(executable: Path, *, timeout_seconds: float = 30.0) -> dict[str, object]:
    if not executable.is_file():
        raise FileNotFoundError(executable)
    profile = Path(tempfile.mkdtemp(prefix="lyra-frozen-smoke-"))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    os.set_inheritable(listener.fileno(), True)
    secret = secrets.token_hex(32)
    environment = os.environ.copy()
    environment.update(
        {
            "LYRA_DATA_DIR": str(profile / "data"),
            "LYRA_CACHE_DIR": str(profile / "cache"),
            "LYRA_LOGS_DIR": str(profile / "logs"),
            "LYRA_MODELS_DIR": str(profile / "models"),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        }
    )
    process = subprocess.Popen(  # noqa: S603 - caller supplies the explicit built artifact
        [str(executable)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=(listener.fileno(),),
        env=environment,
    )
    try:
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("frozen backend pipes were not created")
        json.dump(
            {
                "socket_fd": listener.fileno(),
                "parent_pid": os.getpid(),
                "session_secret": secret,
            },
            process.stdin,
        )
        process.stdin.write("\n")
        process.stdin.close()
        ready, _, _ = select.select([process.stdout], [], [], timeout_seconds)
        if not ready:
            raise TimeoutError("frozen backend readiness timed out")
        readiness_line = process.stdout.readline()
        if not readiness_line:
            returncode = process.poll()
            child_error = process.stderr.read() if returncode is not None and process.stderr else ""
            detail = _safe_failure_detail(child_error, profile=profile, secret=secret)
            reason = detail or "no stderr"
            raise RuntimeError(
                f"frozen backend exited before readiness (code={returncode}): {reason}"
            )
        try:
            readiness = json.loads(readiness_line)
        except json.JSONDecodeError as exc:
            detail = _safe_failure_detail(readiness_line, profile=profile, secret=secret)
            raise RuntimeError(f"frozen backend returned malformed readiness: {detail}") from exc
        if readiness.get("status") != "ready":
            raise RuntimeError("frozen backend returned invalid readiness")
        connection = http.client.HTTPConnection(
            str(readiness["host"]), int(readiness["port"]), timeout=5
        )
        connection.request("GET", "/api/health/live")
        rejected = connection.getresponse()
        rejected.read()
        if rejected.status != 403:
            raise RuntimeError("frozen backend accepted an unauthenticated request")
        connection.close()
        connection = http.client.HTTPConnection(
            str(readiness["host"]), int(readiness["port"]), timeout=5
        )
        connection.request("GET", "/api/health/live", headers={"X-Lyra-Session": secret})
        accepted = connection.getresponse()
        body = json.loads(accepted.read())
        connection.close()
        if accepted.status != 200 or body != {"status": "ok"}:
            raise RuntimeError("frozen backend authenticated health check failed")
        return {
            "status": "passed",
            "authenticated": True,
            "ephemeral_loopback": True,
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        listener.close()
        shutil.rmtree(profile, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(run_smoke(args.executable), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
