"""Integration contracts for the Phase 4 workspace and confirmation router."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_agent
from backend.core import agent_store, confirmations, sessions, tool_audit, workspace_changes
from backend.core.errors import LyraError
from backend.storage.database import connect, get_db

ORIGIN = "http://localhost:3000"


def _request_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def agent_tables(db: sqlite3.Connection) -> None:
    db.executescript(agent_store.TABLE_SQL)
    db.executescript(confirmations.TABLE_SQL)
    db.executescript(tool_audit.TABLE_SQL)


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(routes_agent.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session_id(db: sqlite3.Connection, class_id: int) -> int:
    return int(sessions.create_session(db, class_id)["id"])


def _attach(client: TestClient, class_id: int, root: Path) -> dict[str, object]:
    response = client.put(
        f"/api/classes/{class_id}/workspace",
        json={"root_path": str(root), "display_name": "Repository"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _grants(
    client: TestClient,
    class_id: int,
    *,
    read: bool = False,
    changes: bool = False,
    commands_enabled: bool = False,
) -> dict[str, object]:
    response = client.patch(
        f"/api/classes/{class_id}/workspace/grants",
        json={
            "read_enabled": read,
            "change_proposals_enabled": changes,
            "commands_enabled": commands_enabled,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_unattached_workspace_is_a_normal_null_state(client: TestClient, class_id: int) -> None:
    response = client.get(f"/api/classes/{class_id}/workspace")

    assert response.status_code == 200
    assert response.json() is None


def test_attachment_defaults_off_and_read_is_class_session_scoped(
    client: TestClient,
    db: sqlite3.Connection,
    class_id: int,
    session_id: int,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "notes.txt").write_text("line one\nline two\n", encoding="utf-8")

    attached = _attach(client, class_id, root)
    assert attached["read_enabled"] is False
    assert attached["change_proposals_enabled"] is False
    assert attached["commands_enabled"] is False

    read_url = f"/api/classes/{class_id}/sessions/{session_id}/workspace/read"
    refused = client.get(read_url, params={"path": "notes.txt"})
    assert refused.status_code == 409

    _grants(client, class_id, read=True)
    read = client.get(read_url, params={"path": "notes.txt"})
    assert read.status_code == 200
    assert read.json()["content"] == "line one\nline two\n"

    other_class = int(db.execute("insert into classes (name) values ('Other')").lastrowid or 0)
    db.commit()
    assert (
        client.get(
            f"/api/classes/{other_class}/sessions/{session_id}/workspace/read",
            params={"path": "notes.txt"},
        ).status_code
        == 404
    )

    events = db.execute(
        "select tool, state from tool_audit_events order by started_at, rowid"
    ).fetchall()
    assert ("read_workspace_file", "refused") in {
        (str(row["tool"]), str(row["state"])) for row in events
    }
    assert ("read_workspace_file", "succeeded") in {
        (str(row["tool"]), str(row["state"])) for row in events
    }
    activity = client.get(f"/api/classes/{class_id}/sessions/{session_id}/agent/activity")
    assert activity.status_code == 200
    assert [event["tool"] for event in activity.json()][-2:] == [
        "read_workspace_file",
        "read_workspace_file",
    ]


def test_workspace_change_requires_exact_single_use_confirmation(
    client: TestClient,
    class_id: int,
    session_id: int,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "answer.txt"
    target.write_text("before\n", encoding="utf-8")
    _attach(client, class_id, root)
    _grants(client, class_id, read=True, changes=True)

    observed = workspace_changes.sha256_text("before\n")
    created = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/workspace/changes",
        json={
            "relative_path": "answer.txt",
            "observed_base_hash": observed,
            "proposed_content": "after\n",
            "rationale": "Correct the answer",
        },
    )
    assert created.status_code == 201, created.text
    change = created.json()
    selection = [{"index": change["hunks"][0]["index"], "hash": change["hunks"][0]["hash"]}]
    confirmation_url = (
        f"/api/classes/{class_id}/sessions/{session_id}/workspace/changes/"
        f"{change['id']}/confirmation"
    )
    untrusted = client.post(
        confirmation_url,
        headers={"Origin": "https://attacker.example"},
        json={"accepted_hunks": selection},
    )
    assert untrusted.status_code == 400
    issued = client.post(
        confirmation_url,
        headers={"Origin": ORIGIN},
        json={"accepted_hunks": selection},
    )
    assert issued.status_code == 200, issued.text
    token = issued.json()["token"]
    apply_url = confirmation_url.removesuffix("/confirmation") + "/apply"

    wrong_origin = client.post(
        apply_url,
        headers={"Origin": "http://localhost:9999"},
        json={"accepted_hunks": selection, "confirmation_token": token},
    )
    assert wrong_origin.status_code == 400
    assert target.read_text(encoding="utf-8") == "before\n"

    applied = client.post(
        apply_url,
        headers={"Origin": ORIGIN},
        json={"accepted_hunks": selection, "confirmation_token": token},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["state"] == "applied"
    assert applied.json()["wrote"] is True
    assert target.read_text(encoding="utf-8") == "after\n"

    replay = client.post(
        apply_url,
        headers={"Origin": ORIGIN},
        json={"accepted_hunks": selection, "confirmation_token": token},
    )
    assert replay.status_code in {409, 422}


def test_confirmation_is_bound_to_exact_hunks(
    client: TestClient,
    class_id: int,
    session_id: int,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "multi.txt"
    base = "one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\n"
    target.write_text(base, encoding="utf-8")
    _attach(client, class_id, root)
    _grants(client, class_id, read=True, changes=True)
    created = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/workspace/changes",
        json={
            "relative_path": "multi.txt",
            "observed_base_hash": workspace_changes.sha256_text(base),
            "proposed_content": "ONE\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nTEN\n",
        },
    ).json()
    first = [{"index": created["hunks"][0]["index"], "hash": created["hunks"][0]["hash"]}]
    all_hunks = [{"index": hunk["index"], "hash": hunk["hash"]} for hunk in created["hunks"]]
    confirmation_url = (
        f"/api/classes/{class_id}/sessions/{session_id}/workspace/changes/"
        f"{created['id']}/confirmation"
    )
    token = client.post(
        confirmation_url,
        headers={"Origin": ORIGIN},
        json={"accepted_hunks": first},
    ).json()["token"]

    mismatch = client.post(
        confirmation_url.removesuffix("/confirmation") + "/apply",
        headers={"Origin": ORIGIN},
        json={"accepted_hunks": all_hunks, "confirmation_token": token},
    )
    assert mismatch.status_code == 409
    assert target.read_text(encoding="utf-8") == base


def test_workspace_hunks_can_be_applied_in_separate_confirmed_steps(
    client: TestClient,
    class_id: int,
    session_id: int,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "multi.txt"
    base = "one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\n"
    target.write_text(base, encoding="utf-8")
    _attach(client, class_id, root)
    _grants(client, class_id, read=True, changes=True)
    created = client.post(
        f"/api/classes/{class_id}/sessions/{session_id}/workspace/changes",
        json={
            "relative_path": "multi.txt",
            "observed_base_hash": workspace_changes.sha256_text(base),
            "proposed_content": "ONE\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nTEN\n",
        },
    ).json()
    assert len(created["hunks"]) == 2
    change_url = f"/api/classes/{class_id}/sessions/{session_id}/workspace/changes/{created['id']}"

    first = [{"index": created["hunks"][0]["index"], "hash": created["hunks"][0]["hash"]}]
    token = client.post(
        f"{change_url}/confirmation",
        headers={"Origin": ORIGIN},
        json={"accepted_hunks": first},
    ).json()["token"]
    partial = client.post(
        f"{change_url}/apply",
        headers={"Origin": ORIGIN},
        json={"accepted_hunks": first, "confirmation_token": token},
    )
    assert partial.status_code == 200, partial.text
    partial_body = partial.json()
    assert partial_body["state"] == "partially_applied"
    assert len(partial_body["hunks"]) == 1
    assert target.read_text(encoding="utf-8").startswith("ONE\n")

    remaining = [
        {
            "index": partial_body["hunks"][0]["index"],
            "hash": partial_body["hunks"][0]["hash"],
        }
    ]
    second_token = client.post(
        f"{change_url}/confirmation",
        headers={"Origin": ORIGIN},
        json={"accepted_hunks": remaining},
    ).json()["token"]
    applied = client.post(
        f"{change_url}/apply",
        headers={"Origin": ORIGIN},
        json={"accepted_hunks": remaining, "confirmation_token": second_token},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["state"] == "applied"
    assert target.read_text(encoding="utf-8").endswith("TEN\n")


def test_command_does_not_run_until_confirmed_and_metacharacters_are_literal(
    client: TestClient,
    class_id: int,
    session_id: int,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _attach(client, class_id, root)
    _grants(client, class_id, commands_enabled=True)
    marker = root / "must-not-exist"
    literal = f"hello;touch {marker.name}"
    command_url = f"/api/classes/{class_id}/sessions/{session_id}/workspace/commands"
    proposed = client.post(
        command_url,
        json={
            "argv": ["/bin/echo", literal],
            "relative_cwd": ".",
            "reason": "Verify exact argv",
            "timeout_seconds": 10,
        },
    )
    assert proposed.status_code == 201, proposed.text
    request = proposed.json()
    assert request["state"] == "pending"
    assert marker.exists() is False

    request_url = f"{command_url}/{request['id']}"
    issued = client.post(f"{request_url}/confirmation", headers={"Origin": ORIGIN})
    assert issued.status_code == 200, issued.text
    assert marker.exists() is False

    executed = client.post(
        f"{request_url}/execute",
        headers={"Origin": ORIGIN},
        json={"confirmation_token": issued.json()["token"]},
    )
    assert executed.status_code == 200, executed.text
    body = executed.json()
    assert body["state"] == "completed"
    assert body["exit_code"] == 0
    assert body["stdout_text"] == literal + "\n"
    assert marker.exists() is False

    _grants(client, class_id, commands_enabled=False)
    persisted = client.get(request_url)
    assert persisted.status_code == 200
    assert persisted.json()["stdout_text"] == literal + "\n"
    assert persisted.json()["truncated"] is False

    replay = client.post(
        f"{request_url}/execute",
        headers={"Origin": ORIGIN},
        json={"confirmation_token": issued.json()["token"]},
    )
    assert replay.status_code == 409


def test_command_confirmation_rechecks_revoked_grant(
    client: TestClient,
    class_id: int,
    session_id: int,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _attach(client, class_id, root)
    _grants(client, class_id, commands_enabled=True)
    command_url = f"/api/classes/{class_id}/sessions/{session_id}/workspace/commands"
    request = client.post(
        command_url,
        json={"argv": ["/bin/echo", "safe"], "reason": "Verify"},
    ).json()

    _grants(client, class_id, commands_enabled=False)
    response = client.post(
        f"{command_url}/{request['id']}/confirmation", headers={"Origin": ORIGIN}
    )
    assert response.status_code == 409


def test_workspace_root_replacement_invalidates_fingerprint(
    client: TestClient,
    class_id: int,
    session_id: int,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "file.txt").write_text("safe", encoding="utf-8")
    _attach(client, class_id, root)
    _grants(client, class_id, read=True)

    moved = tmp_path / "old-repo"
    root.rename(moved)
    root.mkdir()
    (root / "file.txt").write_text("replacement", encoding="utf-8")
    response = client.get(
        f"/api/classes/{class_id}/sessions/{session_id}/workspace/read",
        params={"path": "file.txt"},
    )
    assert response.status_code == 409
    assert "Attach it again" in response.json()["detail"]
