"""Security and bounds tests for Phase 4 rooted workspace access."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.core import workspace_paths
from backend.core.errors import ConfigurationError, NotFoundError


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "src").mkdir()
    (root / "docs").mkdir()
    (root / "src" / "main.py").write_text(
        "alpha = 1\nneedle = 'alpha'\nneedle = 'beta'\nneedle = 'gamma'\n", encoding="utf-8"
    )
    (root / "docs" / "notes.md").write_text("# Notes\nneedle appears here.\n", encoding="utf-8")
    (root / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (root / ".npmrc").write_text("token=secret\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"\x00\x01binary")
    (root / "large.txt").write_bytes(b"x" * (workspace_paths.MAX_TEXT_FILE_BYTES + 1))
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret = true\n", encoding="utf-8")
    return root


def test_list_workspace_skips_ignored_binary_and_oversize_entries(workspace_root: Path) -> None:
    listing = workspace_paths.list_workspace(workspace_root)

    assert listing["path"] == "."
    assert listing["truncated"] is False
    assert [entry["name"] for entry in listing["entries"]] == ["docs", "src"]


def test_list_workspace_rejects_traversal_and_secret_directories(workspace_root: Path) -> None:
    with pytest.raises(workspace_paths.WorkspacePathError):
        workspace_paths.list_workspace(workspace_root, "../outside")
    with pytest.raises(workspace_paths.WorkspacePathError):
        workspace_paths.list_workspace(workspace_root, ".git")
    with pytest.raises(workspace_paths.WorkspacePathError):
        workspace_paths.list_workspace(workspace_root, "src\\main.py")


def test_read_workspace_file_returns_hash_and_bounded_lines(workspace_root: Path) -> None:
    result = workspace_paths.read_workspace_file(
        workspace_root,
        "src/main.py",
        start_line=2,
        end_line=99,
        max_lines=2,
    )

    assert result["path"] == "src/main.py"
    assert result["content"] == "needle = 'alpha'\nneedle = 'beta'\n"
    assert result["start_line"] == 2
    assert result["end_line"] == 3
    assert result["total_lines"] == 4
    assert result["truncated"] is True
    assert len(result["sha256"]) == 64


@pytest.mark.parametrize("relative_path", ["/etc/passwd", "../outside.txt", ".env", "large.txt"])
def test_read_workspace_file_rejects_denied_paths(workspace_root: Path, relative_path: str) -> None:
    with pytest.raises(workspace_paths.WorkspacePathError):
        workspace_paths.read_workspace_file(workspace_root, relative_path)


def test_read_workspace_file_rejects_binary_payloads(workspace_root: Path) -> None:
    with pytest.raises(workspace_paths.WorkspacePathError):
        workspace_paths.read_workspace_file(workspace_root, "binary.bin")


def test_read_workspace_file_rejects_missing_paths(workspace_root: Path) -> None:
    with pytest.raises(NotFoundError):
        workspace_paths.read_workspace_file(workspace_root, "missing.txt")


def test_read_workspace_file_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (root / "escape.txt").symlink_to(outside)

    with pytest.raises(workspace_paths.WorkspacePathError):
        workspace_paths.read_workspace_file(root, "escape.txt")


def test_read_workspace_file_rejects_special_files_when_portable(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo is not available on this platform.")
    root = tmp_path / "workspace"
    root.mkdir()
    special = root / "pipe"
    os.mkfifo(special)

    with pytest.raises(workspace_paths.WorkspacePathError):
        workspace_paths.read_workspace_file(root, "pipe")


def test_search_workspace_uses_bounded_rg_and_parses_matches(workspace_root: Path) -> None:
    result = workspace_paths.search_workspace(workspace_root, "needle", glob="*.py", max_results=10)

    assert result["path"] == "."
    assert result["truncated"] is False
    assert result["matches"] == [
        {
            "path": "src/main.py",
            "line": 2,
            "column": 1,
            "preview": "needle = 'alpha'",
        },
        {
            "path": "src/main.py",
            "line": 3,
            "column": 1,
            "preview": "needle = 'beta'",
        },
        {
            "path": "src/main.py",
            "line": 4,
            "column": 1,
            "preview": "needle = 'gamma'",
        },
    ]


def test_search_workspace_enforces_result_cap_with_injected_runner(workspace_root: Path) -> None:
    seen: dict[str, object] = {}

    def runner(
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> workspace_paths.SearchCommandResult:
        seen["argv"] = argv
        seen["cwd"] = cwd
        seen["timeout"] = timeout_seconds
        seen["max_output_bytes"] = max_output_bytes
        payload = "\n".join(
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": "src/main.py"},
                        "lines": {"text": f"needle {index}\n"},
                        "line_number": index,
                        "submatches": [{"start": 0, "end": 6}],
                    },
                }
            )
            for index in range(1, 6)
        )
        return workspace_paths.SearchCommandResult(returncode=0, stdout=payload)

    result = workspace_paths.search_workspace(
        workspace_root,
        "needle $(touch nope)",
        max_results=2,
        timeout_seconds=7.5,
        max_output_bytes=4096,
        runner=runner,
    )

    assert seen["argv"][0] == "rg"
    assert "needle $(touch nope)" in seen["argv"]
    assert seen["argv"][seen["argv"].index("needle $(touch nope)") - 1] == "--"
    assert seen["cwd"] == workspace_root
    assert seen["timeout"] == 7.5
    assert seen["max_output_bytes"] == 4096
    assert result["truncated"] is True
    assert len(result["matches"]) == 2


def test_search_workspace_rejects_secret_subtrees(workspace_root: Path) -> None:
    with pytest.raises(workspace_paths.WorkspacePathError):
        workspace_paths.search_workspace(workspace_root, "secret", relative_path=".git")


def test_default_rg_runner_reports_timeout_and_missing_binary(workspace_root: Path) -> None:
    with pytest.raises(workspace_paths.WorkspaceSearchTimeoutError):
        workspace_paths._run_rg(
            ["python3", "-c", "import time; time.sleep(0.2)"],
            cwd=workspace_root,
            timeout_seconds=0.01,
            max_output_bytes=1024,
        )

    with pytest.raises(ConfigurationError):
        workspace_paths._run_rg(
            ["definitely-not-a-command-lyra"],
            cwd=workspace_root,
            timeout_seconds=0.5,
            max_output_bytes=1024,
        )


def test_search_workspace_rejects_invalid_globs(workspace_root: Path) -> None:
    with pytest.raises(ValueError):
        workspace_paths.search_workspace(workspace_root, "needle", glob="../*.py")


def test_search_query_cannot_inject_ripgrep_options(workspace_root: Path) -> None:
    seen: dict[str, object] = {}

    def runner(
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> workspace_paths.SearchCommandResult:
        seen["argv"] = argv
        return workspace_paths.SearchCommandResult(returncode=1, stdout="")

    workspace_paths.search_workspace(workspace_root, "--files", runner=runner)
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[-3:] == ["--", "--files", "."]


def test_exact_listing_limit_is_not_reported_as_truncated(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "only.txt").write_text("one", encoding="utf-8")
    result = workspace_paths.list_workspace(root, limit=1)
    assert result["truncated"] is False
