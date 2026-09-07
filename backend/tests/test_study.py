"""Contract tests for study generation: the two-phase deck pipeline, quiz validation,
context-window budgeting, source revalidation, truthful completion, and durable recovery.

The model is never called: `client.complete` is stubbed with queued JSON replies, the
locality gate is stubbed open, and `retrieve` is replaced so no embedding server runs.
What is asserted is what the pipeline writes: parts, card states, provenance, counters,
and failure states.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.core import artifacts, scheduler, solver, study
from backend.core.app_settings import TutorAccess, TutorConfig
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


def _use_window(monkeypatch: pytest.MonkeyPatch, window: int) -> None:
    """Point generation at a fake local endpoint with a specific context window."""
    monkeypatch.setattr(
        study,
        "resolve_tutor_access",
        lambda conn, **_kwargs: TutorAccess(
            config=TutorConfig("http://127.0.0.1:9/v1", None, "m", window),
            document_block=None,
            remote_ack=True,
        ),
    )


@pytest.fixture(autouse=True)
def _open_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generation is allowed against a fake local endpoint with a generous window."""
    _use_window(monkeypatch, 8192)


@pytest.fixture(autouse=True)
def _stub_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real chunk rows of the test's own documents, so the provenance a card records
    points at rows that exist (the FK demands it), and the prompt is still checkable."""

    def _fake_retrieve(
        conn: sqlite3.Connection,
        class_id: int,
        query: str,
        budget_tokens: int,
        *,
        document_ids: tuple[int, ...] | None = None,
    ) -> RetrievalResult:
        rows = conn.execute(
            "select c.id, c.document_id, c.content, d.filename from chunks c "
            "join documents d on d.id = c.document_id where c.class_id = ? "
            "and c.document_id in (select value from json_each(?)) limit 3",
            (class_id, json.dumps(document_ids)),
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


def _deck_job(artifact_id: int, document_id: int, **opts: object) -> study._Job:
    return study._Job(artifact_id, source_ids=(document_id,), **opts)


def _cards(*fronts: str) -> dict[str, list[dict[str, str]]]:
    return {
        "cards": [
            {"front": front, "back": f"Answer for {front}", "topic": "model topic"}
            for front in fronts
        ]
    }


# ---------------------------------------------------------------------------
# Deck generation and completion (PLA-299)
# ---------------------------------------------------------------------------


def test_deck_generation_writes_cards_states_and_provenance(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["delta functions", "convolution"]},
        _cards("What is sifting?", "What is scaling?", "What is shifting?", "What is delta?"),
        _cards(
            "Define convolution.",
            "What is its integral?",
            "Is convolution commutative?",
            "What is convolution's identity?",
        ),
    ]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert artifact["problems_total"] == 2
    assert artifact["problems_done"] == 2

    parts = artifacts.list_parts(db, artifact_id)
    assert [part["kind"] for part in parts] == [artifacts.CARD] * 8
    assert [int(part["ordinal"]) for part in parts] == list(range(1, 9))
    payload = json.loads(str(parts[0]["content"]))
    assert payload == {
        "front": "What is sifting?",
        "back": "Answer for What is sifting?",
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


def test_one_card_then_bounded_top_up_reaches_exact_requested_count(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["delta functions"]},
        _cards("one"),
        _cards("two", "three", "four"),
    ]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    parts = artifacts.list_parts(db, artifact_id)
    fronts = [json.loads(str(part["content"]))["front"] for part in parts]
    assert fronts == ["one", "two", "three", "four"]
    assert "only 1 of 4 required" in str(llm.calls[2]["args"][3])


def test_top_up_cards_keep_the_provenance_of_the_attempt_that_proposed_them(
    db: sqlite3.Connection,
    class_id: int,
    llm: _StubLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = _document(db, class_id)
    db.execute(
        "insert into chunks (document_id, class_id, content, token_count, page_number, "
        "doc_type, embedding_model, embedding_dim) values "
        "(?, ?, 'Second course passage.', 10, 2, 'generic', 'test', 768)",
        (document_id, class_id),
    )
    db.commit()
    rows = db.execute(
        "select id, content, page_number from chunks where document_id = ? order by id",
        (document_id,),
    ).fetchall()
    retrieved = [
        RetrievedChunk(
            chunk_id=int(row["id"]),
            document_id=document_id,
            content=str(row["content"]),
            token_count=10,
            page_number=int(row["page_number"]),
            section_title=None,
            section_path=None,
            section_number=None,
            problem_number=None,
            part_index=None,
            filename="notes.pdf",
            similarity=0.5,
            score=0.5,
        )
        for row in rows
    ]
    retrieval_calls = 0

    def retrieve_by_attempt(*_args: object, **_kwargs: object) -> RetrievalResult:
        nonlocal retrieval_calls
        chunk = retrieved[retrieval_calls]
        retrieval_calls += 1
        return RetrievalResult(chunks=[chunk], trimmed=False, omitted_document_count=0)

    monkeypatch.setattr(study, "retrieve", retrieve_by_attempt)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [{"topics": ["delta"]}, _cards("one"), _cards("two", "three", "four")]

    study.run_generation(_deck_job(artifact_id, document_id))

    parts = artifacts.list_parts(db, artifact_id)
    provenance = [artifacts.list_provenance(db, int(part["id"])) for part in parts]
    assert [entries[0]["chunk_id"] for entries in provenance] == [
        int(rows[0]["id"]),
        int(rows[1]["id"]),
        int(rows[1]["id"]),
        int(rows[1]["id"]),
    ]


def test_one_card_on_both_attempts_fails_a_four_card_topic(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [{"topics": ["delta"]}, _cards("one"), _cards("one")]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert "required 4 distinct cards" in str(artifact["error_message"])
    assert artifacts.list_parts(db, artifact_id) == []


def test_duplicate_fronts_reduce_effective_count_and_fail_after_retry(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    duplicates = _cards("one?", "ONE", "two", "two.")
    llm.replies = [{"topics": ["delta"]}, duplicates, duplicates]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert artifacts.list_parts(db, artifact_id) == []


def test_cross_topic_duplicates_trigger_top_up_for_the_second_topic(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    """Deck-wide dedupe counts a repeated first-topic front against topic two's target."""
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["first", "second"]},
        _cards("a1", "a2", "a3", "a4"),
        _cards("a1", "b2", "b3", "b4"),
        _cards("a1", "b2", "b5"),
    ]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    parts = artifacts.list_parts(db, artifact_id)
    fronts = [json.loads(str(part["content"]))["front"] for part in parts]
    assert fronts == ["a1", "a2", "a3", "a4", "b2", "b3", "b4", "b5"]
    assert len(fronts) == len(set(fronts)) == 8


def test_cross_topic_duplicates_that_survive_retry_fail_without_partial_output(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["first", "second"]},
        _cards("a1", "a2", "a3", "a4"),
        _cards("a1", "b2", "b3", "b4"),
        _cards("a2", "b2", "b3", "b4"),
    ]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert "1 of 2 topics" in str(artifact["error_message"])
    assert artifacts.list_parts(db, artifact_id) == []
    assert db.execute("select count(*) from card_states").fetchone()[0] == 0


