"""Deterministic containment barriers using only synthetic outside files."""

import pytest

from backend.core import workspace_paths
from backend.core.errors import LyraError


@pytest.mark.parametrize("operation", ["read", "list", "search"])
def test_ancestor_swap_never_returns_outside(tmp_path, monkeypatch, operation):
    root = tmp_path / "root"
    inside = root / "notes"
    inside.mkdir(parents=True)
    (inside / "chapter.txt").write_text("inside needle")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "chapter.txt").write_text("OUTSIDE needle")
    original = workspace_paths._resolve_existing_path
    swapped = False

    def barrier(*args, **kwargs):
        nonlocal swapped
        result = original(*args, **kwargs)
        if not swapped:
            swapped = True
            inside.rename(root / "original")
            inside.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(workspace_paths, "_resolve_existing_path", barrier)
    try:
        if operation == "read":
            result = workspace_paths.read_workspace_file(root, "notes/chapter.txt")
        elif operation == "list":
            result = workspace_paths.list_workspace(root, "notes")
        else:
            result = workspace_paths.search_workspace(root, "needle", relative_path="notes")
    except LyraError:
        pass
    else:
        assert operation == "read" and "OUTSIDE" not in str(result), result
    assert swapped


def test_search_swapped_and_restored_directory_cannot_leak(tmp_path):
    root = tmp_path / "root"
    inside = root / "notes"
    inside.mkdir(parents=True)
    (inside / "chapter.txt").write_text("inside needle")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "chapter.txt").write_text("OUTSIDE needle")

    def runner(argv, **kwargs):
        inside.rename(root / "original")
        inside.symlink_to(outside, target_is_directory=True)
        try:
            return workspace_paths._run_rg(argv, **kwargs)
        finally:
            inside.unlink()
            (root / "original").rename(inside)

    result = workspace_paths.search_workspace(root, "needle", relative_path="notes", runner=runner)
    assert result["matches"][0]["preview"] == "inside needle"


@pytest.mark.parametrize("replacement", ["symlink", "fifo"])
def test_final_open_swap_refuses_symlink_and_fifo_promptly(tmp_path, monkeypatch, replacement):
    import os
    import time

    root = tmp_path / "root"
    root.mkdir()
    target = root / "chapter.txt"
    target.write_text("inside")
    outside = tmp_path / "outside"
    outside.write_text("OUTSIDE")
    original = os.open
    swapped = False

    def barrier(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "chapter.txt" and not swapped:
            swapped = True
            target.unlink()
            if replacement == "symlink":
                target.symlink_to(outside)
            else:
                os.mkfifo(target)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", barrier)
    start = time.monotonic()
    with pytest.raises(LyraError):
        workspace_paths.read_workspace_file(root, "chapter.txt")
    assert swapped
    assert time.monotonic() - start < 1


def test_root_replacement_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "chapter.txt").write_text("inside")
    original = workspace_paths._resolve_existing_path

    def barrier(*args, **kwargs):
        result = original(*args, **kwargs)
        root.rename(tmp_path / "original")
        root.mkdir()
        (root / "chapter.txt").write_text("OUTSIDE")
        return result

    monkeypatch.setattr(workspace_paths, "_resolve_existing_path", barrier)
    with pytest.raises(LyraError):
        workspace_paths.read_workspace_file(root, "chapter.txt")


def test_safe_in_place_edit_uses_open_handle(tmp_path, monkeypatch):
    import os

    root = tmp_path / "root"
    root.mkdir()
    target = root / "chapter.txt"
    target.write_text("before")
    original = os.open

    def barrier(path, *args, **kwargs):
        descriptor = original(path, *args, **kwargs)
        if path == "chapter.txt":
            target.write_text("after")
        return descriptor

    monkeypatch.setattr(os, "open", barrier)
    assert workspace_paths.read_workspace_file(root, "chapter.txt")["content"] == "after"


def test_proposal_snapshot_does_not_reopen_swapped_ancestor(tmp_path, monkeypatch):
    from backend.core import workspace_changes

    root = tmp_path / "root"
    inside = root / "notes"
    inside.mkdir(parents=True)
    (inside / "chapter.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "chapter.txt").write_text("OUTSIDE")
    original = workspace_changes._open_validated

    def barrier(validated):
        target = original(validated)
        inside.rename(root / "original")
        inside.symlink_to(outside, target_is_directory=True)
        return target

    monkeypatch.setattr(workspace_changes, "_open_validated", barrier)
    with pytest.raises(LyraError):
        workspace_changes.read_workspace_snapshot(root, "notes/chapter.txt")


def test_search_snapshot_preserves_parent_gitignore(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".gitignore").write_text("notes/ignored.txt\n")
    notes = root / "notes"
    notes.mkdir()
    (notes / "ignored.txt").write_text("needle ignored")
    (notes / "allowed.txt").write_text("needle allowed")
    result = workspace_paths.search_workspace(root, "needle", relative_path="notes")
    assert [row["path"] for row in result["matches"]] == ["notes/allowed.txt"]
