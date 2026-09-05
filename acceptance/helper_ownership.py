"""Run-local ownership for acceptance helper fixtures, independent of port occupancy."""

import contextlib
import os
import signal
import socket
import subprocess
import time

from backend.llm.llama_server import _process_start_token


class OwnedHelpers:
    def __init__(self) -> None:
        self._children: dict[int, tuple[str, subprocess.Popen[bytes]]] = {}

    def capture(self, process: subprocess.Popen[bytes]) -> None:
        token = _process_start_token(process.pid)
        if token is None:
            raise RuntimeError(f"Cannot capture acceptance helper identity for PID {process.pid}")
        self._children[process.pid] = (token, process)

    def cleanup(self, port: int) -> list[int]:
        """Stop only captured fixtures; an unrelated listener is a failure, never a target."""
        killed = []
        for pid, (token, process) in list(self._children.items()):
            if process.poll() is None and _process_start_token(pid) == token:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                process.wait(timeout=5)
                killed.append(pid)
            self._children.pop(pid, None)
        deadline = time.monotonic() + 5
        while True:
            with socket.socket() as probe:
                probe.settimeout(0.2)
                if probe.connect_ex(("127.0.0.1", port)) != 0:
                    return killed
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Acceptance helper port {port} is still occupied; "
                    "refusing to kill an unowned listener"
                )
            time.sleep(0.05)
