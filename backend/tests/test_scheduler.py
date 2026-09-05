"""Contract tests for the spaced-repetition scheduler.

Every case pins `now`, because the functions take their clock from the caller; that is
what makes a schedule assertable down to the minute. The same cases are mirrored in
frontend/tests/scheduler.test.ts against the TypeScript port, so the two cannot drift.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path

import pytest

from backend.core import scheduler
from backend.core.scheduler import CardState, bucket, new_card_state, review, study_order

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)


def test_a_new_card_is_due_immediately() -> None:
    card = new_card_state(NOW)

    assert card.due_at == NOW
    assert card.stability == 0.0
    assert card.difficulty == 5.0
    assert card.reps == 0
    assert card.lapses == 0
    assert card.state == "new"
    assert card.last_review_at is None


def test_easy_on_a_new_card_fast_tracks_to_review() -> None:
    card = review(new_card_state(NOW), "easy", NOW)

    assert card.state == "review"
    # The seed floor times the easy factor: 1.0 * 2.8 days out, never a zero interval.
    assert card.stability == pytest.approx(2.8)
    assert card.due_at == NOW + timedelta(days=2.8)


def test_good_twice_grows_the_interval_monotonically() -> None:
    first = review(new_card_state(NOW), "good", NOW)
    second = review(first, "good", first.due_at)

    assert first.state == "learning"
    assert second.state == "review"
    assert first.stability == pytest.approx(2.0)
    assert second.stability == pytest.approx(4.0)
    assert second.due_at > first.due_at


def test_again_on_a_review_card_relearns_with_a_decayed_positive_stability() -> None:
    graduated = review(new_card_state(NOW), "easy", NOW)
    assert graduated.state == "review"

    lapsed = review(graduated, "again", NOW)

    assert lapsed.state == "relearning"
    assert lapsed.due_at == NOW + timedelta(minutes=10)
    assert lapsed.stability == pytest.approx(max(graduated.stability * 0.2, 0.5))
    assert lapsed.stability > 0
    assert lapsed.lapses == 1


def test_again_on_a_card_that_never_graduated_stays_learning() -> None:
    """A card that never reached review has nothing to lapse out of."""
    learning = review(new_card_state(NOW), "good", NOW)
    lapsed = review(learning, "again", NOW)

    assert lapsed.state == "learning"
    assert lapsed.lapses == 1


def test_a_lapse_then_a_success_returns_to_review() -> None:
    graduated = review(new_card_state(NOW), "easy", NOW)
    lapsed = review(graduated, "again", NOW)
    recovered = review(lapsed, "good", lapsed.due_at)

    assert recovered.state == "review"
    # Due relearning graduates without multiplying evidence from immediate recall.
    assert recovered.stability == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("ratings", "expected"),
    [
        (["again"] * 6, 10.0),
        (["easy"] * 8, 1.0),
    ],
)
def test_difficulty_clamps_at_both_ends(ratings: list[str], expected: float) -> None:
    card = new_card_state(NOW)
    for rating in ratings:
        card = review(card, rating, NOW)

    assert card.difficulty == expected


def test_bucket_boundaries() -> None:
    fresh = new_card_state(NOW)
    assert bucket(fresh) == "new"

    seen_once = review(fresh, "good", NOW)
    assert seen_once.reps == 1
    assert bucket(seen_once) == "learning"

    # Exactly at the boundary a review card is mastered; a day under it is not.
    at = CardState(
        due_at=NOW,
        stability=scheduler.MASTERED_STABILITY_DAYS,
        difficulty=5.0,
        reps=3,
        lapses=0,
        state="review",
        last_review_at=NOW,
    )
    under = CardState(**{**vars(at), "stability": scheduler.MASTERED_STABILITY_DAYS - 1})
    assert bucket(at) == "mastered"
    assert bucket(under) == "learning"


def test_study_order_puts_due_first_new_before_learning_before_review() -> None:
    overdue_review = CardState(
        due_at=NOW - timedelta(days=2),
        stability=30.0,
        difficulty=5.0,
        reps=5,
        lapses=0,
        state="review",
        last_review_at=NOW,
    )
    overdue_new = new_card_state(NOW - timedelta(days=1))
    overdue_learning = CardState(
        due_at=NOW - timedelta(hours=3),
        stability=2.0,
        difficulty=5.0,
        reps=1,
        lapses=0,
        state="learning",
        last_review_at=NOW,
    )
    not_due = CardState(
        due_at=NOW + timedelta(days=1),
        stability=4.0,
        difficulty=5.0,
        reps=2,
        lapses=0,
        state="review",
        last_review_at=NOW,
    )
    states = {1: overdue_review, 2: overdue_new, 3: overdue_learning, 4: not_due}

    assert study_order(states, NOW) == [2, 3, 1, 4]


def test_study_order_is_deterministic_for_a_fixed_now() -> None:
    card = new_card_state(NOW - timedelta(hours=1))
    states = {part_id: card for part_id in (5, 3, 1)}

    assert study_order(states, NOW) == [1, 3, 5]
    assert study_order(states, NOW) == study_order(states, NOW)


def test_an_unknown_rating_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown rating"):
        review(new_card_state(NOW), "perfect", NOW)


@pytest.mark.parametrize(
    "case",
    json.loads((Path(__file__).parent / "fixtures/scheduler_contract.json").read_text()),
    ids=lambda case: case["name"],
)
def test_shared_scheduler_contract(case: dict) -> None:
    initial = case["initial"]
    card = replace(
        new_card_state(NOW),
        stability=initial["stability"],
        state=initial["state"],
        reps=1,
        due_at=NOW + timedelta(seconds=initial["due_seconds"]),
        last_review_at=(
            None
            if initial["last_seconds"] is None
            else NOW + timedelta(seconds=initial["last_seconds"])
        ),
    )
    now = NOW + timedelta(seconds=case["at_seconds"])
    result = review(card, case["rating"], now)
    expected = case["expected"]
    assert result.stability == pytest.approx(expected["stability"])
    assert result.state == expected["state"]
    assert result.due_at == NOW + timedelta(seconds=expected["due_seconds"])
    assert result.reps == 2
    assert result.last_review_at == now
    assert result.lapses == (1 if case["rating"] == "again" else 0)


@pytest.mark.parametrize("rating", ["good", "easy"])
def test_100_restart_practice_ratings_preserve_strength_and_deadline(rating: str) -> None:
    first = review(new_card_state(NOW), rating, NOW)
    card = first
    for second in range(1, 101):
        now = NOW + timedelta(seconds=second)
        # Restart deliberately keeps upcoming cards in the available queue.
        assert study_order({1: card}, now) == [1]
        card = review(card, rating, now)
        assert card.stability == first.stability
        assert card.due_at == first.due_at
        assert card.state == first.state
        assert bucket(card) == "learning"
    assert card.reps == 101
    assert card.last_review_at == now


@pytest.mark.parametrize("strength", [float("inf"), float("-inf"), float("nan"), -1e300])
@pytest.mark.parametrize("rating", scheduler.RATINGS)
def test_pathological_numbers_remain_finite(strength: float, rating: str) -> None:
    card = replace(new_card_state(NOW), stability=strength, difficulty=float("nan"))
    result = review(card, rating, NOW)
    assert isfinite(result.stability)
    assert 0 <= result.stability <= scheduler.MAX_STABILITY_DAYS
    assert scheduler.MIN_DIFFICULTY <= result.difficulty <= scheduler.MAX_DIFFICULTY
    assert NOW <= result.due_at <= NOW + timedelta(days=365)


@pytest.mark.parametrize("rating", scheduler.RATINGS)
def test_date_ceiling_is_safe(rating: str) -> None:
    now = scheduler.MAX_SCHEDULE_DATE - timedelta(minutes=1)
    result = review(new_card_state(now), rating, now)
    assert result.due_at == scheduler.MAX_SCHEDULE_DATE
    assert scheduler.from_storage(scheduler.to_storage(result.due_at)).year == 9999
