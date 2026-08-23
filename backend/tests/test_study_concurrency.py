"""Real-connection concurrency tests for study review and quiz attempts.

Every worker here opens its own `sqlite3` connection through `connect()`, and a barrier
releases the racing operations together, so the transactions genuinely contend on the
database rather than being serialized by a shared connection or the GIL. These prove the
`begin immediate` coordination and the idempotency keys behave under real races, which a
single-connection test cannot (PLA-277, PLA-296).
"""

import json
import sqlite3
import threading
from collections.abc import Callable
from typing import Any

from backend.api import routes_study
from backend.core import artifacts
from backend.core.errors import ConflictError, NotFoundError
from backend.storage.database import connect


def _run_together(*workers: Callable[[], Any]) -> list[Any]:
    """Run each worker in its own thread, released together, returning results/exceptions."""
    barrier = threading.Barrier(len(workers))
    results: list[Any] = [None] * len(workers)

    def wrapped(index: int, worker: Callable[[], Any]) -> None:
        barrier.wait()
        try:
            results[index] = worker()
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            results[index] = exc

    threads = [
        threading.Thread(target=wrapped, args=(index, worker))
        for index, worker in enumerate(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def _document(db: sqlite3.Connection, class_id: int) -> int:
    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, state) "
            "values (?, 'notes.pdf', '/tmp/x', 'application/pdf', 1, 'ready')",
            (class_id,),
        ).lastrowid
        or 0
    )
    db.commit()
    return document_id