def test_overproduction_is_truncated_deterministically_to_exact_count(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [{"topics": ["delta"]}, _cards("one", "two", "three", "four", "five")]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    parts = artifacts.list_parts(db, artifact_id)
    assert [json.loads(str(part["content"]))["front"] for part in parts] == [
        "one",
        "two",
        "three",
        "four",
    ]


def test_a_topic_that_fails_after_retry_fails_the_whole_deck(
    db: sqlite3.Connection,
    class_id: int,
    llm: _StubLLM,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A deck is `ready` only when every mapped topic produced a card (PLA-299).

    A topic whose call errors on both the initial attempt and the bounded retry fails the
    whole deck, and the partial cards the other topic wrote are cleaned up so nothing reads
    as a finished deck missing material.
    """
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["good topic", "private-course-topic"]},
        _cards("good one", "good two", "good three", "good four"),
        LyraError("The endpoint fell over."),
        LyraError("The endpoint fell over again."),
    ]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert "1 of 2 topics" in str(artifact["error_message"])
    # No partial cards masquerade as a finished deck.
    assert artifacts.list_parts(db, artifact_id) == []
    assert db.execute("select count(*) from card_states").fetchone()[0] == 0
    assert "private-course-topic" not in caplog.text


def test_a_topic_that_returns_zero_cards_is_retried_then_recovers(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    """A topic returning no usable cards is retried once with a corrective hint, and a good
    retry lets the deck complete."""
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["only topic"]},
        {"cards": []},
        _cards("one", "two", "three", "four"),
    ]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert len(artifacts.list_parts(db, artifact_id)) == 4
    # The retry system prompt carried the corrective hint.
    retry_messages = llm.calls[2]["args"][3]
    assert "only 0 of 4 required" in str(retry_messages)


def test_zero_cards_after_retry_is_a_failed_deck(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["only topic"]},
        {"cards": []},
        {"cards": []},
    ]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert "1 of 1 topics" in str(artifact["error_message"])
    assert artifacts.list_parts(db, artifact_id) == []


@pytest.mark.parametrize("topics", [[], [42], [None, False, {}, [], "", "  "]])
def test_a_deck_with_no_topics_is_failed(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM, topics: list[object]
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [{"topics": topics}]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert artifact["error_message"] == study.NO_TOPICS_MESSAGE


def test_deck_recovers_valid_string_topics_from_mixed_model_output(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": [42, None, {}, "  delta functions  ", ""]},
        _cards("one", "two", "three", "four"),
    ]

    study.run_generation(_deck_job(artifact_id, document_id))

    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.READY
    parts = artifacts.list_parts(db, artifact_id)
    assert len(parts) == 4
    assert all(json.loads(str(part["content"]))["topic"] == "delta functions" for part in parts)
    assert len(llm.calls) == 2


# ---------------------------------------------------------------------------
# Quiz generation and completion (PLA-299)
# ---------------------------------------------------------------------------


def _quiz(db: sqlite3.Connection, class_id: int, document_id: int) -> int:
    created = artifacts.create_artifact(
        db,
        class_id,
        "Week 5 quiz",
        [artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_QUIZ,
    )
    return int(created["id"])


def _quiz_job(artifact_id: int, document_id: int, **opts: object) -> study._Job:
    return study._Job(artifact_id, source_ids=(document_id,), **opts)


def _mcq(topic: str = "delta") -> dict[str, object]:
    return {
        "type": "mcq",
        "question": f"Which property picks x(0) for {topic}?",
        "options": ["sifting", "scaling", "shifting", "sampling"],
        "correct_index": 0,
        "explanation": "The sifting property.",
        "topic": topic,
        "difficulty": "intermediate",
    }


def _flashcard_chunk(
    content: str,
    *,
    filename: str = "notes.pdf",
    chunk_id: int = 1,
    document_id: int = 1,
    page_number: int = 1,
    section_title: str | None = None,
    section_path: str | None = None,
    section_number: str | None = None,
    problem_number: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        filename=filename,
        page_number=page_number,
        section_title=section_title,
        section_path=section_path,
        section_number=section_number,
        problem_number=problem_number,
        chunk_id=chunk_id,
        document_id=document_id,
    )


def _flashcard_prompt_tokens(
    topic: str,
    count: int,
    chunks: list[SimpleNamespace],
    *,
    retry_hint: str | None = None,
) -> int:
    return study._prompt_tokens(
        study._flashcard_messages(topic, count, list(chunks), retry_hint=retry_hint)
    )


def test_quiz_reaches_the_requested_count(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _quiz(db, class_id, document_id)
    llm.replies = [{"questions": [_mcq("a"), _mcq("b"), _mcq("c"), _mcq("d")]}]

    study.run_generation(_quiz_job(artifact_id, document_id, count=4))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    parts = artifacts.list_parts(db, artifact_id)
    assert len(parts) == 4
    assert artifact["problems_total"] == 4
    assert artifact["problems_done"] == 4
    assert len(llm.calls) == 1


def test_a_quiz_that_undershoots_the_requested_count_fails(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    """A request for four questions that only yields three (after retry) is a failure, not a
    smaller quiz quietly presented as the requested one (PLA-299)."""
    document_id = _document(db, class_id)
    artifact_id = _quiz(db, class_id, document_id)
    broken = {**_mcq(), "options": ["only", "three", "here"]}
    llm.replies = [
        {"questions": [_mcq("a"), broken, _mcq("b"), _mcq("c")]},
        {"questions": [_mcq("a"), broken, _mcq("b"), _mcq("c")]},
    ]

    study.run_generation(_quiz_job(artifact_id, document_id, count=4))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert "3 of the 4" in str(artifact["error_message"])
    # No partial questions survive a failed quiz.
    assert artifacts.list_parts(db, artifact_id) == []


def test_a_quiz_caps_extra_questions_to_the_requested_count(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _quiz(db, class_id, document_id)
    llm.replies = [{"questions": [_mcq("a"), _mcq("b"), _mcq("c"), _mcq("d"), _mcq("e")]}]

    study.run_generation(_quiz_job(artifact_id, document_id, count=3))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert len(artifacts.list_parts(db, artifact_id)) == 3
    assert artifact["problems_total"] == 3
    assert artifact["problems_done"] == 3


def test_quiz_validation_drops_invalid_questions(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _quiz(db, class_id, document_id)
    broken = {**_mcq(), "options": ["only", "three", "here"]}
    # Four valid plus one broken; the request is four, met on the first call.
    llm.replies = [{"questions": [_mcq("a"), broken, _mcq("b"), _mcq("c"), _mcq("d")]}]

    study.run_generation(_quiz_job(artifact_id, document_id, count=4))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert len(artifacts.list_parts(db, artifact_id)) == 4
    assert len(llm.calls) == 1


def test_a_quiz_drops_questions_that_repeat_a_stem(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    """Two questions with the same stem are the same question; a repeat undershoots the
    count and fails rather than storing a duplicate."""
    document_id = _document(db, class_id)
    artifact_id = _quiz(db, class_id, document_id)
    original = _mcq("delta")
    cosmetic = {**_mcq("delta"), "question": str(original["question"]).upper()}
    # Three requested, but the second is a cosmetic repeat of the first, so only two stems
    # survive; the retry offers the same, so the quiz fails rather than store a duplicate.
    llm.replies = [
        {"questions": [original, cosmetic, _mcq("convolution")]},
        {"questions": [original, cosmetic, _mcq("convolution")]},
    ]

    study.run_generation(_quiz_job(artifact_id, document_id, count=3))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert "2 of the 3" in str(artifact["error_message"])


@pytest.mark.parametrize(
    ("left", "right", "collide"),
    [
        ("What is a delta?", "what is a delta", True),
        ("What is a delta?", "What is a delta.", True),
        ("  What is   a delta?  ", "What is a delta?", True),
        ("What is a delta?", "What is a ramp?", False),
    ],
)
def test_dedupe_key_folds_only_cosmetic_differences(left: str, right: str, collide: bool) -> None:
    assert (study._dedupe_key(left) == study._dedupe_key(right)) is collide


def test_quiz_questions_are_grounded_at_the_document_level(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    first = _document(db, class_id, filename="signals.pdf")
    second = _document(db, class_id, filename="notes.pdf")
    created = artifacts.create_artifact(
        db,
        class_id,
        "Grounded quiz",
        [
            artifacts.SourceSpec(document_id=first, role=artifacts.STUDY_SOURCE),
            artifacts.SourceSpec(document_id=second, role=artifacts.STUDY_SOURCE),
        ],
        kind=artifacts.KIND_QUIZ,
    )
    artifact_id = int(created["id"])
    llm.replies = [{"questions": [_mcq("delta"), _mcq("convolution"), _mcq("fourier")]}]

    study.run_generation(study._Job(artifact_id, source_ids=(first, second), count=3))

    parts = artifacts.list_parts(db, artifact_id)
    assert len(parts) == 3
    for part in parts:
        provenance = artifacts.list_provenance(db, int(part["id"]))
        assert [entry["document_id"] for entry in provenance] == [first, second]
        assert all(
            entry["chunk_id"] is None and entry["page_number"] is None for entry in provenance
        )
        assert [entry["filename"] for entry in provenance] == ["signals.pdf", "notes.pdf"]


def test_a_quiz_undershoot_is_retried_once_with_the_failures_named(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _quiz(db, class_id, document_id)
    broken = {**_mcq(), "correct_index": 9}
    llm.replies = [
        {"questions": [_mcq("a"), broken, broken, broken, broken]},
        {"questions": [_mcq("a"), _mcq("b"), _mcq("c"), _mcq("d"), _mcq("e")]},
    ]

    study.run_generation(_quiz_job(artifact_id, document_id, count=5))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    assert len(artifacts.list_parts(db, artifact_id)) == 5
    assert len(llm.calls) == 2
    retry_messages = llm.calls[1]["args"][3]
    assert "correct_index out of range" in str(retry_messages)


@pytest.mark.parametrize(
    ("question", "problem"),
    [
        ({**_mcq(), "options": ["a", "a", "b", "c"]}, "four distinct options"),
        ({**_mcq(), "correct_index": 4}, "out of range"),
        ({**_mcq(), "type": "true_false"}, 'exactly ["True", "False"]'),
        (
            {**_mcq(), "type": "true_false", "options": ["True", "False"], "correct_index": 2},
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
        ({**_mcq(), "explanation": ""}, "explanation must be a non-empty string"),
        ({**_mcq(), "topic": ""}, "topic must be a non-empty string"),
        ({**_mcq(), "type": "essay"}, "unknown type"),
    ],
)
def test_the_per_type_rules(question: dict[str, object], problem: str) -> None:
    assert study._question_problem(question) is not None
    assert problem in str(study._question_problem(question))


def test_the_per_type_rules_accept_good_questions() -> None:
    assert study._question_problem(_mcq()) is None
    assert (
        study._question_problem(
            {**_mcq(), "type": "true_false", "options": ["True", "False"], "correct_index": 1}
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


# ---------------------------------------------------------------------------
# Source revalidation at the worker boundary (PLA-291)
# ---------------------------------------------------------------------------


def test_generation_fails_visibly_when_a_source_left_ready(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    """A source that leaves `ready` after the request fails generation, naming the file,
    rather than being silently skipped."""
    document_id = _document(db, class_id, filename="lecture.pdf")
    artifact_id = _quiz(db, class_id, document_id)
    db.execute("update documents set state = 'failed' where id = ?", (document_id,))
    db.commit()

    study.run_generation(_quiz_job(artifact_id, document_id, count=3))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert "lecture.pdf" in str(artifact["error_message"])
    assert "failed to process" in str(artifact["error_message"])
    assert llm.calls == []


def test_generation_fails_visibly_when_a_source_was_deleted(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    keep = _document(db, class_id, filename="keep.pdf")
    drop = _document(db, class_id, filename="drop.pdf")
    created = artifacts.create_artifact(
        db,
        class_id,
        "Two-source quiz",
        [
            artifacts.SourceSpec(document_id=keep, role=artifacts.STUDY_SOURCE),
            artifacts.SourceSpec(document_id=drop, role=artifacts.STUDY_SOURCE),
        ],
        kind=artifacts.KIND_QUIZ,
    )
    artifact_id = int(created["id"])
    db.execute("delete from documents where id = ?", (drop,))
    db.commit()

    study.run_generation(study._Job(artifact_id, source_ids=(keep, drop), count=3))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert "no longer matches" in str(artifact["error_message"])
    assert llm.calls == []


# ---------------------------------------------------------------------------
# Context-window budgeting (PLA-298)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("chunk_size", "expected"), [(1600, 2), (2000, 2)])
def test_gathering_total_cap_and_exact_fit(chunk_size: int, expected: int) -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "create table chunks (id integer primary key, document_id integer, content text)"
        )
        text = "x" * chunk_size
        conn.executemany("insert into chunks (document_id, content) values (7, ?)", [(text,)] * 3)
        gathered, contributing = study._gather_source_text(conn, (7,), total_cap=1000)
    assert gathered.count(text) == expected
    assert contributing == [7]


def test_gathering_round_robins_and_caps(db: sqlite3.Connection, class_id: int) -> None:
    """One textbook must not crowd the syllabus out: each document gives at most
    DOCUMENT_TOKEN_CAP, however many chunks it holds."""
    big = _document(db, class_id, "textbook.pdf")
    small = _document(db, class_id, "syllabus.pdf")
    for index in range(30):
        db.execute(
            "insert into chunks (document_id, class_id, content, token_count, "
            "page_number, doc_type, embedding_model, embedding_dim) values "
            "(?, ?, ?, 501, ?, 'generic', 'test', 768)",
            (big, class_id, "x " * 1000 + f"big chunk {index}", index),
        )
    db.commit()

    gathered, contributing = study._gather_source_text(
        db, (big, small), total_cap=study.TOTAL_TOKEN_CAP
    )

    assert "Some course text." in gathered  # the syllabus made it in
    assert gathered.count("big chunk") == 11
    assert contributing == [big, small]


def test_trim_chunks_skips_an_oversized_first_chunk_and_keeps_later_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192)
    too_large = _flashcard_chunk("x" * 1200)
    first_fit = _flashcard_chunk("y" * 200)
    second_fit = _flashcard_chunk("z" * 160)
    ceiling = _flashcard_prompt_tokens("delta", 4, [first_fit, second_fit])
    monkeypatch.setattr(study, "_input_ceiling", lambda _config: ceiling)

    kept = study._trim_chunks(config, "delta", 4, [too_large, first_fit, second_fit])

    assert kept == [first_fit, second_fit]
    assert _flashcard_prompt_tokens("delta", 4, kept) <= ceiling


def test_trim_chunks_skips_an_oversized_middle_chunk_and_considers_later_smaller_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192)
    first_fit = _flashcard_chunk("x" * 200)
    too_large = _flashcard_chunk("y" * 1200)
    later_fit = _flashcard_chunk("z" * 160)
    ceiling = _flashcard_prompt_tokens("delta", 4, [first_fit, later_fit])
    monkeypatch.setattr(study, "_input_ceiling", lambda _config: ceiling)

    kept = study._trim_chunks(config, "delta", 4, [first_fit, too_large, later_fit])

    assert kept == [first_fit, later_fit]
    assert _flashcard_prompt_tokens("delta", 4, kept) <= ceiling


def test_trim_chunks_admits_an_exact_boundary_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    config = TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192)
    exact = _flashcard_chunk("x" * 240)
    ceiling = _flashcard_prompt_tokens("delta", 4, [exact])
    monkeypatch.setattr(study, "_input_ceiling", lambda _config: ceiling)

    kept = study._trim_chunks(config, "delta", 4, [exact])

    assert kept == [exact]
    assert _flashcard_prompt_tokens("delta", 4, kept) == ceiling


def test_trim_chunks_counts_metadata_heavy_labels_against_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192)
    heavy_label = _flashcard_chunk(
        "same content",
        filename="course-notes-" + ("f" * 1200) + ".pdf",
        section_path="Chapter " + ("s" * 1200),
        section_number="12.4.5",
        problem_number="19",
    )
    small_label = _flashcard_chunk("same content")
    ceiling = _flashcard_prompt_tokens("delta", 4, [small_label])
    monkeypatch.setattr(study, "_input_ceiling", lambda _config: ceiling)

    kept = study._trim_chunks(config, "delta", 4, [heavy_label, small_label])

    assert study.estimate_tokens(heavy_label.content) == study.estimate_tokens(small_label.content)
    assert kept == [small_label]
    assert _flashcard_prompt_tokens("delta", 4, kept) <= ceiling


def test_trim_chunks_counts_the_retry_hint_against_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192)
    retry_hint = study._flashcard_retry_hint(1, 4)
    pair = next(
        (
            _flashcard_chunk("x" * large_size),
            _flashcard_chunk("x" * small_size),
        )
        for large_size in range(80, 4000, 40)
        for small_size in range(40, large_size, 40)
        if _flashcard_prompt_tokens(
            "delta",
            4,
            [_flashcard_chunk("x" * large_size)],
            retry_hint=retry_hint,
        )
        > _flashcard_prompt_tokens("delta", 4, [_flashcard_chunk("x" * large_size)])
        and _flashcard_prompt_tokens(
            "delta",
            4,
            [_flashcard_chunk("x" * small_size)],
            retry_hint=retry_hint,
        )
        <= _flashcard_prompt_tokens("delta", 4, [_flashcard_chunk("x" * large_size)])
    )
    large, small = pair
    ceiling = _flashcard_prompt_tokens("delta", 4, [large])
    monkeypatch.setattr(study, "_input_ceiling", lambda _config: ceiling)

    kept_without_retry = study._trim_chunks(config, "delta", 4, [large])
    kept_with_retry = study._trim_chunks(config, "delta", 4, [large, small], retry_hint=retry_hint)

    assert kept_without_retry == [large]
    assert kept_with_retry == [small]
    assert _flashcard_prompt_tokens("delta", 4, kept_with_retry, retry_hint=retry_hint) <= ceiling


def test_trim_chunks_returns_empty_when_no_candidate_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    config = TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192)
    too_large = _flashcard_chunk("x" * 240)
    ceiling = _flashcard_prompt_tokens("delta", 4, [])
    monkeypatch.setattr(study, "_input_ceiling", lambda _config: ceiling)

    assert study._trim_chunks(config, "delta", 4, [too_large]) == []


def test_flashcard_request_omits_a_later_chunk_when_the_formatted_pair_would_overflow(
    db: sqlite3.Connection,
    class_id: int,
    llm: _StubLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = _document(db, class_id)
    db.execute(
        "insert into chunks (document_id, class_id, content, token_count, page_number, "
        "doc_type, embedding_model, embedding_dim) values "
        "(?, ?, 'Later supporting passage.', 10, 2, 'generic', 'test', 768)",
        (document_id, class_id),
    )
    db.commit()
    rows = db.execute(
        "select id, page_number from chunks where document_id = ? order by id",
        (document_id,),
    ).fetchall()
    first = _flashcard_chunk(
        "First supporting passage.",
        chunk_id=int(rows[0]["id"]),
        document_id=document_id,
        page_number=int(rows[0]["page_number"]),
    )
    second = _flashcard_chunk(
        "Later supporting passage.",
        chunk_id=int(rows[1]["id"]),
        document_id=document_id,
        page_number=int(rows[1]["page_number"]),
    )
    ceiling = _flashcard_prompt_tokens("only topic", 4, [first])
    raw_pair_cost = study.estimate_tokens(first.content) + study.estimate_tokens(second.content)
    fixed = _flashcard_prompt_tokens("only topic", 4, [])
    assert raw_pair_cost <= ceiling - fixed
    assert _flashcard_prompt_tokens("only topic", 4, [first, second]) > ceiling
    monkeypatch.setattr(study, "_input_ceiling", lambda _config: ceiling)
    monkeypatch.setattr(
        study,
        "retrieve",
        lambda *_args, **_kwargs: RetrievalResult(
            chunks=[first, second], trimmed=False, omitted_document_count=0
        ),
    )
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [{"topics": ["only topic"]}, _cards("one", "two", "three", "four")]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    flashcard_messages = llm.calls[1]["args"][3]
    assert "First supporting passage." in str(flashcard_messages[1]["content"])
    assert "Later supporting passage." not in str(flashcard_messages[1]["content"])
    assert study._prompt_tokens(flashcard_messages) <= ceiling


def test_flashcard_request_skips_a_metadata_heavy_first_chunk_and_uses_a_later_fit(
    db: sqlite3.Connection,
    class_id: int,
    llm: _StubLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = _document(db, class_id)
    db.execute(
        "insert into chunks (document_id, class_id, content, token_count, page_number, "
        "doc_type, embedding_model, embedding_dim) values "
        "(?, ?, 'Later chunk text.', 10, 2, 'generic', 'test', 768)",
        (document_id, class_id),
    )
    db.commit()
    rows = db.execute(
        "select id, page_number from chunks where document_id = ? order by id",
        (document_id,),
    ).fetchall()
    heavy = _flashcard_chunk(
        "Heavy meta text.",
        chunk_id=int(rows[0]["id"]),
        document_id=document_id,
        page_number=int(rows[0]["page_number"]),
        filename="notes-" + ("f" * 1200) + ".pdf",
        section_path="Section " + ("s" * 1200),
    )
    later = _flashcard_chunk(
        "Later chunk text.",
        chunk_id=int(rows[1]["id"]),
        document_id=document_id,
        page_number=int(rows[1]["page_number"]),
    )
    ceiling = _flashcard_prompt_tokens("only topic", 4, [later])
    assert study.estimate_tokens(heavy.content) == study.estimate_tokens(later.content)
    assert _flashcard_prompt_tokens("only topic", 4, [heavy]) > ceiling
    monkeypatch.setattr(study, "_input_ceiling", lambda _config: ceiling)
    monkeypatch.setattr(
        study,
        "retrieve",
        lambda *_args, **_kwargs: RetrievalResult(
            chunks=[heavy, later], trimmed=False, omitted_document_count=0
        ),
    )
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [{"topics": ["only topic"]}, _cards("one", "two", "three", "four")]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    flashcard_messages = llm.calls[1]["args"][3]
    assert "Heavy meta text." not in str(flashcard_messages[1]["content"])
    assert "Later chunk text." in str(flashcard_messages[1]["content"])
    provenance = artifacts.list_provenance(db, int(artifacts.list_parts(db, artifact_id)[0]["id"]))
    assert [entry["chunk_id"] for entry in provenance] == [int(rows[1]["id"])]


def test_flashcard_request_admits_an_exact_safe_fit(
    db: sqlite3.Connection,
    class_id: int,
    llm: _StubLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = _document(db, class_id)
    row = db.execute(
        "select id, page_number from chunks where document_id = ? order by id limit 1",
        (document_id,),
    ).fetchone()
    assert row is not None
    exact = _flashcard_chunk(
        "Exact boundary passage.",
        chunk_id=int(row["id"]),
        document_id=document_id,
        page_number=int(row["page_number"]),
    )
    ceiling = _flashcard_prompt_tokens("only topic", 4, [exact])
    monkeypatch.setattr(study, "_input_ceiling", lambda _config: ceiling)
    monkeypatch.setattr(
        study,
        "retrieve",
        lambda *_args, **_kwargs: RetrievalResult(
            chunks=[exact], trimmed=False, omitted_document_count=0
        ),
    )
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [{"topics": ["only topic"]}, _cards("one", "two", "three", "four")]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.READY
    flashcard_messages = llm.calls[1]["args"][3]
    assert study._prompt_tokens(flashcard_messages) == ceiling


def test_flashcard_request_refuses_a_one_token_over_chunk(
    db: sqlite3.Connection,
    class_id: int,
    llm: _StubLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = _document(db, class_id)
    row = db.execute(
        "select id, page_number from chunks where document_id = ? order by id limit 1",
        (document_id,),
    ).fetchone()
    assert row is not None
    exact, one_over, ceiling = next(
        (
            _flashcard_chunk(
                "x" * size,
                chunk_id=int(row["id"]),
                document_id=document_id,
                page_number=int(row["page_number"]),
            ),
            _flashcard_chunk(
                ("x" * size) + "xxxx",
                chunk_id=int(row["id"]),
                document_id=document_id,
                page_number=int(row["page_number"]),
            ),
            _flashcard_prompt_tokens(
                "only topic",
                4,
                [
                    _flashcard_chunk(
                        "x" * size,
                        chunk_id=int(row["id"]),
                        document_id=document_id,
                        page_number=int(row["page_number"]),
                    )
                ],
            ),
        )
        for size in range(40, 4000, 4)
        if _flashcard_prompt_tokens(
            "only topic",
            4,
            [
                _flashcard_chunk(
                    ("x" * size) + "xxxx",
                    chunk_id=int(row["id"]),
                    document_id=document_id,
                    page_number=int(row["page_number"]),
                )
            ],
        )
        == _flashcard_prompt_tokens(
            "only topic",
            4,
            [
                _flashcard_chunk(
                    "x" * size,
                    chunk_id=int(row["id"]),
                    document_id=document_id,
                    page_number=int(row["page_number"]),
                )
            ],
        )
        + 1
    )
    assert _flashcard_prompt_tokens("only topic", 4, [exact]) == ceiling
    assert _flashcard_prompt_tokens("only topic", 4, [one_over]) == ceiling + 1
    monkeypatch.setattr(study, "_input_ceiling", lambda _config: ceiling)
    monkeypatch.setattr(
        study,
        "retrieve",
        lambda *_args, **_kwargs: RetrievalResult(
            chunks=[one_over], trimmed=False, omitted_document_count=0
        ),
    )
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [{"topics": ["only topic"]}]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert artifact["error_message"] == study.CONTEXT_TOO_SMALL_MESSAGE
    assert len(llm.calls) == 1


def test_no_formatted_chunk_fits_due_to_metadata_fails_locally_without_a_card_request(
    db: sqlite3.Connection,
    class_id: int,
    llm: _StubLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = _document(db, class_id)
    row = db.execute(
        "select id, page_number from chunks where document_id = ? order by id limit 1",
        (document_id,),
    ).fetchone()
    assert row is not None
    heavy = _flashcard_chunk(
        "Tiny text.",
        chunk_id=int(row["id"]),
        document_id=document_id,
        page_number=int(row["page_number"]),
        filename="notes-" + ("f" * 1200) + ".pdf",
        section_path="Section " + ("s" * 1200),
    )
    normal = _flashcard_chunk("Tiny text.")
    ceiling = _flashcard_prompt_tokens("only topic", 4, [normal])
    assert study.estimate_tokens(heavy.content) == study.estimate_tokens(normal.content)
    assert _flashcard_prompt_tokens("only topic", 4, [heavy]) > ceiling
    monkeypatch.setattr(study, "_input_ceiling", lambda _config: ceiling)
    monkeypatch.setattr(
        study,
        "retrieve",
        lambda *_args, **_kwargs: RetrievalResult(
            chunks=[heavy], trimmed=False, omitted_document_count=0
        ),
    )
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [{"topics": ["only topic"]}]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert artifact["error_message"] == study.CONTEXT_TOO_SMALL_MESSAGE
    assert len(llm.calls) == 1


def test_no_chunk_fits_fails_locally_without_a_card_request(
    db: sqlite3.Connection,
    class_id: int,
    llm: _StubLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [{"topics": ["only topic"]}]
    oversized = SimpleNamespace(content="x" * 100_000)
    monkeypatch.setattr(
        study,
        "retrieve",
        lambda *_args, **_kwargs: RetrievalResult(
            chunks=[oversized], trimmed=False, omitted_document_count=0
        ),
    )

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert artifact["error_message"] == study.CONTEXT_TOO_SMALL_MESSAGE
    assert len(llm.calls) == 1  # topic mapping only; no oversized card prompt was sent
    assert (
        study._trim_chunks(
            TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192),
            "only topic",
            4,
            [oversized],
        )
        == []
    )


def test_a_tiny_context_window_fails_locally_without_calling_the_model(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window smaller than the output reserve leaves no room for the prompt, so
    generation fails with an actionable message and never sends an oversized request."""
    _use_window(monkeypatch, 256)
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [{"topics": ["only topic"]}]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert artifact["error_message"] == study.CONTEXT_TOO_SMALL_MESSAGE
    assert llm.calls == []


def test_quiz_with_only_retry_reserve_room_reports_context_failure(
    db: sqlite3.Connection,
    class_id: int,
    llm: _StubLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _quiz(db, class_id, document_id)
    job = _quiz_job(artifact_id, document_id, count=3)
    fixed = study._prompt_tokens(
        study.prompts.build_quiz_prompt("", job.count, job.difficulty, list(job.types))
    )
    window = next(
        candidate
        for candidate in range(256, 8192)
        if 0
        < study._input_ceiling(TutorConfig("http://127.0.0.1:9/v1", None, "m", candidate)) - fixed
        <= study._RETRY_HINT_RESERVE
    )
    _use_window(monkeypatch, window)

    study.run_generation(job)

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.FAILED
    assert artifact["error_message"] == study.CONTEXT_TOO_SMALL_MESSAGE
    assert llm.calls == []


def test_call_json_refuses_a_prompt_over_the_input_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single call chokepoint enforces the window as a backstop, not merely calculates
    it: an over-ceiling prompt is refused locally before any request is made."""
    config = TutorConfig("http://127.0.0.1:9/v1", None, "m", 2048)
    huge = [{"role": "user", "content": "x" * 40_000}]
    called = False

    async def _fail(*args: object, **kwargs: object) -> str:
        nonlocal called
        called = True
        return "{}"

    monkeypatch.setattr(study.client, "complete", _fail)
    with pytest.raises(LyraError):
        study._call_json(config, huge, study.prompts.TOPICS_SCHEMA)
    assert called is False


def test_call_json_sends_the_output_reserve_as_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    config = TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192)
    captured: dict[str, object] = {}

    async def _capture(*args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return json.dumps({"topics": []})

    monkeypatch.setattr(study.client, "complete", _capture)
    study._call_json(config, [{"role": "user", "content": "hi"}], study.prompts.TOPICS_SCHEMA)
    from backend.llm.budget import generation_reserve

    assert captured["max_tokens"] == generation_reserve(8192)
    assert captured["fail_on_truncation"] is True


def test_all_study_call_shapes_fit_the_input_ceiling_and_send_generation_reserve(
    db: sqlite3.Connection,
    class_id: int,
    llm: _StubLLM,
) -> None:
    document_id = _document(db, class_id)
    deck_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["delta functions"]},
        _cards("one"),
        _cards("two", "three", "four"),
    ]
    study.run_generation(_deck_job(deck_id, document_id))

    quiz_id = _quiz(db, class_id, document_id)
    llm.replies = [
        {"questions": [_mcq("delta")]},
        {"questions": [_mcq("delta"), _mcq("convolution"), _mcq("sampling")]},
    ]
    study.run_generation(_quiz_job(quiz_id, document_id, count=3))

    config = TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192)
    reserve = study.generation_reserve(config.context_window)
    ceiling = study._input_ceiling(config)

    assert len(llm.calls) == 5
    assert "mapping course material" in str(llm.calls[0]["args"][3][0]["content"])
    assert 'writing flashcards for the topic "delta functions"' in str(
        llm.calls[1]["args"][3][0]["content"]
    )
    assert "The previous attempt produced only 1 of 4 required distinct" in str(
        llm.calls[2]["args"][3][0]["content"]
    )
    assert "writing a 3-question quiz" in str(llm.calls[3]["args"][3][0]["content"])
    assert "The previous reply did not produce enough valid questions." in str(
        llm.calls[4]["args"][3][0]["content"]
    )
    for call in llm.calls:
        messages = call["args"][3]
        kwargs = call["kwargs"]
        assert study._prompt_tokens(messages) <= ceiling
        assert kwargs["max_tokens"] == reserve


# ---------------------------------------------------------------------------
# Durable recovery (PLA-169)
# ---------------------------------------------------------------------------


def _persist_deck_job(
    db: sqlite3.Connection, class_id: int, document_id: int, **opts: object
) -> int:
    artifact_id = _deck(db, class_id, document_id)
    study.persist_job(
        db, _deck_job(artifact_id, document_id, **opts), artifacts.KIND_FLASHCARD_DECK
    )
    return artifact_id


def _persist_quiz_job(
    db: sqlite3.Connection, class_id: int, document_id: int, **opts: object
) -> int:
    artifact_id = _quiz(db, class_id, document_id)
    study.persist_job(db, _quiz_job(artifact_id, document_id, **opts), artifacts.KIND_QUIZ)
    return artifact_id


def test_reconcile_requeues_pending_jobs_with_their_exact_options(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart before the worker started requeues pending decks/quizzes with the exact
    original settings rather than failing them (PLA-169)."""
    enqueued: list[study._Job] = []
    monkeypatch.setattr(study, "enqueue", enqueued.append)
    document_id = _document(db, class_id)
    deck_id = _persist_deck_job(db, class_id, document_id, cards_per_topic=5)
    quiz_id = _persist_quiz_job(
        db, class_id, document_id, count=12, difficulty="exam", types=("mcq", "fill_blank")
    )

    requeued, failed = study.reconcile_interrupted(db)

    assert (requeued, failed) == (2, 0)
    # Requeued in id-ascending order.
    assert [job.artifact_id for job in enqueued] == [deck_id, quiz_id]
    deck_job = next(job for job in enqueued if job.artifact_id == deck_id)
    assert deck_job.cards_per_topic == 5
    quiz_job = next(job for job in enqueued if job.artifact_id == quiz_id)
    assert quiz_job.count == 12
    assert quiz_job.difficulty == "exam"
    assert quiz_job.types == ("mcq", "fill_blank")
    assert quiz_job.source_ids == (document_id,)
    # Still pending, ready for the worker.
    assert artifacts.get_artifact(db, deck_id)["state"] == artifacts.PENDING


def test_reconcile_restarts_a_generating_deck_without_duplicating_cards(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-generation discards the partial cards and requeues the job, so recovery
    restarts cleanly and never appends duplicates to half-written output (PLA-169)."""
    enqueued: list[study._Job] = []
    monkeypatch.setattr(study, "enqueue", enqueued.append)
    document_id = _document(db, class_id)
    deck_id = _persist_deck_job(db, class_id, document_id)
    artifacts.set_artifact_state(db, deck_id, artifacts.GENERATING, "Writing cards")
    card_id = artifacts.create_part(
        db,
        deck_id,
        artifacts.CARD,
        1,
        content=json.dumps({"front": "F", "back": "B"}),
        content_type=artifacts.JSON,
        status=artifacts.PART_COMPLETE,
    )
    study._insert_card_state(db, card_id)

    requeued, failed = study.reconcile_interrupted(db)

    assert (requeued, failed) == (1, 0)
    assert [job.artifact_id for job in enqueued] == [deck_id]
    deck = artifacts.get_artifact(db, deck_id)
    assert deck["state"] == artifacts.PENDING
    # The partial card and its scheduling state are gone: no duplicate on the restart.
    assert artifacts.list_parts(db, deck_id) == []
    assert db.execute("select count(*) from card_states").fetchone()[0] == 0


def test_reconcile_fails_a_job_whose_intent_cannot_be_reconstructed(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued study artifact with no persisted job row cannot be reconstructed, so it is
    failed rather than guessed."""
    enqueued: list[study._Job] = []
    monkeypatch.setattr(study, "enqueue", enqueued.append)
    document_id = _document(db, class_id)
    orphan = _deck(db, class_id, document_id)  # no persist_job
    artifacts.set_artifact_state(db, orphan, artifacts.GENERATING, "Writing cards")
    card_id = artifacts.create_part(
        db,
        orphan,
        artifacts.CARD,
        1,
        content=json.dumps({"front": "partial", "back": "must be removed"}),
        content_type=artifacts.JSON,
        status=artifacts.PART_COMPLETE,
    )
    study._insert_card_state(db, card_id)

    requeued, failed = study.reconcile_interrupted(db)

    assert (requeued, failed) == (0, 1)
    assert enqueued == []
    artifact = artifacts.get_artifact(db, orphan)
    assert artifact["state"] == artifacts.FAILED
    assert artifact["error_message"] == study.INTERRUPTED_MESSAGE
    assert artifacts.list_parts(db, orphan) == []
    assert db.execute("select count(*) from card_states").fetchone()[0] == 0


def test_reconcile_fails_a_job_with_malformed_metadata(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueued: list[study._Job] = []
    monkeypatch.setattr(study, "enqueue", enqueued.append)
    document_id = _document(db, class_id)
    deck_id = _persist_deck_job(db, class_id, document_id)
    db.execute("update study_jobs set types = 'not json' where artifact_id = ?", (deck_id,))
    db.commit()

    requeued, failed = study.reconcile_interrupted(db)

    assert (requeued, failed) == (0, 1)
    assert enqueued == []
    assert artifacts.get_artifact(db, deck_id)["state"] == artifacts.FAILED


def test_reconcile_never_resurrects_a_cancelled_job(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueued: list[study._Job] = []
    monkeypatch.setattr(study, "enqueue", enqueued.append)
    document_id = _document(db, class_id)
    deck_id = _persist_deck_job(db, class_id, document_id)
    artifacts.set_artifact_state(db, deck_id, artifacts.CANCELLED)

    requeued, failed = study.reconcile_interrupted(db)

    assert (requeued, failed) == (0, 0)
    assert enqueued == []
    assert artifacts.get_artifact(db, deck_id)["state"] == artifacts.CANCELLED


def test_reconcile_ignores_a_deleted_artifact(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted artifact took its job row with it (cascade) and is not seen by reconcile."""
    enqueued: list[study._Job] = []
    monkeypatch.setattr(study, "enqueue", enqueued.append)
    document_id = _document(db, class_id)
    deck_id = _persist_deck_job(db, class_id, document_id)
    artifacts.delete_artifact(db, deck_id)
    assert db.execute("select count(*) from study_jobs").fetchone()[0] == 0

    requeued, failed = study.reconcile_interrupted(db)

    assert (requeued, failed) == (0, 0)
    assert enqueued == []


def test_persist_job_round_trips_through_reconstruction(
    db: sqlite3.Connection, class_id: int
) -> None:
    document_id = _document(db, class_id)
    quiz_id = _persist_quiz_job(
        db, class_id, document_id, count=7, difficulty="basic", types=("true_false",)
    )
    row = db.execute("select * from study_jobs where artifact_id = ?", (quiz_id,)).fetchone()
    job = study._job_from_row(row)
    assert job.artifact_id == quiz_id
    assert job.count == 7
    assert job.difficulty == "basic"
    assert job.types == ("true_false",)
    assert job.source_ids == (document_id,)


# ---------------------------------------------------------------------------
# Cancellation (unchanged intent, verified against new machinery)
# ---------------------------------------------------------------------------


def test_a_cancelled_deck_is_skipped_before_generation_starts(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    artifacts.set_artifact_state(db, artifact_id, artifacts.CANCELLED)
    llm.replies = [{"topics": ["delta functions"]}]

    study.run_generation(_deck_job(artifact_id, document_id))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.CANCELLED
    assert artifacts.list_parts(db, artifact_id) == []
    assert llm.calls == []


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

    study.run_generation(_quiz_job(artifact_id, document_id, count=3))

    artifact = artifacts.get_artifact(db, artifact_id)
    assert artifact["state"] == artifacts.CANCELLED
    assert artifacts.list_parts(db, artifact_id) == []


def test_mark_failed_does_not_clobber_a_concurrent_cancellation(
    db: sqlite3.Connection, class_id: int
) -> None:
    """A cancellation that landed keeps the artifact cancelled and its cards, even if a
    late failure write arrives (fix for a mark-failed vs cancel race)."""
    document_id = _document(db, class_id)
    deck_id = _deck(db, class_id, document_id)
    card_id = artifacts.create_part(
        db,
        deck_id,
        artifacts.CARD,
        1,
        content=json.dumps({"front": "F", "back": "B"}),
        content_type=artifacts.JSON,
        status=artifacts.PART_COMPLETE,
    )
    artifacts.set_artifact_state(db, deck_id, artifacts.CANCELLED)

    study._mark_failed(db, deck_id, LyraError("too late"))

    deck = artifacts.get_artifact(db, deck_id)
    assert deck["state"] == artifacts.CANCELLED
    assert [int(part["id"]) for part in artifacts.list_parts(db, deck_id)] == [card_id]


def test_the_solver_reconcile_leaves_study_artifacts_alone(
    db: sqlite3.Connection, class_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueued: list[int] = []
    monkeypatch.setattr(solver, "enqueue", enqueued.append)
    document_id = _document(db, class_id)
    deck_id = _persist_deck_job(db, class_id, document_id)
    quiz_id = _persist_quiz_job(db, class_id, document_id)
    artifacts.set_artifact_state(db, quiz_id, artifacts.GENERATING, "Writing questions")

    solver.reconcile_interrupted(db)

    assert enqueued == []
    assert artifacts.get_artifact(db, deck_id)["state"] == artifacts.PENDING
    assert artifacts.get_artifact(db, quiz_id)["state"] == artifacts.GENERATING


def test_new_card_states_are_due_immediately(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["only topic"]},
        _cards("one", "two", "three", "four"),
    ]

    before = datetime.now(UTC)
    study.run_generation(_deck_job(artifact_id, document_id))

    row = db.execute("select * from card_states").fetchone()
    due = scheduler.from_storage(str(row["due_at"]))
    assert before - timedelta(seconds=5) <= due <= datetime.now(UTC) + timedelta(seconds=5)


@pytest.mark.parametrize("recover", [False, True])
def test_mcq_only_contract_is_enforced_on_initial_and_retry(
    db: sqlite3.Connection,
    class_id: int,
    llm: _StubLLM,
    recover: bool,
) -> None:
    doc = _document(db, class_id)
    artifact_id = _quiz(db, class_id, doc)
    wrong = {**_mcq(), "type": "true_false", "options": ["True", "False"]}
    llm.replies = [{"questions": [wrong]}, {"questions": [_mcq() if recover else wrong]}]
    study.run_generation(_quiz_job(artifact_id, doc, count=1, types=("mcq",)))
    assert len(llm.calls) == 2
    assert artifacts.get_artifact(db, artifact_id)["state"] == (
        artifacts.READY if recover else artifacts.FAILED
    )
    parts = artifacts.list_parts(db, artifact_id)
    assert len(parts) == int(recover)
    assert all(json.loads(str(part["content"]))["type"] == "mcq" for part in parts)


def test_topic_retrieval_retry_never_uses_excluded_document(
    db: sqlite3.Connection,
    class_id: int,
    llm: _StubLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.rag import retrieve as retrieval
    from backend.tests.test_retrieve import _days_ago, _insert_chunk, _insert_document, _vector

    selected = _insert_document(db, class_id, "lecture.pdf", _days_ago(0))
    excluded = _insert_document(db, class_id, "answer-key.pdf", _days_ago(0))
    _insert_chunk(db, class_id, selected, "selected lecture sifting", 1.2)
    for _ in range(80):
        _insert_chunk(db, class_id, excluded, "FORBIDDEN answer-key sifting", 0)
    monkeypatch.setattr(retrieval, "embed_query", lambda _: _vector(0))
    monkeypatch.setattr(type(retrieval.rerank_server), "available", property(lambda _: False))
    monkeypatch.setattr(study, "retrieve", retrieval.retrieve)
    artifact_id = _deck(db, class_id, selected)
    llm.replies = [{"topics": ["sifting"]}, _cards(), _cards("one")]
    study.run_generation(_deck_job(artifact_id, selected, cards_per_topic=1))
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.READY
    assert len(llm.calls) == 3
    assert "FORBIDDEN" not in str(llm.calls)
    assert "selected lecture" in str(llm.calls[1:])
    for part in artifacts.list_parts(db, artifact_id):
        assert {p["document_id"] for p in artifacts.list_provenance(db, int(part["id"]))} == {
            selected
        }


@pytest.mark.parametrize("change", ["delete", "reindex"])
def test_source_change_after_proposal_cannot_publish_stale_card_evidence(
    db: sqlite3.Connection,
    class_id: int,
    llm: _StubLLM,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    doc = _document(db, class_id)
    artifact_id = _deck(db, class_id, doc)
    original = study._complete_deck

    def invalidate(conn: sqlite3.Connection, job: study._Job, topics: list) -> None:
        # The model already saw valid selected chunks. Reindex can finish and return
        # the same document to Ready before publication, so readiness alone is not proof.
        if change == "delete":
            db.execute("delete from documents where id = ?", (doc,))
        else:
            db.execute("delete from chunks where document_id = ?", (doc,))
            db.execute(
                "insert into chunks (document_id, class_id, content, token_count, page_number, "
                "doc_type, embedding_model, embedding_dim) "
                "values (?, ?, 'Replacement indexed text.', 10, 1, 'generic', 'test', 768)",
                (doc, class_id),
            )
        db.commit()
        original(conn, job, topics)

    monkeypatch.setattr(study, "_complete_deck", invalidate)
    llm.replies = [{"topics": ["sifting"]}, _cards("one")]
    study.run_generation(_deck_job(artifact_id, doc, cards_per_topic=1))
    assert len(llm.calls) == 2
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.FAILED
    assert artifacts.list_parts(db, artifact_id) == []


@pytest.mark.parametrize("bad_value", [True, 42, ["answer"], {"answer": "text"}])
@pytest.mark.parametrize("field", ["front", "back"])
def test_deck_rejects_nonstring_card_faces_before_ready(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM, bad_value: object, field: str
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    bad_card: dict[str, object] = {
        "front": "A useful question?",
        "back": "A useful answer",
        "topic": "Topic",
    }
    bad_card[field] = bad_value
    llm.replies = [
        {"topics": ["Topic"]},
        {"cards": [bad_card]},
        {"cards": [{"front": "What is tested?", "back": "Valid string content", "topic": "Topic"}]},
    ]
    study.run_generation(_deck_job(artifact_id, document_id, cards_per_topic=1))
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.READY
    part = artifacts.list_parts(db, artifact_id)[0]
    assert json.loads(str(part["content"]))["front"] == "What is tested?"
    assert len(llm.calls) == 3


def test_deck_tells_later_topics_and_retries_which_questions_are_already_covered(
    db: sqlite3.Connection, class_id: int, llm: _StubLLM
) -> None:
    document_id = _document(db, class_id)
    artifact_id = _deck(db, class_id, document_id)
    llm.replies = [
        {"topics": ["First", "Second"]},
        _cards("Explain the first relationship?", "Compute its value?"),
        _cards("Compare two assumptions?"),
        _cards("Identify the limiting case?"),
    ]
    study.run_generation(_deck_job(artifact_id, document_id, cards_per_topic=2))
    assert artifacts.get_artifact(db, artifact_id)["state"] == artifacts.READY
    first_topic = str(llm.calls[1]["args"][3])
    second_topic = str(llm.calls[2]["args"][3])
    retry = str(llm.calls[3]["args"][3])
    assert "Already covered questions" not in first_topic
    assert "explain the first relationship" in second_topic
    assert "compute its value" in second_topic
    assert "compare two assumptions" in retry
    assert len(llm.calls) == 4


def test_covered_questions_are_bounded_and_count_toward_chunk_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    covered = study._covered_questions({"Explain " + str(i) + "?" for i in range(1_000)})
    assert 0 < len(covered) <= 4_096
    assert study._covered_questions({"x" * 5_000}) == ""
    config = TutorConfig("http://127.0.0.1:9/v1", None, "m", 8192)
    large = _flashcard_chunk("source " * 1_000)
    small = _flashcard_chunk("a usable source")
    ceiling = study._prompt_tokens(study._flashcard_messages("topic", 2, [large]))
    monkeypatch.setattr(study, "_input_ceiling", lambda _config: ceiling)
    assert study._trim_chunks(config, "topic", 2, [large]) == [large]
    assert study._trim_chunks(config, "topic", 2, [large, small], covered=covered) == [small]
    messages = study._flashcard_messages("topic", 2, [small], covered=covered)
    assert study._prompt_tokens(messages) <= ceiling
