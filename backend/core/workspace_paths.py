"""Rooted, bounded workspace reads for the Phase 4 agent surface.

The model never sees host paths. Callers attach one already-approved workspace root and
then route every list, search, and read request back through this module with a relative
path. The policy here is intentionally narrower than what a local user could do:

- no absolute or parent-relative paths;
- no symlink traversal below the approved root;
- no sockets, fifos, devices, or other non-regular files;
- no default-secret paths such as `.env*` or certificate/key material;
- no binary files and nothing over the Phase 4 size ceiling;
- no shell execution for search: ripgrep receives an argv vector only.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from backend.core import commands
from backend.core.errors import ConfigurationError, LyraError, NotFoundError

MAX_TEXT_FILE_BYTES = 1_048_576
DEFAULT_LIST_LIMIT = 200
DEFAULT_READ_LINE_LIMIT = 400
DEFAULT_SEARCH_RESULTS = 50
DEFAULT_SEARCH_MATCHES_PER_FILE = 5
DEFAULT_SEARCH_TIMEOUT_SECONDS = 5.0
DEFAULT_SEARCH_MAX_OUTPUT_BYTES = 262_144
MAX_SEARCH_QUERY_CHARS = 500
_PROBE_BYTES = 4_096

_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".gnupg",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".turbo",
        ".venv",
        ".ssh",
        ".aws",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
)
_SECRET_FILENAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "id_dsa",
        "id_ed25519",
        "id_ecdsa",
        "id_rsa",
    }
)
_SECRET_SUFFIXES = frozenset(
    {
        ".cer",
        ".crt",
        ".csr",
        ".der",
        ".key",
        ".p12",
        ".p7b",
        ".p7c",
        ".p8",
        ".pem",
        ".pfx",
    }
)
_DATABASE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3", ".sqlite-shm", ".sqlite-wal"})


class WorkspacePathError(LyraError):
    """A requested workspace path is disallowed or unavailable."""


class WorkspaceSearchError(LyraError):
    """A bounded workspace search could not complete safely."""


class WorkspaceSearchTimeoutError(WorkspaceSearchError):
    """The search exceeded its bounded runtime."""


@dataclass(frozen=True)
class SearchCommandResult:
    """A bounded search subprocess result."""

    returncode: int
    stdout: str
    stderr: str = ""


class SearchRunner(Protocol):
    """Injected search runner for tests or alternate execution environments."""

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> SearchCommandResult: ...


def canonical_workspace_root(root: str | Path) -> Path:
    """Resolve and validate the one approved root for a class workspace."""
    candidate = Path(root).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
        details = os.lstat(resolved)
    except OSError as exc:
        raise WorkspacePathError("That workspace is not available.") from exc
    if not stat.S_ISDIR(details.st_mode):
        raise WorkspacePathError("That workspace is not available.")
    return resolved


def validate_workspace_file_path(root: str | Path, relative_path: str) -> Path:
    """Return one existing safe text-file path for a user-only downstream operation.

    Callers must still open with no-follow semantics and bind mutations to identity/hash. This
    helper centralizes the attachment-relative path, ignore, regular-file, and size policy.
    """
    resolved_root = canonical_workspace_root(root)
    relative = _normalize_relative_path(relative_path)
    if _is_ignored_relative(relative):
        raise WorkspacePathError("That workspace file cannot be read.")
    target = _resolve_existing_path(resolved_root, relative, require_file=True)
    details = _validated_regular_stat(target)
    if not _is_text_file(target, details):
        raise WorkspacePathError("That workspace file cannot be read.")
    return target


def list_workspace(
    root: str | Path,
    relative_path: str = ".",
    *,
    limit: int = DEFAULT_LIST_LIMIT,
    max_file_bytes: int = MAX_TEXT_FILE_BYTES,
) -> dict[str, object]:
    """Return a sorted, bounded directory listing beneath the attached root."""
    if limit < 1:
        raise ValueError("Workspace listings must allow at least one entry.")
    if max_file_bytes < 1 or max_file_bytes > MAX_TEXT_FILE_BYTES:
        raise ValueError(f"Workspace list files are capped at {MAX_TEXT_FILE_BYTES} bytes.")
    resolved_root = canonical_workspace_root(root)
    root_identity = _path_identity(resolved_root)
    relative = _normalize_relative_path(relative_path)
    if _is_ignored_relative(relative):
        raise WorkspacePathError("That workspace path is not available.")
    directory = _resolve_existing_path(resolved_root, relative, require_directory=True)

    entries: list[dict[str, object]] = []
    try:
        with os.scandir(directory) as listing:
            for entry in sorted(listing, key=lambda current: current.name.lower()):
                relative_entry = _join_relative(relative, entry.name)
                item = _build_listing_entry(entry, relative_entry, max_file_bytes=max_file_bytes)
                if item is None:
                    continue
                entries.append(item)
                if len(entries) > limit:
                    break
    except OSError as exc:
        raise WorkspacePathError("That workspace path is not available.") from exc

    _require_identity(resolved_root, root_identity)
    return {
        "path": relative.as_posix(),
        "entries": entries[:limit],
        "truncated": len(entries) > limit,
    }


def read_workspace_file(
    root: str | Path,
    relative_path: str,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    max_bytes: int = MAX_TEXT_FILE_BYTES,
    max_lines: int = DEFAULT_READ_LINE_LIMIT,
) -> dict[str, object]:
    """Read one bounded UTF-8 text file beneath the approved workspace root."""
    if start_line < 1:
        raise ValueError("Line numbers start at 1.")
    if end_line is not None and end_line < start_line:
        raise ValueError("The requested line range is invalid.")
    if max_lines < 1:
        raise ValueError("Workspace reads must allow at least one line.")
    if max_bytes < 1 or max_bytes > MAX_TEXT_FILE_BYTES:
        raise ValueError(f"Workspace reads are capped at {MAX_TEXT_FILE_BYTES} bytes.")

    resolved_root = canonical_workspace_root(root)
    root_identity = _path_identity(resolved_root)
    relative = _normalize_relative_path(relative_path)
    if _is_ignored_relative(relative):
        raise WorkspacePathError("That workspace file cannot be read.")
    target = _resolve_existing_path(resolved_root, relative, require_file=True)
    initial = _validated_regular_stat(target)
    payload = _read_regular_file_bytes(target, initial, max_bytes=max_bytes)
    _require_identity(resolved_root, root_identity)
    text = _decode_text_payload(payload)

    lines = text.splitlines(keepends=True)
    total_lines = len(lines)
    if total_lines and start_line > total_lines:
        raise ValueError("That line range is not available.")

    requested_end = total_lines if end_line is None else min(end_line, total_lines)
    actual_end = 0
    if total_lines:
        actual_end = min(requested_end, start_line + max_lines - 1)
        content = "".join(lines[start_line - 1 : actual_end])
    else:
        content = ""
    truncated = actual_end < requested_end

    return {
        "path": relative.as_posix(),
        "content": content,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "encoding": "utf-8",
        "size_bytes": len(payload),
        "total_lines": total_lines,
        "start_line": start_line,
        "end_line": actual_end,
        "truncated": truncated,
    }


def search_workspace(
    root: str | Path,
    query: str,
    glob: str | None = None,
    *,
    relative_path: str = ".",
    max_results: int = DEFAULT_SEARCH_RESULTS,
    max_matches_per_file: int = DEFAULT_SEARCH_MATCHES_PER_FILE,
    timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_SEARCH_MAX_OUTPUT_BYTES,
    runner: SearchRunner | None = None,
) -> dict[str, object]:
    """Search one rooted workspace subtree through a bounded ripgrep invocation."""
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("Search query must not be empty.")
    if "\x00" in clean_query or len(clean_query) > MAX_SEARCH_QUERY_CHARS:
        raise ValueError(
            f"Workspace search queries are capped at {MAX_SEARCH_QUERY_CHARS} characters."
        )
    if max_results < 1 or max_results > 200:
        raise ValueError("Workspace search result limit must be between 1 and 200.")
    if max_matches_per_file < 1 or max_matches_per_file > 200:
        raise ValueError("Per-file search matches must be between 1 and 200.")
    if timeout_seconds <= 0:
        raise ValueError("Workspace search timeout must be positive.")
    if max_output_bytes < 1 or max_output_bytes > DEFAULT_SEARCH_MAX_OUTPUT_BYTES:
        raise ValueError(
            f"Workspace search output is capped at {DEFAULT_SEARCH_MAX_OUTPUT_BYTES} bytes."
        )

    resolved_root = canonical_workspace_root(root)
    root_identity = _path_identity(resolved_root)
    relative = _normalize_relative_path(relative_path)
    if _is_ignored_relative(relative):
        raise WorkspacePathError("That workspace path is not available.")
    search_root = _resolve_existing_path(resolved_root, relative, require_directory=True)
    include_glob = _normalize_glob(glob) if glob else None

    argv = _build_rg_argv(
        clean_query,
        include_glob=include_glob,
        max_matches_per_file=max_matches_per_file,
        max_file_bytes=MAX_TEXT_FILE_BYTES,
    )
    command_runner = runner or _run_rg
    result = command_runner(
        argv,
        cwd=search_root,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    _require_identity(resolved_root, root_identity)
    if result.returncode not in (0, 1):
        raise WorkspaceSearchError("That workspace search could not be completed.")

    matches: list[dict[str, object]] = []
    truncated = False
    if result.stdout:
        for line in result.stdout.splitlines():
            event = _load_json_line(line)
            if event.get("type") != "match":
                continue
            match = _parse_rg_match(event, base_relative=relative)
            if match is None:
                continue
            match_path = match.get("path")
            if not isinstance(match_path, str):  # pragma: no cover - parser owns the shape.
                raise WorkspaceSearchError("That workspace search could not be completed.")
            _resolve_existing_path(
                resolved_root,
                _normalize_relative_path(match_path),
                require_file=True,
            )
            matches.append(match)
            if len(matches) >= max_results:
                truncated = True
                break

    return {
        "path": relative.as_posix(),
        "matches": matches[:max_results],
        "truncated": truncated,
    }


def _build_listing_entry(
    entry: os.DirEntry[str], relative_entry: Path, *, max_file_bytes: int
) -> dict[str, object] | None:
    """Return one safe listing row, or None for ignored or disallowed entries."""
    if _is_ignored_relative(relative_entry):
        return None
    try:
        details = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    if stat.S_ISLNK(details.st_mode):
        return None
    if stat.S_ISDIR(details.st_mode):
        return {
            "name": entry.name,
            "path": relative_entry.as_posix(),
            "kind": "directory",
        }
    if not stat.S_ISREG(details.st_mode):
        return None
    if details.st_size > max_file_bytes:
        return None
    candidate = Path(entry.path)
    if not _is_text_file(candidate, details):
        return None
    return {
        "name": entry.name,
        "path": relative_entry.as_posix(),
        "kind": "file",
        "size_bytes": int(details.st_size),
    }


def _resolve_existing_path(
    root: Path,
    relative: Path,
    *,
    require_directory: bool = False,
    require_file: bool = False,
) -> Path:
    """Resolve one existing path component-by-component without following symlinks."""
    current = root
    if relative == Path("."):
        details = _stat_existing(current)
        if require_directory and not stat.S_ISDIR(details.st_mode):
            raise WorkspacePathError("That workspace path is not available.")
        if require_file and not stat.S_ISREG(details.st_mode):
            raise WorkspacePathError("That workspace path is not available.")
        return current

    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        details = _stat_existing(current)
        if stat.S_ISLNK(details.st_mode):
            raise WorkspacePathError("That workspace path is not available.")
        if index < len(parts) - 1 and not stat.S_ISDIR(details.st_mode):
            raise WorkspacePathError("That workspace path is not available.")

    if require_directory and not stat.S_ISDIR(details.st_mode):
        raise WorkspacePathError("That workspace path is not available.")
    if require_file and not stat.S_ISREG(details.st_mode):
        raise WorkspacePathError("That workspace path is not available.")
    return current


def _stat_existing(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except FileNotFoundError as exc:
        raise NotFoundError("That workspace path is not available.") from exc
    except OSError as exc:
        raise WorkspacePathError("That workspace path is not available.") from exc


def _path_identity(path: Path) -> tuple[int, int]:
    details = _stat_existing(path)
    return details.st_dev, details.st_ino


def _require_identity(path: Path, identity: tuple[int, int]) -> None:
    if _path_identity(path) != identity:
        raise WorkspacePathError("That workspace changed while it was being read.")


def _validated_regular_stat(path: Path) -> os.stat_result:
    details = _stat_existing(path)
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise WorkspacePathError("That workspace file cannot be read.")
    if details.st_size > MAX_TEXT_FILE_BYTES:
        raise WorkspacePathError("That workspace file cannot be read.")
    return details


def _read_regular_file_bytes(path: Path, initial: os.stat_result, *, max_bytes: int) -> bytes:
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise WorkspacePathError("That workspace file cannot be read.")
            if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
                raise WorkspacePathError("That workspace file cannot be read.")
            payload = handle.read(max_bytes + 1)
    except WorkspacePathError:
        raise
    except OSError as exc:
        raise WorkspacePathError("That workspace file cannot be read.") from exc
    if len(payload) > max_bytes:
        raise WorkspacePathError("That workspace file cannot be read.")
    return payload


def _decode_text_payload(payload: bytes) -> str:
    if b"\x00" in payload:
        raise WorkspacePathError("That workspace file cannot be read.")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspacePathError("That workspace file cannot be read.") from exc


def _is_text_file(path: Path, details: os.stat_result) -> bool:
    """Probe a small prefix; list entries skip anything that is probably binary."""
    if details.st_size == 0:
        return True
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                return False
            if (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino):
                return False
            payload = handle.read(_PROBE_BYTES)
    except OSError:
        return False
    if b"\x00" in payload:
        return False
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _normalize_relative_path(relative_path: str) -> Path:
    raw = (relative_path or ".").strip()
    if not raw:
        raw = "."
    if "\x00" in raw or "\\" in raw:
        raise WorkspacePathError("That workspace path is not available.")
    normalized = PurePosixPath(raw)
    if normalized.is_absolute():
        raise WorkspacePathError("That workspace path is not available.")

    parts: list[str] = []
    for part in normalized.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise WorkspacePathError("That workspace path is not available.")
        parts.append(part)
    return Path(*parts) if parts else Path(".")


def _normalize_glob(glob: str) -> str:
    raw = glob.strip()
    if not raw or "\x00" in raw or "\\" in raw:
        raise ValueError("Workspace search glob is invalid.")
    if raw.startswith("!"):
        raise ValueError("Workspace search glob is invalid.")
    normalized = PurePosixPath(raw)
    if normalized.is_absolute():
        raise ValueError("Workspace search glob is invalid.")
    for part in normalized.parts:
        if part == "..":
            raise ValueError("Workspace search glob is invalid.")
    return normalized.as_posix()


def _join_relative(base: Path, name: str) -> Path:
    return Path(name) if base == Path(".") else base / name


def _is_ignored_relative(relative: Path) -> bool:
    parts = [part for part in relative.parts if part not in ("", ".")]
    if not parts:
        return False
    for part in parts[:-1]:
        if part in _IGNORED_DIRECTORY_NAMES:
            return True
    leaf = parts[-1]
    if leaf in _IGNORED_DIRECTORY_NAMES:
        return True
    lower = leaf.lower()
    if lower in _SECRET_FILENAMES:
        return True
    if lower.startswith(".env"):
        return True
    suffix = Path(leaf).suffix.lower()
    return suffix in _SECRET_SUFFIXES or suffix in _DATABASE_SUFFIXES


def _build_rg_argv(
    query: str,
    *,
    include_glob: str | None,
    max_matches_per_file: int,
    max_file_bytes: int,
) -> list[str]:
    argv = [
        "rg",
        "--json",
        "--line-number",
        "--column",
        "--hidden",
        "--no-messages",
        "--smart-case",
        "--threads",
        "1",
        "--max-count",
        str(max_matches_per_file),
        "--max-filesize",
        _format_rg_max_filesize(max_file_bytes),
        "--max-columns",
        "240",
        "--max-columns-preview",
        "-I",
    ]
    for pattern in _rg_ignore_globs():
        argv.extend(["--glob", pattern])
    if include_glob is not None:
        argv.extend(["--glob", include_glob])
    # `--` keeps a model-supplied query beginning with `-` from becoming an rg option.
    argv.extend(["--", query, "."])
    return argv


def _rg_ignore_globs() -> tuple[str, ...]:
    patterns = [f"!{name}/**" for name in sorted(_IGNORED_DIRECTORY_NAMES)]
    patterns.extend(
        ["!.env*", "!*.db", "!*.sqlite", "!*.sqlite3", "!*.sqlite-shm", "!*.sqlite-wal"]
    )
    patterns.extend(f"!{name}" for name in sorted(_SECRET_FILENAMES))
    patterns.extend(f"!**/{name}" for name in sorted(_SECRET_FILENAMES))
    patterns.extend(f"!*{suffix}" for suffix in sorted(_SECRET_SUFFIXES))
    return tuple(patterns)


def _format_rg_max_filesize(max_file_bytes: int) -> str:
    if max_file_bytes == MAX_TEXT_FILE_BYTES:
        return "1M"
    return str(max_file_bytes)


def _run_rg(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    max_output_bytes: int,
) -> SearchCommandResult:
    completed = commands.run_command(
        cwd,
        argv,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    if completed.state == "timed_out":
        raise WorkspaceSearchTimeoutError("That workspace search took too long.")
    if completed.exit_code is None:
        raise ConfigurationError("Workspace search is unavailable on this machine.")
    if completed.truncated:
        raise WorkspaceSearchError("That workspace search returned too much data.")
    return SearchCommandResult(
        returncode=completed.exit_code,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _load_json_line(line: str) -> dict[str, object]:
    try:
        payload = json.loads(line)
    except ValueError as exc:
        raise WorkspaceSearchError("That workspace search could not be completed.") from exc
    if not isinstance(payload, dict):
        raise WorkspaceSearchError("That workspace search could not be completed.")
    return payload


def _parse_rg_match(event: dict[str, object], *, base_relative: Path) -> dict[str, object] | None:
    data = event.get("data")
    if not isinstance(data, dict):
        raise WorkspaceSearchError("That workspace search could not be completed.")
    path_data = data.get("path")
    lines_data = data.get("lines")
    line_number = data.get("line_number")
    submatches = data.get("submatches")
    if not isinstance(path_data, dict) or not isinstance(lines_data, dict):
        raise WorkspaceSearchError("That workspace search could not be completed.")
    path_text = path_data.get("text")
    line_text = lines_data.get("text")
    if (
        not isinstance(path_text, str)
        or not isinstance(line_text, str)
        or not isinstance(line_number, int)
    ):
        raise WorkspaceSearchError("That workspace search could not be completed.")

    relative_match = _normalize_relative_path(path_text)
    workspace_relative = (
        relative_match if base_relative == Path(".") else base_relative / relative_match
    )
    if _is_ignored_relative(workspace_relative):
        return None

    column = 1
    if isinstance(submatches, list) and submatches:
        first = submatches[0]
        if isinstance(first, dict):
            start = first.get("start")
            if isinstance(start, int):
                column = start + 1

    return {
        "path": workspace_relative.as_posix(),
        "line": line_number,
        "column": column,
        "preview": line_text.rstrip("\r\n"),
    }
