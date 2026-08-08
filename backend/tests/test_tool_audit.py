"""Tests for durable tool-audit rows."""

from __future__ import annotations

import json
import sqlite3

from backend.core import tool_audit


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(tool_audit.TABLE_SQL)
    return conn


def test_start_event_persists_redacted_and_bounded_arguments() -> None:
    conn = _conn()
    huge = "never-store-this-blob-" * 80
    started = tool_audit.start_event(
        conn,
        caller_kind="chat_session",
        caller_id="session-4",
        class_id=2,
        session_id=4,
        tool="web_search",
        capability="web",
        effect="read",
        arguments={
            "query": "green's theorem",
            "api_key": "secret-123",
            "content": huge,
            "nested": {"authorization": "Bearer token", "count": 3},
        },
        policy_decision="allowed",
        target_kind="source",
        target_id="source-7",
    )

    row = conn.execute(
        "select arguments_json from tool_audit_events where id = ?",
        (started.id,),
    ).fetchone()
    assert row is not None
    raw = str(row["arguments_json"])
    assert "secret-123" not in raw
    assert huge not in raw

    decoded = json.loads(raw)
    assert decoded["query"] == "green's theorem"
    assert decoded["api_key"]["__redacted__"] == "secret"
    assert decoded["content"]["__redacted__"] == "bulky_text"
    assert decoded["nested"]["authorization"]["__redacted__"] == "secret"


def test_finish_event_records_terminal_summary_without_full_output() -> None:
    conn = _conn()
    started = tool_audit.start_event(
        conn,
        caller_kind="chat_session",
        tool="workspace_read",
        capability="workspace_read",
        effect="read",
        arguments={"path": "notes.txt"},
        policy_decision="allowed",
    )
    huge = "console output " * 50

    finished = tool_audit.finish_event(
        conn,
        started.id,
        state="succeeded",
        result_summary={"stdout": huge, "exit_code": 0},
        error_message=None,
    )

    assert finished.state == "succeeded"
    stored = tool_audit.get_event(conn, started.id)
    assert stored["result_summary"]["stdout"]["__redacted__"] == "bulky_text"
    assert stored["result_summary"]["exit_code"] == 0
    assert huge not in json.dumps(stored)


def test_reconcile_inflight_marks_started_rows_abandoned() -> None:
    conn = _conn()
    a = tool_audit.start_event(
        conn,
        caller_kind="chat_session",
        tool="web_search",
        capability="web",
        effect="read",
        arguments={"query": "laplace transform"},
        policy_decision="allowed",
    )
    b = tool_audit.start_event(
        conn,
        caller_kind="chat_session",
        tool="workspace_search",
        capability="workspace_read",
        effect="read",
        arguments={"path": "src"},
        policy_decision="allowed",
    )
    tool_audit.finish_event(conn, b.id, state="refused", result_summary={"reason": "disabled"})

    changed = tool_audit.reconcile_inflight(conn, reason="restart")

    assert changed == 1
    first = tool_audit.get_event(conn, a.id)
    second = tool_audit.get_event(conn, b.id)
    assert first["state"] == tool_audit.ABANDONED
    assert first["abandonment_reason"] == "restart"
    assert first["finished_at"] is not None
    assert second["state"] == "refused"
    assert second["abandonment_reason"] is None


def test_cannot_finish_an_event_twice() -> None:
    conn = _conn()
    started = tool_audit.start_event(
        conn,
        caller_kind="chat_session",
        tool="web_fetch",
        capability="web",
        effect="read",
        arguments={"url": "https://example.test"},
        policy_decision="allowed",
    )
    tool_audit.finish_event(conn, started.id, state="failed", error_message="timed out")

    try:
        tool_audit.finish_event(conn, started.id, state="succeeded")
    except ValueError as exc:
        assert "already terminal" in str(exc)
    else:  # pragma: no cover - defensive guard
        raise AssertionError("finish_event should refuse a second terminal write")


def test_terminal_states_and_sensitive_error_messages_are_bounded() -> None:
    conn = _conn()
    started = tool_audit.start_event(
        conn,
        caller_kind="chat_session",
        tool="web_fetch",
        capability="web",
        effect="network_read",
        arguments={"url": "https://example.test"},
        policy_decision="allowed",
    )
    tool_audit.finish_event(
        conn,
        started.id,
        state="failed",
        error_message="request https://example.test/?token=private failed",
    )
    event = tool_audit.get_event(conn, started.id)
    assert "private" not in str(event["error_message"])
    assert "sensitive_message" in str(event["error_message"])

    second = tool_audit.start_event(
        conn,
        caller_kind="chat_session",
        tool="web_fetch",
        capability="web",
        effect="network_read",
        arguments={},
        policy_decision="allowed",
    )
    try:
        tool_audit.finish_event(conn, second.id, state="invented")
    except ValueError as exc:
        assert "known terminal" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown terminal audit state should be refused")
