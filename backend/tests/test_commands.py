import os
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from backend.core.commands import (
    CommandValidationError,
    minimal_environment,
    run_command,
    validate_argv,
)


def test_validate_argv_requires_bounded_plain_strings() -> None:
    with pytest.raises(CommandValidationError):
        validate_argv([])
    with pytest.raises(CommandValidationError):
        validate_argv(["python", ""])
    with pytest.raises(CommandValidationError):
        validate_argv(["python", "bad\x00argument"])


def test_command_uses_exact_argv_without_shell_interpretation(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    literal = f"$(touch {marker}) && echo expanded"
    result = run_command(
        tmp_path,
        [sys.executable, "-c", "import sys; print(sys.argv[1])", literal],
        timeout_seconds=5,
    )

    assert result.state == "completed"
    assert result.stdout.strip() == literal
    assert not marker.exists()


def test_command_uses_workspace_cwd_and_refuses_escape(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    result = run_command(
        tmp_path,
        [sys.executable, "-c", "import pathlib; print(pathlib.Path.cwd().name)"],
        relative_cwd="child",
        timeout_seconds=5,
    )
    assert result.stdout.strip() == "child"

    with pytest.raises(CommandValidationError):
        run_command(tmp_path, [sys.executable, "-c", "pass"], relative_cwd="../outside")


def test_command_refuses_symlinked_cwd(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CommandValidationError):
        run_command(tmp_path, [sys.executable, "-c", "pass"], relative_cwd="linked")


def test_command_does_not_inherit_application_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LYRA_TEST_SECRET", "private")
    result = run_command(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('LYRA_TEST_SECRET', 'missing'))",
        ],
        timeout_seconds=5,
    )
    assert result.stdout.strip() == "missing"
    assert "LYRA_TEST_SECRET" not in minimal_environment()


def test_command_retains_bounded_output_while_draining_pipes(tmp_path: Path) -> None:
    result = run_command(
        tmp_path,
        [sys.executable, "-c", "import sys; sys.stdout.write('a' * 5000)"],
        timeout_seconds=5,
        max_output_bytes=127,
    )
    assert result.state == "completed"
    assert result.truncated is True
    assert len(result.stdout.encode()) + len(result.stderr.encode()) == 127


def test_timeout_terminates_the_process_group(tmp_path: Path) -> None:
    result = run_command(
        tmp_path,
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
        timeout_seconds=0.1,
    )
    assert result.state == "timed_out"
    assert result.exit_code is not None
    assert result.stdout == "started\n"
    assert result.duration_seconds < 3


def test_missing_executable_is_a_failed_result(tmp_path: Path) -> None:
    result = run_command(tmp_path, ["lyra-command-that-does-not-exist"], timeout_seconds=1)
    assert result.state == "failed"
    assert result.exit_code is None
    assert result.stderr


@pytest.mark.skipif(os.name == "nt", reason="symlink/process-group semantics are POSIX-specific")
def test_relative_cwd_must_be_a_directory(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x")
    with pytest.raises(CommandValidationError):
        run_command(tmp_path, [sys.executable, "-c", "pass"], relative_cwd="file")


@pytest.mark.parametrize(
    "child_code",
    [
        "import time; time.sleep(30)",
        "import os;\nwhile True: os.write(1, b'x' * 8192)",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
    ],
)
def test_exited_leader_descendants_are_reclaimed(tmp_path: Path, child_code: str) -> None:
    import signal
    import time

    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess,sys,pathlib; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))"
    )
    try:
        result = run_command(
            tmp_path, [sys.executable, "-c", code], timeout_seconds=0.2, max_output_bytes=100
        )
        assert result.duration_seconds < 3
        pid = int(pid_file.read_text())
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            pytest.fail("owned child survived command completion")
    finally:
        if pid_file.exists():
            with suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


def test_early_pipe_close_is_bounded_terminal_result(tmp_path: Path) -> None:
    result = run_command(
        tmp_path,
        [sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(30)"],
        timeout_seconds=0.1,
    )
    assert result.state == "timed_out"
    assert result.duration_seconds < 3
