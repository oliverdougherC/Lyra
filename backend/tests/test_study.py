"""Contract tests for study generation: the two-phase deck pipeline, quiz validation,
and the restart reconcile.

The model is never called: `client.complete` is stubbed with queued JSON replies, the
locality gate is stubbed open, and `retrieve` is replaced so no embedding server runs.
What is asserted is what the pipeline writes: parts, card states, provenance, counters,
and failure states.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from backend.core import artifacts, scheduler, solver, study
from backend.core.app_settings import TutorConfig
from backend.core.errors import LyraError
from backend.rag.retrieve import RetrievalResult, RetrievedChunk


class _StubLLM:
    """A queue of replies for `client.complete`, recording the calls it answered."""

    def __init__(self) -> None:
        self.replies: list[object] = []
        self.calls: list[dict[str, object]] = []

    async def complete(self, *args: object, **kwargs: object) -> str:
        self.calls.append({"args": args, "kwargs": kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> _StubLLM:
    stub = _StubLLM()
    monkeypatch.setattr(study.client, "complete", stub.complete)
    return stub


@pytest.fixture(autouse=True)
def _open_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """No endpoint and no locality decision: generation is allowed, config is fake."""
    monkeypatch.setattr(study, "document_text_allowed", lambda conn: None)
    monkeypatch.setattr(
        study,
        "resolve_tutor_config",
        lambda conn: TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192),
    )


@pytest.fixture(autouse=True)
def _stub_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real chunk rows of the test's own documents, so the provenance a card records
    points at rows that exist (the FK demands it), and the prompt is still checkable."""

    def _fake_retrieve(
        conn: sqlite3.Connection, class_id: int, query: str, budget_tokens: int
    ) -> RetrievalResult:
        rows = conn.execute(
            "select c.id, c.document_id, c.content, d.filename from chunks c "
            "join documents d on d.id = c.document_id where c.class_id = ? limit 3",
            (class_id,),
        ).fetchall()
        chunks = [
            RetrievedChunk(
                chunk_id=int(row["id"]),
                document_id=int(row["document_id"]),
                content=str(row["content"]),
                token_count=len(str(row["content"])) // 4,
                page_number=1,
                section_title=None,
                section_path=None,
                section_number=None,
                problem_number=None,
                part_index=None,
                filename=str(row["filename"]),
                similarity=0.5,
                score=0.5,
            )
            for row in rows
        ]
        return RetrievalResult(chunks=chunks, trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(study, "retrieve", _fake_retrieve)


def _document(db: sqlite3.Connection, class_id: int, filename: str = "notes.pdf") -> int:
    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, "
            "state) values (?, ?, '/tmp/x', 'application/pdf', 1, 'ready')",
            (class_id, filename),
        ).lastrowid
        or 0
    )
    db.execute(
        "insert into chunks (document_id, class_id, content, token_count, page_number, "
        "doc_type, embedding_model, embedding_dim) values (?, ?, 'Some course text.', "
        "10, 1, 'generic', 'test', 768)",
        (document_id, class_id),
    )
    db.commit()
    return document_id


