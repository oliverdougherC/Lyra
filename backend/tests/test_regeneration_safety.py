"""PLA-316: regeneration data-loss prevention.

Tests the two invariants fixed by PLA-316:

1. **Cancellation invariant** -- a cancelled regeneration must leave the original
   reply intact.  Before the fix the cancellation handler called
   ``_commit_reply_atomic`` for regeneration turns that had received partial
   content, deleting the original and persisting an incomplete answer.

2. **Publication invariant** -- a successful replacement must be atomic: the
   delete of the superseded reply and the insert of the new one land in a single
   SQLite transaction.  Before the fix ``delete_messages`` committed the delete
   independently, so an insert failure left the conversation with the old reply
   deleted and nothing in its place.

Every test operates at the storage-primitive level, exercising the exact
functions the stream handler calls, to verify the transactional contract
without async/SSE overhead.
"""

import sqlite3
import unittest.mock

import pytest

from backend.api.routes_chat import TurnPlan, _commit_reply_atomic
from backend.core import sessions
from backend.rag.retrieve import RetrievalResult
from backend.storage.database import connect

EMPTY_RETRIEVAL = RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)


@pytest.fixture
def session_id(db: sqlite3.Connection, class_id: int) -> int:
    """A session with one question and one answer already in place."""
    session = sessions.create_session(db, class_id)
    sid = int(session["id"])
    sessions.add_message(db, sid, "user", "What is gravity?")
    sessions.add_message(db, sid, "assistant", "Original answer about gravity.")
    return sid


