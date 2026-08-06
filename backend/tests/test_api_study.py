"""Contract tests for the study endpoints.

The worker is never started here: `study.enqueue` is stubbed so creating a deck or quiz
stays a pure write. The worker's behavior is test_study.py; this file is the HTTP
surface - status codes, guards, and the round-trips a session makes.
"""

import json
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.api import routes_study
from backend.core import artifacts, study
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
    assert "finished processing" in response.json()["detail"]


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

    response = client.post(f"/api/cards/{part_id}/review", json={"rating": "good"})

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

    response = client.post(f"/api/cards/{part_id}/review", json={"rating": "good"})

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
        "correct_index": 0,
        "explanation": "The sifting property.",
    }
    # Any index but the stored one is wrong; -1 is how a fill_blank miss arrives.
    wrong = client.post(
        f"/api/attempts/{attempt['attempt_id']}/answers",
        json={"part_id": second, "selected_index": -1},
    )
    assert wrong.json()["correct"] is False

    finished = client.post(f"/api/attempts/{attempt['attempt_id']}/finish")
    assert finished.status_code == 200
    body = finished.json()
    assert body["score"] == 1
    assert body["total"] == 2
    assert body["by_topic"] == [
        {"topic": "convolution", "correct": 0, "total": 1},
        {"topic": "delta", "correct": 1, "total": 1},
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
        f"/api/attempts/{attempt_id}/answers", json={"part_id": part_id, "selected_index": 3}
    )
    client.post(
        f"/api/attempts/{attempt_id}/answers", json={"part_id": part_id, "selected_index": 0}
    )

    rows = db.execute("select selected_index, correct from quiz_answers").fetchall()
    assert len(rows) == 1
    assert (rows[0]["selected_index"], rows[0]["correct"]) == (0, 1)


def test_an_answer_for_another_quizs_question_is_a_404(
    client: TestClient, db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    quiz_id = _quiz(db, class_id, document_id)
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
