"""Pure workspace-change primitives for Phase 4 review and apply flows.

This module does not touch the database or routes. It models one proposed full-text file
change, derives server-authoritative hunks from it, refreshes those hunks against the
current file contents, and atomically applies a selected subset. The model never writes:
future API layers can store these values and call the apply primitive only from a
user-confirmed endpoint.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from backend.core import workspace_paths
from backend.core.errors import ConflictError, LyraError
from backend.core.suggestions import Hunk, apply_hunk, apply_patch, compute_hunks

_INVALID_PATH_MESSAGE = "That workspace file is not available."
_INVALID_FILE_MESSAGE = "That workspace file cannot be reviewed."
_STALE_PROPOSAL_MESSAGE = "That file changed since the proposal was fetched. Re-fetch it."
_HUNK_RACE_MESSAGE = "That hunk changed since it was fetched. Re-fetch the proposal."
_WRITE_FAILED_MESSAGE = "That workspace file could not be written."


type PathValidator = Callable[[Path, str], Path]


@dataclass(frozen=True)
class FileIdentity:
    """The stable identity of a file path, for replacement-race detection."""

    device: int
    inode: int

    @classmethod
    def from_stat(cls, file_stat: os.stat_result) -> FileIdentity:
        return cls(device=int(file_stat.st_dev), inode=int(file_stat.st_ino))


@dataclass(frozen=True)
class FileVersion:
    """A content version for one opened file snapshot."""

    size: int
    mtime_ns: int

    @classmethod
    def from_stat(cls, file_stat: os.stat_result) -> FileVersion:
        return cls(size=int(file_stat.st_size), mtime_ns=int(file_stat.st_mtime_ns))


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """One validated read of a workspace file."""

    relative_path: str
    path: Path
    content: str
    content_hash: str
    identity: FileIdentity
    version: FileVersion
    mode: int
    newline: str | None


@dataclass(frozen=True)
class WorkspaceProposal:
    """A full-text proposed file change bound to one observed base file."""

    relative_path: str
    base_content: str
    base_hash: str
    proposed_content: str
    identity: FileIdentity
    file_mode: int
    newline: str | None

    @property
    def hunks(self) -> list[Hunk]:
        return compute_hunks(self.base_content, self.proposed_content)


@dataclass(frozen=True)
class WorkspaceReview:
    """The proposal refreshed against the current file."""

    proposal: WorkspaceProposal
    status: str
    current_content: str
    current_hash: str
    current_identity: FileIdentity
    current_version: FileVersion
    current_mode: int
    current_newline: str | None
    effective_base_content: str | None
    effective_base_hash: str | None
    effective_proposed_content: str | None
    hunks: list[Hunk]


@dataclass(frozen=True)
class WorkspaceApplyResult:
    """The result of applying a selected subset of review hunks."""

    content: str
    content_hash: str
    applied_hunk_indices: tuple[int, ...]
    remaining_hunks: list[Hunk]
    remaining_proposed_content: str | None
    file_mode: int
    newline: str | None
    wrote: bool
    status: str


def sha256_text(text: str) -> str:
    """Stable SHA-256 over the exact proposed text."""
    return hashlib.sha256(text.encode()).hexdigest()


def detect_newline(text: str) -> str | None:
    """The dominant newline marker, or None for a single-line file."""
    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    if "\r" in text:
        return "\r"
    return None


def read_workspace_snapshot(
    root: Path | str,
    relative_path: str,
    *,
    validate_path: PathValidator | None = None,
) -> WorkspaceSnapshot:
    """Read one validated text file from the attached workspace."""
    validated = _validate_existing_file(Path(root), relative_path, validate_path=validate_path)
    target = _open_validated(validated)
    content, file_stat = _read_text(target)
    return WorkspaceSnapshot(
        relative_path=validated.relative_path,
        path=target,
        content=content,
        content_hash=sha256_text(content),
        identity=FileIdentity.from_stat(file_stat),
        version=FileVersion.from_stat(file_stat),
        mode=stat.S_IMODE(file_stat.st_mode),
        newline=detect_newline(content),
    )


def build_workspace_proposal(
    root: Path | str,
    relative_path: str,
    observed_base_hash: str,
    proposed_content: str,
    *,
    validate_path: PathValidator | None = None,
) -> WorkspaceProposal:
    """Bind a full-text proposal to the current file snapshot.

    The caller must supply the base hash it observed earlier. A mismatch means the file
    moved before the proposal reached the server and should be re-read first.
    """
    snapshot = read_workspace_snapshot(root, relative_path, validate_path=validate_path)
    if snapshot.content_hash != observed_base_hash:
        raise ConflictError(_STALE_PROPOSAL_MESSAGE)
    normalized_proposal = _normalize_newlines(proposed_content, snapshot.newline)
    if len(normalized_proposal.encode("utf-8")) > workspace_paths.MAX_TEXT_FILE_BYTES:
        raise ValueError("A workspace proposal cannot exceed 1 MiB.")
    return WorkspaceProposal(
        relative_path=snapshot.relative_path,
        base_content=snapshot.content,
        base_hash=snapshot.content_hash,
        proposed_content=normalized_proposal,
        identity=snapshot.identity,
        file_mode=snapshot.mode,
        newline=snapshot.newline,
    )


def review_workspace_proposal(
    root: Path | str,
    proposal: WorkspaceProposal,
    *,
    validate_path: PathValidator | None = None,
) -> WorkspaceReview:
    """Refresh a proposal against the current file contents.

    A replacement at the same path is a race and therefore stale. In-place user edits on
    the same file identity rebase through the same exact hunk math used by suggestions.
    """
    snapshot = read_workspace_snapshot(root, proposal.relative_path, validate_path=validate_path)
    if snapshot.identity != proposal.identity:
        return WorkspaceReview(
            proposal=proposal,
            status="stale",
            current_content=snapshot.content,
            current_hash=snapshot.content_hash,
            current_identity=snapshot.identity,
            current_version=snapshot.version,
            current_mode=snapshot.mode,
            current_newline=snapshot.newline,
            effective_base_content=None,
            effective_base_hash=None,
            effective_proposed_content=None,
            hunks=[],
        )
    if snapshot.content_hash == proposal.base_hash:
        return WorkspaceReview(
            proposal=proposal,
            status="fresh",
            current_content=snapshot.content,
            current_hash=snapshot.content_hash,
            current_identity=snapshot.identity,
            current_version=snapshot.version,
            current_mode=snapshot.mode,
            current_newline=snapshot.newline,
            effective_base_content=snapshot.content,
            effective_base_hash=snapshot.content_hash,
            effective_proposed_content=proposal.proposed_content,
            hunks=proposal.hunks,
        )
    if snapshot.content == proposal.proposed_content:
        return WorkspaceReview(
            proposal=proposal,
            status="applied",
            current_content=snapshot.content,
            current_hash=snapshot.content_hash,
            current_identity=snapshot.identity,
            current_version=snapshot.version,
            current_mode=snapshot.mode,
            current_newline=snapshot.newline,
            effective_base_content=snapshot.content,
            effective_base_hash=snapshot.content_hash,
            effective_proposed_content=snapshot.content,
            hunks=[],
        )
    rebased = apply_patch(snapshot.content, proposal.base_content, proposal.proposed_content)
    if rebased is None:
        return WorkspaceReview(
            proposal=proposal,
            status="stale",
            current_content=snapshot.content,
            current_hash=snapshot.content_hash,
            current_identity=snapshot.identity,
            current_version=snapshot.version,
            current_mode=snapshot.mode,
            current_newline=snapshot.newline,
            effective_base_content=None,
            effective_base_hash=None,
            effective_proposed_content=None,
            hunks=[],
        )
    return WorkspaceReview(
        proposal=proposal,
        status="fresh",
        current_content=snapshot.content,
        current_hash=snapshot.content_hash,
        current_identity=snapshot.identity,
        current_version=snapshot.version,
        current_mode=snapshot.mode,
        current_newline=snapshot.newline,
        effective_base_content=snapshot.content,
        effective_base_hash=snapshot.content_hash,
        effective_proposed_content=rebased,
        hunks=compute_hunks(snapshot.content, rebased),
    )


def apply_workspace_hunks(
    root: Path | str,
    proposal: WorkspaceProposal,
    accepted_hunks: list[Mapping[str, object]],
    *,
    validate_path: PathValidator | None = None,
) -> WorkspaceApplyResult:
    """Apply the selected hunks and atomically replace the file.

    The selection is server-validated against the freshly derived hunk set. Unaccepted
    hunks remain in the returned review state for the future persistence layer.
    """
    review = review_workspace_proposal(root, proposal, validate_path=validate_path)
    if review.status == "stale":
        raise ConflictError(_STALE_PROPOSAL_MESSAGE)
    if review.status == "applied":
        return WorkspaceApplyResult(
            content=review.current_content,
            content_hash=review.current_hash,
            applied_hunk_indices=(),
            remaining_hunks=[],
            remaining_proposed_content=None,
            file_mode=review.current_mode,
            newline=review.current_newline,
            wrote=False,
            status="applied",
        )
    selected = _resolve_hunk_selection(review.hunks, accepted_hunks)
    if not selected:
        return WorkspaceApplyResult(
            content=review.current_content,
            content_hash=review.current_hash,
            applied_hunk_indices=(),
            remaining_hunks=review.hunks,
            remaining_proposed_content=review.effective_proposed_content,
            file_mode=review.current_mode,
            newline=review.current_newline,
            wrote=False,
            status="fresh",
        )
    patched = _apply_selected(review.effective_base_content or "", selected)
    _atomic_replace(
        Path(root),
        proposal.relative_path,
        patched,
        expected_identity=review.current_identity,
        expected_version=review.current_version,
        expected_hash=review.current_hash,
        file_mode=review.current_mode,
        validate_path=validate_path,
    )
    remaining_hunks = compute_hunks(patched, review.effective_proposed_content or patched)
    return WorkspaceApplyResult(
        content=patched,
        content_hash=sha256_text(patched),
        applied_hunk_indices=tuple(hunk.index for hunk in selected),
        remaining_hunks=remaining_hunks,
        remaining_proposed_content=(review.effective_proposed_content if remaining_hunks else None),
        file_mode=review.current_mode,
        newline=detect_newline(patched),
        wrote=True,
        status="partially_applied" if remaining_hunks else "applied",
    )


def _resolve_hunk_selection(
    hunks: list[Hunk], accepted_hunks: list[Mapping[str, object]]
) -> list[Hunk]:
    available = {hunk.index: hunk for hunk in hunks}
    seen: set[int] = set()
    selected: list[Hunk] = []
    for item in accepted_hunks:
        index_value = item.get("index")
        digest_value = item.get("hash")
        if (
            isinstance(index_value, bool)
            or not isinstance(index_value, int)
            or not isinstance(digest_value, str)
            or not digest_value
        ):
            raise ConflictError(_HUNK_RACE_MESSAGE)
        index = index_value
        digest = digest_value
        if index in seen or index not in available:
            raise ConflictError(_HUNK_RACE_MESSAGE)
        hunk = available[index]
        if hunk.hash != digest:
            raise ConflictError(_HUNK_RACE_MESSAGE)
        seen.add(index)
        selected.append(hunk)
    selected.sort(key=lambda hunk: hunk.index)
    return selected


def _apply_selected(content: str, hunks: list[Hunk]) -> str:
    offset = 0
    for hunk in hunks:
        shifted = replace(hunk, old_start=hunk.old_start + offset)
        patched = apply_hunk(content, shifted)
        if patched is None:
            raise ConflictError(_HUNK_RACE_MESSAGE)
        content = patched
        offset += hunk.new_lines - hunk.old_lines
    return content


def _atomic_replace(
    root: Path,
    relative_path: str,
    content: str,
    *,
    expected_identity: FileIdentity,
    expected_version: FileVersion,
    expected_hash: str,
    file_mode: int,
    validate_path: PathValidator | None,
) -> None:
    snapshot = read_workspace_snapshot(root, relative_path, validate_path=validate_path)
    if snapshot.identity != expected_identity or snapshot.version != expected_version:
        raise ConflictError(_STALE_PROPOSAL_MESSAGE)
    if snapshot.content_hash != expected_hash:
        raise ConflictError(_STALE_PROPOSAL_MESSAGE)
    parent = snapshot.path.parent
    target_name = snapshot.path.name
    temp_name = f".{target_name}.{secrets.token_hex(16)}.tmp"
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd: int | None = None
    temp_created = False
    try:
        parent_fd = os.open(parent, directory_flags)
        parent_opened = os.fstat(parent_fd)
        parent_current = os.lstat(parent)
        if FileIdentity.from_stat(parent_opened) != FileIdentity.from_stat(parent_current):
            raise ConflictError(_STALE_PROPOSAL_MESSAGE)
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        temp_created = True
        try:
            payload = content.encode("utf-8")
            with os.fdopen(temp_fd, "wb", closefd=False) as handle:
                handle.write(payload)
                handle.flush()
            # Preserve ordinary permissions but never copy setuid/setgid/sticky bits onto newly
            # generated content.
            os.fchmod(temp_fd, file_mode & 0o777)
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)

        current_content, current_stat = _read_relative_file(parent_fd, target_name)
        if (
            FileIdentity.from_stat(current_stat) != expected_identity
            or FileVersion.from_stat(current_stat) != expected_version
            or sha256_text(current_content) != expected_hash
        ):
            raise ConflictError(_STALE_PROPOSAL_MESSAGE)
        latest_parent = os.lstat(parent)
        if FileIdentity.from_stat(latest_parent) != FileIdentity.from_stat(parent_opened):
            raise ConflictError(_STALE_PROPOSAL_MESSAGE)
        os.replace(
            temp_name,
            target_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temp_created = False
        # The file itself is already fsynced and the atomic replacement succeeded. A directory
        # fsync failure must not be reported as "no write" after the effect already happened.
        with suppress(OSError):
            os.fsync(parent_fd)
    except ConflictError:
        raise
    except OSError as exc:
        raise LyraError(_WRITE_FAILED_MESSAGE) from exc
    finally:
        if temp_created and parent_fd is not None:
            with suppress(OSError):
                os.unlink(temp_name, dir_fd=parent_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _validate_existing_file(
    root: Path,
    relative_path: str,
    *,
    validate_path: PathValidator | None,
) -> WorkspaceSnapshotPath:
    normalized = _normalize_relative_path(relative_path)
    if validate_path is not None:
        candidate = Path(validate_path(root, normalized))
    else:
        candidate = _external_or_fallback_validate(root, normalized)
    return _finalize_validated_path(root.resolve(), normalized, candidate)


@dataclass(frozen=True)
class WorkspaceSnapshotPath:
    """A resolved, still-to-be-opened workspace path."""

    relative_path: str
    path: Path


def _external_or_fallback_validate(root: Path, relative_path: str) -> Path:
    return workspace_paths.validate_workspace_file_path(root, relative_path)


def _fallback_validate(root: Path, relative_path: str) -> Path:
    candidate = root.resolve()
    for part in PurePosixPath(relative_path).parts:
        candidate = candidate / part
        _refuse_symlink(candidate)
    return candidate


def _finalize_validated_path(
    root: Path, relative_path: str, candidate: Path
) -> WorkspaceSnapshotPath:
    _ensure_no_symlink_segments(root, candidate)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LyraError(_INVALID_FILE_MESSAGE) from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise LyraError(_INVALID_PATH_MESSAGE) from exc
    _refuse_symlink(resolved)
    file_stat = os.lstat(resolved)
    if not stat.S_ISREG(file_stat.st_mode):
        raise LyraError(_INVALID_FILE_MESSAGE)
    return WorkspaceSnapshotPath(relative_path=relative_path, path=resolved)


def _ensure_no_symlink_segments(root: Path, candidate: Path) -> None:
    current = root
    absolute = candidate if candidate.is_absolute() else root / candidate
    try:
        tail = absolute.relative_to(root)
    except ValueError as exc:
        raise LyraError(_INVALID_PATH_MESSAGE) from exc
    for part in tail.parts:
        current = current / part
        _refuse_symlink(current)


def _open_validated(validated: WorkspaceSnapshotPath) -> Path:
    _refuse_symlink(validated.path)
    return validated.path


def _read_text(path: Path) -> tuple[str, os.stat_result]:
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(path, flags | nofollow)
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise LyraError(_INVALID_FILE_MESSAGE)
        if file_stat.st_size > workspace_paths.MAX_TEXT_FILE_BYTES:
            raise LyraError(_INVALID_FILE_MESSAGE)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            content = handle.read(workspace_paths.MAX_TEXT_FILE_BYTES + 1)
        if len(content) > workspace_paths.MAX_TEXT_FILE_BYTES or b"\x00" in content:
            raise LyraError(_INVALID_FILE_MESSAGE)
        return content.decode("utf-8"), file_stat
    except OSError as exc:
        raise LyraError(_INVALID_FILE_MESSAGE) from exc
    except UnicodeDecodeError as exc:
        raise LyraError(_INVALID_FILE_MESSAGE) from exc
    finally:
        if fd is not None:
            os.close(fd)


def _normalize_relative_path(relative_path: str) -> str:
    if not relative_path or "\\" in relative_path:
        raise LyraError(_INVALID_PATH_MESSAGE)
    parsed = PurePosixPath(relative_path)
    if parsed.is_absolute():
        raise LyraError(_INVALID_PATH_MESSAGE)
    parts = parsed.parts
    if not parts:
        raise LyraError(_INVALID_PATH_MESSAGE)
    if any(part in ("", ".", "..") for part in parts):
        raise LyraError(_INVALID_PATH_MESSAGE)
    return str(parsed)


def _normalize_newlines(content: str, newline: str | None) -> str:
    if newline is None:
        return content
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return normalized if newline == "\n" else normalized.replace("\n", newline)


def _read_relative_file(parent_fd: int, filename: str) -> tuple[str, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(filename, flags, dir_fd=parent_fd)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ConflictError(_STALE_PROPOSAL_MESSAGE)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            payload = handle.read(workspace_paths.MAX_TEXT_FILE_BYTES + 1)
        if len(payload) > workspace_paths.MAX_TEXT_FILE_BYTES or b"\x00" in payload:
            raise ConflictError(_STALE_PROPOSAL_MESSAGE)
        return payload.decode("utf-8"), file_stat
    except (OSError, UnicodeDecodeError) as exc:
        raise ConflictError(_STALE_PROPOSAL_MESSAGE) from exc
    finally:
        os.close(fd)


def _refuse_symlink(path: Path) -> None:
    try:
        file_stat = os.lstat(path)
    except OSError as exc:
        raise LyraError(_INVALID_FILE_MESSAGE) from exc
    if stat.S_ISLNK(file_stat.st_mode):
        raise LyraError(_INVALID_FILE_MESSAGE)