def _deck_with_card(db: sqlite3.Connection, class_id: int) -> int:
    created = artifacts.create_artifact(
        db,
        class_id,
        "Deck",
        [artifacts.SourceSpec(document_id=_document(db, class_id), role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_FLASHCARD_DECK,
    )
    deck_id = int(created["id"])
    artifacts.set_artifact_state(db, deck_id, artifacts.READY)
    part_id = artifacts.create_part(
        db,
        deck_id,
        artifacts.CARD,
        1,
        label="delta",
        content=json.dumps({"front": "F", "back": "B", "topic": "delta"}),
        content_type=artifacts.JSON,
        status=artifacts.PART_COMPLETE,
    )
    db.execute("insert into card_states (part_id, due_at) values (?, datetime('now'))", (part_id,))
    db.commit()
    return part_id


def _quiz_with_questions(
    db: sqlite3.Connection, class_id: int, topics: list[str]
) -> tuple[int, list[int]]:
    created = artifacts.create_artifact(
        db,
        class_id,
        "Quiz",
        [artifacts.SourceSpec(document_id=_document(db, class_id), role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_QUIZ,
    )
    quiz_id = int(created["id"])
    artifacts.set_artifact_state(db, quiz_id, artifacts.READY)
    part_ids = [
        artifacts.create_part(
            db,
            quiz_id,
            artifacts.QUIZ_QUESTION,
            ordinal,
            label=topic,
            content=json.dumps(
                {
                    "type": "mcq",
                    "question": f"Q {topic}?",
                    "options": ["a", "b", "c", "d"],
                    "correct_index": 0,
                    "explanation": "because",
                    "topic": topic,
                    "difficulty": "intermediate",
                }
            ),
            content_type=artifacts.JSON,
            status=artifacts.PART_COMPLETE,
        )
        for ordinal, topic in enumerate(topics, start=1)
    ]
    db.commit()
    return quiz_id, part_ids


def _review(part_id: int, rating: str, operation_id: str) -> Callable[[], Any]:
    def call() -> Any:
        conn = connect()
        try:
            return routes_study.review_card(
                part_id,
                routes_study.CardReview(rating=rating, operation_id=operation_id),
                conn,
            )
        finally:
            conn.close()

    return call


# ---------------------------------------------------------------------------
# Flashcard review (PLA-296)
# ---------------------------------------------------------------------------


def test_same_operation_id_applies_a_review_exactly_once(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _deck_with_card(db, class_id)

    results = _run_together(
        _review(part_id, "good", "op-dup"),
        _review(part_id, "good", "op-dup"),
    )

    assert all(not isinstance(result, Exception) for result in results)
    # Both requests return the same stored result.
    assert results[0] == results[1]
    assert db.execute("select count(*) from card_review_log").fetchone()[0] == 1
    assert (
        db.execute("select reps from card_states where part_id = ?", (part_id,)).fetchone()[0] == 1
    )


def test_distinct_operations_serialize_and_neither_transition_is_lost(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _deck_with_card(db, class_id)

    results = _run_together(
        _review(part_id, "good", "op-1"),
        _review(part_id, "good", "op-2"),
    )

    assert all(not isinstance(result, Exception) for result in results)
    # Two distinct reviews both landed, computed against the latest state in turn.
    assert db.execute("select count(*) from card_review_log").fetchone()[0] == 2
    assert (
        db.execute("select reps from card_states where part_id = ?", (part_id,)).fetchone()[0] == 2
    )


def test_a_review_racing_a_delete_is_a_truthful_conflict(
    db: sqlite3.Connection, class_id: int
) -> None:
    part_id = _deck_with_card(db, class_id)

    def delete_card() -> Any:
        conn = connect()
        try:
            artifacts.delete_part(conn, part_id)
            return "deleted"
        finally:
            conn.close()

    results = _run_together(_review(part_id, "good", "op-x"), delete_card)

    # Either the review committed before the delete, or it found the card gone; never a
    # partial write. The review either returns a state or raises NotFound.
    review_result = results[0]
    if isinstance(review_result, Exception):
        assert isinstance(review_result, NotFoundError)
    # The log never holds an orphan row for a card that no longer exists with its state.
    logged = db.execute("select count(*) from card_review_log").fetchone()[0]
    remaining = db.execute(
        "select count(*) from card_states where part_id = ?", (part_id,)
    ).fetchone()[0]
    assert logged <= 1
    if remaining == 0:
        # Card deleted: its log rows cascaded away too.
        assert (
            db.execute(
                "select count(*) from card_review_log where part_id = ?", (part_id,)
            ).fetchone()[0]
            == 0
        )


def test_a_lost_response_retry_after_reconnect_returns_the_stored_result(
    db: sqlite3.Connection, class_id: int
) -> None:
    """The first review commits but its response is 'lost'; the retry on a fresh connection
    with the same operation id returns the same result and does not review again."""
    part_id = _deck_with_card(db, class_id)

    first = _review(part_id, "hard", "op-retry")()
    second = _review(part_id, "hard", "op-retry")()

    assert first == second
    assert db.execute("select count(*) from card_review_log").fetchone()[0] == 1
    assert (
        db.execute("select reps from card_states where part_id = ?", (part_id,)).fetchone()[0] == 1
    )


# ---------------------------------------------------------------------------
# Quiz attempts (PLA-277)
# ---------------------------------------------------------------------------


def _start(quiz_id: int, restart: bool = False) -> Callable[[], Any]:
    def call() -> Any:
        conn = connect()
        try:
            return routes_study.start_attempt(quiz_id, conn, restart=restart)
        finally:
            conn.close()

    return call


def _answer(attempt_id: int, part_id: int, selected_index: int) -> Callable[[], Any]:
    def call() -> Any:
        conn = connect()
        try:
            return routes_study.answer_question(
                attempt_id,
                routes_study.AnswerCreate(part_id=part_id, selected_index=selected_index),
                conn,
            )
        finally:
            conn.close()

    return call


def _finish(attempt_id: int) -> Callable[[], Any]:
    def call() -> Any:
        conn = connect()
        try:
            return routes_study.finish_attempt(attempt_id, conn)
        finally:
            conn.close()

    return call


def test_concurrent_starts_yield_one_active_attempt(db: sqlite3.Connection, class_id: int) -> None:
    quiz_id, _ = _quiz_with_questions(db, class_id, ["delta", "convolution"])

    results = _run_together(_start(quiz_id), _start(quiz_id))

    assert all(not isinstance(result, Exception) for result in results)
    # Both starts observe the same single active attempt.
    assert results[0]["attempt_id"] == results[1]["attempt_id"]
    assert db.execute("select count(*) from quiz_attempts").fetchone()[0] == 1


def test_concurrent_answers_to_different_questions_both_land(
    db: sqlite3.Connection, class_id: int
) -> None:
    quiz_id, part_ids = _quiz_with_questions(db, class_id, ["delta", "convolution"])
    attempt = _start(quiz_id)()
    attempt_id = attempt["attempt_id"]

    results = _run_together(
        _answer(attempt_id, part_ids[0], 0),
        _answer(attempt_id, part_ids[1], 1),
    )

    assert all(not isinstance(result, Exception) for result in results)
    rows = db.execute(
        "select part_id, correct from quiz_answers where attempt_id = ? order by part_id",
        (attempt_id,),
    ).fetchall()
    assert [(int(r["part_id"]), int(r["correct"])) for r in rows] == [
        (part_ids[0], 1),
        (part_ids[1], 0),
    ]


def test_an_answer_racing_finish_never_lands_after_the_finish(
    db: sqlite3.Connection, class_id: int
) -> None:
    """The answer either commits before the finish (and is scored) or is rejected because
    the attempt is finished; it can never land after the finish and corrupt the score."""
    quiz_id, part_ids = _quiz_with_questions(db, class_id, ["delta", "convolution"])
    attempt_id = _start(quiz_id)()["attempt_id"]
    # Pre-answer the first question so a finish always has something to score.
    _answer(attempt_id, part_ids[0], 0)()

    results = _run_together(
        _answer(attempt_id, part_ids[1], 0),
        _finish(attempt_id),
    )

    answer_result, finish_result = results
    assert not isinstance(finish_result, Exception)
    # If the answer was rejected, it must be a clean conflict, not a corrupt write.
    if isinstance(answer_result, Exception):
        assert isinstance(answer_result, ConflictError)
    stored = db.execute("select result from quiz_attempts where id = ?", (attempt_id,)).fetchone()[
        "result"
    ]
    result = json.loads(stored)
    answered = db.execute(
        "select count(*) from quiz_answers where attempt_id = ?", (attempt_id,)
    ).fetchone()[0]
    # The stored score's answered count matches the answers that actually committed.
    assert result["answered"] == answered
    assert result["total"] == 2


def test_concurrent_finishes_score_once(db: sqlite3.Connection, class_id: int) -> None:
    quiz_id, part_ids = _quiz_with_questions(db, class_id, ["delta", "convolution"])
    attempt_id = _start(quiz_id)()["attempt_id"]
    _answer(attempt_id, part_ids[0], 0)()

    results = _run_together(_finish(attempt_id), _finish(attempt_id))

    assert all(not isinstance(result, Exception) for result in results)
    # Both finishes return the same result; the attempt is finished exactly once.
    assert results[0] == results[1]
    finished = db.execute(
        "select finished_at, result from quiz_attempts where id = ?", (attempt_id,)
    ).fetchone()
    assert finished["finished_at"] is not None
    assert json.loads(finished["result"])["score"] == 1


def test_finish_after_a_lost_response_returns_the_same_stored_result(
    db: sqlite3.Connection, class_id: int
) -> None:
    quiz_id, part_ids = _quiz_with_questions(db, class_id, ["delta"])
    attempt_id = _start(quiz_id)()["attempt_id"]
    _answer(attempt_id, part_ids[0], 0)()

    first = _finish(attempt_id)()
    second = _finish(attempt_id)()

    assert first == second
    assert first["score"] == 1
    assert first["total"] == 1
