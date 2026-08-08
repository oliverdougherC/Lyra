"""Tests for single-use confirmation nonces."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.core import confirmations


def _conn(path: Path | str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(confirmations.TABLE_SQL)
    return conn


def test_issue_stores_only_the_hash_and_default_expiry() -> None:
    conn = _conn()
    now = datetime(2026, 8, 7, 19, 0, tzinfo=UTC)

    issued = confirmations.issue_confirmation(
        conn,
        origin="http://127.0.0.1:3000",
        class_id=7,
        session_id=11,
        action_kind="execute_command",
        target_id="cmd-1",
        current_hash="abc123",
        payload={"argv": ["pytest", "-q"]},
        now=now,
    )

    assert len(issued.token) >= 64
    row = conn.execute("select * from confirmation_nonces where id = ?", (issued.id,)).fetchone()
    assert row is not None
    assert row["token_hash"] == confirmations.token_hash(issued.token)
    assert issued.token not in str(dict(row))
    assert row["expires_at"] == (now + timedelta(seconds=120)).isoformat(timespec="seconds")


def test_consume_requires_exact_bindings() -> None:
    conn = _conn()
    issued = confirmations.issue_confirmation(
        conn,
        origin="http://127.0.0.1:3000",
        class_id=3,
        session_id=5,
        action_kind="apply_change",
        target_id="change-1",
        current_hash="before",
        payload={"path": "notes.md", "accepted_hunks": [0, 2]},
    )

    with pytest.raises(confirmations.ConfirmationError, match="invalid"):
        confirmations.consume_confirmation(
            conn,
            token=issued.token,
            origin="http://127.0.0.1:3000",
            class_id=3,
            session_id=5,
            action_kind="apply_change",
            target_id="change-1",
            current_hash="after",
            payload={"path": "notes.md", "accepted_hunks": [0, 2]},
        )
    with pytest.raises(confirmations.ConfirmationError, match="invalid"):
        confirmations.consume_confirmation(
            conn,
            token=issued.token,
            origin="http://127.0.0.1:3000",
            class_id=3,
            session_id=5,
            action_kind="apply_change",
            target_id="change-1",
            current_hash="before",
            payload={"accepted_hunks": [2, 0], "path": "notes.md"},
        )


def test_consume_rejects_expired_tokens() -> None:
    conn = _conn()
    now = datetime(2026, 8, 7, 19, 0, tzinfo=UTC)
    issued = confirmations.issue_confirmation(
        conn,
        origin="http://127.0.0.1:3000",
        class_id=1,
        session_id=2,
        action_kind="execute_command",
        target_id="cmd-2",
        current_hash=None,
        payload={"argv": ["python", "-m", "pytest"]},
        now=now,
        ttl_seconds=120,
    )

    with pytest.raises(confirmations.ConfirmationExpiredError, match="expired"):
        confirmations.consume_confirmation(
            conn,
            token=issued.token,
            origin="http://127.0.0.1:3000",
            class_id=1,
            session_id=2,
            action_kind="execute_command",
            target_id="cmd-2",
            current_hash=None,
            payload={"argv": ["python", "-m", "pytest"]},
            now=now + timedelta(seconds=121),
        )

    with pytest.raises(confirmations.ConfirmationExpiredError, match="expired"):
        confirmations.consume_confirmation(
            conn,
            token=issued.token,
            origin="http://127.0.0.1:3000",
            class_id=1,
            session_id=2,
            action_kind="execute_command",
            target_id="cmd-2",
            current_hash=None,
            payload={"argv": ["python", "-m", "pytest"]},
            now=now + timedelta(seconds=120),
        )


def test_confirmation_contract_refuses_unbounded_or_unknown_tokens() -> None:
    conn = _conn()
    with pytest.raises(ValueError, match="between 1 and 120"):
        confirmations.issue_confirmation(
            conn,
            origin="http://127.0.0.1:3000",
            class_id=1,
            session_id=2,
            action_kind="execute_command",
            target_id="cmd-2",
            current_hash=None,
            payload={"argv": ["pytest"]},
            ttl_seconds=121,
        )
    with pytest.raises(ValueError, match="Unknown"):
        confirmations.issue_confirmation(
            conn,
            origin="http://127.0.0.1:3000",
            class_id=1,
            session_id=2,
            action_kind="model_side_effect",
            target_id="bad",
            current_hash=None,
            payload={},
        )
    invalid_token = chr(ord("f") + 1) * (confirmations.TOKEN_BYTES * 2)
    with pytest.raises(confirmations.ConfirmationError, match="invalid"):
        confirmations.consume_confirmation(
            conn,
            token=invalid_token,
            origin="http://127.0.0.1:3000",
            class_id=1,
            session_id=2,
            action_kind="execute_command",
            target_id="cmd-2",
            current_hash=None,
            payload={"argv": ["pytest"]},
        )


def test_consume_is_single_use() -> None:
    conn = _conn()
    issued = confirmations.issue_confirmation(
        conn,
        origin="http://127.0.0.1:3000",
        class_id=4,
        session_id=8,
        action_kind="execute_command",
        target_id="cmd-3",
        current_hash=None,
        payload={"argv": ["pytest"]},
    )

    first = confirmations.consume_confirmation(
        conn,
        token=issued.token,
        origin="http://127.0.0.1:3000",
        class_id=4,
        session_id=8,
        action_kind="execute_command",
        target_id="cmd-3",
        current_hash=None,
        payload={"argv": ["pytest"]},
    )

    assert first.id == issued.id
    with pytest.raises(confirmations.ConfirmationReplayError, match="already been used"):
        confirmations.consume_confirmation(
            conn,
            token=issued.token,
            origin="http://127.0.0.1:3000",
            class_id=4,
            session_id=8,
            action_kind="execute_command",
            target_id="cmd-3",
            current_hash=None,
            payload={"argv": ["pytest"]},
        )


def test_concurrent_consumers_only_allow_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "confirmations.db"
    bootstrap = _conn(path)
    issued = confirmations.issue_confirmation(
        bootstrap,
        origin="http://127.0.0.1:3000",
        class_id=9,
        session_id=12,
        action_kind="apply_change",
        target_id="change-2",
        current_hash="hash-1",
        payload={"path": "draft.md", "accepted_hunks": [1]},
    )
    bootstrap.close()

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker() -> None:
        conn = _conn(path)
        try:
            barrier.wait()
            confirmations.consume_confirmation(
                conn,
                token=issued.token,
                origin="http://127.0.0.1:3000",
                class_id=9,
                session_id=12,
                action_kind="apply_change",
                target_id="change-2",
                current_hash="hash-1",
                payload={"path": "draft.md", "accepted_hunks": [1]},
            )
        except confirmations.ConfirmationReplayError:
            results.append("replay")
        else:
            results.append("ok")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["ok", "replay"]
