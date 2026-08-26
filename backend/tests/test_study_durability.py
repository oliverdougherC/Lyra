"""Adversarial regression tests for the study durability tranche.

PLA-169: Atomic artifact + sources + job persistence.
PLA-291: Source class membership enforcement at worker execution.
PLA-312: Ready-state enforcement on content/session read routes.

These tests inject failure at the exact boundary each fix closes and verify
the system's response is the one the fix promises.
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
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def no_worker(monkeypatch: pytest.MonkeyPatch) -> list[study._Job]:
    queued: list[study._Job] = []
    monkeypatch.setattr(routes_study.study, "enqueue", queued.append)
    return queued


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
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
        "Test deck",
        [artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_FLASHCARD_DECK,
    )
    artifact_id = int(created["id"])
    if state != artifacts.PENDING:
        artifacts.set_artifact_state(db, artifact_id, state)
    return artifact_id


def _quiz(db: sqlite3.Connection, class_id: int, document_id: int, state: str = "ready") -> int:
    created = artifacts.create_artifact(
        db,
        class_id,
        "Test quiz",
        [artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_QUIZ,
    )
    artifact_id = int(created["id"])
    if state != artifacts.PENDING:
        artifacts.set_artifact_state(db, artifact_id, state)
    return artifact_id


def _card(db: sqlite3.Connection, artifact_id: int, ordinal: int = 1) -> int:
    part_id = artifacts.create_part(
        db,
        artifact_id,
        artifacts.CARD,
        ordinal,
        label="topic",
        content=json.dumps({"front": "Q", "back": "A", "topic": "topic"}),
        content_type=artifacts.JSON,
        status=artifacts.PART_COMPLETE,
    )
    db.execute("insert into card_states (part_id, due_at) values (?, datetime('now'))", (part_id,))
    db.commit()
    return part_id


def _question(db: sqlite3.Connection, artifact_id: int, ordinal: int) -> int:
    return artifacts.create_part(
        db,
        artifact_id,
        artifacts.QUIZ_QUESTION,
        ordinal,
        label="q",
        content=json.dumps({
            "type": "mcq",
            "question": "Q?",
            "options": ["a", "b", "c", "d"],
            "correct_index": 0,
            "explanation": "Because.",
            "topic": "t",
            "difficulty": "basic",
        }),
        content_type=artifacts.JSON,
        status=artifacts.PART_COMPLETE,
    )


# ---------------------------------------------------------------------------
# PLA-169: Atomic creation -- artifact + sources + job in one transaction
# ---------------------------------------------------------------------------


class TestAtomicCreation:
    """The artifact, its sources, and the study job land in one commit."""

    def test_deck_creation_persists_job_atomically(
        self, client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list
    ) -> None:
        doc_id = _document(db, class_id)
        resp = client.post(
            f"/api/classes/{class_id}/decks",
            json={"title": "Atomic deck", "document_ids": [doc_id]},
        )
        assert resp.status_code == 202
        artifact_id = resp.json()["id"]
        job_row = db.execute(
            "select * from study_jobs where artifact_id = ?", (artifact_id,)
        ).fetchone()
        assert job_row is not None
        assert job_row["kind"] == "flashcard_deck"
        source_row = db.execute(
            "select * from artifact_sources where artifact_id = ?", (artifact_id,)
        ).fetchone()
        assert source_row is not None
        assert int(source_row["document_id"]) == doc_id

    def test_quiz_creation_persists_job_atomically(
        self, client: TestClient, db: sqlite3.Connection, class_id: int, no_worker: list
    ) -> None:
        doc_id = _document(db, class_id)
        resp = client.post(
            f"/api/classes/{class_id}/quizzes",
            json={"title": "Atomic quiz", "document_ids": [doc_id], "count": 5},
        )
        assert resp.status_code == 202
        artifact_id = resp.json()["id"]
        job_row = db.execute(
            "select * from study_jobs where artifact_id = ?", (artifact_id,)
        ).fetchone()
        assert job_row is not None
        assert job_row["kind"] == "quiz"
        assert int(job_row["count"]) == 5

    def test_class_touched_in_same_commit(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        before = db.execute(
            "select last_active_at from classes where id = ?", (class_id,)
        ).fetchone()
        client.post(
            f"/api/classes/{class_id}/decks",
            json={"title": "Touch test", "document_ids": [doc_id]},
        )
        after = db.execute(
            "select last_active_at from classes where id = ?", (class_id,)
        ).fetchone()
        assert after["last_active_at"] is not None
        assert after["last_active_at"] >= (before["last_active_at"] or "")

    def test_job_source_ids_match_artifact_sources(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        d1 = _document(db, class_id, "a.pdf")
        d2 = _document(db, class_id, "b.pdf")
        resp = client.post(
            f"/api/classes/{class_id}/decks",
            json={"title": "Multi-source", "document_ids": [d1, d2]},
        )
        artifact_id = resp.json()["id"]
        job_row = db.execute(
            "select source_ids from study_jobs where artifact_id = ?", (artifact_id,)
        ).fetchone()
        persisted_ids = json.loads(job_row["source_ids"])
        source_rows = db.execute(
            "select document_id from artifact_sources where artifact_id = ? order by ordinal",
            (artifact_id,),
        ).fetchall()
        artifact_source_ids = [int(r["document_id"]) for r in source_rows]
        assert persisted_ids == artifact_source_ids


# ---------------------------------------------------------------------------
# PLA-291: Source class membership at worker execution
# ---------------------------------------------------------------------------


class TestSourceClassMembership:
    """A source moved to a different class after request acceptance is rejected."""

    def test_source_moved_to_another_class_is_rejected(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        other_class_id = int(
            db.execute(
                "insert into classes (name) values ('Other')"
            ).lastrowid or 0
        )
        db.commit()
        job = study._Job(artifact_id=0, source_ids=(doc_id,))
        db.execute("update documents set class_id = ? where id = ?", (other_class_id, doc_id))
        db.commit()
        with pytest.raises(LyraError, match="moved to a different class"):
            study._validate_sources(db, job, class_id)

    def test_source_in_correct_class_passes(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        job = study._Job(artifact_id=0, source_ids=(doc_id,))
        study._validate_sources(db, job, class_id)

    def test_deleted_source_is_rejected(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        job = study._Job(artifact_id=0, source_ids=(doc_id,))
        db.execute("delete from documents where id = ?", (doc_id,))
        db.commit()
        with pytest.raises(LyraError, match="removed"):
            study._validate_sources(db, job, class_id)

    def test_unready_source_is_rejected(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        job = study._Job(artifact_id=0, source_ids=(doc_id,))
        db.execute("update documents set state = 'failed' where id = ?", (doc_id,))
        db.commit()
        with pytest.raises(LyraError, match="failed to process"):
            study._validate_sources(db, job, class_id)

    def test_class_check_precedes_state_check(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        """A moved-and-failed doc reports the move, not the failure."""
        doc_id = _document(db, class_id)
        other_class_id = int(
            db.execute("insert into classes (name) values ('Other')").lastrowid or 0
        )
        db.execute(
            "update documents set class_id = ?, state = 'failed' where id = ?",
            (other_class_id, doc_id),
        )
        db.commit()
        job = study._Job(artifact_id=0, source_ids=(doc_id,))
        with pytest.raises(LyraError, match="moved to a different class"):
            study._validate_sources(db, job, class_id)


# ---------------------------------------------------------------------------
# PLA-312: Ready-state enforcement on content reads
# ---------------------------------------------------------------------------


class TestReadyStateEnforcement:
    """Content and session reads reject non-ready artifacts with 409."""

    @pytest.mark.parametrize("state", ["pending", "generating", "failed", "cancelled"])
    def test_read_deck_rejects_non_ready(
        self, client: TestClient, db: sqlite3.Connection, class_id: int, state: str
    ) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state=state)
        resp = client.get(f"/api/decks/{deck_id}")
        assert resp.status_code == 409

    @pytest.mark.parametrize("state", ["pending", "generating", "failed", "cancelled"])
    def test_read_deck_session_rejects_non_ready(
        self, client: TestClient, db: sqlite3.Connection, class_id: int, state: str
    ) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state=state)
        resp = client.get(f"/api/decks/{deck_id}/session")
        assert resp.status_code == 409

    @pytest.mark.parametrize("state", ["pending", "generating", "failed", "cancelled"])
    def test_read_quiz_rejects_non_ready(
        self, client: TestClient, db: sqlite3.Connection, class_id: int, state: str
    ) -> None:
        doc_id = _document(db, class_id)
        quiz_id = _quiz(db, class_id, doc_id, state=state)
        resp = client.get(f"/api/quizzes/{quiz_id}")
        assert resp.status_code == 409

    def test_read_deck_allows_ready(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state="ready")
        _card(db, deck_id)
        resp = client.get(f"/api/decks/{deck_id}")
        assert resp.status_code == 200

    def test_read_deck_session_allows_ready(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state="ready")
        _card(db, deck_id)
        resp = client.get(f"/api/decks/{deck_id}/session")
        assert resp.status_code == 200

    def test_read_quiz_allows_ready(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        quiz_id = _quiz(db, class_id, doc_id, state="ready")
        _question(db, quiz_id, 1)
        resp = client.get(f"/api/quizzes/{quiz_id}")
        assert resp.status_code == 200

    def test_cancelled_message_is_descriptive(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state="cancelled")
        resp = client.get(f"/api/decks/{deck_id}")
        assert resp.status_code == 409
        assert "cancelled" in resp.json()["detail"].lower()