def _deck(db: sqlite3.Connection, class_id: int, document_id: int) -> int:
    created = artifacts.create_artifact(
        db,
        class_id,
        "Midterm deck",
        [artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_FLASHCARD_DECK,
    )
    return int(created["id"])


def test_deck_generation_writes_cards_states_and_provenance(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["delta functions", "convolution"]},
        {"cards": [{"front": "What is sifting?", "back": "It picks x(0).", "topic": "t"}]},
        {
            "cards": [
                {"front": "Define convolution.", "back": "An integral.", "topic": "t"},
                {"front": "Its commutativity?", "back": "f*g = g*f.", "topic": "t"},
            ]
        },
    ]

    study.run_generation(study._Job(artifact_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert artifact["problems_total"] == 2
    assert artifact["problems_done"] == 2

    parts = artifacts.list_parts(db, artifact_id)
    assert [part["kind"] for part in parts] == [artifacts.CARD] * 3
    assert [int(part["ordinal"]) for part in parts] == [1, 2, 3]
    payload = json.loads(str(parts[0]["content"]))
    # The recorded topic is the pipeline's, not the model's per-card field: the model's
    # output is a proposal, and which topic a card belongs to is a fact about the run.
    assert payload == {
        "front": "What is sifting?",
        "back": "It picks x(0).",
        "topic": "delta functions",
    }
    assert parts[0]["label"] == "delta functions"
    assert parts[0]["status"] == artifacts.PART_COMPLETE

    for part in parts:
        row = db.execute("select * from card_states where part_id = ?", (part["id"],)).fetchone()
        assert row is not None
        assert row["state"] == "new"
        assert row["reps"] == 0

    chunk_ids = [
        int(row["id"])
        for row in db.execute("select id from chunks where class_id = ?", (class_id,))
    ]
    provenance = artifacts.list_provenance(db, int(parts[0]["id"]))
    assert [entry["chunk_id"] for entry in provenance] == chunk_ids
    assert provenance[0]["label"] == "delta functions"


def test_a_topic_failure_is_counted_not_fatal(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["good topic", "bad topic"]},
        {"cards": [{"front": "F", "back": "B", "topic": "good topic"}]},
        LyraError("The endpoint fell over."),
    ]

    study.run_generation(study._Job(artifact_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert artifact["stage_detail"] == "1 of 2 topics failed"
    assert artifact["problems_done"] == 2
    assert len(artifacts.list_parts(db, artifact_id)) == 1


def test_zero_cards_is_a_failed_deck(db: sqlite3.Connection, class_id: int, llm: _StubLLM) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["only topic"]},
        LyraError("The endpoint fell over."),
    ]

    study.run_generation(study._Job(artifact_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert artifact["error_message"] == study.NO_CARDS_MESSAGE


def test_a_deck_with_no_topics_is_failed(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [{"topics": []}]

    study.run_generation(study._Job(artifact_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert artifact["error_message"] == study.NO_TOPICS_MESSAGE


def _quiz(db: sqlite3.Connection, class_id: int, document_id: int) -> int:
    created = artifacts.create_artifact(
        db,
        class_id,
        "Week 5 quiz",
        [artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_QUIZ,
    )
    return int(created["id"])


def _mcq(topic: str = "delta") -> dict[str, object]:
    return {
        "type": "mcq",
        "question": "Which picks x(0)?",
        "options": ["sifting", "scaling", "shifting", "sampling"],
        "correct_index": 0,
        "explanation": "The sifting property.",
        "topic": topic,
        "difficulty": "intermediate",
    }


def test_quiz_validation_drops_invalid_questions(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _quiz(db, class_id, document_id)
    broken = {**_mcq(), "options": ["only", "three", "here"]}
    llm.replies = [{"questions": [_mcq("a"), broken, _mcq("b"), _mcq("c")]}]

    study.run_generation(study._Job(artifact_id, count=4))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    parts = artifacts.list_parts(db, artifact_id)
    assert len(parts) == 3
    assert artifact["problems_total"] == 4
    assert artifact["problems_done"] == 3
    assert len(llm.calls) == 1, "three of four surviving is above the retry floor"


def test_a_mostly_broken_reply_is_retried_once_with_the_failures_named(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _quiz(db, class_id, document_id)
    broken = {**_mcq(), "correct_index": 9}
    llm.replies = [
        {"questions": [_mcq("a"), broken, broken, broken, broken]},
        {"questions": [_mcq("a"), _mcq("b"), _mcq("c"), _mcq("d"), _mcq("e")]},
    ]

    study.run_generation(study._Job(artifact_id, count=5))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert len(artifacts.list_parts(db, artifact_id)) == 5
    assert len(llm.calls) == 2
    retry_messages = llm.calls[1]["args"][3]
    assert "correct_index out of range" in str(retry_messages)


def test_a_quiz_with_too_few_survivors_fails(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _quiz(db, class_id, document_id)
    broken = {**_mcq(), "options": []}
    llm.replies = [
        {"questions": [_mcq("a"), broken, broken, broken]},
        {"questions": [_mcq("a"), broken, broken, broken]},
    ]

    study.run_generation(study._Job(artifact_id, count=4))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert artifact["error_message"] == study.NO_QUESTIONS_MESSAGE


@pytest.mark.parametrize(
    ("question", "problem"),
    [
        ({**_mcq(), "options": ["a", "a", "b", "c"]}, "four distinct options"),
        ({**_mcq(), "correct_index": 4}, "out of range"),
        ({**_mcq(), "type": "true_false"}, 'exactly ["True", "False"]'),
        (
            {
                **_mcq(),
                "type": "true_false",
                "options": ["True", "False"],
                "correct_index": 2,
            },
            "out of range",
        ),
        ({**_mcq(), "type": "fill_blank", "options": ["x(0)"], "correct_index": 0}, "___"),
        (
            {
                **_mcq(),
                "type": "fill_blank",
                "question": "The sifting property picks ___.",
                "options": ["x(0)", "x(1)"],
                "correct_index": 0,
            },
            "exactly one option",
        ),
        ({**_mcq(), "explanation": ""}, "empty explanation"),
        ({**_mcq(), "topic": ""}, "missing topic"),
        ({**_mcq(), "type": "essay"}, "unknown type"),
    ],
)
def test_the_per_type_rules(question: dict[str, object], problem: str) -> None:
    """Every rule the prompt states is enforced in code when the reply is parsed."""
    assert study._question_problem(question) is not None
    assert problem in str(study._question_problem(question))


def test_the_per_type_rules_accept_good_questions() -> None:
    assert study._question_problem(_mcq()) is None
    assert (
        study._question_problem(
            {
                **_mcq(),
                "type": "true_false",
                "options": ["True", "False"],
                "correct_index": 1,
            }
        )
        is None
    )
    assert (
        study._question_problem(
            {
                **_mcq(),
                "type": "fill_blank",
                "question": "The sifting property picks ___.",
                "options": ["x(0)"],
                "correct_index": 0,
            }
        )
        is None
    )


def test_reconcile_fails_interrupted_study_runs(db: sqlite3.Connection, class_id: int) -> None:
    document_id = _document(db, class_id)
    pending_deck = _deck(db, class_id, document_id)
    generating_quiz = _quiz(db, class_id, document_id)
    artifacts.set_artifact_state(db, generating_quiz, artifacts.GENERATING, "Writing")
    solution_set = artifacts.create_artifact(
        db, class_id, "Solver set", [artifacts.SourceSpec(document_id=document_id)]
    )

    failed = study.reconcile_interrupted(db)

    assert failed == 2
    for artifact_id in (pending_deck, generating_quiz):
        artifact = artifacts.get_artifact(db, artifact_id)
        assert artifact["state"] == artifacts.FAILED
        assert artifact["error_message"] == study.INTERRUPTED_MESSAGE
    assert artifacts.get_artifact(db, int(solution_set["id"]))["state"] == artifacts.PENDING


def test_a_cancelled_deck_is_skipped_before_generation_starts(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.CANCELLED)
    llm.replies = [{"topics": ["delta functions"]}]

    study.run_generation(study._Job(artifact_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.CANCELLED
    assert artifacts.list_parts(db, artifact_id) == []
    assert llm.calls == []


def test_cancelling_a_deck_mid_run_keeps_finished_cards_and_stops(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["delta functions", "convolution"]},
        {"cards": [{"front": "What is sifting?", "back": "It picks x(0).", "topic": "t"}]},
        {"cards": [{"front": "Define convolution.", "back": "An integral.", "topic": "t"}]},
    ]

    original = study._write_topic_cards

    def cancel_after_first_topic(*args: object, **kwargs: object) -> int:
        written = original(*args, **kwargs)
        if not artifacts.list_parts(db, artifact_id):
            return written
        from backend.storage.database import connect

        other = connect()
        try:
            artifacts.set_artifact_state(other, artifact_id, artifacts.CANCELLED)
        finally:
            other.close()
        return written

    monkeypatch.setattr(study, "_write_topic_cards", cancel_after_first_topic)

    study.run_generation(study._Job(artifact_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.CANCELLED
    parts = artifacts.list_parts(db, artifact_id)
    assert len(parts) == 1
    assert json.loads(str(parts[0]["content"]))["front"] == "What is sifting?"


def test_cancelling_a_quiz_before_writing_questions_keeps_it_empty(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _quiz(db, class_id, document_id)
    llm.replies = [{"questions": [_mcq("a"), _mcq("b"), _mcq("c")]}]

    original = study._call_json

    def cancel_before_write(*args: object, **kwargs: object) -> object:
        reply = original(*args, **kwargs)
        from backend.storage.database import connect

        other = connect()
        try:
            artifacts.set_artifact_state(other, artifact_id, artifacts.CANCELLED)
        finally:
            other.close()
        return reply

    monkeypatch.setattr(study, "_call_json", cancel_before_write)

    study.run_generation(study._Job(artifact_id, count=3))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.CANCELLED
    assert artifacts.list_parts(db, artifact_id) == []


def test_the_solver_reconcile_leaves_study_artifacts_alone(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconciles own disjoint kinds; a pending deck must never be requeued as a
    solve job, and a generating one must never read as a stalled solve."""
    enqueued: list[int] = []
    monkeypatch.setattr(solver, "enqueue", enqueued.append)
    document_id = _document(db, class_id)
    deck_id = _deck(db, class_id, document_id)
    quiz_id = _quiz(db, class_id, document_id)
    artifacts.set_artifact_state(db, quiz_id, artifacts.GENERATING, "Writing questions")

    solver.reconcile_interrupted(db)

    assert enqueued == []
    assert artifacts.get_artifact(db, deck_id)["state"] == artifacts.PENDING
    assert artifacts.get_artifact(db, quiz_id)["state"] == artifacts.GENERATING


def test_gathering_round_robins_and_caps(db: sqlite3.Connection, class_id: int) -> None:
    """One textbook must not crowd the syllabus out of the mapping pass: each document
    gives at most DOCUMENT_TOKEN_CAP, however many chunks it holds."""
    big = _document(db, class_id, "textbook.pdf")
    small = _document(db, class_id, "syllabus.pdf")
    # Each of these is 2008 characters, so 501 estimated tokens; the 6000-token
    # per-document cap admits eleven of them and refuses the twelfth (6012 > 6000).
    for index in range(30):
        db.execute(
            "insert into chunks (document_id, class_id, content, token_count, "
            "page_number, doc_type, embedding_model, embedding_dim) values "
            "(?, ?, ?, 501, ?, 'generic', 'test', 768)",
            (big, class_id, "x " * 1000 + f"big chunk {index}", index),
        )
    db.commit()
    artifact_id = _deck(db, class_id, big)
    db.execute(
        "insert into artifact_sources (artifact_id, document_id, role, ordinal) "
        "values (?, ?, 'study_source', 1)",
        (artifact_id, small),
    )
    db.commit()

    gathered = study._gather_source_text(db, artifact_id)

    assert "Some course text." in gathered  # the syllabus made it in
    assert gathered.count("big chunk") == 11


def test_new_card_states_are_due_immediately(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["only topic"]},
        {"cards": [{"front": "F", "back": "B", "topic": "t"}]},
    ]

    before = datetime.now(UTC)
    study.run_generation(study._Job(artifact_id))

    row = db.execute("select * from card_states").fetchone()
    due = scheduler.from_storage(str(row["due_at"]))
    assert before - timedelta(seconds=5) <= due <= datetime.now(UTC) + timedelta(seconds=5)