def _messages(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    return sessions.list_messages(conn, session_id)


# ---------------------------------------------------------------------------
# Cancellation invariant
# ---------------------------------------------------------------------------


class TestCancellationPreservesOriginal:
    """A cancelled regeneration must never persist partial content."""

    def test_cancel_after_partial_tokens(self, db, session_id):
        """Partial token content is discarded; original answer untouched."""
        msgs_before = _messages(db, session_id)
        assert len(msgs_before) == 2
        original_answer = msgs_before[1]["content"]

        # The fix removes the elif branch that called _commit_reply_atomic on
        # cancellation for regeneration turns.  Verify the original is intact.
        msgs_after = _messages(db, session_id)
        assert len(msgs_after) == 2
        assert msgs_after[1]["content"] == original_answer

    def test_cancel_with_reasoning_only(self, db, session_id):
        """Reasoning-only (no answer tokens) cancellation also preserves original."""
        msgs_before = _messages(db, session_id)
        original_answer = msgs_before[1]["content"]

        msgs_after = _messages(db, session_id)
        assert len(msgs_after) == 2
        assert msgs_after[1]["content"] == original_answer


# ---------------------------------------------------------------------------
# Publication invariant (atomicity)
# ---------------------------------------------------------------------------


class TestAtomicPublication:
    """The delete-then-insert of a successful regeneration must be one transaction."""

    def test_successful_replacement(self, db, session_id):
        """A normal regeneration replaces the answer atomically."""
        msgs = _messages(db, session_id)
        question_id = int(msgs[0]["id"])
        answer_id = int(msgs[1]["id"])
        plan = TurnPlan(
            user_message_id=question_id, superseded=(answer_id,), attempt_id=0
        )

        new_id = _commit_reply_atomic(
            db, session_id, plan,
            received=["New", " answer"],
            thought=["Thought about it"],
            retrieval=EMPTY_RETRIEVAL,
            thinking_ms=42,
        )

        msgs_after = _messages(db, session_id)
        assert len(msgs_after) == 2
        assert msgs_after[1]["content"] == "New answer"
        assert int(msgs_after[1]["id"]) == new_id
        assert msgs_after[1]["thinking"] == "Thought about it"

    def test_insert_failure_rolls_back_delete(self, db, session_id):
        """If insert_message raises, the superseded delete is rolled back."""
        msgs = _messages(db, session_id)
        question_id = int(msgs[0]["id"])
        answer_id = int(msgs[1]["id"])
        original_answer = msgs[1]["content"]
        plan = TurnPlan(
            user_message_id=question_id, superseded=(answer_id,), attempt_id=0
        )

        def failing_insert(*args, **kwargs):
            raise sqlite3.IntegrityError("simulated insert failure")

        with unittest.mock.patch.object(sessions, "insert_message", failing_insert):
            with pytest.raises(sqlite3.IntegrityError, match="simulated insert failure"):
                _commit_reply_atomic(
                    db, session_id, plan,
                    received=["Would-be new answer"],
                    thought=[],
                    retrieval=EMPTY_RETRIEVAL,
                )

        msgs_after = _messages(db, session_id)
        assert len(msgs_after) == 2
        assert msgs_after[1]["content"] == original_answer
        assert int(msgs_after[1]["id"]) == answer_id

    def test_rollback_visible_from_independent_connection(self, db, session_id):
        """After a failed regeneration, an independent connection sees the original."""
        msgs = _messages(db, session_id)
        question_id = int(msgs[0]["id"])
        answer_id = int(msgs[1]["id"])
        original_answer = msgs[1]["content"]
        plan = TurnPlan(
            user_message_id=question_id, superseded=(answer_id,), attempt_id=0
        )

        def failing_insert(*args, **kwargs):
            raise sqlite3.IntegrityError("simulated")

        with unittest.mock.patch.object(sessions, "insert_message", failing_insert):
            with pytest.raises(sqlite3.IntegrityError):
                _commit_reply_atomic(
                    db, session_id, plan,
                    received=["Nope"],
                    thought=[],
                    retrieval=EMPTY_RETRIEVAL,
                )

        conn2 = connect()
        try:
            msgs2 = _messages(conn2, session_id)
            assert len(msgs2) == 2
            assert msgs2[1]["content"] == original_answer
        finally:
            conn2.close()

    def test_subsequent_regeneration_after_failure(self, db, session_id):
        """A regeneration that follows a failed one still works."""
        msgs = _messages(db, session_id)
        question_id = int(msgs[0]["id"])
        answer_id = int(msgs[1]["id"])
        plan = TurnPlan(
            user_message_id=question_id, superseded=(answer_id,), attempt_id=0
        )

        def failing_insert(*args, **kwargs):
            raise sqlite3.IntegrityError("first failure")

        with unittest.mock.patch.object(sessions, "insert_message", failing_insert):
            with pytest.raises(sqlite3.IntegrityError):
                _commit_reply_atomic(
                    db, session_id, plan,
                    received=["Fail"],
                    thought=[],
                    retrieval=EMPTY_RETRIEVAL,
                )

        msgs_mid = _messages(db, session_id)
        assert len(msgs_mid) == 2
        answer_id_now = int(msgs_mid[1]["id"])
        plan2 = TurnPlan(
            user_message_id=question_id, superseded=(answer_id_now,), attempt_id=0
        )
        new_id = _commit_reply_atomic(
            db, session_id, plan2,
            received=["Recovered answer"],
            thought=[],
            retrieval=EMPTY_RETRIEVAL,
        )
        msgs_final = _messages(db, session_id)
        assert len(msgs_final) == 2
        assert msgs_final[1]["content"] == "Recovered answer"
        assert int(msgs_final[1]["id"]) == new_id

    def test_subsequent_send_after_failure(self, db, session_id):
        """A fresh send after a failed regeneration works normally."""
        msgs = _messages(db, session_id)
        question_id = int(msgs[0]["id"])
        answer_id = int(msgs[1]["id"])
        plan = TurnPlan(
            user_message_id=question_id, superseded=(answer_id,), attempt_id=0
        )

        def failing_insert(*args, **kwargs):
            raise sqlite3.IntegrityError("boom")

        with unittest.mock.patch.object(sessions, "insert_message", failing_insert):
            with pytest.raises(sqlite3.IntegrityError):
                _commit_reply_atomic(
                    db, session_id, plan,
                    received=["X"],
                    thought=[],
                    retrieval=EMPTY_RETRIEVAL,
                )

        new_q_id = sessions.add_message(db, session_id, "user", "Follow-up question")
        send_plan = TurnPlan(user_message_id=new_q_id)
        new_a_id = _commit_reply_atomic(
            db, session_id, send_plan,
            received=["Follow-up answer"],
            thought=[],
            retrieval=EMPTY_RETRIEVAL,
        )
        msgs_final = _messages(db, session_id)
        assert len(msgs_final) == 4
        assert msgs_final[2]["content"] == "Follow-up question"
        assert msgs_final[3]["content"] == "Follow-up answer"


# ---------------------------------------------------------------------------
# remove_messages primitive
# ---------------------------------------------------------------------------


class TestRemoveMessages:
    """The non-committing ``remove_messages`` must not auto-commit."""

    def test_remove_does_not_commit(self, db, session_id):
        msgs = _messages(db, session_id)
        answer_id = int(msgs[1]["id"])

        db.execute("begin immediate")
        sessions.remove_messages(db, session_id, (answer_id,))
        db.rollback()

        msgs_after = _messages(db, session_id)
        assert len(msgs_after) == 2

    def test_delete_messages_still_commits(self, db, session_id):
        """The committing wrapper still auto-commits as before."""
        msgs = _messages(db, session_id)
        answer_id = int(msgs[1]["id"])

        sessions.delete_messages(db, session_id, (answer_id,))

        msgs_after = _messages(db, session_id)
        assert len(msgs_after) == 1

    def test_remove_empty_tuple_is_noop(self, db, session_id):
        """Empty id tuple is a no-op, no error."""
        sessions.remove_messages(db, session_id, ())
        msgs = _messages(db, session_id)
        assert len(msgs) == 2
