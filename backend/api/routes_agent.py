"""Class-scoped APIs for Phase 4 workspaces, reviewed changes, and commands.

The agent can create inert proposals through its contextual tool registry. Only these
ordinary HTTP routes issue and consume confirmation nonces, keeping file application and
process execution outside the model-callable surface.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status
from pydantic import BaseModel, Field, field_validator

from backend.core import (
    agent_store,
    commands,
    confirmations,
    sessions,
    tool_audit,
    workspace_changes,
    workspace_paths,
)
from backend.core.classes import get_class
from backend.core.errors import ConflictError, LyraError, NotFoundError
from backend.core.origins import ALLOWED_BROWSER_ORIGIN_SET
from backend.storage.database import get_db

router = APIRouter(prefix="/api", tags=["agent"])

DbConn = Annotated[sqlite3.Connection, Depends(get_db)]
Origin = Annotated[str | None, Header(alias="Origin")]


class WorkspaceAttach(BaseModel):
    root_path: str = Field(min_length=1, max_length=4096)
    display_name: str | None = Field(default=None, max_length=200)

    @field_validator("root_path")
    @classmethod
    def clean_root(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A workspace path cannot be blank.")
        return value


class WorkspaceGrantsUpdate(BaseModel):
    read_enabled: bool | None = None
    change_proposals_enabled: bool | None = None
    commands_enabled: bool | None = None


class WorkspaceChangeCreate(BaseModel):
    relative_path: str = Field(min_length=1, max_length=4096)
    observed_base_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    proposed_content: str = Field(max_length=workspace_paths.MAX_TEXT_FILE_BYTES)
    rationale: str | None = Field(default=None, max_length=4000)


class HunkSelection(BaseModel):
    index: int = Field(ge=0)
    hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class ChangeConfirmationRequest(BaseModel):
    accepted_hunks: list[HunkSelection] = Field(min_length=1, max_length=1000)


class ChangeApplyRequest(ChangeConfirmationRequest):
    confirmation_token: str = Field(min_length=64, max_length=64)


class ChangeRejectRequest(BaseModel):
    rejected_hunks: list[HunkSelection] = Field(default_factory=list, max_length=1000)


class CommandCreate(BaseModel):
    argv: list[str] = Field(min_length=1, max_length=commands.MAX_ARGUMENTS)
    relative_cwd: str = Field(default=".", min_length=1, max_length=4096)
    reason: str = Field(min_length=1, max_length=4000)
    expected_signal: str | None = Field(default=None, max_length=1000)
    timeout_seconds: int = Field(
        default=agent_store.DEFAULT_TIMEOUT_SECONDS,
        ge=1,
        le=agent_store.MAX_TIMEOUT_SECONDS,
    )


class CommandExecuteRequest(BaseModel):
    confirmation_token: str = Field(min_length=64, max_length=64)


def _as_domain_error(exc: ValueError) -> LyraError:
    return LyraError(str(exc))


def _require_origin(origin: str | None) -> str:
    clean = "" if origin is None else origin.strip()
    if clean not in ALLOWED_BROWSER_ORIGIN_SET:
        raise LyraError("That origin cannot confirm local effects.")
    return clean


def _require_session(conn: sqlite3.Connection, class_id: int, session_id: int) -> None:
    get_class(conn, class_id)
    session = sessions.get_session(conn, session_id)
    if int(session["class_id"]) != class_id:
        raise NotFoundError("That conversation does not exist in this class.")


def _workspace(
    conn: sqlite3.Connection,
    class_id: int,
    *,
    read: bool = False,
    changes: bool = False,
    command: bool = False,
) -> dict[str, object]:
    workspace = agent_store.get_workspace_for_class(conn, class_id)
    if workspace is None:
        raise NotFoundError("That class has no attached workspace.")
    root = workspace_paths.canonical_workspace_root(str(workspace["root_path"]))
    details = os.lstat(root)
    if int(details.st_dev) != int(workspace["root_device"]) or int(details.st_ino) != int(
        workspace["root_inode"]
    ):
        raise ConflictError("The attached workspace changed. Attach it again before continuing.")
    if read and not workspace["read_enabled"]:
        raise ConflictError("Workspace read is not enabled for this class.")
    if changes and not workspace["change_proposals_enabled"]:
        raise ConflictError("Change proposals are not enabled for this workspace.")
    if command and not workspace["commands_enabled"]:
        raise ConflictError("Commands are not enabled for this workspace.")
    return workspace


def _audit_call[T](
    conn: sqlite3.Connection,
    *,
    tool: str,
    capability: str,
    effect: str,
    arguments: object,
    class_id: int,
    session_id: int | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    operation: Callable[[], T],
) -> T:
    event = tool_audit.start_event(
        conn,
        caller_kind="user_api",
        tool=tool,
        capability=capability,
        effect=effect,
        arguments=arguments,
        policy_decision="allowed",
        class_id=class_id,
        session_id=session_id,
        target_kind=target_kind,
        target_id=target_id,
    )
    try:
        result = operation()
    except ValueError as exc:
        tool_audit.finish_event(conn, event.id, state="refused", error_message=str(exc))
        raise _as_domain_error(exc) from exc
    except LyraError as exc:
        terminal = "refused" if exc.status in {400, 404, 409} else "failed"
        tool_audit.finish_event(conn, event.id, state=terminal, error_message=exc.message)
        raise
    except Exception as exc:
        tool_audit.finish_event(conn, event.id, state="failed", error_message=str(exc))
        raise
    tool_audit.finish_event(
        conn, event.id, state="succeeded", result_summary=_result_summary(result)
    )
    return result


def _result_summary(result: object) -> dict[str, object]:
    if isinstance(result, Mapping):
        summary: dict[str, object] = {}
        for key in ("id", "state", "status", "path", "truncated", "exit_code"):
            if key in result:
                summary[key] = result[key]
        for key in ("entries", "matches", "hunks"):
            value = result.get(key)
            if isinstance(value, list):
                summary[f"{key}_count"] = len(value)
        return summary
    return {"completed": True}


def _proposal_from_row(row: Mapping[str, object]) -> workspace_changes.WorkspaceProposal:
    return workspace_changes.WorkspaceProposal(
        relative_path=str(row["relative_path"]),
        base_content=str(row["base_content"]),
        base_hash=str(row["base_hash"]),
        proposed_content=str(row["proposed_content"]),
        identity=workspace_changes.FileIdentity(
            device=int(row["file_device"]), inode=int(row["file_inode"])
        ),
        file_mode=int(row["file_mode"]),
        newline=None if row["newline"] is None else str(row["newline"]),
    )


def _selection_payload(items: list[HunkSelection]) -> list[dict[str, object]]:
    values = [{"index": item.index, "hash": item.hash} for item in items]
    if len({item["index"] for item in values}) != len(values):
        raise LyraError("Each selected hunk may appear only once.")
    return sorted(values, key=lambda item: int(item["index"]))


def _review_json(
    row: Mapping[str, object], review: workspace_changes.WorkspaceReview
) -> dict[str, object]:
    accepted = {int(value) for value in row["accepted_hunks"]}  # type: ignore[union-attr]
    rejected = {int(value) for value in row["rejected_hunks"]}  # type: ignore[union-attr]
    hunks = []
    for hunk in review.hunks:
        item = hunk.to_dict()
        if hunk.index in accepted:
            item["decision"] = "accepted"
        elif hunk.index in rejected:
            item["decision"] = "rejected"
        hunks.append(item)
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "session_id": row["session_id"],
        "path": row["relative_path"],
        "rationale": row["rationale"],
        "state": row["state"] if review.status == "fresh" else review.status,
        "stored_state": row["state"],
        "current_hash": review.current_hash,
        "current_content": review.current_content if review.status == "stale" else None,
        "proposed_content": row["proposed_content"] if review.status == "stale" else None,
        "hunks": hunks,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _stored_change_json(row: Mapping[str, object]) -> dict[str, object]:
    proposal = _proposal_from_row(row)
    accepted = {int(value) for value in row["accepted_hunks"]}  # type: ignore[union-attr]
    rejected = {int(value) for value in row["rejected_hunks"]}  # type: ignore[union-attr]
    hunks: list[dict[str, object]] = []
    for hunk in proposal.hunks:
        item = hunk.to_dict()
        if hunk.index in accepted:
            item["decision"] = "accepted"
        elif hunk.index in rejected:
            item["decision"] = "rejected"
        hunks.append(item)
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "session_id": row["session_id"],
        "path": row["relative_path"],
        "rationale": row["rationale"],
        "state": row["state"],
        "stored_state": row["state"],
        "current_hash": row["after_hash"] or row["base_hash"],
        "current_content": None,
        "proposed_content": None,
        "hunks": hunks,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _change_context(
    conn: sqlite3.Connection, class_id: int, session_id: int, change_id: int
) -> tuple[dict[str, object], dict[str, object], workspace_changes.WorkspaceReview]:
    _require_session(conn, class_id, session_id)
    workspace = _workspace(conn, class_id, read=True, changes=True)
    row = agent_store.get_workspace_change(
        conn,
        change_id,
        class_id=class_id,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
    )
    proposal = _proposal_from_row(row)
    review = workspace_changes.review_workspace_proposal(str(workspace["root_path"]), proposal)
    return workspace, row, review


def _validated_change_payload(
    workspace: Mapping[str, object],
    row: Mapping[str, object],
    review: workspace_changes.WorkspaceReview,
    selections: list[HunkSelection],
) -> dict[str, object]:
    if review.status != "fresh":
        raise ConflictError("That workspace change is not fresh enough to apply.")
    selected = _selection_payload(selections)
    available = {hunk.index: hunk.hash for hunk in review.hunks}
    if any(available.get(int(item["index"])) != item["hash"] for item in selected):
        raise ConflictError("That hunk changed since it was fetched. Re-fetch the proposal.")
    return {
        "change_id": int(row["id"]),
        "workspace_id": int(workspace["id"]),
        "session_id": int(row["session_id"]),
        "relative_path": str(row["relative_path"]),
        "current_hash": review.current_hash,
        "accepted_hunks": selected,
    }


def _command_payload(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "request_id": int(row["id"]),
        "workspace_id": int(row["workspace_id"]),
        "session_id": int(row["session_id"]),
        "argv": list(row["argv"]),  # type: ignore[arg-type]
        "relative_cwd": str(row["relative_cwd"]),
        "timeout_seconds": int(row["timeout_seconds"]),
    }


def _command_json(row: Mapping[str, object]) -> dict[str, object]:
    response = dict(row)
    response["truncated"] = row.get("state_reason") == "output_truncated"
    return response


def _command_context(
    conn: sqlite3.Connection,
    class_id: int,
    session_id: int,
    request_id: int,
    *,
    require_grant: bool = True,
) -> tuple[dict[str, object], dict[str, object]]:
    _require_session(conn, class_id, session_id)
    workspace = _workspace(conn, class_id, command=require_grant)
    row = agent_store.get_command_request(
        conn,
        request_id,
        class_id=class_id,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
    )
    return workspace, row


@router.get("/classes/{class_id}/sessions/{session_id}/agent/activity")
def list_agent_activity(
    class_id: int,
    session_id: int,
    conn: DbConn,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[dict[str, object]]:
    _require_session(conn, class_id, session_id)
    return tool_audit.list_events(
        conn,
        class_id=class_id,
        session_id=session_id,
        limit=limit,
    )


@router.put("/classes/{class_id}/workspace", status_code=status.HTTP_201_CREATED)
def attach_workspace(class_id: int, payload: WorkspaceAttach, conn: DbConn) -> dict[str, object]:
    get_class(conn, class_id)
    return _audit_call(
        conn,
        tool="attach_workspace",
        capability="workspace_attachment",
        effect="database_write",
        arguments={"display_name": payload.display_name},
        class_id=class_id,
        operation=lambda: agent_store.attach_workspace(
            conn,
            class_id,
            root_path=payload.root_path,
            display_name=payload.display_name,
        ),
    )


@router.get("/classes/{class_id}/workspace")
def read_workspace_attachment(class_id: int, conn: DbConn) -> dict[str, object] | None:
    """Return null for the normal unattached state so browser consoles stay error-free."""

    return agent_store.get_workspace_for_class(conn, class_id)


@router.delete("/classes/{class_id}/workspace", status_code=status.HTTP_204_NO_CONTENT)
def detach_workspace(class_id: int, conn: DbConn) -> None:
    return _audit_call(
        conn,
        tool="detach_workspace",
        capability="workspace_attachment",
        effect="database_write",
        arguments={},
        class_id=class_id,
        operation=lambda: agent_store.detach_workspace(conn, class_id),
    )


@router.patch("/classes/{class_id}/workspace/grants")
def update_workspace_grants(
    class_id: int, payload: WorkspaceGrantsUpdate, conn: DbConn
) -> dict[str, object]:
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise LyraError("At least one workspace grant must be supplied.")
    return _audit_call(
        conn,
        tool="update_workspace_grants",
        capability="workspace_policy",
        effect="database_write",
        arguments=updates,
        class_id=class_id,
        operation=lambda: agent_store.update_workspace_grants(conn, class_id, **updates),
    )


@router.get("/classes/{class_id}/sessions/{session_id}/workspace/list")
def list_workspace(
    class_id: int,
    session_id: int,
    conn: DbConn,
    path: str = Query(default=".", max_length=4096),
    limit: int = Query(default=workspace_paths.DEFAULT_LIST_LIMIT, ge=1, le=1000),
) -> dict[str, object]:
    def operation() -> dict[str, object]:
        _require_session(conn, class_id, session_id)
        workspace = _workspace(conn, class_id, read=True)
        return workspace_paths.list_workspace(str(workspace["root_path"]), path, limit=limit)

    return _audit_call(
        conn,
        tool="list_workspace",
        capability="workspace_read",
        effect="filesystem_read",
        arguments={"path": path, "limit": limit},
        class_id=class_id,
        session_id=session_id,
        target_kind="workspace_path",
        target_id=path,
        operation=operation,
    )


@router.get("/classes/{class_id}/sessions/{session_id}/workspace/read")
def read_workspace_file(
    class_id: int,
    session_id: int,
    conn: DbConn,
    path: str = Query(min_length=1, max_length=4096),
    start_line: int = Query(default=1, ge=1),
    end_line: int | None = Query(default=None, ge=1),
) -> dict[str, object]:
    def operation() -> dict[str, object]:
        _require_session(conn, class_id, session_id)
        workspace = _workspace(conn, class_id, read=True)
        return workspace_paths.read_workspace_file(
            str(workspace["root_path"]), path, start_line=start_line, end_line=end_line
        )

    return _audit_call(
        conn,
        tool="read_workspace_file",
        capability="workspace_read",
        effect="filesystem_read",
        arguments={"path": path, "start_line": start_line, "end_line": end_line},
        class_id=class_id,
        session_id=session_id,
        target_kind="workspace_file",
        target_id=path,
        operation=operation,
    )


@router.get("/classes/{class_id}/sessions/{session_id}/workspace/search")
def search_workspace(
    class_id: int,
    session_id: int,
    conn: DbConn,
    query: str = Query(min_length=1, max_length=workspace_paths.MAX_SEARCH_QUERY_CHARS),
    glob: str | None = Query(default=None, max_length=500),
    path: str = Query(default=".", max_length=4096),
    limit: int = Query(default=workspace_paths.DEFAULT_SEARCH_RESULTS, ge=1, le=200),
) -> dict[str, object]:
    def operation() -> dict[str, object]:
        _require_session(conn, class_id, session_id)
        workspace = _workspace(conn, class_id, read=True)
        return workspace_paths.search_workspace(
            str(workspace["root_path"]),
            query,
            glob,
            relative_path=path,
            max_results=limit,
        )

    return _audit_call(
        conn,
        tool="search_workspace",
        capability="workspace_read",
        effect="filesystem_read",
        arguments={
            "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
            "query_chars": len(query),
            "glob": glob,
            "path": path,
            "limit": limit,
        },
        class_id=class_id,
        session_id=session_id,
        target_kind="workspace_path",
        target_id=path,
        operation=operation,
    )


@router.post(
    "/classes/{class_id}/sessions/{session_id}/workspace/changes",
    status_code=status.HTTP_201_CREATED,
)
def create_workspace_change(
    class_id: int, session_id: int, payload: WorkspaceChangeCreate, conn: DbConn
) -> dict[str, object]:
    def operation() -> dict[str, object]:
        _require_session(conn, class_id, session_id)
        workspace = _workspace(conn, class_id, read=True, changes=True)
        proposal = workspace_changes.build_workspace_proposal(
            str(workspace["root_path"]),
            payload.relative_path,
            payload.observed_base_hash,
            payload.proposed_content,
        )
        row = agent_store.create_workspace_change(
            conn,
            class_id,
            workspace_id=int(workspace["id"]),
            session_id=session_id,
            relative_path=proposal.relative_path,
            base_hash=proposal.base_hash,
            base_content=proposal.base_content,
            proposed_content=proposal.proposed_content,
            file_device=proposal.identity.device,
            file_inode=proposal.identity.inode,
            file_mode=proposal.file_mode,
            newline=proposal.newline,
            rationale=payload.rationale,
        )
        review = workspace_changes.review_workspace_proposal(str(workspace["root_path"]), proposal)
        return _review_json(row, review)

    return _audit_call(
        conn,
        tool="propose_workspace_change",
        capability="change_proposal",
        effect="database_proposal",
        arguments=payload.model_dump(),
        class_id=class_id,
        session_id=session_id,
        target_kind="workspace_file",
        target_id=payload.relative_path,
        operation=operation,
    )


@router.get("/classes/{class_id}/sessions/{session_id}/workspace/changes")
def list_workspace_changes(
    class_id: int,
    session_id: int,
    conn: DbConn,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[dict[str, object]]:
    _require_session(conn, class_id, session_id)
    workspace = _workspace(conn, class_id)
    rows = agent_store.list_workspace_changes(
        conn,
        class_id=class_id,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
        limit=limit,
    )
    return [_stored_change_json(row) for row in rows]


@router.get("/classes/{class_id}/sessions/{session_id}/workspace/changes/{change_id}")
def get_workspace_change(
    class_id: int, session_id: int, change_id: int, conn: DbConn
) -> dict[str, object]:
    _require_session(conn, class_id, session_id)
    workspace = _workspace(conn, class_id)
    row = agent_store.get_workspace_change(
        conn,
        change_id,
        class_id=class_id,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
    )
    return _stored_change_json(row)


@router.get("/classes/{class_id}/sessions/{session_id}/workspace/changes/{change_id}/review")
def review_workspace_change(
    class_id: int, session_id: int, change_id: int, conn: DbConn
) -> dict[str, object]:
    def operation() -> dict[str, object]:
        _workspace, row, review = _change_context(conn, class_id, session_id, change_id)
        return _review_json(row, review)

    return _audit_call(
        conn,
        tool="review_workspace_change",
        capability="change_proposal",
        effect="filesystem_read",
        arguments={"change_id": change_id},
        class_id=class_id,
        session_id=session_id,
        target_kind="workspace_change",
        target_id=str(change_id),
        operation=operation,
    )


@router.post("/classes/{class_id}/sessions/{session_id}/workspace/changes/{change_id}/confirmation")
def confirm_workspace_change(
    class_id: int,
    session_id: int,
    change_id: int,
    payload: ChangeConfirmationRequest,
    conn: DbConn,
    origin: Origin = None,
) -> dict[str, object]:
    def operation() -> dict[str, object]:
        request_origin = _require_origin(origin)
        workspace, row, review = _change_context(conn, class_id, session_id, change_id)
        if row["state"] not in {
            agent_store.WORKSPACE_CHANGE_PENDING,
            agent_store.WORKSPACE_CHANGE_PARTIALLY_APPLIED,
        }:
            raise ConflictError("That workspace change is no longer pending review.")
        exact = _validated_change_payload(workspace, row, review, payload.accepted_hunks)
        issued = confirmations.issue_confirmation(
            conn,
            origin=request_origin,
            class_id=class_id,
            session_id=session_id,
            action_kind="apply_change",
            target_id=str(change_id),
            current_hash=review.current_hash,
            payload=exact,
        )
        return {"token": issued.token, "expires_at": issued.expires_at, "payload": exact}

    return _audit_call(
        conn,
        tool="confirm_workspace_change",
        capability="change_proposal",
        effect="confirmation_issue",
        arguments={"change_id": change_id, "accepted_hunks": payload.accepted_hunks},
        class_id=class_id,
        session_id=session_id,
        target_kind="workspace_change",
        target_id=str(change_id),
        operation=operation,
    )


@router.post("/classes/{class_id}/sessions/{session_id}/workspace/changes/{change_id}/apply")
def apply_workspace_change(
    class_id: int,
    session_id: int,
    change_id: int,
    payload: ChangeApplyRequest,
    conn: DbConn,
    origin: Origin = None,
) -> dict[str, object]:
    def operation() -> dict[str, object]:
        request_origin = _require_origin(origin)
        workspace, row, review = _change_context(conn, class_id, session_id, change_id)
        exact = _validated_change_payload(workspace, row, review, payload.accepted_hunks)
        confirmations.consume_confirmation(
            conn,
            token=payload.confirmation_token,
            origin=request_origin,
            class_id=class_id,
            session_id=session_id,
            action_kind="apply_change",
            target_id=str(change_id),
            current_hash=review.current_hash,
            payload=exact,
        )
        proposal = _proposal_from_row(row)
        result = workspace_changes.apply_workspace_hunks(
            str(workspace["root_path"]),
            proposal,
            exact["accepted_hunks"],  # type: ignore[arg-type]
        )
        transition_arguments: dict[str, object] = {}
        if result.status == agent_store.WORKSPACE_CHANGE_PARTIALLY_APPLIED:
            snapshot = workspace_changes.read_workspace_snapshot(
                str(workspace["root_path"]), str(row["relative_path"])
            )
            transition_arguments = {
                "base_hash": snapshot.content_hash,
                "base_content": snapshot.content,
                "proposed_content": result.remaining_proposed_content or snapshot.content,
                "file_device": snapshot.identity.device,
                "file_inode": snapshot.identity.inode,
                "file_mode": snapshot.mode,
                "newline": snapshot.newline,
            }
        transitioned = agent_store.transition_workspace_change(
            conn,
            change_id,
            class_id=class_id,
            workspace_id=int(workspace["id"]),
            session_id=session_id,
            state=result.status,
            accepted_hunks=(
                []
                if result.status == agent_store.WORKSPACE_CHANGE_PARTIALLY_APPLIED
                else list(result.applied_hunk_indices)
            ),
            rejected_hunks=(
                []
                if result.status == agent_store.WORKSPACE_CHANGE_PARTIALLY_APPLIED
                else list(row["rejected_hunks"])  # type: ignore[arg-type]
            ),
            after_hash=result.content_hash,
            **transition_arguments,
        )
        if result.status == agent_store.WORKSPACE_CHANGE_PARTIALLY_APPLIED:
            refreshed = workspace_changes.review_workspace_proposal(
                str(workspace["root_path"]), _proposal_from_row(transitioned)
            )
            response = _review_json(transitioned, refreshed)
            response["wrote"] = result.wrote
            return response
        response = dict(transitioned)
        response["path"] = response.pop("relative_path")
        response["state"] = result.status
        response["current_hash"] = result.content_hash
        response["current_content"] = None
        response["proposed_content"] = None
        response["hunks"] = [hunk.to_dict() for hunk in result.remaining_hunks]
        response["wrote"] = result.wrote
        return response

    return _audit_call(
        conn,
        tool="apply_workspace_change",
        capability="change_proposal",
        effect="host_commit",
        arguments={"change_id": change_id, "accepted_hunks": payload.accepted_hunks},
        class_id=class_id,
        session_id=session_id,
        target_kind="workspace_change",
        target_id=str(change_id),
        operation=operation,
    )


@router.post("/classes/{class_id}/sessions/{session_id}/workspace/changes/{change_id}/reject")
def reject_workspace_change(
    class_id: int,
    session_id: int,
    change_id: int,
    payload: ChangeRejectRequest,
    conn: DbConn,
) -> dict[str, object]:
    def operation() -> dict[str, object]:
        workspace, row, review = _change_context(conn, class_id, session_id, change_id)
        rejected = _selection_payload(payload.rejected_hunks)
        if rejected:
            available = {hunk.index: hunk.hash for hunk in review.hunks}
            if any(available.get(int(item["index"])) != item["hash"] for item in rejected):
                raise ConflictError(
                    "That hunk changed since it was fetched. Re-fetch the proposal."
                )
        return agent_store.transition_workspace_change(
            conn,
            change_id,
            class_id=class_id,
            workspace_id=int(workspace["id"]),
            session_id=session_id,
            state=agent_store.WORKSPACE_CHANGE_REJECTED,
            accepted_hunks=list(row["accepted_hunks"]),  # type: ignore[arg-type]
            rejected_hunks=[int(item["index"]) for item in rejected],
            state_reason="user_rejected",
        )

    return _audit_call(
        conn,
        tool="reject_workspace_change",
        capability="change_proposal",
        effect="database_write",
        arguments={"change_id": change_id, "rejected_hunks": payload.rejected_hunks},
        class_id=class_id,
        session_id=session_id,
        target_kind="workspace_change",
        target_id=str(change_id),
        operation=operation,
    )


@router.post(
    "/classes/{class_id}/sessions/{session_id}/workspace/commands",
    status_code=status.HTTP_201_CREATED,
)
def create_command_request(
    class_id: int, session_id: int, payload: CommandCreate, conn: DbConn
) -> dict[str, object]:
    def operation() -> dict[str, object]:
        _require_session(conn, class_id, session_id)
        workspace = _workspace(conn, class_id, command=True)
        exact_argv = list(commands.validate_argv(payload.argv))
        commands.validate_command_cwd(Path(str(workspace["root_path"])), payload.relative_cwd)
        row = agent_store.create_command_request(
            conn,
            class_id,
            workspace_id=int(workspace["id"]),
            session_id=session_id,
            argv=exact_argv,
            relative_cwd=payload.relative_cwd,
            reason=payload.reason,
            expected_signal=payload.expected_signal,
            timeout_seconds=payload.timeout_seconds,
        )
        return _command_json(row)

    return _audit_call(
        conn,
        tool="propose_verification_command",
        capability="command_proposal",
        effect="database_proposal",
        arguments={
            "argv_count": len(payload.argv),
            "argv_sha256": hashlib.sha256(
                confirmations.canonical_payload(payload.argv).encode()
            ).hexdigest(),
            "relative_cwd": payload.relative_cwd,
            "reason": payload.reason,
            "expected_signal": payload.expected_signal,
            "timeout_seconds": payload.timeout_seconds,
        },
        class_id=class_id,
        session_id=session_id,
        target_kind="workspace_command",
        operation=operation,
    )


@router.get("/classes/{class_id}/sessions/{session_id}/workspace/commands")
def list_command_requests(
    class_id: int,
    session_id: int,
    conn: DbConn,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[dict[str, object]]:
    _require_session(conn, class_id, session_id)
    workspace = _workspace(conn, class_id)
    return [
        _command_json(row)
        for row in agent_store.list_command_requests(
            conn,
            class_id=class_id,
            workspace_id=int(workspace["id"]),
            session_id=session_id,
            limit=limit,
        )
    ]


@router.get("/classes/{class_id}/sessions/{session_id}/workspace/commands/{request_id}")
def get_command_request(
    class_id: int, session_id: int, request_id: int, conn: DbConn
) -> dict[str, object]:
    _workspace, row = _command_context(conn, class_id, session_id, request_id, require_grant=False)
    return _command_json(row)


@router.post(
    "/classes/{class_id}/sessions/{session_id}/workspace/commands/{request_id}/confirmation"
)
def confirm_command_request(
    class_id: int,
    session_id: int,
    request_id: int,
    conn: DbConn,
    origin: Origin = None,
) -> dict[str, object]:
    def operation() -> dict[str, object]:
        request_origin = _require_origin(origin)
        workspace, row = _command_context(conn, class_id, session_id, request_id)
        if row["state"] != agent_store.COMMAND_PENDING:
            raise ConflictError("That command is no longer waiting for confirmation.")
        commands.validate_argv(row["argv"])  # type: ignore[arg-type]
        commands.validate_command_cwd(Path(str(workspace["root_path"])), str(row["relative_cwd"]))
        exact = _command_payload(row)
        command_hash = confirmations.payload_hash(exact)
        issued = confirmations.issue_confirmation(
            conn,
            origin=request_origin,
            class_id=class_id,
            session_id=session_id,
            action_kind="execute_command",
            target_id=str(request_id),
            current_hash=command_hash,
            payload=exact,
        )
        return {"token": issued.token, "expires_at": issued.expires_at, "payload": exact}

    return _audit_call(
        conn,
        tool="confirm_verification_command",
        capability="command_proposal",
        effect="confirmation_issue",
        arguments={"request_id": request_id},
        class_id=class_id,
        session_id=session_id,
        target_kind="command_request",
        target_id=str(request_id),
        operation=operation,
    )


@router.post("/classes/{class_id}/sessions/{session_id}/workspace/commands/{request_id}/execute")
def execute_command_request(
    class_id: int,
    session_id: int,
    request_id: int,
    payload: CommandExecuteRequest,
    conn: DbConn,
    origin: Origin = None,
) -> dict[str, object]:
    event = tool_audit.start_event(
        conn,
        caller_kind="user_api",
        tool="execute_verification_command",
        capability="command_proposal",
        effect="process_execute",
        arguments={"request_id": request_id},
        policy_decision="allowed",
        class_id=class_id,
        session_id=session_id,
        target_kind="command_request",
        target_id=str(request_id),
    )
    try:
        request_origin = _require_origin(origin)
        workspace, row = _command_context(conn, class_id, session_id, request_id)
        if row["state"] != agent_store.COMMAND_PENDING:
            raise ConflictError("That command is no longer waiting for confirmation.")
        exact = _command_payload(row)
        command_hash = confirmations.payload_hash(exact)
        confirmations.consume_confirmation(
            conn,
            token=payload.confirmation_token,
            origin=request_origin,
            class_id=class_id,
            session_id=session_id,
            action_kind="execute_command",
            target_id=str(request_id),
            current_hash=command_hash,
            payload=exact,
        )
        agent_store.transition_command_request(
            conn,
            request_id,
            class_id=class_id,
            workspace_id=int(workspace["id"]),
            session_id=session_id,
            state=agent_store.COMMAND_RUNNING,
        )
        result = commands.run_command(
            Path(str(workspace["root_path"])),
            row["argv"],  # type: ignore[arg-type]
            relative_cwd=str(row["relative_cwd"]),
            timeout_seconds=float(row["timeout_seconds"]),
        )
        stored = agent_store.transition_command_request(
            conn,
            request_id,
            class_id=class_id,
            workspace_id=int(workspace["id"]),
            session_id=session_id,
            state=result.state,
            exit_code=result.exit_code,
            stdout_text=result.stdout,
            stderr_text=result.stderr,
            state_reason="output_truncated" if result.truncated else None,
        )
    except ValueError as exc:
        tool_audit.finish_event(conn, event.id, state="refused", error_message=str(exc))
        raise _as_domain_error(exc) from exc
    except LyraError as exc:
        tool_audit.finish_event(conn, event.id, state="refused", error_message=exc.message)
        raise
    except Exception as exc:
        tool_audit.finish_event(conn, event.id, state="failed", error_message=str(exc))
        raise
    audit_state = (
        "timed_out"
        if result.state == "timed_out"
        else ("succeeded" if result.state == "completed" else "failed")
    )
    tool_audit.finish_event(
        conn,
        event.id,
        state=audit_state,
        result_summary={
            "request_id": request_id,
            "state": result.state,
            "exit_code": result.exit_code,
            "truncated": result.truncated,
            "duration_seconds": result.duration_seconds,
        },
    )
    return _command_json(stored)


@router.post("/classes/{class_id}/sessions/{session_id}/workspace/commands/{request_id}/reject")
def reject_command_request(
    class_id: int, session_id: int, request_id: int, conn: DbConn
) -> dict[str, object]:
    def operation() -> dict[str, object]:
        workspace, _row = _command_context(conn, class_id, session_id, request_id)
        return agent_store.transition_command_request(
            conn,
            request_id,
            class_id=class_id,
            workspace_id=int(workspace["id"]),
            session_id=session_id,
            state=agent_store.COMMAND_REJECTED,
            state_reason="user_rejected",
        )

    return _audit_call(
        conn,
        tool="reject_verification_command",
        capability="command_proposal",
        effect="database_write",
        arguments={"request_id": request_id},
        class_id=class_id,
        session_id=session_id,
        target_kind="command_request",
        target_id=str(request_id),
        operation=operation,
    )
