"""Tests for Phase 4's contextual, proposal-only class-agent registries."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from backend.core import (
    agent_store,
    agent_tools,
    classes,
    profiles,
    sessions,
    source_ledger,
    tool_audit,
    web_research,
)
from backend.llm.tools import REGISTRY as COMPUTE_REGISTRY


@pytest.fixture(autouse=True)
def phase4_tables(db: sqlite3.Connection) -> None:
    db.executescript(agent_store.TABLE_SQL)
    db.executescript(tool_audit.TABLE_SQL)


def _session(db: sqlite3.Connection, class_id: int) -> int:
    return int(sessions.create_session(db, class_id)["id"])


def _workspace(
    db: sqlite3.Connection,
    class_id: int,
    root: Path,
    *,
    read: bool = False,
    changes: bool = False,
    commands: bool = False,
) -> dict[str, object]:
    root.mkdir(exist_ok=True)
    agent_store.attach_workspace(db, class_id, root_path=str(root))
    return agent_store.update_workspace_grants(
        db,
        class_id,
        read_enabled=read,
        change_proposals_enabled=changes,
        commands_enabled=commands,
    )


def _enable_web(db: sqlite3.Connection, *, scrape: bool = True) -> None:
    db.execute(
        "update settings set allow_web_research = 1, firecrawl_scrape_enabled = ? where id = 1",
        (int(scrape),),
    )
    db.commit()


def _events(db: sqlite3.Connection, tool: str) -> list[sqlite3.Row]:
    return list(
        db.execute(
            "select * from tool_audit_events where tool = ? order by started_at, id", (tool,)
        )
    )


def test_capabilities_are_default_off_and_profiles_are_strictly_separated(
    db: sqlite3.Connection, class_id: int
) -> None:
    session_id = _session(db, class_id)

    research, _ = agent_tools.build_agent_registry(db, class_id, session_id, "research")
    code, _ = agent_tools.build_agent_registry(db, class_id, session_id, "code")
    command, _ = agent_tools.build_agent_registry(db, class_id, session_id, "command")

    assert set(research) == set(COMPUTE_REGISTRY)
    assert set(code) == set(COMPUTE_REGISTRY)
    assert set(command) == set(COMPUTE_REGISTRY)


def test_profile_enumeration_never_combines_web_workspace_or_command_tools(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    session_id = _session(db, class_id)
    _enable_web(db)
    _workspace(
        db,
        class_id,
        tmp_path / "repo",
        read=True,
        changes=True,
        commands=True,
    )

    research, _ = agent_tools.build_agent_registry(db, class_id, session_id, "research")
    code, _ = agent_tools.build_agent_registry(db, class_id, session_id, "code")
    command, _ = agent_tools.build_agent_registry(db, class_id, session_id, "command")

    assert {"search_web", "fetch_source", "propose_source_snapshot"} <= set(research)
    assert not {"list_workspace", "create_workspace_change", "create_command_request"} & set(
        research
    )
    assert {"list_workspace", "search_workspace", "read_workspace_file"} <= set(code)
    assert "create_workspace_change" in code
    assert not {"search_web", "fetch_source", "create_command_request"} & set(code)
    assert set(command) == {*COMPUTE_REGISTRY, "create_command_request"}
    assert not any(
        "apply" in name.lower() or "execute" in name.lower() for name in research | code | command
    )


def test_web_query_guard_refuses_private_overlap_before_network_and_audits_metadata(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _session(db, class_id)
    _enable_web(db)
    called = False

    def unexpected_search(*args: object, **kwargs: object) -> list[dict[str, str]]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(web_research, "search_web", unexpected_search)
    private = "student accommodation details require private scheduling support"
    registry, activity = agent_tools.build_agent_registry(
        db, class_id, session_id, "research", private_context=(private,)
    )

    result = registry["search_web"].handler(query=private)

    assert result.ok is False
    assert called is False
    event = _events(db, "search_web")[0]
    assert event["state"] == "refused"
    assert private not in event["arguments_json"]
    assert json.loads(event["arguments_json"])["query_sha256"]
    assert activity.events[-1].state == "refused"


def test_research_fetch_is_run_local_until_snapshot_is_proposed(
    db: sqlite3.Connection,
    class_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _session(db, class_id)
    _enable_web(db)
    snapshot = "Evidence sentence. " * 700

    def fake_fetch(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "url": "https://example.com/method",
            "final_url": "https://example.com/method",
            "title": "A method",
            "accessed_at": "2026-08-07T12:00:00+00:00",
            "content_type": "text/markdown",
            "snapshot": snapshot,
            "truncated": False,
            "warning": None,
        }

    monkeypatch.setattr(web_research, "fetch_source", fake_fetch)
    registry, activity = agent_tools.build_agent_registry(db, class_id, session_id, "research")

    fetched = registry["fetch_source"].handler(url="https://example.com/method")
    assert fetched.ok is True
    assert len(str(fetched.value["preview"])) == agent_tools.FETCH_PREVIEW_CHARS
    assert "snapshot" not in fetched.value
    assert db.execute("select count(*) from writer_sources").fetchone()[0] == 0

    proposed = registry["propose_source_snapshot"].handler(fetch_id=str(fetched.value["fetch_id"]))
    assert proposed.ok is True
    source_id = int(proposed.value["source"]["id"])  # type: ignore[index]
    assert activity.source_ids == [source_id]
    stored = db.execute("select snapshot from writer_sources where id = ?", (source_id,)).fetchone()
    assert stored["snapshot"] == snapshot
    assert [event["state"] for event in _events(db, "fetch_source")] == ["succeeded"]
    assert [event["state"] for event in _events(db, "propose_source_snapshot")] == ["succeeded"]


def test_source_excerpt_and_profile_fact_proposals_are_class_scoped(
    db: sqlite3.Connection, class_id: int
) -> None:
    other_class = int(classes.create_class(db, "Other")["id"])
    session_id = _session(db, class_id)
    _enable_web(db)
    source = db.execute(
        "insert into writer_sources (class_id, source_type, url, title, accessed_at, snapshot) "
        "values (?, 'web', 'https://example.com/', 'Other source', '2026-08-07', 'Exact quote')",
        (other_class,),
    )
    db.commit()
    registry, _ = agent_tools.build_agent_registry(db, class_id, session_id, "research")

    result = registry["propose_source_excerpt"].handler(
        source_id=int(source.lastrowid or 0), excerpt="Exact quote"
    )

    assert result.ok is False
    assert _events(db, "propose_source_excerpt")[0]["state"] == "refused"
    assert "propose_profile_fact" in registry


def test_research_profile_fact_stays_inactive_until_user_confirmation(
    db: sqlite3.Connection, class_id: int
) -> None:
    session_id = _session(db, class_id)
    _enable_web(db)
    source = source_ledger.upsert_source(
        db,
        class_id,
        source_type="web",
        url="https://example.com/method",
        title="Method reference",
        snapshot="Use the bilinear transform for this laboratory method.",
        final_url="https://example.com/method",
    )
    registry, activity = agent_tools.build_agent_registry(db, class_id, session_id, "research")
    excerpt = registry["propose_source_excerpt"].handler(
        source_id=int(source["id"]),
        excerpt="Use the bilinear transform for this laboratory method.",
    )
    assert excerpt.ok is True

    proposed = registry["propose_profile_fact"].handler(
        kind="note",
        label="Required method",
        value="Use the bilinear transform",
        source_id=int(source["id"]),
        excerpt_id=int(excerpt.value["excerpt"]["id"]),  # type: ignore[index]
    )

    assert proposed.ok is True
    fact_id = int(proposed.value["fact_id"])
    fact = db.execute("select * from profile_facts where id = ?", (fact_id,)).fetchone()
    assert fact["confidence"] == "low"
    assert fact["confirmed"] == 0
    assert fact["source_writer_id"] == source["id"]
    assert activity.profile_fact_ids == [fact_id]
    assert not any(int(row["id"]) == fact_id for row in profiles.select_active_facts(db, class_id))


def test_workspace_reads_are_relative_bounded_and_durably_audited(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    _workspace(db, class_id, root, read=True)
    (root / "main.py").write_text("one\ntwo\nthree\n")
    session_id = _session(db, class_id)
    registry, activity = agent_tools.build_agent_registry(db, class_id, session_id, "code")

    result = registry["read_workspace_file"].handler(
        relative_path="main.py", start_line=2, end_line=2
    )

    assert result.ok is True
    assert result.value["path"] == "main.py"
    assert result.value["content"] == "two\n"
    event = _events(db, "read_workspace_file")[0]
    assert event["state"] == "succeeded"
    assert event["effect"] == "filesystem_read"
    assert activity.events[-1].target_id == "main.py"


def test_retained_workspace_handler_refuses_after_grant_revocation(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    _workspace(db, class_id, root, read=True)
    (root / "main.py").write_text("safe\n")
    session_id = _session(db, class_id)
    registry, _ = agent_tools.build_agent_registry(db, class_id, session_id, "code")

    agent_store.update_workspace_grants(db, class_id, read_enabled=False)
    result = registry["read_workspace_file"].handler(relative_path="main.py")

    assert result.ok is False
    event = _events(db, "read_workspace_file")[0]
    assert event["policy_decision"] == "refused"
    assert event["state"] == "refused"


def test_workspace_change_tool_creates_inert_proposal_without_writing_file(
    db: sqlite3.Connection, class_id: int, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    _workspace(db, class_id, root, read=True, changes=True)
    target = root / "main.py"
    target.write_text("before\n")
    session_id = _session(db, class_id)
    registry, activity = agent_tools.build_agent_registry(db, class_id, session_id, "code")
    observed = registry["read_workspace_file"].handler(relative_path="main.py")

    result = registry["create_workspace_change"].handler(
        relative_path="main.py",
        observed_base_hash=str(observed.value["sha256"]),
        proposed_content="after\n",
        rationale="Demonstrate the fix",
    )

    assert result.ok is True
    assert target.read_text() == "before\n"
    change_id = int(result.value["change_id"])
    assert activity.workspace_change_ids == [change_id]
    row = db.execute(
        "select state, proposed_content from workspace_changes where id = ?", (change_id,)
    ).fetchone()
    assert dict(row) == {"state": "pending", "proposed_content": "after\n"}
    assert _events(db, "create_workspace_change")[0]["state"] == "succeeded"


def test_command_tool_creates_pending_row_and_never_starts_a_process(
    db: sqlite3.Connection,
    class_id: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    _workspace(db, class_id, root, commands=True)
    session_id = _session(db, class_id)
    registry, activity = agent_tools.build_agent_registry(db, class_id, session_id, "command")

    def forbidden_process(*args: object, **kwargs: object) -> None:
        raise AssertionError("a command proposal must not start a process")

    monkeypatch.setattr("subprocess.Popen", forbidden_process)
    result = registry["create_command_request"].handler(
        argv=["pytest", "-q"],
        relative_cwd=".",
        reason="Verify the proposed change",
        expected_signal="tests pass",
        timeout_seconds=30,
    )

    assert result.ok is True
    request_id = int(result.value["request_id"])
    assert activity.command_request_ids == [request_id]
    row = db.execute(
        "select state, argv_json from command_requests where id = ?", (request_id,)
    ).fetchone()
    assert row["state"] == "pending"
    assert json.loads(row["argv_json"]) == ["pytest", "-q"]
    assert _events(db, "create_command_request")[0]["state"] == "succeeded"


def test_compute_handlers_are_also_durably_audited(db: sqlite3.Connection, class_id: int) -> None:
    session_id = _session(db, class_id)
    registry, activity = agent_tools.build_agent_registry(db, class_id, session_id, "code")

    result = registry["cas_evaluate"].handler(expression="2 + 2")

    assert result.ok is True
    event = _events(db, "cas_evaluate")[0]
    assert event["state"] == "succeeded"
    assert event["capability"] == "compute"
    assert event["effect"] == "pure"
    assert activity.events[-1].audit_id == event["id"]
