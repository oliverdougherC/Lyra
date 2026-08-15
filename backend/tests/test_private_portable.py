"""Portable coverage for the non-POSIX best-effort permissions path."""

import os
from pathlib import Path

import pytest

from backend.storage import private


def test_non_posix_private_write_does_not_require_fchmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "private.bin"
    chmod_calls: list[tuple[Path, int]] = []

    monkeypatch.setattr(private, "_POSIX", False)
    monkeypatch.setattr(
        private,
        "_chmod_best_effort",
        lambda target, mode: chmod_calls.append((target, mode)),
    )

    def unsupported_on_python_312_windows(_descriptor: int, _mode: int) -> None:
        raise AssertionError("the non-POSIX path must not call os.fchmod")

    monkeypatch.setattr(os, "fchmod", unsupported_on_python_312_windows, raising=False)

    private.write_private_bytes(path, b"course notes")

    assert path.read_bytes() == b"course notes"
    assert chmod_calls == [(path, private.FILE_MODE)]
