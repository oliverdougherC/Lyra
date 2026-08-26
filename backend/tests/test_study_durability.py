"""Adversarial regression tests for the study durability tranche.

PLA-169: Atomic artifact + sources + job persistence; failure injection at
         every boundary (pre-commit, post-commit/pre-enqueue, enqueue failure,
         restart reconciliation).
PLA-291: Source class membership enforcement at worker execution; artifact
         source snapshot consistency, unready/missing/moved/reordered sources.
PLA-305: Operation ID idempotency lifecycle (backend half).
PLA-312: Ready-state enforcement on content/session read routes.

These tests inject failure at the exact boundary each fix closes and verify
the system's response is the one the fix promises.
"""

import json
import sqlite3
import unittest.mock
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
        content=json.dumps(
            {
                "type": "mcq",
                "question": "Q?",
                "options": ["a", "b", "c", "d"],
                "correct_index": 0,
                "explanation": "Because.",
                "topic": "t",
                "difficulty": "basic",
            }
        ),
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

    def _artifact_with_sources(
        self, db: sqlite3.Connection, class_id: int, doc_ids: list[int]
    ) -> int:
        """Create an artifact with matching artifact_sources rows."""
        created = artifacts.create_artifact(
            db,
            class_id,
            "Test artifact",
            [artifacts.SourceSpec(document_id=d, role=artifacts.STUDY_SOURCE) for d in doc_ids],
            kind=artifacts.KIND_FLASHCARD_DECK,
        )
        return int(created["id"])

    def test_source_moved_to_another_class_is_rejected(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        artifact_id = self._artifact_with_sources(db, class_id, [doc_id])
        other_class_id = int(
            db.execute("insert into classes (name) values ('Other')").lastrowid or 0
        )
        db.commit()
        job = study._Job(artifact_id=artifact_id, source_ids=(doc_id,))
        db.execute("update documents set class_id = ? where id = ?", (other_class_id, doc_id))
        db.commit()
        with pytest.raises(LyraError, match="moved to a different class"):
            study._validate_sources(db, job, class_id)

    def test_source_in_correct_class_passes(self, db: sqlite3.Connection, class_id: int) -> None:
        doc_id = _document(db, class_id)
        artifact_id = self._artifact_with_sources(db, class_id, [doc_id])
        job = study._Job(artifact_id=artifact_id, source_ids=(doc_id,))
        study._validate_sources(db, job, class_id)

    def test_deleted_source_is_rejected(self, db: sqlite3.Connection, class_id: int) -> None:
        doc_id = _document(db, class_id)
        artifact_id = self._artifact_with_sources(db, class_id, [doc_id])
        job = study._Job(artifact_id=artifact_id, source_ids=(doc_id,))
        db.execute("delete from documents where id = ?", (doc_id,))
        db.commit()
        with pytest.raises(LyraError, match="no longer matches"):
            study._validate_sources(db, job, class_id)

    def test_unready_source_is_rejected(self, db: sqlite3.Connection, class_id: int) -> None:
        doc_id = _document(db, class_id)
        artifact_id = self._artifact_with_sources(db, class_id, [doc_id])
        job = study._Job(artifact_id=artifact_id, source_ids=(doc_id,))
        db.execute("update documents set state = 'failed' where id = ?", (doc_id,))
        db.commit()
        with pytest.raises(LyraError, match="failed to process"):
            study._validate_sources(db, job, class_id)

    def test_class_check_precedes_state_check(self, db: sqlite3.Connection, class_id: int) -> None:
        """A moved-and-failed doc reports the move, not the failure."""
        doc_id = _document(db, class_id)
        artifact_id = self._artifact_with_sources(db, class_id, [doc_id])
        other_class_id = int(
            db.execute("insert into classes (name) values ('Other')").lastrowid or 0
        )
        db.execute(
            "update documents set class_id = ?, state = 'failed' where id = ?",
            (other_class_id, doc_id),
        )
        db.commit()
        job = study._Job(artifact_id=artifact_id, source_ids=(doc_id,))
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

    def test_failed_message_is_descriptive(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state="failed")
        resp = client.get(f"/api/decks/{deck_id}")
        assert resp.status_code == 409
        assert "failed" in resp.json()["detail"].lower()

    def test_pending_message_is_descriptive(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state="pending")
        resp = client.get(f"/api/decks/{deck_id}")
        assert resp.status_code == 409
        assert "queued" in resp.json()["detail"].lower()

    def test_generating_quiz_session_rejects(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        quiz_id = _quiz(db, class_id, doc_id, state="generating")
        resp = client.get(f"/api/quizzes/{quiz_id}")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# PLA-169: Crash-consistency infrastructure
# ---------------------------------------------------------------------------


class _SimulatedCrash(Exception):
    """Controlled process crash at a transaction boundary."""


class _FailAtBoundary:
    """Connection proxy that injects _SimulatedCrash at a chosen write boundary.

    Delegates every operation to a real sqlite3.Connection. Reads pass through
    untouched. Writes are intercepted by SQL pattern so the crash lands at the
    exact point in _create_study_artifact's write sequence.
    """

    BEFORE_ANY_WRITE = "before_any_write"
    AFTER_ARTIFACT_INSERT = "after_artifact_insert"
    AFTER_FIRST_SOURCE = "after_first_source"
    AFTER_LATER_SOURCE = "after_later_source"
    BEFORE_STUDY_JOBS = "before_study_jobs"
    AFTER_STUDY_JOBS = "after_study_jobs"
    ON_COMMIT = "on_commit"
    AFTER_COMMIT = "after_commit"

    def __init__(self, real_conn: sqlite3.Connection, boundary: str) -> None:
        self._conn = real_conn
        self._boundary = boundary
        self._fired = False

    def _crash(self, label: str) -> None:
        self._fired = True
        raise _SimulatedCrash(label)

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        lower = sql.lower()
        is_write = lower.lstrip().startswith(("insert", "update"))
        if not self._fired and is_write:
            if self._boundary == self.BEFORE_ANY_WRITE:
                self._crash(f"before write: {sql[:50]}")
            if self._boundary == self.BEFORE_STUDY_JOBS and "study_jobs" in lower:
                self._crash("before study_jobs insert")
        result = self._conn.execute(sql, parameters)
        if not self._fired:
            if self._boundary == self.AFTER_ARTIFACT_INSERT and "insert into artifacts" in lower:
                self._crash("after artifact insert")
            if self._boundary == self.AFTER_STUDY_JOBS and "study_jobs" in lower:
                self._crash("after study_jobs insert")
        return result

    def executemany(self, sql: str, params_seq: object) -> sqlite3.Cursor:
        lower = sql.lower()
        if (
            not self._fired
            and "artifact_sources" in lower
            and self._boundary in (self.AFTER_FIRST_SOURCE, self.AFTER_LATER_SOURCE)
        ):
            params_list = list(params_seq)
            target = 0 if self._boundary == self.AFTER_FIRST_SOURCE else 1
            cursor = self._conn.cursor()
            for i, params in enumerate(params_list):
                cursor.execute(sql, params)
                if i == target:
                    self._crash(f"after source ordinal {i}")
            return cursor
        return self._conn.executemany(sql, params_seq)

    def commit(self) -> None:
        if not self._fired and self._boundary == self.ON_COMMIT:
            self._crash("on commit")
        self._conn.commit()
        if not self._fired and self._boundary == self.AFTER_COMMIT:
            self._crash("after successful commit")

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


def _assert_no_phantom_state(db: sqlite3.Connection, class_id: int) -> None:
    """Verify a crashed creation left no partial rows visible to a separate connection."""
    assert (
        db.execute(
            "select count(*) as n from artifacts where class_id = ?", (class_id,)
        ).fetchone()["n"]
        == 0
    )
    assert db.execute("select count(*) as n from artifact_sources").fetchone()["n"] == 0
    assert db.execute("select count(*) as n from study_jobs").fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# PLA-169: Failure injection at every boundary of atomic creation
# ---------------------------------------------------------------------------


class TestAtomicCreationFailureInjection:
    """Crash or exception at each step of _create_study_artifact leaves the DB
    consistent: either everything committed or nothing did."""

    def test_all_rows_land_in_same_transaction(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        """Artifact, sources, and job are all visible after one successful creation."""
        doc_id = _document(db, class_id)
        resp = client.post(
            f"/api/classes/{class_id}/decks",
            json={"title": "Atomic check", "document_ids": [doc_id]},
        )
        assert resp.status_code == 202
        artifact_id = resp.json()["id"]
        artifact = db.execute("select * from artifacts where id = ?", (artifact_id,)).fetchone()
        assert artifact is not None
        assert artifact["state"] == "pending"
        source = db.execute(
            "select * from artifact_sources where artifact_id = ?", (artifact_id,)
        ).fetchone()
        assert source is not None
        assert int(source["document_id"]) == doc_id
        job = db.execute(
            "select * from study_jobs where artifact_id = ?", (artifact_id,)
        ).fetchone()
        assert job is not None
        assert job["kind"] == "flashcard_deck"

    def test_enqueue_failure_still_returns_artifact(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        class_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When enqueue raises after the durable commit, the client still gets a
        202 with the artifact, and the durable intent (study_jobs row) is intact
        for the reconciler."""
        doc_id = _document(db, class_id)
        monkeypatch.setattr(
            routes_study.study,
            "enqueue",
            unittest.mock.Mock(side_effect=RuntimeError("queue full")),
        )
        resp = client.post(
            f"/api/classes/{class_id}/decks",
            json={"title": "Enqueue fail deck", "document_ids": [doc_id]},
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

    def test_separate_requests_after_enqueue_failure_are_independent(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        class_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two POST requests each produce their own artifact+job pair, even
        when the first request's in-memory enqueue fails. No request
        idempotency contract exists: each accepted request is independent."""
        doc_id = _document(db, class_id)
        call_count = 0

        def enqueue_first_fail(job: study._Job) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first enqueue fails")

        monkeypatch.setattr(routes_study.study, "enqueue", enqueue_first_fail)
        resp1 = client.post(
            f"/api/classes/{class_id}/decks",
            json={"title": "Retry deck", "document_ids": [doc_id]},
        )
        assert resp1.status_code == 202
        resp2 = client.post(
            f"/api/classes/{class_id}/decks",
            json={"title": "Retry deck 2", "document_ids": [doc_id]},
        )
        assert resp2.status_code == 202
        assert resp1.json()["id"] != resp2.json()["id"]
        jobs = db.execute("select count(*) as n from study_jobs").fetchone()["n"]
        assert jobs == 2

    def test_quiz_enqueue_failure_still_returns_quiz(
        self,
        client: TestClient,
        db: sqlite3.Connection,
        class_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        doc_id = _document(db, class_id)
        monkeypatch.setattr(
            routes_study.study, "enqueue", unittest.mock.Mock(side_effect=RuntimeError("boom"))
        )
        resp = client.post(
            f"/api/classes/{class_id}/quizzes",
            json={"title": "Enqueue fail quiz", "document_ids": [doc_id], "count": 5},
        )
        assert resp.status_code == 202
        artifact_id = resp.json()["id"]
        job_row = db.execute(
            "select * from study_jobs where artifact_id = ?", (artifact_id,)
        ).fetchone()
        assert job_row is not None
        assert job_row["kind"] == "quiz"
        assert int(job_row["count"]) == 5


# ---------------------------------------------------------------------------
# PLA-169: Crash consistency — deterministic failure injection at every
#          write boundary of _create_study_artifact
# ---------------------------------------------------------------------------


class TestCrashConsistency:
    """PLA-169 crash-consistency evidence.

    Each test wraps a real connection in _FailAtBoundary, calls
    _create_study_artifact with a proto-job, and verifies:

    - Pre-commit crash (cases 1-7): no artifact, no sources, no study_jobs
      row visible to the independent ``db`` fixture connection.
    - Post-commit crash (cases 8-9): artifact + sources + study_jobs are all
      committed and intact; reconciliation re-enqueues with the exact
      original options and source ordering.
    """

    def _crash_create(
        self,
        db: sqlite3.Connection,
        class_id: int,
        boundary: str,
        doc_ids: list[int],
        kind: str = artifacts.KIND_FLASHCARD_DECK,
        proto: study._Job | None = None,
    ) -> _FailAtBoundary:
        if proto is None:
            proto = study._Job(0, source_ids=(), cards_per_topic=4)
        real_conn = connect()
        proxy = _FailAtBoundary(real_conn, boundary)
        try:
            with pytest.raises(_SimulatedCrash):
                routes_study._create_study_artifact(
                    proxy,
                    class_id,
                    kind,
                    "Crash test",
                    doc_ids,
                    job=proto,
                )
        finally:
            real_conn.close()
        return proxy

    # -- Pre-commit failures (cases 1-7): nothing committed ----------------

    def test_crash_before_any_write(self, db: sqlite3.Connection, class_id: int) -> None:
        """1. Failure before any write leaves a clean database."""
        doc_id = _document(db, class_id)
        proxy = self._crash_create(db, class_id, _FailAtBoundary.BEFORE_ANY_WRITE, [doc_id])
        assert proxy._fired
        _assert_no_phantom_state(db, class_id)

    def test_crash_after_artifact_insert(self, db: sqlite3.Connection, class_id: int) -> None:
        """2. Failure after artifact INSERT but before sources."""
        doc_id = _document(db, class_id)
        proxy = self._crash_create(db, class_id, _FailAtBoundary.AFTER_ARTIFACT_INSERT, [doc_id])
        assert proxy._fired
        _assert_no_phantom_state(db, class_id)

    def test_crash_after_first_source_row(self, db: sqlite3.Connection, class_id: int) -> None:
        """3. Failure after the first artifact_sources row in a two-source request."""
        d1 = _document(db, class_id, "a.pdf")
        d2 = _document(db, class_id, "b.pdf")
        proxy = self._crash_create(db, class_id, _FailAtBoundary.AFTER_FIRST_SOURCE, [d1, d2])
        assert proxy._fired
        _assert_no_phantom_state(db, class_id)

    def test_crash_after_later_source_row(self, db: sqlite3.Connection, class_id: int) -> None:
        """4. Failure after the second source row in a three-source request."""
        d1 = _document(db, class_id, "a.pdf")
        d2 = _document(db, class_id, "b.pdf")
        d3 = _document(db, class_id, "c.pdf")
        proxy = self._crash_create(db, class_id, _FailAtBoundary.AFTER_LATER_SOURCE, [d1, d2, d3])
        assert proxy._fired
        _assert_no_phantom_state(db, class_id)

    def test_crash_after_sources_before_study_jobs(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        """5. Failure after all sources and class touch but before study_jobs INSERT."""
        doc_id = _document(db, class_id)
        proxy = self._crash_create(db, class_id, _FailAtBoundary.BEFORE_STUDY_JOBS, [doc_id])
        assert proxy._fired
        _assert_no_phantom_state(db, class_id)

    def test_crash_after_study_jobs_before_commit(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        """6. Failure after study_jobs INSERT but before commit."""
        doc_id = _document(db, class_id)
        proxy = self._crash_create(db, class_id, _FailAtBoundary.AFTER_STUDY_JOBS, [doc_id])
        assert proxy._fired
        _assert_no_phantom_state(db, class_id)

    def test_crash_on_commit(self, db: sqlite3.Connection, class_id: int) -> None:
        """7. Commit itself fails (raised before the real commit executes)."""
        doc_id = _document(db, class_id)
        proxy = self._crash_create(db, class_id, _FailAtBoundary.ON_COMMIT, [doc_id])
        assert proxy._fired
        _assert_no_phantom_state(db, class_id)

    # -- Post-commit failures (cases 8-9): durable state is complete -------

    def test_crash_after_commit_leaves_complete_durable_state(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        """8. Commit succeeded, process crashed before enqueue. The durable
        intent (artifact + sources + job) must be fully committed."""
        d1 = _document(db, class_id, "a.pdf")
        d2 = _document(db, class_id, "b.pdf")
        real_conn = connect()
        proxy = _FailAtBoundary(real_conn, _FailAtBoundary.AFTER_COMMIT)
        proto = study._Job(0, source_ids=(), cards_per_topic=7)
        try:
            with pytest.raises(_SimulatedCrash):
                routes_study._create_study_artifact(
                    proxy,
                    class_id,
                    artifacts.KIND_FLASHCARD_DECK,
                    "Durable test",
                    [d1, d2],
                    job=proto,
                )
        finally:
            real_conn.close()

        artifact = db.execute("select * from artifacts where class_id = ?", (class_id,)).fetchone()
        assert artifact is not None
        assert artifact["kind"] == "flashcard_deck"
        assert artifact["state"] == "pending"
        artifact_id = int(artifact["id"])

        sources = db.execute(
            "select document_id from artifact_sources where artifact_id = ? order by ordinal",
            (artifact_id,),
        ).fetchall()
        assert [int(r["document_id"]) for r in sources] == [d1, d2]

        job_row = db.execute(
            "select * from study_jobs where artifact_id = ?", (artifact_id,)
        ).fetchone()
        assert job_row is not None
        assert job_row["kind"] == "flashcard_deck"
        assert int(job_row["cards_per_topic"]) == 7
        assert json.loads(job_row["source_ids"]) == [d1, d2]

    def test_reconciliation_recovers_post_commit_crash(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        """9. Restart reconciliation from complete post-commit durable state
        re-enqueues the job exactly once with the original options."""
        d1 = _document(db, class_id, "x.pdf")
        d2 = _document(db, class_id, "y.pdf")
        real_conn = connect()
        proxy = _FailAtBoundary(real_conn, _FailAtBoundary.AFTER_COMMIT)
        proto = study._Job(
            0,
            source_ids=(),
            cards_per_topic=5,
            count=12,
            difficulty="exam",
            types=("mcq", "true_false"),
        )
        try:
            with pytest.raises(_SimulatedCrash):
                routes_study._create_study_artifact(
                    proxy,
                    class_id,
                    artifacts.KIND_QUIZ,
                    "Recover test",
                    [d1, d2],
                    job=proto,
                )
        finally:
            real_conn.close()

        queued: list[study._Job] = []
        with unittest.mock.patch.object(study, "enqueue", queued.append):
            requeued, failed = study.reconcile_interrupted(db)
        assert requeued == 1
        assert failed == 0
        assert len(queued) == 1
        recovered = queued[0]
        assert recovered.source_ids == (d1, d2)
        assert recovered.cards_per_topic == 5
        assert recovered.count == 12
        assert recovered.difficulty == "exam"
        assert recovered.types == ("mcq", "true_false")


# ---------------------------------------------------------------------------
# PLA-169: Restart reconciliation at each state
# ---------------------------------------------------------------------------


class TestReconciliation:
    """reconcile_interrupted recovers pending, restarts generating, and fails
    unrecoverable artifacts."""

    def test_pending_with_job_is_requeued(self, db: sqlite3.Connection, class_id: int) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state="pending")
        job = study._Job(artifact_id=deck_id, source_ids=(doc_id,))
        study.persist_job(db, job, "flashcard_deck")
        queued: list[study._Job] = []
        with unittest.mock.patch.object(study, "enqueue", queued.append):
            requeued, failed = study.reconcile_interrupted(db)
        assert requeued == 1
        assert failed == 0
        assert len(queued) == 1
        assert queued[0].artifact_id == deck_id
        assert queued[0].source_ids == (doc_id,)

    def test_generating_is_reset_to_pending_and_requeued(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state="generating")
        _card(db, deck_id, ordinal=1)
        job = study._Job(artifact_id=deck_id, source_ids=(doc_id,))
        study.persist_job(db, job, "flashcard_deck")
        queued: list[study._Job] = []
        with unittest.mock.patch.object(study, "enqueue", queued.append):
            requeued, failed = study.reconcile_interrupted(db)
        assert requeued == 1
        assert failed == 0
        after = artifacts.get_artifact(db, deck_id)
        assert after["state"] == "pending"
        parts = artifacts.list_parts(db, deck_id)
        assert len(parts) == 0

    def test_pending_without_job_is_failed(self, db: sqlite3.Connection, class_id: int) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state="pending")
        queued: list[study._Job] = []
        with unittest.mock.patch.object(study, "enqueue", queued.append):
            requeued, failed = study.reconcile_interrupted(db)
        assert requeued == 0
        assert failed == 1
        assert len(queued) == 0
        after = artifacts.get_artifact(db, deck_id)
        assert after["state"] == "failed"

    def test_generating_without_job_is_failed(self, db: sqlite3.Connection, class_id: int) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state="generating")
        queued: list[study._Job] = []
        with unittest.mock.patch.object(study, "enqueue", queued.append):
            requeued, failed = study.reconcile_interrupted(db)
        assert requeued == 0
        assert failed == 1
        after = artifacts.get_artifact(db, deck_id)
        assert after["state"] == "failed"

    def test_cancelled_is_never_reconciled(self, db: sqlite3.Connection, class_id: int) -> None:
        doc_id = _document(db, class_id)
        _deck(db, class_id, doc_id, state="cancelled")
        queued: list[study._Job] = []
        with unittest.mock.patch.object(study, "enqueue", queued.append):
            requeued, failed = study.reconcile_interrupted(db)
        assert requeued == 0
        assert failed == 0
        assert len(queued) == 0

    def test_ready_is_never_reconciled(self, db: sqlite3.Connection, class_id: int) -> None:
        doc_id = _document(db, class_id)
        _deck(db, class_id, doc_id, state="ready")
        queued: list[study._Job] = []
        with unittest.mock.patch.object(study, "enqueue", queued.append):
            requeued, failed = study.reconcile_interrupted(db)
        assert requeued == 0
        assert failed == 0

    def test_malformed_job_metadata_is_failed(self, db: sqlite3.Connection, class_id: int) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state="pending")
        db.execute(
            "insert into study_jobs "
            "(artifact_id, kind, cards_per_topic, count, difficulty, types, source_ids) "
            "values (?, 'flashcard_deck', 4, 10, 'intermediate', 'not-json', '[]')",
            (deck_id,),
        )
        db.commit()
        queued: list[study._Job] = []
        with unittest.mock.patch.object(study, "enqueue", queued.append):
            requeued, failed = study.reconcile_interrupted(db)
        assert requeued == 0
        assert failed == 1
        after = artifacts.get_artifact(db, deck_id)
        assert after["state"] == "failed"

    def test_reconciliation_preserves_job_options(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        quiz_id = _quiz(db, class_id, doc_id, state="pending")
        job = study._Job(
            artifact_id=quiz_id,
            source_ids=(doc_id,),
            count=15,
            difficulty="exam",
            types=("mcq", "true_false"),
        )
        study.persist_job(db, job, "quiz")
        queued: list[study._Job] = []
        with unittest.mock.patch.object(study, "enqueue", queued.append):
            study.reconcile_interrupted(db)
        assert len(queued) == 1
        recovered = queued[0]
        assert recovered.count == 15
        assert recovered.difficulty == "exam"
        assert recovered.types == ("mcq", "true_false")
        assert recovered.source_ids == (doc_id,)

    def test_multiple_artifacts_requeued_in_creation_order(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        d1 = _document(db, class_id, "a.pdf")
        d2 = _document(db, class_id, "b.pdf")
        deck1 = _deck(db, class_id, d1, state="pending")
        deck2 = _deck(db, class_id, d2, state="pending")
        study.persist_job(db, study._Job(artifact_id=deck1, source_ids=(d1,)), "flashcard_deck")
        study.persist_job(db, study._Job(artifact_id=deck2, source_ids=(d2,)), "flashcard_deck")
        queued: list[study._Job] = []
        with unittest.mock.patch.object(study, "enqueue", queued.append):
            study.reconcile_interrupted(db)
        assert len(queued) == 2
        assert queued[0].artifact_id < queued[1].artifact_id


# ---------------------------------------------------------------------------
# PLA-291: Source class membership – comprehensive coverage
# ---------------------------------------------------------------------------


class TestSourceMembershipComprehensive:
    """Every failure mode of _validate_sources at worker execution."""

    def _artifact_with_sources(
        self, db: sqlite3.Connection, class_id: int, doc_ids: list[int]
    ) -> int:
        """Create an artifact with matching artifact_sources rows."""
        created = artifacts.create_artifact(
            db,
            class_id,
            "Test artifact",
            [artifacts.SourceSpec(document_id=d, role=artifacts.STUDY_SOURCE) for d in doc_ids],
            kind=artifacts.KIND_FLASHCARD_DECK,
        )
        return int(created["id"])

    def test_artifact_sources_divergence_refuses_generation(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        """job.source_ids disagrees with artifact_sources rows -> rejected."""
        d1 = _document(db, class_id, "a.pdf")
        d2 = _document(db, class_id, "b.pdf")
        deck_id = _deck(db, class_id, d1, state="pending")
        job = study._Job(artifact_id=deck_id, source_ids=(d1, d2))
        with pytest.raises(LyraError, match="no longer matches"):
            study._validate_sources(db, job, class_id)

    def test_substituted_source_in_artifact_sources_is_caught(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        """artifact_sources has doc A, but job says doc B -> divergence."""
        d1 = _document(db, class_id, "a.pdf")
        d2 = _document(db, class_id, "b.pdf")
        deck_id = _deck(db, class_id, d1, state="pending")
        job = study._Job(artifact_id=deck_id, source_ids=(d2,))
        with pytest.raises(LyraError, match="no longer matches"):
            study._validate_sources(db, job, class_id)

    def test_reordered_sources_are_rejected(self, db: sqlite3.Connection, class_id: int) -> None:
        """Order matters: (d1, d2) != (d2, d1)."""
        d1 = _document(db, class_id, "a.pdf")
        d2 = _document(db, class_id, "b.pdf")
        artifact_id = self._artifact_with_sources(db, class_id, [d1, d2])
        job = study._Job(artifact_id=artifact_id, source_ids=(d2, d1))
        with pytest.raises(LyraError, match="no longer matches"):
            study._validate_sources(db, job, class_id)

    def test_matching_sources_pass_validation(self, db: sqlite3.Connection, class_id: int) -> None:
        """Happy path: job and artifact_sources agree in membership and order."""
        d1 = _document(db, class_id, "a.pdf")
        d2 = _document(db, class_id, "b.pdf")
        artifact_id = self._artifact_with_sources(db, class_id, [d1, d2])
        job = study._Job(artifact_id=artifact_id, source_ids=(d1, d2))
        study._validate_sources(db, job, class_id)

    def test_pending_source_is_rejected(self, db: sqlite3.Connection, class_id: int) -> None:
        doc_id = _document(db, class_id, state="pending")
        artifact_id = self._artifact_with_sources(db, class_id, [doc_id])
        job = study._Job(artifact_id=artifact_id, source_ids=(doc_id,))
        db.execute("update documents set state = 'pending' where id = ?", (doc_id,))
        db.commit()
        with pytest.raises(LyraError, match="still processing"):
            study._validate_sources(db, job, class_id)

    def test_unsupported_source_is_rejected(self, db: sqlite3.Connection, class_id: int) -> None:
        doc_id = _document(db, class_id)
        artifact_id = self._artifact_with_sources(db, class_id, [doc_id])
        job = study._Job(artifact_id=artifact_id, source_ids=(doc_id,))
        db.execute("update documents set state = 'unsupported' where id = ?", (doc_id,))
        db.commit()
        with pytest.raises(LyraError, match="could not be read"):
            study._validate_sources(db, job, class_id)

    def test_reingesting_source_is_rejected(self, db: sqlite3.Connection, class_id: int) -> None:
        doc_id = _document(db, class_id)
        artifact_id = self._artifact_with_sources(db, class_id, [doc_id])
        job = study._Job(artifact_id=artifact_id, source_ids=(doc_id,))
        db.execute("update documents set state = 'pending' where id = ?", (doc_id,))
        db.commit()
        with pytest.raises(LyraError, match="still processing"):
            study._validate_sources(db, job, class_id)

    def test_multiple_problems_are_all_reported(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        """Two bad sources name both in the error, not just the first."""
        d1 = _document(db, class_id, "bad1.pdf")
        d2 = _document(db, class_id, "bad2.pdf")
        artifact_id = self._artifact_with_sources(db, class_id, [d1, d2])
        job = study._Job(artifact_id=artifact_id, source_ids=(d1, d2))
        db.execute("update documents set state = 'failed' where id = ?", (d1,))
        db.execute("update documents set state = 'unsupported' where id = ?", (d2,))
        db.commit()
        with pytest.raises(LyraError) as exc_info:
            study._validate_sources(db, job, class_id)
        assert "bad1.pdf" in exc_info.value.message
        assert "bad2.pdf" in exc_info.value.message

    def test_deleted_mid_generation_is_detected(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        """Source deleted between creation and worker execution -> rejected.
        Deleting a document cascades to artifact_sources, so the snapshot
        divergence fires first."""
        d1 = _document(db, class_id, "alive.pdf")
        d2 = _document(db, class_id, "doomed.pdf")
        artifact_id = self._artifact_with_sources(db, class_id, [d1, d2])
        job = study._Job(artifact_id=artifact_id, source_ids=(d1, d2))
        db.execute("delete from documents where id = ?", (d2,))
        db.commit()
        with pytest.raises(LyraError, match="no longer matches"):
            study._validate_sources(db, job, class_id)

    def test_empty_sources_bypass_validation(self, db: sqlite3.Connection, class_id: int) -> None:
        """A job with no source_ids (whole-class) skips validation entirely."""
        job = study._Job(artifact_id=0, source_ids=())
        study._validate_sources(db, job, class_id)

    def test_class_check_precedes_unready_check(
        self, db: sqlite3.Connection, class_id: int
    ) -> None:
        """A source that moved AND failed reports the move, not the failure state."""
        doc_id = _document(db, class_id)
        artifact_id = self._artifact_with_sources(db, class_id, [doc_id])
        other_class = int(db.execute("insert into classes (name) values ('X')").lastrowid or 0)
        db.execute(
            "update documents set class_id = ?, state = 'unsupported' where id = ?",
            (other_class, doc_id),
        )
        db.commit()
        job = study._Job(artifact_id=artifact_id, source_ids=(doc_id,))
        with pytest.raises(LyraError, match="moved to a different class"):
            study._validate_sources(db, job, class_id)


# ---------------------------------------------------------------------------
# PLA-291: HTTP boundary validation (_study_sources)
# ---------------------------------------------------------------------------


class TestStudySourcesHTTPBoundary:
    """_study_sources validates at the HTTP boundary before creation."""

    def test_duplicate_document_ids_are_normalized(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        resp = client.post(
            f"/api/classes/{class_id}/decks",
            json={"title": "Dedup deck", "document_ids": [doc_id, doc_id]},
        )
        assert resp.status_code == 202
        artifact_id = resp.json()["id"]
        sources = db.execute(
            "select document_id from artifact_sources where artifact_id = ? order by ordinal",
            (artifact_id,),
        ).fetchall()
        assert len(sources) == 1

    def test_document_from_another_class_is_404(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        other_class = int(db.execute("insert into classes (name) values ('Other')").lastrowid or 0)
        db.commit()
        doc_id = _document(db, other_class)
        resp = client.post(
            f"/api/classes/{class_id}/decks",
            json={"title": "Wrong class", "document_ids": [doc_id]},
        )
        assert resp.status_code == 404

    def test_unready_document_is_409(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id, state="failed")
        resp = client.post(
            f"/api/classes/{class_id}/decks",
            json={"title": "Failed source", "document_ids": [doc_id]},
        )
        assert resp.status_code == 409
        assert "failed to process" in resp.json()["detail"]

    def test_no_ready_documents_is_409(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        resp = client.post(
            f"/api/classes/{class_id}/decks",
            json={"title": "Nothing ready"},
        )
        assert resp.status_code == 409
        assert "no processed documents" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# PLA-305: Operation ID idempotency at the backend
# ---------------------------------------------------------------------------


class TestOperationIdIdempotency:
    """A repeated operation_id returns the original result, never double-applies."""

    def test_duplicate_review_returns_same_result(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state="ready")
        part_id = _card(db, deck_id)
        op_id = "test-op-001"
        resp1 = client.post(
            f"/api/cards/{part_id}/review",
            json={"rating": "good", "operation_id": op_id},
        )
        assert resp1.status_code == 200
        resp2 = client.post(
            f"/api/cards/{part_id}/review",
            json={"rating": "good", "operation_id": op_id},
        )
        assert resp2.status_code == 200
        assert resp1.json() == resp2.json()

    def test_duplicate_does_not_advance_reps(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state="ready")
        part_id = _card(db, deck_id)
        op_id = "test-op-002"
        client.post(
            f"/api/cards/{part_id}/review",
            json={"rating": "good", "operation_id": op_id},
        )
        client.post(
            f"/api/cards/{part_id}/review",
            json={"rating": "good", "operation_id": op_id},
        )
        log_rows = db.execute(
            "select count(*) as n from card_review_log where part_id = ? and op_id = ?",
            (part_id, op_id),
        ).fetchone()
        assert log_rows["n"] == 1

    def test_different_operation_id_advances_schedule(
        self, client: TestClient, db: sqlite3.Connection, class_id: int
    ) -> None:
        doc_id = _document(db, class_id)
        deck_id = _deck(db, class_id, doc_id, state="ready")
        part_id = _card(db, deck_id)
        resp1 = client.post(
            f"/api/cards/{part_id}/review",
            json={"rating": "good", "operation_id": "op-a"},
        )
        resp2 = client.post(
            f"/api/cards/{part_id}/review",
            json={"rating": "good", "operation_id": "op-b"},
        )
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp2.json()["reps"] > resp1.json()["reps"]
        log_rows = db.execute(
            "select count(*) as n from card_review_log where part_id = ?",
            (part_id,),
        ).fetchone()
        assert log_rows["n"] == 2
