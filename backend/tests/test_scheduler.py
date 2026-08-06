"""Contract tests for the spaced-repetition scheduler.

Every case pins `now`, because the functions take their clock from the caller; that is
what makes a schedule assertable down to the minute. The same cases are mirrored in
frontend/tests/scheduler.test.ts against the TypeScript port, so the two cannot drift.
"""

from datetime import UTC, datetime, timedelta

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
    graduated = review(review(new_card_state(NOW), "good", NOW), "good", NOW)
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
    graduated = review(review(new_card_state(NOW), "good", NOW), "good", NOW)
    lapsed = review(graduated, "again", NOW)
    recovered = review(lapsed, "good", NOW)

    assert recovered.state == "review"
    # The seed floor applies before scaling: the lapsed 0.8 is seeded to 1.0, then doubled.
    assert recovered.stability == pytest.approx(2.0)


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
