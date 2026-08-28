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

Cancellation tests exercise ``_stream_turn`` as a real async generator, delivering
``CancelledError`` or ``GeneratorExit`` at controlled points during streaming.
Publication tests operate at the storage-primitive level.
"""

import asyncio
import json
import sqlite3
import types
import unittest.mock
from collections.abc import Iterator

import pytest

from backend.api.routes_chat import (
    Turn,
    TurnInput,
    TurnPlan,
    TurnPreparation,
    _commit_reply_atomic,
    _stream_turn,
)
from backend.core import sessions
from backend.llm.client import StreamDelta
from backend.rag.retrieve import RetrievalResult
from backend.storage.database import connect

EMPTY_RETRIEVAL = RetrievalResult(chunks=[], trimmed=False, omitted_document_count=0)

ORIGINAL_ANSWER = "Original answer about gravity."


@pytest.fixture
def session_id(db: sqlite3.Connection, class_id: int) -> int:
    """A session with one question and one answer already in place."""
    session = sessions.create_session(db, class_id)
    sid = int(session["id"])
    sessions.add_message(db, sid, "user", "What is gravity?")
    sessions.add_message(db, sid, "assistant", ORIGINAL_ANSWER)
    return sid


def _messages(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    return sessions.list_messages(conn, session_id)


def _parse_frame(raw: str) -> dict:
    """Parse an SSE frame ('data: {...}\\n\\n') into its JSON payload."""
    return json.loads(raw.removeprefix("data: ").rstrip())


def _regen_plan(db: sqlite3.Connection, sid: int) -> TurnPlan:
    """Build a regeneration plan targeting the existing answer."""
    msgs = _messages(db, sid)
    question_id = int(msgs[0]["id"])
    answer_id = int(msgs[1]["id"])
    return TurnPlan(user_message_id=question_id, superseded=(answer_id,))


_STUB_PREPARATION = TurnPreparation(
    class_id=1,
    system_prompt="You are a tutor.",
    history=[],
    retrieval_budget=1000,
)

_STUB_TURN = Turn(
    messages=[{"role": "user", "content": "What is gravity?"}],
    retrieval=EMPTY_RETRIEVAL,
)

_STUB_REQUEST = TurnInput(content="What is gravity?", mode="guide")


def _stub_config():
    """A mock TutorConfig with just enough attributes for stream_chat."""
    return types.SimpleNamespace(endpoint_url="http://stub", api_key="stub", model="stub")


def _stub_cost():
    """A mock TurnCost; never inspected because _prepare_turn is mocked."""
    return unittest.mock.MagicMock()


def _patches():
    """Context manager that stubs all I/O paths inside _stream_turn."""
    return unittest.mock.patch.multiple(
        "backend.api.routes_chat",
        _prepare_turn=unittest.mock.MagicMock(return_value=_STUB_PREPARATION),
        retrieve=unittest.mock.MagicMock(return_value=EMPTY_RETRIEVAL),
        _fit_retrieval_to_prompt=unittest.mock.MagicMock(return_value=EMPTY_RETRIEVAL),
        _build_turn=unittest.mock.MagicMock(return_value=_STUB_TURN),
    )


async def _drain(gen) -> list[str]:
    """Collect all frames from an async generator."""
    frames = []
    async for frame in gen:
        frames.append(frame)
    return frames


# ---------------------------------------------------------------------------
# Cancellation invariant
# ---------------------------------------------------------------------------


class TestCancellationPreservesOriginal:
    """A cancelled regeneration must never persist partial content.

    Each test drives ``_stream_turn`` as a real async generator with a mocked
    ``stream_chat`` that yields controlled ``StreamDelta`` objects. Cancellation
    is delivered via ``athrow(CancelledError)`` or ``aclose()`` (GeneratorExit)
    at a specific point in the stream.
    """

    @pytest.fixture(autouse=True)
    def _clear_turn_claims(self, session_id) -> Iterator[None]:
        yield
        claim = sessions.active_turn(session_id)
        if claim is not None:
            sessions.end_turn(session_id, claim.token)

    async def test_cancel_before_first_model_output(self, db, session_id):
        """CancelledError before any StreamDelta leaves the original intact."""
        plan = _regen_plan(db, session_id)
        turn_token = sessions.begin_turn(session_id)

        never_yields: list[StreamDelta] = []

        async def mock_stream(*_args, **_kwargs):
            await asyncio.sleep(100)
            for d in never_yields:
                yield d

        with _patches(), unittest.mock.patch("backend.api.routes_chat.stream_chat", mock_stream):
            gen = _stream_turn(
                session_id, _STUB_REQUEST, _stub_config(), plan, _stub_cost(), turn_token
            )
            # Drain the setup frames (start, status, status, status).
            for _ in range(4):
                await gen.__anext__()
            # Cancel before any model output arrives.
            with pytest.raises(asyncio.CancelledError):
                await gen.athrow(asyncio.CancelledError())

        msgs = _messages(db, session_id)
        assert len(msgs) == 2
        assert msgs[1]["content"] == ORIGINAL_ANSWER
        assert sessions.active_turn(session_id) is None

    async def test_cancel_after_partial_answer_tokens(self, db, session_id):
        """CancelledError after several answer tokens discards the partial content."""
        plan = _regen_plan(db, session_id)
        turn_token = sessions.begin_turn(session_id)

        async def mock_stream(*_args, **_kwargs):
            yield StreamDelta(channel="answer", text="Partial ")
            yield StreamDelta(channel="answer", text="replacement ")
            yield StreamDelta(channel="answer", text="that ")
            await asyncio.sleep(100)
            yield StreamDelta(channel="answer", text="never finishes")

        with _patches(), unittest.mock.patch("backend.api.routes_chat.stream_chat", mock_stream):
            gen = _stream_turn(
                session_id, _STUB_REQUEST, _stub_config(), plan, _stub_cost(), turn_token
            )
            frames = []
            # Drain until we have some token frames.
            async for frame in gen:
                frames.append(frame)
                parsed = _parse_frame(frame)
                if (
                    parsed.get("type") == "token"
                    and len([f for f in frames if '"token"' in f]) >= 3
                ):
                    break
            # Cancel after receiving partial tokens.
            with pytest.raises(asyncio.CancelledError):
                await gen.athrow(asyncio.CancelledError())

        msgs = _messages(db, session_id)
        assert len(msgs) == 2
        assert msgs[1]["content"] == ORIGINAL_ANSWER
        assert sessions.active_turn(session_id) is None

    async def test_cancel_after_reasoning_before_answer(self, db, session_id):
        """CancelledError after reasoning but before answer tokens preserves original."""
        plan = _regen_plan(db, session_id)
        turn_token = sessions.begin_turn(session_id)

        async def mock_stream(*_args, **_kwargs):
            yield StreamDelta(channel="reasoning", text="Let me think ")
            yield StreamDelta(channel="reasoning", text="about gravity...")
            await asyncio.sleep(100)
            yield StreamDelta(channel="answer", text="Never reached")

        with _patches(), unittest.mock.patch("backend.api.routes_chat.stream_chat", mock_stream):
            gen = _stream_turn(
                session_id, _STUB_REQUEST, _stub_config(), plan, _stub_cost(), turn_token
            )
            frames = []
            async for frame in gen:
                frames.append(frame)
                parsed = _parse_frame(frame)
                if (
                    parsed.get("type") == "reasoning"
                    and len([f for f in frames if '"reasoning"' in f]) >= 2
                ):
                    break
            with pytest.raises(asyncio.CancelledError):
                await gen.athrow(asyncio.CancelledError())

        msgs = _messages(db, session_id)
        assert len(msgs) == 2
        assert msgs[1]["content"] == ORIGINAL_ANSWER
        assert sessions.active_turn(session_id) is None

    async def test_generator_exit_after_tokens(self, db, session_id):
        """GeneratorExit (aclose) after partial tokens also preserves original."""
        plan = _regen_plan(db, session_id)
        turn_token = sessions.begin_turn(session_id)

        async def mock_stream(*_args, **_kwargs):
            yield StreamDelta(channel="answer", text="Partial ")
            yield StreamDelta(channel="answer", text="answer")
            await asyncio.sleep(100)
            yield StreamDelta(channel="answer", text="never")

        with _patches(), unittest.mock.patch("backend.api.routes_chat.stream_chat", mock_stream):
            gen = _stream_turn(
                session_id, _STUB_REQUEST, _stub_config(), plan, _stub_cost(), turn_token
            )
            async for frame in gen:
                if _parse_frame(frame).get("type") == "token":
                    break
            await gen.aclose()

        msgs = _messages(db, session_id)
        assert len(msgs) == 2
        assert msgs[1]["content"] == ORIGINAL_ANSWER
        assert sessions.active_turn(session_id) is None

    async def test_subsequent_regeneration_succeeds_after_cancel(self, db, session_id):
        """After a cancelled regeneration, a new one replaces the original normally."""
        plan = _regen_plan(db, session_id)
        turn_token = sessions.begin_turn(session_id)

        async def mock_stream_partial(*_args, **_kwargs):
            yield StreamDelta(channel="answer", text="Partial")
            await asyncio.sleep(100)

        with (
            _patches(),
            unittest.mock.patch("backend.api.routes_chat.stream_chat", mock_stream_partial),
        ):
            gen = _stream_turn(
                session_id, _STUB_REQUEST, _stub_config(), plan, _stub_cost(), turn_token
            )
            async for frame in gen:
                if _parse_frame(frame).get("type") == "token":
                    break
            with pytest.raises(asyncio.CancelledError):
                await gen.athrow(asyncio.CancelledError())

        msgs_mid = _messages(db, session_id)
        assert len(msgs_mid) == 2
        assert msgs_mid[1]["content"] == ORIGINAL_ANSWER

        plan2 = _regen_plan(db, session_id)
        turn_token2 = sessions.begin_turn(session_id)

        async def mock_stream_full(*_args, **_kwargs):
            yield StreamDelta(channel="answer", text="Complete ")
            yield StreamDelta(channel="answer", text="new answer")

        with (
            _patches(),
            unittest.mock.patch("backend.api.routes_chat.stream_chat", mock_stream_full),
        ):
            gen2 = _stream_turn(
                session_id, _STUB_REQUEST, _stub_config(), plan2, _stub_cost(), turn_token2
            )
            frames = await _drain(gen2)

        done_frames = [f for f in frames if '"done"' in f]
        assert len(done_frames) == 1

        msgs_final = _messages(db, session_id)
        assert len(msgs_final) == 2
        assert msgs_final[1]["content"] == "Complete new answer"
        assert sessions.active_turn(session_id) is None

    async def test_regular_send_keeps_partial_on_cancel(self, db, session_id):
        """A non-regeneration send DOES persist partial content on cancel."""
        new_q_id = sessions.add_message(db, session_id, "user", "Follow-up question")
        plan = TurnPlan(user_message_id=new_q_id)
        turn_token = sessions.begin_turn(session_id)

        async def mock_stream(*_args, **_kwargs):
            yield StreamDelta(channel="answer", text="Partial ")
            yield StreamDelta(channel="answer", text="reply")
            await asyncio.sleep(100)

        with _patches(), unittest.mock.patch("backend.api.routes_chat.stream_chat", mock_stream):
            gen = _stream_turn(
                session_id, _STUB_REQUEST, _stub_config(), plan, _stub_cost(), turn_token
            )
            token_count = 0
            async for frame in gen:
                if _parse_frame(frame).get("type") == "token":
                    token_count += 1
                    if token_count >= 2:
                        break
            with pytest.raises(asyncio.CancelledError):
                await gen.athrow(asyncio.CancelledError())

        msgs = _messages(db, session_id)
        assert len(msgs) == 4
        assert msgs[2]["content"] == "Follow-up question"
        assert msgs[3]["content"] == "Partial reply"


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
        plan = TurnPlan(user_message_id=question_id, superseded=(answer_id,), attempt_id=0)

        new_id = _commit_reply_atomic(
            db,
            session_id,
            plan,
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
        plan = TurnPlan(user_message_id=question_id, superseded=(answer_id,), attempt_id=0)

        def failing_insert(*args, **kwargs):
            raise sqlite3.IntegrityError("simulated insert failure")

        with (
            unittest.mock.patch.object(sessions, "insert_message", failing_insert),
            pytest.raises(sqlite3.IntegrityError, match="simulated insert failure"),
        ):
            _commit_reply_atomic(
                db,
                session_id,
                plan,
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
        plan = TurnPlan(user_message_id=question_id, superseded=(answer_id,), attempt_id=0)

        def failing_insert(*args, **kwargs):
            raise sqlite3.IntegrityError("simulated")

        with (
            unittest.mock.patch.object(sessions, "insert_message", failing_insert),
            pytest.raises(sqlite3.IntegrityError),
        ):
            _commit_reply_atomic(
                db,
                session_id,
                plan,
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
        plan = TurnPlan(user_message_id=question_id, superseded=(answer_id,), attempt_id=0)

        def failing_insert(*args, **kwargs):
            raise sqlite3.IntegrityError("first failure")

        with (
            unittest.mock.patch.object(sessions, "insert_message", failing_insert),
            pytest.raises(sqlite3.IntegrityError),
        ):
            _commit_reply_atomic(
                db,
                session_id,
                plan,
                received=["Fail"],
                thought=[],
                retrieval=EMPTY_RETRIEVAL,
            )

        msgs_mid = _messages(db, session_id)
        assert len(msgs_mid) == 2
        answer_id_now = int(msgs_mid[1]["id"])
        plan2 = TurnPlan(user_message_id=question_id, superseded=(answer_id_now,), attempt_id=0)
        new_id = _commit_reply_atomic(
            db,
            session_id,
            plan2,
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
        plan = TurnPlan(user_message_id=question_id, superseded=(answer_id,), attempt_id=0)

        def failing_insert(*args, **kwargs):
            raise sqlite3.IntegrityError("boom")

        with (
            unittest.mock.patch.object(sessions, "insert_message", failing_insert),
            pytest.raises(sqlite3.IntegrityError),
        ):
            _commit_reply_atomic(
                db,
                session_id,
                plan,
                received=["X"],
                thought=[],
                retrieval=EMPTY_RETRIEVAL,
            )

        new_q_id = sessions.add_message(db, session_id, "user", "Follow-up question")
        send_plan = TurnPlan(user_message_id=new_q_id)
        _commit_reply_atomic(
            db,
            session_id,
            send_plan,
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
