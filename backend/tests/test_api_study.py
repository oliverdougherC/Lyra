"""Contract tests for the study endpoints.

The worker is never started here: `study.enqueue` is stubbed so creating a deck or quiz
stays a pure write. The worker's behavior is test_study.py; this file is the HTTP
surface - status codes, guards, and the round-trips a session makes.
"""

import json
import sqlite3
import threading
import time
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_study
from backend.core import artifacts, grading, study
from backend.core.errors import LyraError
from backend.storage.database import connect, get_db


def _request_db() -> Iterator[sqlite3.Connection]:
    """A connection to the temporary database, opened inside the calling thread."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def no_worker(monkeypatch: pytest.MonkeyPatch) -> list[study._Job]:
    """Record what would have been queued instead of running it."""
    queued: list[study._Job] = []
    monkeypatch.setattr(routes_study.study, "enqueue", queued.append)
    return queued


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    """A TestClient over an app carrying only the study router."""
    app = FastAPI()

    @app.exception_handler(LyraError)
    async def handle_lyra_error(request: Request, exc: LyraError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    app.include_router(routes_study.router)
    app.dependency_overrides[get_db] = _request_db
    with TestClient(app) as test_client:
        yield test_client


def _document(
    db: sqlite3.Connection, class_id: int, filename: str = "notes.pdf", state: str = "ready"
) -> int:
    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, "
            "state) values (?, ?, '/tmp/x', 'application/pdf', 1, ?)",
            (class_id, filename, state),
        ).lastrowid
        or 0
    )
    db.commit()
    return document_id


def _deck(db: sqlite3.Connection, class_id: int, document_id: int, state: str = "ready") -> int:
    created = artifacts.create_artifact(
        db,
        class_id,
        "Midterm deck",
        [artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_FLASHCARD_DECK,
    )
    artifact_id = int(created["id"])
    if state != artifacts.PENDING:
        artifacts.set_artifact_state(db, artifact_id, state)
    return artifact_id


def _card(db: sqlite3.Connection, artifact_id: int, ordinal: int = 1, topic: str = "delta") -> int:
    part_id = artifacts.create_part(
        db,
        artifact_id,
        artifacts.CARD,
        ordinal,
        label=topic,
        content=json.dumps({"front": "What sifts?", "back": "The delta.", "topic": topic}),
        content_type=artifacts.JSON,
        status=artifacts.PART_COMPLETE,
    )
    db.execute("insert into card_states (part_id, due_at) values (?, datetime('now'))", (part_id,))
    db.commit()
    return part_id


def _quiz(db: sqlite3.Connection, class_id: int, document_id: int, state: str = "ready") -> int:
    created = artifacts.create_artifact(
        db,
        class_id,
        "Week 5 quiz",
        [artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_QUIZ,
    )
    artifact_id = int(created["id"])
    if state != artifacts.PENDING:
        artifacts.set_artifact_state(db, artifact_id, state)
    return artifact_id


def _question(db: sqlite3.Connection, artifact_id: int, ordinal: int, topic: str) -> int:
    return artifacts.create_part(
        db,
        artifact_id,
        artifacts.QUIZ_QUESTION,
        ordinal,
        label=topic,
        content=json.dumps(
            {
                "type": "mcq",
                "question": "Which picks x(0)?",
                "options": ["sifting", "scaling", "shifting", "sampling"],
                "correct_index": 0,
                "explanation": "The sifting property.",
                "topic": topic,
                "difficulty": "intermediate",
            }
        ),
        content_type=artifacts.JSON,
        status=artifacts.PART_COMPLETE,
    )


def test_creating_a_deck_returns_202_and_queues_it(
    client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list
) -> None:
    _document(db, class_id)

    response = client.post(f"/api/classes/{class_id}/decks", json={"title": "Midterm deck"})

    assert response.status_code == 202
    body = response.json()
    assert body["kind"] == artifacts.KIND_FLASHCARD_DECK
    assert body["state"] == artifacts.PENDING
    assert [job.artifact_id for job in no_worker] == [body["id"]]
    assert no_worker[0].cards_per_topic == 4


def test_a_deck_needs_a_ready_document(client: TestClient, class_id: int) -> None:
    response = client.post(f"/api/classes/{class_id}/decks", json={"title": "Deck"})

    assert response.status_code == 409
    assert "no processed documents" in response.json()["detail"].lower()


def test_named_documents_that_are_not_ready_are_a_409(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id, state="embedding")

    response = client.post(
        f"/api/classes/{class_id}/decks",
        json={"title": "Deck", "document_ids": [document_id]},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "notes.pdf" in detail
    assert "still processing" in detail


def test_a_named_document_from_another_class_is_a_404(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    other_class = int(db.execute("insert into classes (name) values ('x')").lastrowid or 0)
    document_id = _document(db, other_class)

    response = client.post(
        f"/api/classes/{class_id}/decks",
        json={"title": "Deck", "document_ids": [document_id]},
    )

    assert response.status_code == 404


def test_creating_a_quiz_passes_its_options_to_the_job(
    client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list
) -> None:
    _document(db, class_id)

    response = client.post(
        f"/api/classes/{class_id}/quizzes",
        json={"title": "Quiz", "count": 5, "difficulty": "exam", "types": ["mcq"]},
    )

    assert response.status_code == 202
    job = no_worker[0]
    assert job.count == 5
    assert job.difficulty == "exam"
    assert job.types == ("mcq",)


def test_the_study_list_groups_decks_and_quizzes_with_counts(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    deck_id = _deck(db, class_id, document_id)
    _card(db, deck_id, 1)
    _card(db, deck_id, 2)
    _quiz(db, class_id, document_id)

    response = client.get(f"/api/classes/{class_id}/study")

    assert response.status_code == 200
    body = response.json()
    assert len(body["decks"]) == 1
    assert body["decks"][0]["cards_total"] == 2
    assert body["decks"][0]["due_count"] == 2
    assert body["decks"][0]["buckets"] == {"new": 2, "learning": 0, "mastered": 0}
    assert len(body["quizzes"]) == 1
    assert "buckets" not in body["quizzes"][0]


def test_reading_a_deck_carries_cards_and_states(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    deck_id = _deck(db, class_id, _document(db, class_id))
    part_id = _card(db, deck_id)

    response = client.get(f"/api/decks/{deck_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["cards"][0]["part_id"] == part_id
    assert body["cards"][0]["card"]["front"] == "What sifts?"
    assert body["cards"][0]["card_state"]["state"] == "new"
    assert body["cards"][0]["card_state"]["bucket"] == "new"


def test_a_session_serves_due_cards_in_study_order(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    deck_id = _deck(db, class_id, _document(db, class_id))
    first = _card(db, deck_id, 1)
    second = _card(db, deck_id, 2)
    # Push the second card a day out: the first is due, it is not.
    db.execute(
        "update card_states set due_at = datetime('now', '+1 day'), state = 'review', "
        "stability = 4 where part_id = ?",
        (second,),
    )
    db.commit()

    response = client.get(f"/api/decks/{deck_id}/session")

    assert response.status_code == 200
    cards = response.json()["cards"]
    assert [card["part_id"] for card in cards] == [first, second]
    assert [card["due"] for card in cards] == [True, False]


def test_a_review_round_trips_through_the_scheduler(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    deck_id = _deck(db, class_id, _document(db, class_id))
    part_id = _card(db, deck_id)

    response = client.post(
        f"/api/cards/{part_id}/review", json={"rating": "good", "operation_id": "op-1"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reps"] == 1
    assert body["state"] == "learning"
    assert body["stability"] == pytest.approx(2.0)
    log = db.execute("select rating from card_review_log where part_id = ?", (part_id,)).fetchone()
    assert log["rating"] == "good"


def test_a_review_on_an_unready_deck_is_a_409(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    deck_id = _deck(db, class_id, _document(db, class_id), state=artifacts.GENERATING)
    part_id = _card(db, deck_id)

    response = client.post(
        f"/api/cards/{part_id}/review", json={"rating": "good", "operation_id": "op-1"}
    )

    assert response.status_code == 409


def test_editing_a_card_preserves_its_scheduling_state(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    deck_id = _deck(db, class_id, _document(db, class_id))
    part_id = _card(db, deck_id)
    db.execute(
        "update card_states set state = 'review', stability = 12.5, reps = 4 where part_id = ?",
        (part_id,),
    )
    db.commit()

    response = client.patch(
        f"/api/cards/{part_id}",
        json={"front": "Better front", "back": "Better back", "topic": "delta"},
    )

    assert response.status_code == 200
    state = db.execute("select * from card_states where part_id = ?", (part_id,)).fetchone()
    assert state["state"] == "review"
    assert state["stability"] == pytest.approx(12.5)
    revisions = artifacts.list_revisions(db, part_id)
    assert revisions[0]["origin"] == artifacts.USER_CORRECTED


def test_deleting_a_card_cascades_its_state(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    deck_id = _deck(db, class_id, _document(db, class_id))
    part_id = _card(db, deck_id)

    response = client.delete(f"/api/cards/{part_id}")

    assert response.status_code == 204
    assert db.execute("select count(*) from card_states").fetchone()[0] == 0


def test_reading_a_quiz_carries_full_payloads(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _question(db, quiz_id, 1, "delta")

    response = client.get(f"/api/quizzes/{quiz_id}")

    assert response.status_code == 200
    question = response.json()["questions"][0]
    assert question["part_id"] == part_id
    # Local and trusted: the interface, not the API, decides when to reveal.
    assert question["question"]["correct_index"] == 0
    assert question["question"]["explanation"] == "The sifting property."


def test_cancelling_a_running_deck_marks_it_cancelled(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    deck_id = _deck(db, class_id, _document(db, class_id), state=artifacts.GENERATING)

    response = client.post(f"/api/decks/{deck_id}/cancel")

    assert response.status_code == 200
    assert response.json()["state"] == artifacts.CANCELLED


def test_cancelling_a_pending_quiz_marks_it_cancelled(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    quiz_id = _quiz(db, class_id, _document(db, class_id), state=artifacts.PENDING)

    response = client.post(f"/api/quizzes/{quiz_id}/cancel")

    assert response.status_code == 200
    assert response.json()["state"] == artifacts.CANCELLED


def test_cancelling_a_ready_deck_is_refused(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    deck_id = _deck(db, class_id, _document(db, class_id), state=artifacts.READY)

    response = client.post(f"/api/decks/{deck_id}/cancel")

    assert response.status_code == 409
    assert response.json()["detail"] == f"{routes_study.NOT_RUNNING_MESSAGE} Current state: ready."


def test_an_attempt_grades_answers_and_scores_by_topic(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    first = _question(db, quiz_id, 1, "delta")
    second = _question(db, quiz_id, 2, "convolution")

    attempt = client.post(f"/api/quizzes/{quiz_id}/attempts").json()
    assert attempt["question_part_ids"] == [first, second]

    right = client.post(
        f"/api/attempts/{attempt['attempt_id']}/answers",
        json={"part_id": first, "selected_index": 0},
    )
    assert right.json() == {
        "correct": True,
        "uncertain": False,
        "correct_index": 0,
        "explanation": "The sifting property.",
    }
    # Any index but the stored one is wrong; -1 is how a fill_blank miss arrives.
    wrong = client.post(
        f"/api/attempts/{attempt['attempt_id']}/answers",
        json={"part_id": second, "selected_index": -1, "response_text": ""},
    )
    assert wrong.json()["correct"] is False
    assert wrong.json()["uncertain"] is False

    finished = client.post(f"/api/attempts/{attempt['attempt_id']}/finish")
    assert finished.status_code == 200
    body = finished.json()
    assert body["score"] == 1
    assert body["total"] == 2
    assert body["unresolved"] == 0
    assert body["by_topic"] == [
        {"topic": "convolution", "correct": 0, "total": 1, "unresolved": 0},
        {"topic": "delta", "correct": 1, "total": 1, "unresolved": 0},
    ]

    again = client.post(
        f"/api/attempts/{attempt['attempt_id']}/answers",
        json={"part_id": first, "selected_index": 0},
    )
    assert again.status_code == 409


def test_reanswering_updates_rather_than_duplicates(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _question(db, quiz_id, 1, "delta")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]

    client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={"part_id": part_id, "selected_index": 3},
    )
    client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={"part_id": part_id, "selected_index": 0},
    )

    rows = db.execute("select selected_index, correct from quiz_answers").fetchall()
    assert len(rows) == 1
    assert (rows[0]["selected_index"], rows[0]["correct"]) == (0, 1)


def test_an_answer_for_another_quizs_question_is_a_404(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    quiz_id = _quiz(db, class_id, document_id)
    _question(db, quiz_id, 1, "delta")
    other_quiz = _quiz(db, class_id, document_id)
    foreign_part = _question(db, other_quiz, 1, "delta")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]

    response = client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={"part_id": foreign_part, "selected_index": 0},
    )

    assert response.status_code == 404


def test_kind_guards_return_404_across_decks_and_quizzes(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    deck_id = _deck(db, class_id, document_id)
    quiz_id = _quiz(db, class_id, document_id)

    assert client.get(f"/api/decks/{quiz_id}").status_code == 404
    assert client.get(f"/api/quizzes/{deck_id}").status_code == 404
    assert client.get(f"/api/decks/{quiz_id}/status").status_code == 404
    assert client.get(f"/api/quizzes/{deck_id}/status").status_code == 404
    assert client.post(f"/api/decks/{quiz_id}/cancel").status_code == 404
    assert client.post(f"/api/quizzes/{deck_id}/cancel").status_code == 404
    assert client.patch(f"/api/decks/{quiz_id}", json={"title": "x"}).status_code == 404
    assert client.delete(f"/api/quizzes/{deck_id}").status_code == 404


def test_status_reports_generation_progress(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    deck_id = _deck(db, class_id, _document(db, class_id), state=artifacts.GENERATING)
    artifacts.set_problems_total(db, deck_id, 6)
    artifacts.set_problems_done(db, deck_id, 2)

    response = client.get(f"/api/decks/{deck_id}/status")

    assert response.status_code == 200
    assert response.json()["problems_total"] == 6
    assert response.json()["problems_done"] == 2
    assert response.json()["state"] == artifacts.GENERATING


def test_rename_and_delete_round_trip(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    deck_id = _deck(db, class_id, _document(db, class_id))

    renamed = client.patch(f"/api/decks/{deck_id}", json={"title": "Final deck"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Final deck"

    assert client.delete(f"/api/decks/{deck_id}").status_code == 204
    assert client.get(f"/api/decks/{deck_id}").status_code == 404


# ---------------------------------------------------------------------------
# Durable job persistence (PLA-169)
# ---------------------------------------------------------------------------


def test_creating_a_deck_persists_its_durable_job(
    client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list
) -> None:
    document_id = _document(db, class_id)

    response = client.post(
        f"/api/classes/{class_id}/decks",
        json={"title": "Deck", "document_ids": [document_id], "cards_per_topic": 5},
    )

    assert response.status_code == 202
    artifact_id = response.json()["id"]
    row = db.execute("select * from study_jobs where artifact_id = ?", (artifact_id,)).fetchone()
    assert row is not None
    assert row["kind"] == artifacts.KIND_FLASHCARD_DECK
    assert row["cards_per_topic"] == 5
    assert json.loads(row["source_ids"]) == [document_id]


# ---------------------------------------------------------------------------
# Exact source selection (PLA-291)
# ---------------------------------------------------------------------------


def test_a_mixed_ready_and_unready_selection_is_refused_naming_the_file(
    client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list
) -> None:
    ready = _document(db, class_id, filename="ready.pdf", state="ready")
    pending = _document(db, class_id, filename="pending.pdf", state="pending")

    response = client.post(
        f"/api/classes/{class_id}/quizzes",
        json={"title": "Quiz", "document_ids": [ready, pending]},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "pending.pdf" in detail
    assert "ready.pdf" not in detail
    # No artifact, no job, nothing queued.
    assert no_worker == []
    assert db.execute("select count(*) from artifacts").fetchone()[0] == 0


def test_a_failed_source_reason_is_named(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    ready = _document(db, class_id, filename="ready.pdf", state="ready")
    failed = _document(db, class_id, filename="broken.pdf", state="failed")

    response = client.post(
        f"/api/classes/{class_id}/decks",
        json={"title": "Deck", "document_ids": [ready, failed]},
    )

    assert response.status_code == 409
    assert "broken.pdf failed to process" in response.json()["detail"]


def test_duplicate_source_ids_are_normalized_to_one_source(
    client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list
) -> None:
    document_id = _document(db, class_id)

    response = client.post(
        f"/api/classes/{class_id}/decks",
        json={"title": "Deck", "document_ids": [document_id, document_id]},
    )

    assert response.status_code == 202
    artifact_id = response.json()["id"]
    sources = artifacts.list_sources(db, artifact_id, artifacts.STUDY_SOURCE)
    assert [source["document_id"] for source in sources] == [document_id]
    assert no_worker[0].source_ids == (document_id,)


# ---------------------------------------------------------------------------
# Quiz attempt lifecycle (PLA-277)
# ---------------------------------------------------------------------------


def test_starting_an_attempt_on_a_pending_quiz_is_refused(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    quiz_id = _quiz(db, class_id, _document(db, class_id), state=artifacts.GENERATING)
    response = client.post(f"/api/quizzes/{quiz_id}/attempts")
    assert response.status_code == 409
    assert response.json()["detail"] == routes_study.QUIZ_NOT_READY_MESSAGE


def test_starting_an_attempt_on_a_ready_quiz_with_no_questions_is_refused(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    quiz_id = _quiz(db, class_id, _document(db, class_id), state=artifacts.READY)
    response = client.post(f"/api/quizzes/{quiz_id}/attempts")
    assert response.status_code == 409


def test_starting_an_attempt_on_a_partial_quiz_is_refused(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """A quiz that is ready but holds fewer questions than it claims cannot be attempted."""
    quiz_id = _quiz(db, class_id, _document(db, class_id), state=artifacts.READY)
    _question(db, quiz_id, 1, "delta")
    artifacts.set_problems_total(db, quiz_id, 5)  # claims five, holds one

    response = client.post(f"/api/quizzes/{quiz_id}/attempts")

    assert response.status_code == 409


def test_starting_an_attempt_is_idempotent_and_resumes(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    first = _question(db, quiz_id, 1, "delta")
    _question(db, quiz_id, 2, "convolution")

    started = client.post(f"/api/quizzes/{quiz_id}/attempts").json()
    client.post(
        f"/api/attempts/{started['attempt_id']}/answers",
        json={"part_id": first, "selected_index": 0},
    )
    # A second start returns the same attempt with the answer already recorded; a legacy
    # choice body that carries no text is still graded, recording the option it chose.
    resumed = client.post(f"/api/quizzes/{quiz_id}/attempts").json()
    assert resumed["attempt_id"] == started["attempt_id"]
    assert resumed["question_count"] == 2
    assert resumed["answers"] == [
        {
            "part_id": first,
            "selected_index": 0,
            "correct": True,
            "uncertain": False,
            "response_text": "sifting",
        }
    ]
    # Exactly one attempt exists.
    assert db.execute("select count(*) from quiz_attempts").fetchone()[0] == 1


def test_current_attempt_read_surface_hides_unearned_keys(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    first = _question(db, quiz_id, 1, "delta")
    second = _question(db, quiz_id, 2, "convolution")
    started = client.post(f"/api/quizzes/{quiz_id}/attempts").json()
    client.post(
        f"/api/attempts/{started['attempt_id']}/answers",
        json={"part_id": first, "selected_index": 0},
    )

    current = client.get(f"/api/quizzes/{quiz_id}/attempts/current").json()

    assert current["attempt"]["attempt_id"] == started["attempt_id"]
    assert current["attempt"]["question_part_ids"] == [first, second]
    # Only the answered question is reported, and no answer key rides along.
    assert current["attempt"]["answers"] == [
        {
            "part_id": first,
            "selected_index": 0,
            "correct": True,
            "uncertain": False,
            "response_text": "sifting",
        }
    ]
    assert "correct_index" not in json.dumps(current["attempt"])


def test_current_attempt_is_none_when_the_quiz_changed(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """An attempt whose quiz was regenerated is not offered for resume, so an answer never
    attaches to a different question set."""
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    first = _question(db, quiz_id, 1, "delta")
    client.post(f"/api/quizzes/{quiz_id}/attempts")
    # The quiz's questions are replaced (a regeneration): the snapshot no longer matches.
    artifacts.delete_part(db, first)
    _question(db, quiz_id, 1, "fresh")

    current = client.get(f"/api/quizzes/{quiz_id}/attempts/current").json()

    assert current["attempt"] is None


def test_start_retires_a_stale_attempt_and_snapshots_regenerated_questions(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    old_question = _question(db, quiz_id, 1, "delta")
    old_attempt = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    artifacts.delete_part(db, old_question)
    fresh_question = _question(db, quiz_id, 1, "fresh")

    resumed = client.post(f"/api/quizzes/{quiz_id}/attempts").json()

    assert resumed["attempt_id"] != old_attempt
    assert resumed["question_part_ids"] == [fresh_question]
    stale = db.execute(
        "select abandoned, finished_at from quiz_attempts where id = ?", (old_attempt,)
    ).fetchone()
    assert stale["abandoned"] == 1
    assert stale["finished_at"] is not None


def test_restart_abandons_the_old_attempt_and_opens_a_fresh_one(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    _question(db, quiz_id, 1, "delta")
    first = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]

    second = client.post(f"/api/quizzes/{quiz_id}/attempts?restart=true").json()["attempt_id"]

    assert second != first
    old = db.execute(
        "select abandoned, finished_at from quiz_attempts where id = ?", (first,)
    ).fetchone()
    assert old["abandoned"] == 1
    assert old["finished_at"] is not None
    # The old attempt is retained, not deleted.
    assert db.execute("select count(*) from quiz_attempts").fetchone()[0] == 2


def test_finish_is_idempotent_and_uses_the_full_question_count(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    first = _question(db, quiz_id, 1, "delta")
    _question(db, quiz_id, 2, "convolution")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={"part_id": first, "selected_index": 0},
    )

    # Only one of two questions answered: the denominator is still the full count.
    first_finish = client.post(f"/api/attempts/{attempt_id}/finish").json()
    assert first_finish["score"] == 1
    assert first_finish["total"] == 2
    assert first_finish["unresolved"] == 0
    assert first_finish["answered"] == 1

    # A retried finish returns the same stored result without double-counting.
    second_finish = client.post(f"/api/attempts/{attempt_id}/finish").json()
    assert second_finish == first_finish
    finished_rows = db.execute(
        "select count(*) from quiz_attempts where id = ? and finished_at is not null",
        (attempt_id,),
    ).fetchone()[0]
    assert finished_rows == 1


# ---------------------------------------------------------------------------
# Free-response answer grading (PLA-496)
# ---------------------------------------------------------------------------


def _fill_question(
    db: sqlite3.Connection, artifact_id: int, ordinal: int, reference: str, topic: str
) -> int:
    """A fill-blank question whose one option is the reference free response."""
    return artifacts.create_part(
        db,
        artifact_id,
        artifacts.QUIZ_QUESTION,
        ordinal,
        label=topic,
        content=json.dumps(
            {
                "type": "fill_blank",
                "question": "Express the angular sampling frequency as ___ .",
                "options": [reference],
                "correct_index": 0,
                "explanation": "The angular sampling frequency.",
                "topic": topic,
                "difficulty": "intermediate",
            }
        ),
        content_type=artifacts.JSON,
        status=artifacts.PART_COMPLETE,
    )


def test_an_equivalent_free_response_is_graded_not_string_matched(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """`2pi/Ts` is semantically the same expression as `2π/T_s` and must grade correct."""
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "2π/T_s", "sampling")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]

    response = client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={"part_id": part_id, "selected_index": -1, "response_text": "2pi/Ts"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["correct"] is True
    assert body["uncertain"] is False
    # The student's own words and the verdict persist beside the answer.
    row = db.execute(
        "select response_text, verdict, correct, grading_version from quiz_answers "
        "where attempt_id = ? and part_id = ?",
        (attempt_id, part_id),
    ).fetchone()
    assert row["response_text"] == "2pi/Ts"
    assert row["verdict"] == "correct"
    assert row["correct"] == 1
    assert row["grading_version"] == grading.GRADING_VERSION


def test_a_free_response_no_layer_settles_is_uncertain_not_wrong(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """With no endpoint configured the judge cannot run; an unsettled conceptual answer
    records `uncertain`, never a confident wrong, and keeps the student's words."""
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(
        db, quiz_id, 1, "the conversion of light into chemical energy", "biology"
    )
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]

    response = client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={
            "part_id": part_id,
            "selected_index": -1,
            "response_text": "plants making food from sunlight",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["correct"] is False
    assert body["uncertain"] is True
    row = db.execute(
        "select response_text, verdict, correct from quiz_answers where part_id = ?",
        (part_id,),
    ).fetchone()
    assert row["response_text"] == "plants making food from sunlight"
    assert row["verdict"] == "uncertain"
    assert row["correct"] == 0
    # The original response survives reopening the attempt.
    current = client.get(f"/api/quizzes/{quiz_id}/attempts/current").json()["attempt"]
    assert current["answers"][0]["response_text"] == "plants making food from sunlight"
    assert current["answers"][0]["uncertain"] is True
    # An unsettled answer is not a confident wrong in the score either: it is reported
    # separately, and the settled total excludes it.
    finished = client.post(f"/api/attempts/{attempt_id}/finish").json()
    assert finished["score"] == 0
    assert finished["total"] == 1
    assert finished["unresolved"] == 1
    assert finished["answered"] == 1
    assert finished["by_topic"] == [{"topic": "biology", "correct": 0, "total": 0, "unresolved": 1}]


def test_a_resubmitted_free_response_replays_its_stored_result(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry of the same submission reuses the stored result rather than charging for
    another semantic judgment; a different response regrades and updates the row."""
    calls: list[str] = []

    def judge(
        *,
        question: str,
        rubric: dict[str, object] | None,
        reference: str,
        response: str,
    ) -> grading.GradingResult:
        calls.append(response)
        return grading.GradingResult(
            grading.VERDICT_CORRECT, {"grader": "judge-fixture", "reason": response}
        )

    monkeypatch.setattr(routes_study, "_judge_for", lambda conn: judge)
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    endpoint = f"/api/attempts/{attempt_id}/answers"
    body = {"part_id": part_id, "selected_index": -1}

    first = client.post(endpoint, json={**body, "response_text": "the carbon oxidation cycle"})
    replay = client.post(endpoint, json={**body, "response_text": "the carbon oxidation cycle"})
    regraded = client.post(endpoint, json={**body, "response_text": "a mitochondrial cycle"})

    assert first.status_code == replay.status_code == regraded.status_code == 200
    assert first.json() == replay.json()
    assert calls == ["the carbon oxidation cycle", "a mitochondrial cycle"]
    rows = db.execute("select response_text, verdict from quiz_answers").fetchall()
    assert len(rows) == 1
    assert rows[0]["response_text"] == "a mitochondrial cycle"
    assert rows[0]["verdict"] == "correct"


def test_a_restart_while_a_judgment_is_in_flight_cannot_write_to_the_new_attempt(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result that lands late after a restart must not contaminate the new attempt: the
    old attempt is gone, so the publish is refused and no answer row is written."""
    entered = threading.Event()
    release = threading.Event()

    def judge(
        *,
        question: str,
        rubric: dict[str, object] | None,
        reference: str,
        response: str,
    ) -> grading.GradingResult:
        entered.set()
        assert release.wait(timeout=15)
        return grading.GradingResult(grading.VERDICT_CORRECT, {"grader": "judge-fixture"})

    monkeypatch.setattr(routes_study, "_judge_for", lambda conn: judge)
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    endpoint = f"/api/attempts/{attempt_id}/answers"

    outcome: dict[str, object] = {}

    def submit() -> None:
        outcome["response"] = client.post(
            endpoint,
            json={
                "part_id": part_id,
                "selected_index": -1,
                "response_text": "the carbon oxidation cycle",
            },
        )

    worker = threading.Thread(target=submit)
    worker.start()
    assert entered.wait(timeout=15)
    # The student restarts while the judgment is in flight: the old attempt is abandoned.
    fresh = client.post(f"/api/quizzes/{quiz_id}/attempts?restart=true").json()
    assert fresh["attempt_id"] != attempt_id
    release.set()
    worker.join(timeout=30)

    assert outcome["response"].status_code == 409  # type: ignore[union-attr]
    assert "changed" in str(outcome["response"].json()["detail"])  # type: ignore[union-attr]
    # The old attempt may hold its in-flight marker (the words were stored before the
    # judgment); no settled answer was written anywhere.
    rows = db.execute("select attempt_id, verdict, grading_version from quiz_answers").fetchall()
    assert len(rows) <= 1
    for row in rows:
        assert row["attempt_id"] == attempt_id
        assert row["verdict"] is None
        assert row["grading_version"] == grading.GRADING_VERSION


def test_a_legacy_fill_blank_answer_stays_readable(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """A row written before grading carried responses reads back with the original
    fields intact, a null response, and no fabricated verdict: a legacy fill-blank miss
    carried no recoverable words, so it reads as *unresolved*, never as a confident
    wrong, and stays retryable."""
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    # A pre-grading miss: the legacy -1 index, nothing else.
    db.execute(
        "insert into quiz_answers (attempt_id, part_id, selected_index, correct) "
        "values (?, ?, -1, 0)",
        (attempt_id, part_id),
    )
    db.commit()

    current = client.get(f"/api/quizzes/{quiz_id}/attempts/current").json()["attempt"]
    assert current["answers"] == [
        {
            "part_id": part_id,
            "selected_index": -1,
            "correct": False,
            "uncertain": True,
            "response_text": None,
        }
    ]
    # A fresh graded submission to the same question regrades and upgrades the row.
    response = client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={
            "part_id": part_id,
            "selected_index": -1,
            "response_text": "the carbon oxidation cycle",
        },
    )
    assert response.status_code == 200
    row = db.execute(
        "select response_text, verdict, grading_version from quiz_answers where part_id = ?",
        (part_id,),
    ).fetchone()
    assert row["response_text"] == "the carbon oxidation cycle"
    assert row["verdict"] == "uncertain"
    assert row["grading_version"] == grading.GRADING_VERSION


def test_an_answer_requires_the_response_text_contract(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """The layered grader needs the student's actual words: a typed answer without them
    is refused by the route (422) before any grading, while a choice answer may omit
    them entirely and still be accepted."""
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]

    response = client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={"part_id": part_id, "selected_index": -1},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "A typed answer needs the student's words."


def test_a_legacy_choice_client_omits_the_response_text(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """A choice body that predates `response_text` grades exactly as before, and the
    route records the chosen option's text rather than fabricating one."""
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _question(db, quiz_id, 1, "delta")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]

    response = client.post(
        f"/api/attempts/{attempt_id}/answers",
        json={"part_id": part_id, "selected_index": 2},
    )

    assert response.status_code == 200
    assert response.json()["correct"] is False
    row = db.execute(
        "select response_text, verdict from quiz_answers where part_id = ?", (part_id,)
    ).fetchone()
    assert row["response_text"] == "shifting"
    assert row["verdict"] == "incorrect"


def test_a_transient_judge_failure_can_be_retried_without_a_new_attempt(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An endpoint that is down records `uncertain` with the words stored, not a wrong;
    resubmitting the same answer regrades it in place until it settles."""
    calls: list[str] = []

    def judge(
        *,
        question: str,
        rubric: dict[str, object] | None,
        reference: str,
        response: str,
    ) -> grading.GradingResult:
        calls.append(response)
        if len(calls) == 1:
            raise RuntimeError("endpoint down")
        return grading.GradingResult(grading.VERDICT_CORRECT, {"grader": "judge-fixture"})

    monkeypatch.setattr(routes_study, "_judge_for", lambda conn: judge)
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    endpoint = f"/api/attempts/{attempt_id}/answers"
    body = {"part_id": part_id, "selected_index": -1, "response_text": "a carbon oxidation cycle"}

    first = client.post(endpoint, json=body)
    assert first.status_code == 200
    assert first.json()["uncertain"] is True
    row = db.execute("select verdict, correct, response_text from quiz_answers").fetchone()
    assert row["verdict"] == "uncertain"
    assert row["correct"] == 0
    assert row["response_text"] == "a carbon oxidation cycle"

    retry = client.post(endpoint, json=body)
    assert retry.status_code == 200
    assert retry.json()["correct"] is True
    assert retry.json()["uncertain"] is False
    assert calls == ["a carbon oxidation cycle", "a carbon oxidation cycle"]
    row = db.execute("select verdict, correct from quiz_answers").fetchone()
    assert row["verdict"] == "correct"
    assert row["correct"] == 1


def test_a_regenerated_question_regrades_an_identical_submission(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay is keyed on the question the verdict was earned against: the same text
    submitted against regenerated content is a fresh judgment, not a stale replay."""
    calls: list[str] = []

    def judge(
        *,
        question: str,
        rubric: dict[str, object] | None,
        reference: str,
        response: str,
    ) -> grading.GradingResult:
        calls.append(response)
        return grading.GradingResult(grading.VERDICT_CORRECT, {"grader": "judge-fixture"})

    monkeypatch.setattr(routes_study, "_judge_for", lambda conn: judge)
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    endpoint = f"/api/attempts/{attempt_id}/answers"
    body = {"part_id": part_id, "selected_index": -1, "response_text": "the carbon oxidation cycle"}

    first = client.post(endpoint, json=body)
    replay = client.post(endpoint, json=body)
    assert first.status_code == replay.status_code == 200
    assert calls == ["the carbon oxidation cycle"]

    # The question is regenerated: same text, new content, new judgment.
    db.execute(
        "update artifact_parts set content = ? where id = ?",
        (
            json.dumps(
                {
                    "type": "fill_blank",
                    "question": "Express the angular sampling frequency as ___ .",
                    "options": ["2pi/Ts"],
                    "correct_index": 0,
                    "explanation": "The angular sampling frequency.",
                    "topic": "sampling",
                    "difficulty": "intermediate",
                }
            ),
            part_id,
        ),
    )
    db.commit()

    regraded = client.post(endpoint, json=body)
    assert regraded.status_code == 200
    assert calls == ["the carbon oxidation cycle", "the carbon oxidation cycle"]


def test_simultaneous_identical_submissions_charge_one_judgment(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests for the same submission race to the judge: one grades, the other
    waits for the published result, so a retry storm cannot double-charge a judgment."""
    calls: list[str] = []

    def judge(
        *,
        question: str,
        rubric: dict[str, object] | None,
        reference: str,
        response: str,
    ) -> grading.GradingResult:
        calls.append(response)
        time.sleep(0.2)  # Keep the in-flight window open for the second request.
        return grading.GradingResult(grading.VERDICT_CORRECT, {"grader": "judge-fixture"})

    monkeypatch.setattr(routes_study, "_judge_for", lambda conn: judge)
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    endpoint = f"/api/attempts/{attempt_id}/answers"
    body = {"part_id": part_id, "selected_index": -1, "response_text": "the carbon oxidation cycle"}

    start = threading.Event()
    results: list[int] = []

    def submit() -> None:
        start.wait(timeout=15)
        results.append(client.post(endpoint, json=body).status_code)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=30)

    assert results == [200, 200]
    assert calls == ["the carbon oxidation cycle"]
    rows = db.execute("select verdict, response_text from quiz_answers").fetchall()
    assert len(rows) == 1
    assert rows[0]["verdict"] == "correct"


def test_an_older_submission_cannot_overwrite_a_newer_one(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow judgment of an older answer publishes into a row a newer answer already
    took: the older result is discarded (409) and the newer one stands."""
    entered = threading.Event()
    release = threading.Event()

    def judge(
        *,
        question: str,
        rubric: dict[str, object] | None,
        reference: str,
        response: str,
    ) -> grading.GradingResult:
        if response == "the older answer":
            entered.set()
            release.wait(timeout=20)
        return grading.GradingResult(grading.VERDICT_CORRECT, {"grader": "judge-fixture"})

    monkeypatch.setattr(routes_study, "_judge_for", lambda conn: judge)
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    endpoint = f"/api/attempts/{attempt_id}/answers"

    outcome: dict[str, object] = {}

    def submit_older() -> None:
        outcome["response"] = client.post(
            endpoint,
            json={"part_id": part_id, "selected_index": -1, "response_text": "the older answer"},
        )

    worker = threading.Thread(target=submit_older)
    worker.start()
    assert entered.wait(timeout=15)
    # The student improves the answer while the older judgment is still running.
    newer = client.post(
        endpoint,
        json={"part_id": part_id, "selected_index": -1, "response_text": "the newer answer"},
    )
    assert newer.status_code == 200
    release.set()
    worker.join(timeout=30)

    assert outcome["response"].status_code == 409  # type: ignore[union-attr]
    row = db.execute(
        "select response_text, verdict from quiz_answers where part_id = ?", (part_id,)
    ).fetchone()
    assert row["response_text"] == "the newer answer"
    assert row["verdict"] == "correct"


def test_a_duplicate_claiming_after_publish_replays_rather_than_regrades(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduling window: a duplicate read the row as absent before the first
    request's publish landed. Its claim then finds the row already settled and
    identical - it must not overwrite the settled result with a fresh marker and
    charge a second judgment; the wait returns the stored result instead."""
    calls: list[str] = []

    def judge(
        *,
        question: str,
        rubric: dict[str, object] | None,
        reference: str,
        response: str,
    ) -> grading.GradingResult:
        calls.append(response)
        return grading.GradingResult(
            grading.VERDICT_CORRECT, {"grader": "judge-fixture", "reason": response}
        )

    monkeypatch.setattr(routes_study, "_judge_for", lambda conn: judge)
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    endpoint = f"/api/attempts/{attempt_id}/answers"
    words = "the carbon oxidation cycle"
    body = {"part_id": part_id, "selected_index": -1, "response_text": words}

    # The duplicate's read: the row does not exist yet (it ran before the first
    # request's publish committed).
    assert (
        db.execute(
            "select * from quiz_answers where attempt_id = ? and part_id = ?",
            (attempt_id, part_id),
        ).fetchone()
        is None
    )

    # The first request grades and publishes through the real route.
    first = client.post(endpoint, json=body)
    assert first.status_code == 200
    settled = db.execute(
        "select * from quiz_answers where attempt_id = ? and part_id = ?",
        (attempt_id, part_id),
    ).fetchone()
    assert settled["verdict"] == "correct"
    settled_detail = settled["grade_detail"]

    # The duplicate's claim lands after the publish: it must not take the row.
    attempt = db.execute("select * from quiz_attempts where id = ?", (attempt_id,)).fetchone()
    part = artifacts.get_part(db, part_id)
    content = str(part["content"])
    claimed = routes_study._claim_in_flight(
        db, attempt, part_id, content, words, -1, supersede=True
    )
    assert claimed is None
    row = db.execute(
        "select * from quiz_answers where attempt_id = ? and part_id = ?",
        (attempt_id, part_id),
    ).fetchone()
    assert row["verdict"] == "correct"  # not overwritten with a NULL marker
    assert row["grade_detail"] == settled_detail  # the settled result stands intact

    # ...and the duplicate's wait returns the stored result without a second judgment.
    read = routes_study._wait_for_verdict(
        db, attempt, part_id, words, -1, json.loads(content), content
    )
    assert read is not None
    assert read["correct"] is True and read["uncertain"] is False
    assert calls == [words]  # still one judgment


def test_a_superseded_claim_cannot_publish_into_a_newer_claim(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ABA: A1's slow judgment, a superseding answer, then a newer claim carrying A1's
    own words again. Identical text and version no longer identify the claim - the
    claim token does: A1's late publish must not land in the newest claim's pending
    row, and the newest judgment is the one that stands."""
    a1_entered = threading.Event()
    a1_release = threading.Event()
    a2_entered = threading.Event()
    a2_release = threading.Event()
    calls: list[str] = []
    older_calls = 0

    def judge(
        *,
        question: str,
        rubric: dict[str, object] | None,
        reference: str,
        response: str,
    ) -> grading.GradingResult:
        nonlocal older_calls
        calls.append(response)
        if response == "the older answer":
            older_calls += 1
            if older_calls == 1:
                a1_entered.set()
                assert a1_release.wait(timeout=20)
            else:
                a2_entered.set()
                assert a2_release.wait(timeout=20)
        return grading.GradingResult(
            grading.VERDICT_CORRECT, {"grader": "judge-fixture", "order": len(calls)}
        )

    monkeypatch.setattr(routes_study, "_judge_for", lambda conn: judge)
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    endpoint = f"/api/attempts/{attempt_id}/answers"

    a1_outcome: dict[str, object] = {}
    a2_outcome: dict[str, object] = {}

    def submit_older() -> None:
        a1_outcome["response"] = client.post(
            endpoint,
            json={"part_id": part_id, "selected_index": -1, "response_text": "the older answer"},
        )

    def submit_older_again() -> None:
        a2_outcome["response"] = client.post(
            endpoint,
            json={"part_id": part_id, "selected_index": -1, "response_text": "the older answer"},
        )

    a1 = threading.Thread(target=submit_older)
    a1.start()
    assert a1_entered.wait(timeout=15)  # A1 claimed and is judging
    # A newer, distinct answer supersedes the in-flight claim.
    newer = client.post(
        endpoint,
        json={"part_id": part_id, "selected_index": -1, "response_text": "the newer answer"},
    )
    assert newer.status_code == 200
    # Then the student returns to the older wording: a fresh claim with A1's own text.
    a2 = threading.Thread(target=submit_older_again)
    a2.start()
    assert a2_entered.wait(timeout=15)  # A2 claimed the row and is judging

    # A1's late judgment now publishes: its text matches the pending row, but its
    # claim token does not - the stale owner's result is discarded.
    a1_release.set()
    a1.join(timeout=30)
    assert a1_outcome["response"].status_code == 409  # type: ignore[union-attr]
    # The newest judgment is the one that stands.
    a2_release.set()
    a2.join(timeout=30)
    assert a2_outcome["response"].status_code == 200  # type: ignore[union-attr]
    assert calls == ["the older answer", "the newer answer", "the older answer"]
    row = db.execute(
        "select response_text, verdict, grade_detail from quiz_answers where part_id = ?",
        (part_id,),
    ).fetchone()
    assert row["response_text"] == "the older answer"
    assert row["verdict"] == "correct"
    assert json.loads(str(row["grade_detail"]))["order"] == 3  # A2's judgment, not A1's


def test_a_stale_claim_is_reclaimed_and_the_dead_owner_cannot_publish(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-flight marker older than the bound has lost its owner. An identical
    duplicate reclaims it, and the dead owner's late judgment cannot publish into the
    reclaiming claim - again the token, not the matching text, decides."""
    a1_entered = threading.Event()
    a1_release = threading.Event()
    a2_entered = threading.Event()
    a2_release = threading.Event()
    calls: list[str] = []

    def judge(
        *,
        question: str,
        rubric: dict[str, object] | None,
        reference: str,
        response: str,
    ) -> grading.GradingResult:
        calls.append(response)
        if len(calls) == 1:
            a1_entered.set()
            assert a1_release.wait(timeout=20)
        else:
            a2_entered.set()
            assert a2_release.wait(timeout=20)
        return grading.GradingResult(
            grading.VERDICT_CORRECT, {"grader": "judge-fixture", "order": len(calls)}
        )

    monkeypatch.setattr(routes_study, "_judge_for", lambda conn: judge)
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    endpoint = f"/api/attempts/{attempt_id}/answers"

    a1_outcome: dict[str, object] = {}
    a2_outcome: dict[str, object] = {}

    def submit() -> dict[str, object]:
        return client.post(
            endpoint,
            json={"part_id": part_id, "selected_index": -1, "response_text": "the same words"},
        )

    a1 = threading.Thread(target=lambda: a1_outcome.update(response=submit()))
    a1.start()
    assert a1_entered.wait(timeout=15)  # A1 claimed and is judging
    # The owner vanished: the marker is now older than the staleness bound.
    db.execute(
        "update quiz_answers set answered_at = datetime('now', '-190 seconds') "
        "where attempt_id = ? and part_id = ?",
        (attempt_id, part_id),
    )
    db.commit()
    # An identical duplicate arrives and reclaims the dead claim.
    a2 = threading.Thread(target=lambda: a2_outcome.update(response=submit()))
    a2.start()
    assert a2_entered.wait(timeout=15)  # A2 holds the reclaimed claim and is judging

    # The dead owner's judgment now lands: same text, same version, wrong token.
    a1_release.set()
    a1.join(timeout=30)
    assert a1_outcome["response"].status_code == 409  # type: ignore[union-attr]
    a2_release.set()
    a2.join(timeout=30)
    assert a2_outcome["response"].status_code == 200  # type: ignore[union-attr]
    row = db.execute(
        "select verdict, grade_detail from quiz_answers where part_id = ?", (part_id,)
    ).fetchone()
    assert row["verdict"] == "correct"
    assert json.loads(str(row["grade_detail"]))["order"] == 2  # A2's judgment stands


def test_a_fresh_claim_cannot_be_stolen_by_a_waiting_duplicate(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """A reclaim never takes a live claim: while the owner's marker is fresh, an
    identical duplicate's reclaim is refused (the route turns it into a conflict)
    rather than overwriting the in-flight judgment."""
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    attempt = db.execute("select * from quiz_attempts where id = ?", (attempt_id,)).fetchone()
    content = str(artifacts.get_part(db, part_id)["content"])

    owner = routes_study._claim_in_flight(
        db, attempt, part_id, content, "words", -1, supersede=True
    )
    assert owner is not None
    steal = routes_study._claim_in_flight(
        db, attempt, part_id, content, "words", -1, supersede=False
    )
    assert steal is None
    row = db.execute(
        "select verdict, grade_detail from quiz_answers where attempt_id = ? and part_id = ?",
        (attempt_id, part_id),
    ).fetchone()
    assert row["verdict"] is None
    # The owner's token is untouched: its publish will still succeed.
    assert json.loads(str(row["grade_detail"]))["claim_token"] == owner


def test_a_waiting_duplicate_shares_a_failed_judgment_end_to_end(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate that enters its wait while the first judgment is still in flight
    shares that judgment's terminal result when it fails to the uncertain floor -
    one provider call, both clients see the same unsettled result."""
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def judge(
        *,
        question: str,
        rubric: dict[str, object] | None,
        reference: str,
        response: str,
    ) -> grading.GradingResult:
        calls.append(response)
        assert len(calls) == 1  # a second judgment would be a contract violation
        entered.set()
        assert release.wait(timeout=20)
        raise RuntimeError("the provider is down")

    monkeypatch.setattr(routes_study, "_judge_for", lambda conn: judge)
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    endpoint = f"/api/attempts/{attempt_id}/answers"
    body = {"part_id": part_id, "selected_index": -1, "response_text": "a carbon cycle"}

    outcomes: list[object] = []

    def submit() -> None:
        outcomes.append(client.post(endpoint, json=body))

    a = threading.Thread(target=submit)
    a.start()
    assert entered.wait(timeout=15)  # the first judgment is in flight
    # The duplicate arrives while the first is still judging: it waits.
    b = threading.Thread(target=submit)
    b.start()
    time.sleep(0.6)  # the duplicate has entered its wait by now
    release.set()  # the first judgment fails and publishes its uncertain result
    a.join(timeout=30)
    b.join(timeout=30)

    assert [outcome.status_code for outcome in outcomes] == [200, 200]
    assert calls == ["a carbon cycle"]  # the failed judgment ran once
    row = db.execute(
        "select verdict from quiz_answers where attempt_id = ? and part_id = ?",
        (attempt_id, part_id),
    ).fetchone()
    assert row["verdict"] == "uncertain"


def test_a_waiting_duplicate_shares_the_uncertain_terminal_result(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """The scheduling contract itself, deterministically: a duplicate that read the
    in-flight marker must not take a row whose only terminal state is `uncertain`;
    its wait returns that terminal result (uncertain included), and a later deliberate
    retry - one that saw the row already settled - is the only path that regrades."""
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    attempt = db.execute("select * from quiz_attempts where id = ?", (attempt_id,)).fetchone()
    part = artifacts.get_part(db, part_id)
    content = str(part["content"])
    words = "a carbon cycle"

    # The owner's claim: in flight.
    owner_token = routes_study._claim_in_flight(
        db, attempt, part_id, content, words, -1, supersede=True
    )
    assert owner_token is not None
    # The duplicate reads the in-flight marker...
    seen = db.execute(
        "select * from quiz_answers where attempt_id = ? and part_id = ?",
        (attempt_id, part_id),
    ).fetchone()
    assert seen is not None and seen["verdict"] is None
    # ...and the owner's judgment fails and publishes its uncertain terminal result
    # before the duplicate's claim runs.
    detail = json.dumps(
        {"question_digest": grading.question_digest(content), "grader": "judge-fixture"},
        ensure_ascii=False,
    )
    db.execute(
        "update quiz_answers set verdict = 'uncertain', correct = 0, grade_detail = ?, "
        "grading_version = ?, answered_at = datetime('now') "
        "where attempt_id = ? and part_id = ?",
        (detail, grading.GRADING_VERSION, attempt_id, part_id),
    )
    db.commit()
    # The duplicate's claim must not take the row: it read no settled result, so it is
    # a duplicate of the judgment that just published, not a deliberate retry.
    claimed = routes_study._claim_in_flight(
        db, attempt, part_id, content, words, -1, supersede=True, previously_seen=seen
    )
    assert claimed is None
    # The duplicate's wait shares the terminal result: uncertain, not a regrade.
    read = routes_study._wait_for_verdict(
        db, attempt, part_id, words, -1, json.loads(content), content
    )
    assert read is not None
    assert read["uncertain"] is True
    # A request that saw the row already settled is a deliberate retry: it regrades.
    settled_seen = db.execute(
        "select * from quiz_answers where attempt_id = ? and part_id = ?",
        (attempt_id, part_id),
    ).fetchone()
    retried = routes_study._claim_in_flight(
        db,
        attempt,
        part_id,
        content,
        words,
        -1,
        supersede=True,
        previously_seen=settled_seen,
    )
    assert retried is not None  # the retry takes the row to regrade


def test_a_restart_stops_a_waiting_duplicate_promptly(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate waiting on an in-flight judgment must stop at once when the attempt
    is restarted (the owner's result would belong to an abandoned attempt), not sleep
    out the wait bound. The restart's publish is refused as well."""
    entered = threading.Event()
    release = threading.Event()

    def judge(
        *,
        question: str,
        rubric: dict[str, object] | None,
        reference: str,
        response: str,
    ) -> grading.GradingResult:
        entered.set()
        assert release.wait(timeout=20)
        return grading.GradingResult(grading.VERDICT_CORRECT, {"grader": "judge-fixture"})

    monkeypatch.setattr(routes_study, "_judge_for", lambda conn: judge)
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    endpoint = f"/api/attempts/{attempt_id}/answers"
    body = {"part_id": part_id, "selected_index": -1, "response_text": "the same words"}

    owner_outcome: dict[str, object] = {}
    waiter_outcome: dict[str, object] = {}
    waiter_elapsed: list[float] = []

    def owner() -> None:
        owner_outcome["response"] = client.post(endpoint, json=body)

    def waiter() -> None:
        started = time.monotonic()
        waiter_outcome["response"] = client.post(endpoint, json=body)
        waiter_elapsed.append(time.monotonic() - started)

    a = threading.Thread(target=owner)
    a.start()
    assert entered.wait(timeout=15)  # the owner claimed and is judging
    b = threading.Thread(target=waiter)
    b.start()
    time.sleep(0.6)  # the duplicate has claimed the wait by now
    fresh = client.post(f"/api/quizzes/{quiz_id}/attempts?restart=true").json()
    assert fresh["attempt_id"] != attempt_id  # the waiting attempt is abandoned

    b.join(timeout=30)
    release.set()
    a.join(timeout=30)

    assert waiter_outcome["response"].status_code == 409  # type: ignore[union-attr]
    assert waiter_elapsed[0] < 10  # stopped by the restart, not the 150s bound
    assert owner_outcome["response"].status_code == 409  # type: ignore[union-attr]


def test_a_regenerated_question_stops_a_waiting_duplicate_promptly(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate waiting on an in-flight judgment must stop at once when the
    question is regenerated under it: the owner's judgment is about different content,
    and neither it nor the wait can serve a result from the old question."""
    entered = threading.Event()
    release = threading.Event()

    def judge(
        *,
        question: str,
        rubric: dict[str, object] | None,
        reference: str,
        response: str,
    ) -> grading.GradingResult:
        entered.set()
        assert release.wait(timeout=20)
        return grading.GradingResult(grading.VERDICT_CORRECT, {"grader": "judge-fixture"})

    monkeypatch.setattr(routes_study, "_judge_for", lambda conn: judge)
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    endpoint = f"/api/attempts/{attempt_id}/answers"
    body = {"part_id": part_id, "selected_index": -1, "response_text": "the same words"}

    owner_outcome: dict[str, object] = {}
    waiter_outcome: dict[str, object] = {}
    waiter_elapsed: list[float] = []

    def owner() -> None:
        owner_outcome["response"] = client.post(endpoint, json=body)

    def waiter() -> None:
        started = time.monotonic()
        waiter_outcome["response"] = client.post(endpoint, json=body)
        waiter_elapsed.append(time.monotonic() - started)

    a = threading.Thread(target=owner)
    a.start()
    assert entered.wait(timeout=15)
    b = threading.Thread(target=waiter)
    b.start()
    time.sleep(0.6)  # the duplicate has claimed the wait by now
    # The question is regenerated in place: same stem, a different reference.
    part_row = db.execute("select content from artifact_parts where id = ?", (part_id,)).fetchone()
    stored = json.loads(str(part_row["content"]))
    stored["options"] = ["a renamed reference"]
    db.execute(
        "update artifact_parts set content = ? where id = ?",
        (json.dumps(stored), part_id),
    )
    db.commit()
    # The duplicate must surface promptly, and the owner's late result must be refused.
    b.join(timeout=30)
    release.set()
    a.join(timeout=30)

    assert waiter_outcome["response"].status_code == 409  # type: ignore[union-attr]
    assert waiter_elapsed[0] < 10
    assert owner_outcome["response"].status_code == 409  # type: ignore[union-attr]
    row = db.execute("select verdict from quiz_answers where part_id = ?", (part_id,)).fetchone()
    assert row["verdict"] is None  # nothing judged against the old content stands


def test_a_finish_stops_a_waiting_duplicate_promptly(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finishing the attempt while a duplicate waits: the wait stops at once (the
    attempt no longer has live answers to serve), and the in-flight owner's late
    publish is refused into a finished attempt."""
    entered = threading.Event()
    release = threading.Event()

    def judge(
        *,
        question: str,
        rubric: dict[str, object] | None,
        reference: str,
        response: str,
    ) -> grading.GradingResult:
        entered.set()
        assert release.wait(timeout=20)
        return grading.GradingResult(grading.VERDICT_CORRECT, {"grader": "judge-fixture"})

    monkeypatch.setattr(routes_study, "_judge_for", lambda conn: judge)
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    endpoint = f"/api/attempts/{attempt_id}/answers"
    body = {"part_id": part_id, "selected_index": -1, "response_text": "the same words"}

    owner_outcome: dict[str, object] = {}
    waiter_outcome: dict[str, object] = {}
    waiter_elapsed: list[float] = []

    def owner() -> None:
        owner_outcome["response"] = client.post(endpoint, json=body)

    def waiter() -> None:
        started = time.monotonic()
        waiter_outcome["response"] = client.post(endpoint, json=body)
        waiter_elapsed.append(time.monotonic() - started)

    a = threading.Thread(target=owner)
    a.start()
    assert entered.wait(timeout=15)
    b = threading.Thread(target=waiter)
    b.start()
    time.sleep(0.6)  # the duplicate has claimed the wait by now
    finished = client.post(f"/api/attempts/{attempt_id}/finish").json()
    # The in-flight judgment is unresolved at finish time, not a wrong answer.
    assert finished["unresolved"] == 1
    assert finished["score"] == 0

    b.join(timeout=30)
    release.set()
    a.join(timeout=30)

    assert waiter_outcome["response"].status_code == 409  # type: ignore[union-attr]
    assert waiter_elapsed[0] < 10
    assert owner_outcome["response"].status_code == 409  # type: ignore[union-attr]


def test_a_replay_never_survives_its_attempt(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replay fast path serves a stored result: it may only do so for a live
    attempt. After a restart the old attempt is abandoned, and an identical resubmission
    to it is a conflict, not a replay."""
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(db, quiz_id, 1, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    endpoint = f"/api/attempts/{attempt_id}/answers"
    body = {"part_id": part_id, "selected_index": -1, "response_text": "the same words"}

    first = client.post(endpoint, json=body)
    assert first.status_code == 200
    fresh = client.post(f"/api/quizzes/{quiz_id}/attempts?restart=true").json()
    assert fresh["attempt_id"] != attempt_id
    replay = client.post(endpoint, json=body)
    assert replay.status_code == 409  # the settled result is never replayed into a dead attempt


def test_an_unresolved_answer_is_reported_not_wrong_at_finish(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """Finishing with an unsettled judgment reports it separately: not in the score,
    not in the settled totals, not in the topic weakness."""
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    first = _fill_question(
        db, quiz_id, 1, "the conversion of light into chemical energy", "biology"
    )
    second = _fill_question(db, quiz_id, 2, "the Krebs cycle", "biochemistry")
    attempt_id = client.post(f"/api/quizzes/{quiz_id}/attempts").json()["attempt_id"]
    for part_id, words in ((first, "plants making food from sunlight"), (second, "a cycle")):
        response = client.post(
            f"/api/attempts/{attempt_id}/answers",
            json={"part_id": part_id, "selected_index": -1, "response_text": words},
        )
        assert response.status_code == 200
        assert response.json()["uncertain"] is True

    finished = client.post(f"/api/attempts/{attempt_id}/finish").json()
    assert finished["score"] == 0
    assert finished["total"] == 2
    assert finished["unresolved"] == 2
    assert finished["answered"] == 2
    assert finished["by_topic"] == [
        {"topic": "biochemistry", "correct": 0, "total": 0, "unresolved": 1},
        {"topic": "biology", "correct": 0, "total": 0, "unresolved": 1},
    ]


def test_reading_a_quiz_never_carries_the_grading_contract(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """The hidden grading contract stays in the store: the quiz read shape never
    exposes it, so a client cannot see or steer the grader's hidden reference."""
    quiz_id = _quiz(db, class_id, _document(db, class_id))
    part_id = _fill_question(
        db,
        quiz_id,
        1,
        "the Krebs cycle",
        "biochemistry",
    )
    stored = json.loads(
        str(
            db.execute("select content from artifact_parts where id = ?", (part_id,)).fetchone()[
                "content"
            ]
        )
    )
    stored["grading"] = {
        "answer_kind": "text",
        "tolerance": None,
        "units": None,
        "acceptable_alternatives": ["citric acid cycle"],
        "required_ideas": ["central carbon oxidation"],
        "common_misconceptions": [],
        "contradictions": [],
        "partial_understanding_accepted": False,
    }
    db.execute(
        "update artifact_parts set content = ? where id = ?",
        (json.dumps(stored), part_id),
    )
    db.commit()

    response = client.get(f"/api/quizzes/{quiz_id}")

    assert response.status_code == 200
    question = response.json()["questions"][0]
    assert question["question"]["options"] == ["the Krebs cycle"]
    assert "grading" not in question["question"]
    # The grader itself still sees the stored contract.
    assert "grading" in stored


# ---------------------------------------------------------------------------
# Flashcard review idempotency (PLA-296)
# ---------------------------------------------------------------------------


def test_a_repeated_review_operation_returns_the_stored_result_once(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    deck_id = _deck(db, class_id, _document(db, class_id))
    part_id = _card(db, deck_id)

    first = client.post(
        f"/api/cards/{part_id}/review", json={"rating": "good", "operation_id": "op-A"}
    )
    second = client.post(
        f"/api/cards/{part_id}/review", json={"rating": "good", "operation_id": "op-A"}
    )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    # One logical review: one log row, reps advanced exactly once.
    assert db.execute("select count(*) from card_review_log").fetchone()[0] == 1
    assert (
        db.execute("select reps from card_states where part_id = ?", (part_id,)).fetchone()[0] == 1
    )


def test_the_same_operation_id_on_two_cards_reviews_both(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    """The idempotency key is scoped to its card: reusing one id across two cards must not
    swallow the second review or return the first card's state."""
    deck_id = _deck(db, class_id, _document(db, class_id))
    first = _card(db, deck_id, 1)
    second = _card(db, deck_id, 2)

    a = client.post(f"/api/cards/{first}/review", json={"rating": "good", "operation_id": "shared"})
    b = client.post(
        f"/api/cards/{second}/review", json={"rating": "good", "operation_id": "shared"}
    )

    assert a.status_code == b.status_code == 200
    assert db.execute("select count(*) from card_review_log").fetchone()[0] == 2
    for part_id in (first, second):
        reps = db.execute("select reps from card_states where part_id = ?", (part_id,)).fetchone()[
            0
        ]
        assert reps == 1


def test_a_review_without_an_operation_id_is_rejected(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    deck_id = _deck(db, class_id, _document(db, class_id))
    part_id = _card(db, deck_id)

    response = client.post(f"/api/cards/{part_id}/review", json={"rating": "good"})

    assert response.status_code == 422


def test_review_operation_rejects_changed_rating_after_lost_acknowledgement(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    deck_id = _deck(db, class_id, document_id)
    part_id = _card(db, deck_id)
    endpoint = f"/api/cards/{part_id}/review"
    # The response is deliberately discarded by the client; the real route committed.
    committed = client.post(endpoint, json={"rating": "easy", "operation_id": "lost-ack"})
    assert committed.status_code == 200
    conflict = client.post(endpoint, json={"rating": "again", "operation_id": "lost-ack"})
    assert conflict.status_code == 409
    assert "different rating" in conflict.json()["detail"]
    replay = client.post(endpoint, json={"rating": "easy", "operation_id": "lost-ack"})
    assert replay.json() == committed.json()
    rows = db.execute("select rating from card_review_log where part_id = ?", (part_id,)).fetchall()
    assert [row["rating"] for row in rows] == ["easy"]
    assert replay.json()["reps"] == 1


def test_review_retry_after_failure_before_commit_preserves_one_review(
    client: TestClient, db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = _document(db, class_id)
    deck_id = _deck(db, class_id, document_id)
    part_id = _card(db, deck_id)
    endpoint = f"/api/cards/{part_id}/review"
    original = routes_study.scheduler.review

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("failure before commit")

    monkeypatch.setattr(routes_study.scheduler, "review", fail)
    with pytest.raises(RuntimeError, match="failure before commit"):
        client.post(endpoint, json={"rating": "easy", "operation_id": "before-commit"})
    assert db.execute("select count(*) from card_review_log").fetchone()[0] == 0
    monkeypatch.setattr(routes_study.scheduler, "review", original)
    saved = client.post(endpoint, json={"rating": "easy", "operation_id": "before-commit"})
    replay = client.post(endpoint, json={"rating": "easy", "operation_id": "before-commit"})
    assert saved.status_code == replay.status_code == 200
    assert saved.json() == replay.json()
    assert db.execute("select count(*) from card_review_log").fetchone()[0] == 1


def test_legacy_unkeyed_review_does_not_conflict_with_new_operation(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    deck_id = _deck(db, class_id, document_id)
    part_id = _card(db, deck_id)
    db.execute("insert into card_review_log (part_id, rating) values (?, 'again')", (part_id,))
    db.commit()
    response = client.post(
        f"/api/cards/{part_id}/review", json={"rating": "easy", "operation_id": "new-key"}
    )
    assert response.status_code == 200
    assert db.execute("select count(*) from card_review_log").fetchone()[0] == 2
