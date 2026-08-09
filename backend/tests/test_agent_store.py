"""Tests for isolated Phase 4 workspace attachment and request persistence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.core import agent_store, classes, sessions
from backend.core.errors import ConflictError, NotFoundError


@pytest.fixture(autouse=True)
def agent_tables(db: sqlite3.Connection) -> None:
    db.executescript(agent_store.TABLE_SQL)


def _class(db: sqlite3.Connection, name: str) -> int:
    return int(classes.create_class(db, name)["id"])


def _attach(db: sqlite3.Connection, class_id: int, root: Path) -> dict[str, object]:
    return agent_store.attach_workspace(db, class_id, root_path=str(root), display_name="Repo")


def _session(db: sqlite3.Connection, class_id: int) -> int:
    return int(sessions.create_session(db, class_id)["id"])


def test_attach_workspace_stores_canonical_root_fingerprint_and_defaults(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    attached = _attach(db, class_id, root)

    assert attached["class_id"] == class_id
    assert attached["root_path"] == str(root.resolve())
    assert attached["display_name"] == "Repo"
    assert attached["root_device"] >= 0
    assert attached["root_inode"] >= 0
    assert attached["read_enabled"] is False
    assert attached["change_proposals_enabled"] is False
    assert attached["commands_enabled"] is False


def test_change_proposals_require_read_and_revocation_invalidates_pending_rows(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = _attach(db, class_id, root)

    with pytest.raises(ValueError, match="require workspace read"):
        agent_store.update_workspace_grants(db, class_id, change_proposals_enabled=True)

    workspace = agent_store.update_workspace_grants(
        db,
        class_id,
        read_enabled=True,
        change_proposals_enabled=True,
        commands_enabled=True,
    )
    session_id = _session(db, class_id)
    change = agent_store.create_workspace_change(
        db,
        class_id,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
        relative_path="src/main.py",
        base_hash="base-hash",
        base_content="before",
        proposed_content="after",
        file_device=1,
        file_inode=2,
        file_mode=0o644,
        newline="\n",
        rationale="Tighten the check",
    )
    command = agent_store.create_command_request(
        db,
        class_id,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
        argv=["pytest", "-q"],
        relative_cwd=".",
        reason="Verify the patch",
        expected_signal="tests_pass",
    )

    updated = agent_store.update_workspace_grants(db, class_id, read_enabled=False)

    assert updated["read_enabled"] is False
    assert updated["change_proposals_enabled"] is False
    assert updated["commands_enabled"] is True
    assert (
        agent_store.get_workspace_change(
            db,
            int(change["id"]),
            class_id=class_id,
            workspace_id=int(workspace["id"]),
            session_id=session_id,
        )["state"]
        == agent_store.WORKSPACE_CHANGE_REJECTED
    )
    assert (
        agent_store.get_command_request(
            db,
            int(command["id"]),
            class_id=class_id,
            workspace_id=int(workspace["id"]),
            session_id=session_id,
        )["state"]
        == agent_store.COMMAND_PENDING
    )


def test_commands_revocation_rejects_pending_commands_only(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = _attach(db, class_id, root)
    workspace = agent_store.update_workspace_grants(db, class_id, commands_enabled=True)
    session_id = _session(db, class_id)
    request = agent_store.create_command_request(
        db,
        class_id,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
        argv=["ruff", "check"],
        relative_cwd=".",
        reason="Lint changed files",
        expected_signal="lint_clean",
    )

    updated = agent_store.update_workspace_grants(db, class_id, commands_enabled=False)

    assert updated["commands_enabled"] is False
    stored = agent_store.get_command_request(
        db,
        int(request["id"]),
        class_id=class_id,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
    )
    assert stored["state"] == agent_store.COMMAND_REJECTED
    assert stored["state_reason"] == "commands_grant_revoked"
    assert stored["finished_at"] is not None


def test_detach_removes_workspace_and_class_delete_cascades_children(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    first_class = _class(db, "Physics")
    second_class = _class(db, "Chemistry")
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = _attach(db, first_class, first_root)
    second = _attach(db, second_class, second_root)
    first = agent_store.update_workspace_grants(
        db, first_class, read_enabled=True, change_proposals_enabled=True, commands_enabled=True
    )
    session_id = _session(db, first_class)
    change = agent_store.create_workspace_change(
        db,
        first_class,
        workspace_id=int(first["id"]),
        session_id=session_id,
        relative_path="draft.md",
        base_hash="h1",
        base_content="old",
        proposed_content="new",
        file_device=1,
        file_inode=2,
        file_mode=0o644,
        newline="\n",
        rationale=None,
    )
    command = agent_store.create_command_request(
        db,
        first_class,
        workspace_id=int(first["id"]),
        session_id=session_id,
        argv=["pytest"],
        relative_cwd=".",
        reason="Run tests",
        expected_signal=None,
    )

    agent_store.detach_workspace(db, first_class)

    assert agent_store.get_workspace_for_class(db, first_class) is None
    with pytest.raises(NotFoundError):
        agent_store.get_workspace_change(
            db,
            int(change["id"]),
            class_id=first_class,
            workspace_id=int(first["id"]),
            session_id=session_id,
        )
    with pytest.raises(NotFoundError):
        agent_store.get_command_request(
            db,
            int(command["id"]),
            class_id=first_class,
            workspace_id=int(first["id"]),
            session_id=session_id,
        )

    classes.delete_class(db, second_class)
    assert db.execute("select count(*) from class_workspaces").fetchone()[0] == 0
    assert db.execute("select count(*) from workspace_changes").fetchone()[0] == 0
    assert db.execute("select count(*) from command_requests").fetchone()[0] == 0
    assert second["id"] != first["id"]


def test_invalid_state_transitions_and_class_session_isolation(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    class_a = _class(db, "Algebra")
    class_b = _class(db, "Analysis")
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = _attach(db, class_a, root)
    workspace = agent_store.update_workspace_grants(
        db, class_a, read_enabled=True, change_proposals_enabled=True, commands_enabled=True
    )
    session_id = _session(db, class_a)
    change = agent_store.create_workspace_change(
        db,
        class_a,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
        relative_path="src/file.py",
        base_hash="before",
        base_content="before",
        proposed_content="after",
        file_device=1,
        file_inode=2,
        file_mode=0o644,
        newline="\n",
        rationale="Edit",
    )
    command = agent_store.create_command_request(
        db,
        class_a,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
        argv=["python", "-m", "pytest"],
        relative_cwd=".",
        reason="Verify",
        expected_signal="tests_pass",
        timeout_seconds=600,
    )

    with pytest.raises(NotFoundError):
        agent_store.get_workspace_change(
            db,
            int(change["id"]),
            class_id=class_b,
            workspace_id=int(workspace["id"]),
            session_id=session_id,
        )
    with pytest.raises(NotFoundError):
        agent_store.transition_workspace_change(
            db,
            int(change["id"]),
            class_id=class_a,
            workspace_id=int(workspace["id"]),
            session_id=session_id + 100,
            state=agent_store.WORKSPACE_CHANGE_APPLIED,
        )
    with pytest.raises(ConflictError, match="cannot move"):
        agent_store.transition_command_request(
            db,
            int(command["id"]),
            class_id=class_a,
            workspace_id=int(workspace["id"]),
            session_id=session_id,
            state=agent_store.COMMAND_COMPLETED,
        )

    running = agent_store.transition_command_request(
        db,
        int(command["id"]),
        class_id=class_a,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
        state=agent_store.COMMAND_RUNNING,
    )
    assert running["confirmed_at"] is not None
    assert running["started_at"] is not None

    completed = agent_store.transition_command_request(
        db,
        int(command["id"]),
        class_id=class_a,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
        state=agent_store.COMMAND_COMPLETED,
        exit_code=0,
        stdout_text="x" * (agent_store.MAX_OUTPUT_BYTES + 50),
    )
    assert completed["state"] == agent_store.COMMAND_COMPLETED
    assert len(completed["stdout_text"].encode("utf-8")) == agent_store.MAX_OUTPUT_BYTES

    with pytest.raises(ConflictError, match="cannot move"):
        agent_store.transition_command_request(
            db,
            int(command["id"]),
            class_id=class_a,
            workspace_id=int(workspace["id"]),
            session_id=session_id,
            state=agent_store.COMMAND_RUNNING,
        )


def test_partial_change_can_refresh_its_snapshot_for_the_next_review(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = _attach(db, class_id, root)
    workspace = agent_store.update_workspace_grants(
        db, class_id, read_enabled=True, change_proposals_enabled=True
    )
    session_id = _session(db, class_id)
    change = agent_store.create_workspace_change(
        db,
        class_id,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
        relative_path="main.py",
        base_hash="before",
        base_content="one\ntwo\n",
        proposed_content="ONE\nTWO\n",
        file_device=1,
        file_inode=2,
        file_mode=0o644,
        newline="\n",
        rationale="Two independent edits",
    )

    partial = agent_store.transition_workspace_change(
        db,
        int(change["id"]),
        class_id=class_id,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
        state=agent_store.WORKSPACE_CHANGE_PARTIALLY_APPLIED,
        accepted_hunks=[0],
        rejected_hunks=[],
        after_hash="mid",
        base_hash="mid",
        base_content="ONE\ntwo\n",
        proposed_content="ONE\nTWO\n",
        file_device=1,
        file_inode=3,
        file_mode=0o644,
        newline="\n",
    )

    assert partial["state"] == agent_store.WORKSPACE_CHANGE_PARTIALLY_APPLIED
    assert partial["base_hash"] == "mid"
    assert partial["base_content"] == "ONE\ntwo\n"
    assert partial["file_inode"] == 3


def test_only_one_command_runs_while_multiple_proposals_can_wait(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = _attach(db, class_id, root)
    workspace = agent_store.update_workspace_grants(db, class_id, commands_enabled=True)
    session_id = _session(db, class_id)
    request = agent_store.create_command_request(
        db,
        class_id,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
        argv=["python", "-V"],
        relative_cwd=".",
        reason="Check Python",
        expected_signal=None,
    )
    second = agent_store.create_command_request(
        db,
        class_id,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
        argv=["python", "-m", "pytest"],
        relative_cwd=".",
        reason="Run tests",
        expected_signal=None,
    )

    agent_store.transition_command_request(
        db,
        int(request["id"]),
        class_id=class_id,
        workspace_id=int(workspace["id"]),
        session_id=session_id,
        state=agent_store.COMMAND_RUNNING,
    )
    with pytest.raises(ConflictError, match="active command"):
        agent_store.transition_command_request(
            db,
            int(second["id"]),
            class_id=class_id,
            workspace_id=int(workspace["id"]),
            session_id=session_id,
            state=agent_store.COMMAND_RUNNING,
        )
    assert agent_store.reconcile_running_commands(db) == 1
    assert agent_store.get_command_request(db, int(request["id"]))["state"] == "abandoned"
    # A restart abandons what was running; a command still waiting for approval was not
    # working when the process stopped, so the reconcile leaves it pending to run later.
    assert (
        agent_store.get_command_request(db, int(second["id"]))["state"]
        == agent_store.COMMAND_PENDING
    )
