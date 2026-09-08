"""Deck count equivalence and boundedness tests for the batched study-list helper.

The production route (`list_study`) still counts each deck through
`routes_study._deck_counts` until the integration wiring lands; those tests compare the
new `deck_counts_for_class` output against that per-deck counter on synthetic fixtures -
empty decks, mixed states, threshold and due-boundary values, lifecycle states, and
separate classes - and check with a trace callback that the helper's statement count
does not grow with the number of decks.
"""

import json
import sqlite3
from contextlib import contextmanager

import pytest

from backend.api import routes_study
from backend.core import artifacts, deck_counts
from backend.core.scheduler import to_storage

NOW = "2026-09-08 12:00:00"


def _document(db: sqlite3.Connection, class_id: int, filename: str = "notes.pdf") -> int:
    document_id = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, "
            "state) values (?, ?, '/tmp/x', 'application/pdf', 1, 'ready')",
            (class_id, filename),
        ).lastrowid
        or 0
    )
    db.commit()
    return document_id


def _deck(
    db: sqlite3.Connection,
    class_id: int,
    document_id: int,
    state: str = "ready",
    title: str = "Midterm deck",
) -> int:
    created = artifacts.create_artifact(
        db,
        class_id,
        title,
        [artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_FLASHCARD_DECK,
    )
    artifact_id = int(created["id"])
    if state != artifacts.PENDING:
        artifacts.set_artifact_state(db, artifact_id, state)
    return artifact_id


def _card(
    db: sqlite3.Connection,
    artifact_id: int,
    ordinal: int,
    *,
    due_at: str = NOW,
    stability: float = 0.0,
    reps: int = 0,
    lapses: int = 0,
    state: str = "new",
    last_review_at: str | None = None,
    commit: bool = True,
) -> int:
    part_id = artifacts.create_part(
        db,
        artifact_id,
        artifacts.CARD,
        ordinal,
        label=f"Card {ordinal}",
        content=json.dumps({"front": "Q", "back": "A"}),
        content_type=artifacts.JSON,
        status=artifacts.PART_COMPLETE,
        commit=False,
    )
    db.execute(
        "insert into card_states (part_id, due_at, stability, difficulty, reps, lapses, "
        "state, last_review_at) values (?, ?, ?, 5.0, ?, ?, ?, ?)",
        (part_id, due_at, stability, reps, lapses, state, last_review_at),
    )
    if commit:
        db.commit()
    return part_id


def _old_counts(db: sqlite3.Connection, class_id: int) -> dict[int, dict[str, object]]:
    """What the current route computes: one per-deck call for each deck in the class."""
    deck_ids = [
        int(row["id"])
        for row in db.execute(
            "select id from artifacts where class_id = ? and kind = ?",
            (class_id, artifacts.KIND_FLASHCARD_DECK),
        )
    ]
    return {deck_id: routes_study._deck_counts(db, deck_id, NOW) for deck_id in deck_ids}


class _StatementCounter:
    """A sqlite3 trace callback counting executed statements on one connection."""

    def __init__(self) -> None:
        self.statements = 0

    def __call__(self, statement: str) -> None:
        self.statements += 1


@contextmanager
def _with_counter(db: sqlite3.Connection, counter: _StatementCounter):
    db.set_trace_callback(counter)
    try:
        yield
    finally:
        db.set_trace_callback(None)


def test_counts_every_deck_including_empty_and_stateless(db, class_id) -> None:
    document_id = _document(db, class_id)
    empty = _deck(db, class_id, document_id, title="Empty deck")
    stateless = _deck(db, class_id, document_id, title="Parts, no states")
    populated = _deck(db, class_id, document_id, title="Populated deck")

    part_id = artifacts.create_part(
        db,
        stateless,
        artifacts.CARD,
        1,
        label="Card 1",
        content=json.dumps({"front": "Q", "back": "A"}),
        content_type=artifacts.JSON,
        status=artifacts.PART_COMPLETE,
    )
    db.commit()
    assert part_id > 0
    _card(db, populated, 1, state="new")

    counts = deck_counts.deck_counts_for_class(db, class_id, NOW)

    assert set(counts) == {empty, stateless, populated}
    assert counts[empty] == {
        "cards_total": 0,
        "buckets": {"new": 0, "learning": 0, "mastered": 0},
        "due_count": 0,
    }
    assert counts[stateless] == counts[empty]
    assert counts[populated] == {
        "cards_total": 1,
        "buckets": {"new": 1, "learning": 0, "mastered": 0},
        "due_count": 1,
    }
    # Same answers as the per-deck counter the route still uses.
    assert counts == _old_counts(db, class_id)


def test_matches_the_per_deck_counter_on_mixed_states(db, class_id) -> None:
    document_id = _document(db, class_id)
    deck_id = _deck(db, class_id, document_id)
    # Six cards covering every scheduler state and the reps/state corner cases:
    # - never reviewed -> new
    # - learning / relearning / review under the mastered threshold -> learning
    # - review at or past the threshold with reviews -> mastered
    # - review state with zero reps -> new (the bucket rule checks reps first)
    _card(db, deck_id, 1)
    _card(db, deck_id, 2, due_at="2026-09-09 12:00:00", stability=3.0, reps=2, state="learning")
    _card(
        db,
        deck_id,
        3,
        due_at="2026-09-08 12:10:00",
        stability=1.5,
        reps=1,
        lapses=1,
        state="relearning",
    )
    _card(
        db,
        deck_id,
        4,
        due_at="2026-09-10 12:00:00",
        stability=10.0,
        reps=5,
        state="review",
    )
    _card(
        db,
        deck_id,
        5,
        due_at="2026-10-08 12:00:00",
        stability=30.0,
        reps=9,
        state="review",
    )
    _card(db, deck_id, 6, due_at="2026-09-08 12:00:00", stability=30.0, reps=0, state="review")

    counts = deck_counts.deck_counts_for_class(db, class_id, NOW)
    # Due now: card 1 (exactly now) and card 6; the relearning card is ten minutes
    # ahead of the horizon, the rest are tomorrow or later.
    expected = {
        "cards_total": 6,
        "buckets": {"new": 2, "learning": 3, "mastered": 1},
        "due_count": 2,
    }
    assert counts[deck_id] == expected
    assert counts[deck_id] == routes_study._deck_counts(db, deck_id, NOW)


def test_keeps_the_mastered_threshold_exact(db, class_id) -> None:
    document_id = _document(db, class_id)
    deck_id = _deck(db, class_id, document_id)
    below = 20.999999999999996  # the largest double just under 21.0
    above = 21.000000000000004  # and just over
    _card(db, deck_id, 1, due_at="2026-09-09 00:00:00", stability=below, reps=4, state="review")
    _card(db, deck_id, 2, due_at="2026-09-09 00:00:00", stability=21.0, reps=4, state="review")
    _card(db, deck_id, 3, due_at="2026-09-09 00:00:00", stability=above, reps=4, state="review")

    counts = deck_counts.deck_counts_for_class(db, class_id, NOW)
    assert counts[deck_id]["buckets"] == {"new": 0, "learning": 1, "mastered": 2}
    assert counts[deck_id] == routes_study._deck_counts(db, deck_id, NOW)


def test_keeps_the_due_boundary_exact(db, class_id) -> None:
    document_id = _document(db, class_id)
    deck_id = _deck(db, class_id, document_id)
    # Due when the stored string is at or before `now`, second precision included.
    _card(db, deck_id, 1, due_at="2026-09-08 12:00:00")  # exactly now
    _card(db, deck_id, 2, due_at="2026-09-08 11:59:59")  # one second early
    _card(db, deck_id, 3, due_at="2026-09-08 12:00:01")  # one second late
    _card(db, deck_id, 4, due_at="2026-09-09 12:00:00")  # tomorrow

    counts = deck_counts.deck_counts_for_class(db, class_id, NOW)
    assert counts[deck_id]["cards_total"] == 4
    assert counts[deck_id]["due_count"] == 2
    assert counts[deck_id] == routes_study._deck_counts(db, deck_id, NOW)


def test_counts_every_lifecycle_state_and_only_decks(db, class_id) -> None:
    document_id = _document(db, class_id)
    generating = _deck(db, class_id, document_id, state="generating", title="Building")
    ready = _deck(db, class_id, document_id, state="ready", title="Done")
    failed = _deck(db, class_id, document_id, state="failed", title="Broken")
    pending = _deck(db, class_id, document_id, state="pending", title="Queued")
    _card(db, generating, 1)
    _card(db, ready, 1)

    artifacts.create_artifact(
        db,
        class_id,
        "Week 5 quiz",
        [artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_QUIZ,
    )
    db.commit()

    counts = deck_counts.deck_counts_for_class(db, class_id, NOW)
    # Every deck state is counted (the list shows them all); quizzes are not decks.
    assert set(counts) == {generating, ready, failed, pending}
    assert counts[generating]["cards_total"] == 1
    assert counts[ready]["cards_total"] == 1
    assert counts[failed]["cards_total"] == 0
    assert counts[pending]["cards_total"] == 0
    assert counts == _old_counts(db, class_id)


def test_never_mixes_classes(db, class_id) -> None:
    other_class = int(
        db.execute("insert into classes (name, code) values ('Econ 101', 'ECON 101')").lastrowid
        or 0
    )
    db.commit()
    document_a = _document(db, class_id)
    document_b = _document(db, other_class)
    deck_a = _deck(db, class_id, document_a)
    deck_b = _deck(db, other_class, document_b)
    _card(db, deck_a, 1)
    _card(db, deck_a, 2)
    _card(db, deck_b, 1)

    counts_a = deck_counts.deck_counts_for_class(db, class_id, NOW)
    counts_b = deck_counts.deck_counts_for_class(db, other_class, NOW)

    assert set(counts_a) == {deck_a}
    assert set(counts_b) == {deck_b}
    assert counts_a[deck_a]["cards_total"] == 2
    assert counts_b[deck_b]["cards_total"] == 1


def test_issues_one_statement_regardless_of_deck_count(db, class_id) -> None:
    document_id = _document(db, class_id)
    deck_ids = [_deck(db, class_id, document_id, title=f"Deck {n}") for n in range(12)]
    for deck_id in deck_ids[:5]:
        _card(db, deck_id, 1)

    counter = _StatementCounter()
    with _with_counter(db, counter):
        counts = deck_counts.deck_counts_for_class(db, class_id, NOW)
    assert counter.statements == 1
    assert set(counts) == set(deck_ids)

    old_counter = _StatementCounter()
    with _with_counter(db, old_counter):
        for deck_id in deck_ids:
            routes_study._deck_counts(db, deck_id, NOW)
    assert old_counter.statements == len(deck_ids)


@pytest.mark.parametrize("deck_count", [1, 5, 16])
def test_statement_count_does_not_grow_with_decks(db, class_id, deck_count: int) -> None:
    document_id = _document(db, class_id)
    for n in range(deck_count):
        deck_id = _deck(db, class_id, document_id, title=f"Deck {n}")
        for ordinal in range(1, (n % 4) + 2):
            _card(db, deck_id, ordinal)

    counter = _StatementCounter()
    with _with_counter(db, counter):
        deck_counts.deck_counts_for_class(db, class_id, NOW)
    assert counter.statements == 1


def test_agrees_with_the_per_deck_counter_on_a_semester_fixture(db, class_id) -> None:
    document_id = _document(db, class_id)
    # A representative semester: lectures (mostly ready), one building, one failed,
    # one empty, one with parts but no states, plus a quiz that must not appear.
    profiles = [
        ("ready", 24),
        ("ready", 18),
        ("generating", 7),
        ("ready", 0),
        ("failed", 12),
        ("pending", 0),
        ("ready", 41),
        ("ready", 0),
    ]
    deck_ids = []
    for index, (state, card_count) in enumerate(profiles):
        deck_id = _deck(db, class_id, document_id, state=state, title=f"Deck {index}")
        deck_ids.append(deck_id)
        for ordinal in range(1, card_count + 1):
            _card(
                db,
                deck_id,
                ordinal,
                commit=False,
                state="review" if ordinal % 3 == 0 else "learning",
                stability=21.0 if ordinal % 5 == 0 else 4.0,
                reps=3 if ordinal % 2 == 0 else 0,
                due_at=NOW if ordinal % 4 == 0 else "2026-09-12 08:00:00",
            )
        db.commit()
    stateless = _deck(db, class_id, document_id, state="ready", title="Stateless deck")
    artifacts.create_part(
        db,
        stateless,
        artifacts.CARD,
        1,
        label="Card 1",
        content=json.dumps({"front": "Q", "back": "A"}),
        content_type=artifacts.JSON,
        status=artifacts.PART_COMPLETE,
    )
    artifacts.create_artifact(
        db,
        class_id,
        "Week 5 quiz",
        [artifacts.SourceSpec(document_id=document_id, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_QUIZ,
    )
    db.commit()

    counts = deck_counts.deck_counts_for_class(db, class_id, NOW)
    assert set(counts) == set(deck_ids) | {stateless}
    assert counts == _old_counts(db, class_id)


def test_due_horizon_is_the_callers_storage_string(db, class_id) -> None:
    """The helper takes `now` from the caller, exactly like the per-deck counter does."""
    from datetime import UTC, datetime

    document_id = _document(db, class_id)
    deck_id = _deck(db, class_id, document_id)
    card_due_at = to_storage(datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC))
    _card(db, deck_id, 1, due_at=card_due_at)

    later = to_storage(datetime(2026, 9, 8, 12, 0, 1, tzinfo=UTC))
    assert deck_counts.deck_counts_for_class(db, class_id, later)[deck_id]["cards_total"] == 1
    assert deck_counts.deck_counts_for_class(db, class_id, later)[deck_id]["due_count"] == 1
    early = to_storage(datetime(2026, 9, 8, 11, 59, 59, tzinfo=UTC))
    assert deck_counts.deck_counts_for_class(db, class_id, early)[deck_id]["due_count"] == 0
