"""Tests for the filesystem-only Phase 4 workspace-change primitive."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from backend.core.errors import ConflictError, LyraError
from backend.core.workspace_changes import (
    apply_workspace_hunks,
    build_workspace_proposal,
    read_workspace_snapshot,
    review_workspace_proposal,
)

BASE = (
    "alpha\n"
    "\n"
    "first paragraph\n"
    "line after first paragraph\n"
    "\n"
    "middle one\n"
    "middle two\n"
    "middle three\n"
    "middle four\n"
    "middle five\n"
    "omega\n"
    "tail line\n"
)

PROPOSED = BASE.replace("first paragraph\n", "first paragraph, revised\n").replace(
    "omega\n", "omega, revised\n"
)


def test_apply_workspace_hunks_accepts_a_subset_and_leaves_the_rest_pending(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text(BASE)
    observed = read_workspace_snapshot(tmp_path, "notes.txt")
    proposal = build_workspace_proposal(tmp_path, "notes.txt", observed.content_hash, PROPOSED)

    result = apply_workspace_hunks(
        tmp_path,
        proposal,
        [{"index": proposal.hunks[0].index, "hash": proposal.hunks[0].hash}],
    )

    assert result.wrote is True
    assert result.status == "partially_applied"
    assert result.applied_hunk_indices == (0,)
    assert result.remaining_proposed_content == PROPOSED
    assert len(result.remaining_hunks) == 1
    assert file_path.read_text() == BASE.replace("first paragraph\n", "first paragraph, revised\n")


def test_a_conflicting_edit_on_the_same_file_identity_is_stale(tmp_path: Path) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text(BASE)
    observed = read_workspace_snapshot(tmp_path, "notes.txt")
    proposal = build_workspace_proposal(tmp_path, "notes.txt", observed.content_hash, PROPOSED)

    file_path.write_text(BASE.replace("first paragraph\n", "first paragraph, local edit\n"))

    review = review_workspace_proposal(tmp_path, proposal)
    assert review.status == "stale"
    with pytest.raises(ConflictError, match="changed since the proposal was fetched"):
        apply_workspace_hunks(
            tmp_path,
            proposal,
            [{"index": proposal.hunks[0].index, "hash": proposal.hunks[0].hash}],
        )
    assert "local edit" in file_path.read_text()


def test_replacing_the_file_path_is_treated_as_a_race(tmp_path: Path) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text(BASE)
    observed = read_workspace_snapshot(tmp_path, "notes.txt")
    proposal = build_workspace_proposal(tmp_path, "notes.txt", observed.content_hash, PROPOSED)

    replacement = tmp_path / "replacement.txt"
    replacement.write_text(BASE.replace("tail line\n", "tail line, replaced\n"))
    os.replace(replacement, file_path)

    review = review_workspace_proposal(tmp_path, proposal)
    assert review.status == "stale"
    with pytest.raises(ConflictError, match="changed since the proposal was fetched"):
        apply_workspace_hunks(
            tmp_path,
            proposal,
            [{"index": proposal.hunks[0].index, "hash": proposal.hunks[0].hash}],
        )
    assert "replaced" in file_path.read_text()


def test_symlink_paths_are_refused(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text(BASE)
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    with pytest.raises(LyraError, match="not available"):
        read_workspace_snapshot(tmp_path, "link.txt")


def test_atomic_replace_preserves_mode_and_newlines(tmp_path: Path) -> None:
    file_path = tmp_path / "windows.txt"
    base = "alpha\r\nbeta\r\ngamma\r\n"
    proposed = "alpha\r\nbeta revised\r\ngamma\r\n"
    file_path.write_bytes(base.encode())
    os.chmod(file_path, 0o640)
    observed = read_workspace_snapshot(tmp_path, "windows.txt")
    proposal = build_workspace_proposal(tmp_path, "windows.txt", observed.content_hash, proposed)

    result = apply_workspace_hunks(
        tmp_path,
        proposal,
        [{"index": proposal.hunks[0].index, "hash": proposal.hunks[0].hash}],
    )

    assert result.newline == "\r\n"
    assert file_path.read_bytes() == proposed.encode()
    assert stat.S_IMODE(file_path.stat().st_mode) == 0o640


def test_proposal_normalizes_model_newlines_to_the_existing_file(tmp_path: Path) -> None:
    file_path = tmp_path / "windows.txt"
    file_path.write_bytes(b"alpha\r\nbeta\r\n")
    observed = read_workspace_snapshot(tmp_path, "windows.txt")
    proposal = build_workspace_proposal(
        tmp_path,
        "windows.txt",
        observed.content_hash,
        "alpha\nbeta revised\n",
    )
    assert proposal.proposed_content == "alpha\r\nbeta revised\r\n"


def test_atomic_replace_failure_leaves_the_original_file_and_no_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text(BASE)
    observed = read_workspace_snapshot(tmp_path, "notes.txt")
    proposal = build_workspace_proposal(tmp_path, "notes.txt", observed.content_hash, PROPOSED)

    def fail_replace(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
        **kwargs: object,
    ) -> None:
        raise OSError("boom")

    monkeypatch.setattr("backend.core.workspace_changes.os.replace", fail_replace)

    with pytest.raises(LyraError, match="could not be written"):
        apply_workspace_hunks(
            tmp_path,
            proposal,
            [{"index": proposal.hunks[0].index, "hash": proposal.hunks[0].hash}],
        )

    assert file_path.read_text() == BASE
    assert sorted(path.name for path in tmp_path.iterdir()) == ["data", "notes.txt"]
