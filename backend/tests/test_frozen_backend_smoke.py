"""Frozen smoke failures must remain bounded and clean up isolated resources."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "frozen_backend_smoke", Path(__file__).resolve().parents[2] / "scripts/frozen_backend_smoke.py"
)
assert _SPEC and _SPEC.loader
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)


def test_partial_readiness_obeys_deadline(monkeypatch):
    """A real child writes one byte, then stalls before its terminating newline."""
    popen = subprocess.Popen
    children = []

    def start_partial_child(args, **kwargs):
        child = popen(
            [
                sys.executable,
                "-c",
                'import sys,time;sys.stdin.readline();sys.stdout.write("{");'
                'sys.stdout.flush();time.sleep(1);sys.stdout.write("\\n");'
                "sys.stdout.flush();time.sleep(2)",
            ],
            **kwargs,
        )
        children.append(child)
        return child

    monkeypatch.setattr(smoke.subprocess, "Popen", start_partial_child)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="readiness timed out"):
        smoke.run_smoke(Path(sys.executable), timeout_seconds=0.1)
    assert time.monotonic() - started < 0.8
    assert children[0].poll() is not None


def test_partial_pipe_readiness_obeys_deadline():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'{"status":')
        with (
            os.fdopen(read_fd, "rb") as stream,
            pytest.raises(TimeoutError, match="readiness timed out"),
        ):
            smoke.read_readiness(stream, timeout_seconds=0.02)
    finally:
        os.close(write_fd)


def test_readiness_rejects_oversized_pipe_payload():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"x" * 128)
        with (
            os.fdopen(read_fd, "rb") as stream,
            pytest.raises(RuntimeError, match="size limit"),
        ):
            smoke.read_readiness(stream, timeout_seconds=0.1, max_bytes=64)
    finally:
        os.close(write_fd)


def test_readiness_accepts_complete_line():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'{"status":"ready"}\n')
        with os.fdopen(read_fd, "rb") as stream:
            assert smoke.read_readiness(stream, timeout_seconds=0.1) == '{"status":"ready"}\n'
    finally:
        os.close(write_fd)


def test_spawn_failure_cleans_profile_and_listener(monkeypatch):
    captured = {}

    def fail_spawn(args, **kwargs):
        captured.update(kwargs)
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(smoke.subprocess, "Popen", fail_spawn)
    with pytest.raises(OSError, match="synthetic spawn failure"):
        smoke.run_smoke(Path(sys.executable))
    profile = Path(captured["env"]["LYRA_DATA_DIR"]).parent
    try:
        assert not profile.exists()
        with pytest.raises(OSError):
            os.fstat(captured["pass_fds"][0])
    finally:
        # Keep the regression run tidy even against the unfixed implementation.
        if profile.exists():
            profile.rmdir()
