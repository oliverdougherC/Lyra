"""Bounded execution for an already confirmed workspace command.

This module deliberately does not decide whether a command is allowed or confirmed. The API layer
must validate and atomically consume the bound confirmation before calling ``run_command``. Keeping
the runner ignorant of model tools makes the security boundary explicit: it accepts only argv,
never a shell string, and never imports the application's environment wholesale.
"""

import os
import selectors
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TIMEOUT_SECONDS = 600.0
DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
MAX_ARGUMENTS = 128
MAX_ARGUMENT_CHARACTERS = 16_384
_READ_SIZE = 16 * 1024

CommandState = Literal["completed", "failed", "timed_out"]


class CommandValidationError(ValueError):
    """The proposed invocation cannot be executed by the bounded runner."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    state: CommandState
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    duration_seconds: float


def validate_command_cwd(root: Path, relative_cwd: str) -> Path:
    """Resolve a command cwd inside ``root`` without following symlinked components."""
    if not relative_cwd or "\x00" in relative_cwd:
        raise CommandValidationError("The command working directory is invalid.")
    relative = Path(relative_cwd)
    if relative.is_absolute() or ".." in relative.parts:
        raise CommandValidationError("The command working directory must stay in the workspace.")

    canonical_root = root.resolve(strict=True)
    if not canonical_root.is_dir():
        raise CommandValidationError("The workspace root is not a directory.")

    current = canonical_root
    for part in relative.parts:
        if part in ("", "."):
            continue
        current = current / part
        if current.is_symlink():
            raise CommandValidationError("Symlinked command working directories are not allowed.")

    try:
        canonical_cwd = current.resolve(strict=True)
        canonical_cwd.relative_to(canonical_root)
    except (OSError, ValueError) as exc:
        raise CommandValidationError(
            "The command working directory must stay in the workspace."
        ) from exc
    if not canonical_cwd.is_dir():
        raise CommandValidationError("The command working directory is not a directory.")
    return canonical_cwd


def validate_argv(argv: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Validate an exact argv vector without interpreting shell syntax."""
    if not argv or len(argv) > MAX_ARGUMENTS:
        raise CommandValidationError("A command needs a bounded, non-empty argument list.")
    if any(
        not isinstance(argument, str) or not argument or "\x00" in argument for argument in argv
    ):
        raise CommandValidationError("Every command argument must be a non-empty string.")
    if sum(len(argument) for argument in argv) > MAX_ARGUMENT_CHARACTERS:
        raise CommandValidationError("The command argument list is too large.")
    return tuple(argv)


def minimal_environment() -> dict[str, str]:
    """Return the small, credential-free environment inherited by a confirmed command."""
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "CI": "1",
        "NO_COLOR": "1",
    }
    # Some macOS command-line tools require this public system selector. It is not an app secret.
    if value := os.environ.get("SYSTEM_VERSION_COMPAT"):
        environment["SYSTEM_VERSION_COMPAT"] = value
    return environment


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def _retain_chunk(
    retained: dict[str, bytearray],
    stream_name: str,
    chunk: bytes,
    remaining: int,
) -> tuple[int, bool]:
    kept = chunk[:remaining]
    retained[stream_name].extend(kept)
    return remaining - len(kept), len(kept) < len(chunk)


def run_command(
    root: Path,
    argv: list[str] | tuple[str, ...],
    *,
    relative_cwd: str = ".",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> CommandResult:
    """Run one confirmed command with no shell and bounded retained output.

    Pipes are always drained, including after the retention ceiling is reached, so a noisy child
    cannot deadlock while the retained transcript remains bounded. A timeout terminates the whole
    process group rather than only its parent.
    """
    exact_argv = validate_argv(argv)
    cwd = validate_command_cwd(root, relative_cwd)
    if timeout_seconds <= 0 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise CommandValidationError(
            "Command timeout must be greater than zero and at most "
            f"{MAX_TIMEOUT_SECONDS:g} seconds."
        )
    if max_output_bytes < 1 or max_output_bytes > DEFAULT_MAX_OUTPUT_BYTES:
        raise CommandValidationError(
            f"Retained command output must be between 1 and {DEFAULT_MAX_OUTPUT_BYTES} bytes."
        )

    started = time.monotonic()
    try:
        process = subprocess.Popen(  # noqa: S603 - exact argv is explicitly user-confirmed.
            exact_argv,
            cwd=cwd,
            env=minimal_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        duration = time.monotonic() - started
        return CommandResult(
            state="failed",
            exit_code=None,
            stdout="",
            stderr=str(exc),
            truncated=False,
            duration_seconds=duration,
        )

    selector = selectors.DefaultSelector()
    stdout = process.stdout
    stderr = process.stderr
    if stdout is None or stderr is None:  # pragma: no cover - guaranteed by Popen arguments.
        _terminate_process_group(process)
        raise RuntimeError("The command runner could not create output pipes.")
    selector.register(stdout, selectors.EVENT_READ, "stdout")
    selector.register(stderr, selectors.EVENT_READ, "stderr")
    retained = {"stdout": bytearray(), "stderr": bytearray()}
    remaining = max_output_bytes
    truncated = False
    timed_out = False

    try:
        while selector.get_map():
            elapsed = time.monotonic() - started
            wait = timeout_seconds - elapsed
            if wait <= 0:
                timed_out = True
                _terminate_process_group(process)
                wait = 0
            events = selector.select(min(max(wait, 0), 0.1))
            for key, _ in events:
                stream = key.fileobj
                chunk = os.read(stream.fileno(), _READ_SIZE)
                if not chunk:
                    selector.unregister(stream)
                    continue
                remaining, chunk_truncated = _retain_chunk(retained, key.data, chunk, remaining)
                truncated = truncated or chunk_truncated
            if timed_out and process.poll() is not None and not events:
                # A killed descendant may have inherited a pipe briefly. Do not wait forever for it.
                for key in list(selector.get_map().values()):
                    selector.unregister(key.fileobj)
            if process.poll() is not None and not events:
                # Give EOF one final selector pass; regular pipe EOF is immediately readable.
                for key, _ in selector.select(0):
                    stream = key.fileobj
                    chunk = os.read(stream.fileno(), _READ_SIZE)
                    if chunk:
                        remaining, chunk_truncated = _retain_chunk(
                            retained, key.data, chunk, remaining
                        )
                        truncated = truncated or chunk_truncated
                    selector.unregister(stream)
        if process.poll() is None:
            process.wait(timeout=1.0)
    finally:
        selector.close()
        if process.poll() is None:
            _terminate_process_group(process)
        stdout.close()
        stderr.close()

    exit_code = process.returncode
    state: CommandState
    if timed_out:
        state = "timed_out"
    elif exit_code == 0:
        state = "completed"
    else:
        state = "failed"
    return CommandResult(
        state=state,
        exit_code=exit_code,
        stdout=retained["stdout"].decode("utf-8", errors="replace"),
        stderr=retained["stderr"].decode("utf-8", errors="replace"),
        truncated=truncated,
        duration_seconds=time.monotonic() - started,
    )
