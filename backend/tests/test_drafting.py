"""Contract tests for the drafting worker: suggestion runs land as pending edits, and a
failed or interrupted run costs the suggestion, never the draft.

The model is never called: `client.complete` is stubbed with queued replies, the
locality gate is stubbed open, and retrieval is replaced so no embedding server runs.
"""

import sqlite3

import pytest

from backend.core import artifacts, drafting, solver, study
from backend.core.app_settings import TutorConfig
from backend.core.errors import LyraError
from backend.rag.retrieve import RetrievalResult

BASE = "# Essay\n\nThe delta function is even.\n"
REVISED = "# Essay\n\nThe delta function is even, delta(t) = delta(-t).\n"


class _StubLLM:
    def __init__(self) -> None:
        self.replies: list[object] = []
        self.calls: list[dict[str, object]] = []

    async def complete(self, *args: object, **kwargs: object) -> str:
        self.calls.append({"args": args, "kwargs": kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return str(reply)


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> _StubLLM:
    stub = _StubLLM()
    monkeypatch.setattr(drafting.client, "complete", stub.complete)
    return stub


@pytest.fixture(autouse=True)
def _open_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drafting, "document_text_allowed", lambda conn: None)
    monkeypatch.setattr(
        drafting,
        "resolve_tutor_config",
        lambda conn: TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192),
    )
    monkeypatch.setattr(
        drafting,
        "retrieve",
        lambda conn, class_id, query, budget: RetrievalResult(
            chunks=[], trimmed=False, omitted_document_count=0
        ),
    )


def _draft(db: sqlite3.Connection, class_id: int, content: str = BASE) -> tuple[int, int]:
    created = artifacts.create_artifact(db, class_id, "Essay", [], kind=artifacts.KIND_DRAFT)
    part_id = artifacts.create_part(
        db,
        int(created["id"]),
        artifacts.DRAFT_BODY,
        1,
        content=content,
        status=artifacts.PART_COMPLETE,
    )
    artifacts.set_artifact_state(db, int(created["id"]), artifacts.READY)
    return int(created["id"]), part_id


def test_a_suggestion_lands_as_a_pending_edit(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    llm.replies = [REVISED]

    drafting.run_suggestion(drafting._Job(artifact_id, "State the symmetry"))

    row = db.execute("select * from pending_edits where part_id = ?", (part_id,)).fetchone()
    assert row is not None
    assert row["base_content"] == BASE
    # The reply is edge-stripped: a document is not reviewed over a trailing newline.
    assert row["proposed_content"] == REVISED.strip()
    assert row["note"] == "State the symmetry"
    # The document itself is untouched until the student accepts.
    assert str(artifacts.get_part(db, part_id)["content"]) == BASE
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.READY


def test_an_empty_reply_proposes_nothing(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    artifact_id, part_id = _draft(db, class_id)
    llm.replies = ["   "]

    drafting.run_suggestion(drafting._Job(artifact_id, "Improve it"))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert artifact["stage_detail"] == drafting.NO_CHANGES_DETAIL
    assert db.execute("select count(*) from pending_edits").fetchone()[0] == 0


def test_an_identical_reply_proposes_nothing(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    artifact_id, _ = _draft(db, class_id)
    llm.replies = [BASE]

    drafting.run_suggestion(drafting._Job(artifact_id, "Improve it"))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["stage_detail"] == drafting.NO_CHANGES_DETAIL
    assert db.execute("select count(*) from pending_edits").fetchone()[0] == 0


def test_a_failed_run_returns_to_ready_with_the_reason(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    artifact_id, _ = _draft(db, class_id)
    llm.replies = [LyraError("The endpoint fell over.")]

    drafting.run_suggestion(drafting._Job(artifact_id, "Improve it"))

    artifact = artifacts.get_artifact(db, artifact_id)
    # The run failed, not the draft: ready again, with the reason carried.
    assert artifact["state"] == artifacts.READY
    assert artifact["error_message"] == "The endpoint fell over."


def test_reconcile_returns_interrupted_runs_to_ready(db: sqlite3.Connection, class_id: int) -> None:
    artifact_id, _ = _draft(db, class_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.GENERATING, "Revising the draft")
    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, "
            "state) values (?, 'notes.pdf', '/tmp/x', 'application/pdf', 1, 'ready')",
            (class_id,),
        ).lastrowid
        or 0
    )
    deck = artifacts.create_artifact(
        db,
        class_id,
        "Deck",
        [artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_FLASHCARD_DECK,
    )
    artifacts.set_artifact_state(db, int(deck["id"]), artifacts.GENERATING)

    resumed = drafting.reconcile_interrupted(db)

    assert resumed == 1
    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert artifact["stage_detail"] == drafting.INTERRUPTED_DETAIL
    # The study reconcile owns decks; this one leaves them alone.
    assert artifacts.get_artifact(db, int(deck["id"]))["state"] == artifacts.GENERATING


def test_study_and_solver_reconciles_leave_drafts_alone(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(solver, "enqueue", lambda artifact_id: None)
    artifact_id, _ = _draft(db, class_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.GENERATING, "Revising")

    study.reconcile_interrupted(db)
    solver.reconcile_interrupted(db)

    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.GENERATING
