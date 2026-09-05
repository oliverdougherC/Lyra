"""Spaced repetition for flashcards: a simplified FSRS/SM-2 hybrid.

Pure functions, no I/O, and no ambient clock: every function takes `now` from the caller,
so a test pins time and the API layer owns the wall clock. Scheduling state lives on
`card_states` rows (migration 016), one per card part, and is rewritten in full on every
review; there is no history here because the review log is the history.

Success on a new card seeds its interval. Subsequent growth requires both a due card
and at least 24 hours since its last rating. Early practice is still recorded, but keeps
its strength, state, and deadline. A due learning/relearning success can graduate within
24 hours without growth (one-day floor). Again always decays strength and schedules ten
minutes. Stability and intervals are capped at 365 days, including legacy inflated state.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite

RATINGS: tuple[str, ...] = ("again", "hard", "good", "easy")

NEW = "new"
LEARNING = "learning"
RELEARNING = "relearning"
REVIEW = "review"
CARD_STATES: tuple[str, ...] = (NEW, LEARNING, RELEARNING, REVIEW)

INITIAL_DIFFICULTY = 5.0
MIN_DIFFICULTY = 1.0
MAX_DIFFICULTY = 10.0

# How one rating moves the display knob. `again` teaches the least per review, so it
# pushes hardest toward difficult; `easy` is the one rating that lowers it.
DIFFICULTY_DELTAS: dict[str, float] = {"again": 1.0, "hard": 0.5, "good": -0.1, "easy": -0.5}

# Keep legacy inflated states and new schedules within a useful, finite horizon.
MAX_STABILITY_DAYS = 365.0
MIN_SPACED_ELAPSED = timedelta(days=1)
MAX_SCHEDULE_DATE = datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC)
# Seed new cards and successful relearning at one day before any earned growth.
STABILITY_SEED_FLOOR = 1.0
STABILITY_FACTORS: dict[str, float] = {"hard": 1.2, "good": 2.0, "easy": 2.8}

# A lapse keeps a fifth of the strength the card had earned, but never schedules inside
# half a day: the floor is what stops a well-worn card from collapsing to "again in an
# hour" after one slip months in.
LAPSE_DECAY = 0.2
LAPSE_FLOOR_DAYS = 0.5

# `again` re-shows the card in the same session rather than tomorrow.
RELEARN_INTERVAL = timedelta(minutes=10)

# A card is mastered when it is in the long-term state and its interval has reached three
# weeks: past that, forgetting is slow enough that the deck stops being daily work.
MASTERED_STABILITY_DAYS = 21.0


@dataclass(frozen=True)
class CardState:
    """One card's scheduling state, as values; updates return a new one.

    Attributes:
        due_at: When the card next wants review. New cards are due immediately.
        stability: Estimated memory strength in days.
        difficulty: Display knob in [1, 10]; not a scheduling input.
        reps: Completed reviews.
        lapses: Times a graduated or learning card was rated `again`.
        state: new, learning, relearning, or review.
        last_review_at: When the last review happened, if any.
    """

    due_at: datetime
    stability: float
    difficulty: float
    reps: int
    lapses: int
    state: str
    last_review_at: datetime | None


def new_card_state(now: datetime) -> CardState:
    """A fresh card: due immediately, nothing known about it yet."""
    return CardState(
        due_at=now,
        stability=0.0,
        difficulty=INITIAL_DIFFICULTY,
        reps=0,
        lapses=0,
        state=NEW,
        last_review_at=None,
    )


# The storage format for due_at and last_review_at: what SQLite's `datetime('now')`
# writes, so a lexicographic comparison in SQL is a chronological one.
_STORAGE_FORMAT = "%Y-%m-%d %H:%M:%S"


def to_storage(value: datetime) -> str:
    """Serialize a UTC-aware datetime for a card_states row."""
    return value.astimezone(UTC).strftime(_STORAGE_FORMAT)


def from_storage(value: str) -> datetime:
    """Read a card_states timestamp back, UTC-aware."""
    return datetime.strptime(value, _STORAGE_FORMAT).replace(tzinfo=UTC)


def _bounded(value: float, low: float, high: float, fallback: float) -> float:
    return min(high, max(low, value)) if isfinite(value) else fallback


def _due_after(now: datetime, interval: timedelta) -> datetime:
    return now + min(interval, MAX_SCHEDULE_DATE - now)


def review(card: CardState, rating: str, now: datetime) -> CardState:
    """Apply one rating and return the next state.

    Args:
        card: The card's current state.
        rating: One of `again`, `hard`, `good`, `easy`.
        now: When the review happened.

    Returns:
        The new state. The input is not mutated.

    Raises:
        ValueError: on an unknown rating.
    """
    if rating not in RATINGS:
        raise ValueError(f"Unknown rating: {rating}")

    strength = _bounded(card.stability, 0.0, MAX_STABILITY_DAYS, STABILITY_SEED_FLOOR)
    difficulty = _bounded(
        _bounded(card.difficulty, MIN_DIFFICULTY, MAX_DIFFICULTY, INITIAL_DIFFICULTY)
        + DIFFICULTY_DELTAS[rating],
        MIN_DIFFICULTY,
        MAX_DIFFICULTY,
        INITIAL_DIFFICULTY,
    )

    if rating == "again":
        # A card that never graduated has nothing to lapse out of, so it stays in
        # learning; only a card that reached review (or was already relearning) enters
        # relearning.
        state = RELEARNING if card.state in (REVIEW, RELEARNING) else LEARNING
        return CardState(
            due_at=_due_after(now, RELEARN_INTERVAL),
            stability=max(strength * LAPSE_DECAY, LAPSE_FLOOR_DAYS),
            difficulty=difficulty,
            reps=card.reps + 1,
            lapses=card.lapses + 1,
            state=state,
            last_review_at=now,
        )

    fresh = card.state == NEW and card.reps == 0
    due = card.due_at <= now
    spaced = (
        due
        and card.last_review_at is not None
        and (now - card.last_review_at >= MIN_SPACED_ELAPSED)
    )
    if fresh or spaced:
        stability = min(
            MAX_STABILITY_DAYS, max(strength, STABILITY_SEED_FLOOR) * STABILITY_FACTORS[rating]
        )
        state = REVIEW if rating == "easy" or card.state != NEW else LEARNING
        due_at = _due_after(now, timedelta(days=stability))
    elif due:
        stability = max(strength, STABILITY_SEED_FLOOR)
        state = REVIEW
        due_at = _due_after(now, timedelta(days=stability))
    else:
        stability = strength
        state = card.state
        due_at = max(now, min(card.due_at, _due_after(now, timedelta(days=MAX_STABILITY_DAYS))))
    return CardState(
        due_at=due_at,
        stability=stability,
        difficulty=difficulty,
        reps=card.reps + 1,
        lapses=card.lapses,
        state=state,
        last_review_at=now,
    )


def bucket(card: CardState) -> str:
    """The deck-panel grouping: new, still being learned, or mastered."""
    if card.reps == 0 or card.state == NEW:
        return NEW
    if card.state == REVIEW and card.stability >= MASTERED_STABILITY_DAYS:
        return "mastered"
    return LEARNING


def study_order(states: dict[int, CardState], now: datetime) -> list[int]:
    """Part ids in the order a session should serve them.

    Due cards first, ordered new before learning before review (a card never seen is the
    most valuable thing a session can serve, and a review card can absorb a delay better
    than a learning one), ties by soonest due. Not-yet-due cards follow by soonest due,
    so a session queue never runs dry. Part id is the final tiebreak, so one queue is the
    same queue on every call.
    """
    priority = {NEW: 0, LEARNING: 1, RELEARNING: 1, REVIEW: 2}
    due = sorted(
        (part_id for part_id, card in states.items() if card.due_at <= now),
        key=lambda part_id: (priority[states[part_id].state], states[part_id].due_at, part_id),
    )
    upcoming = sorted(
        (part_id for part_id, card in states.items() if card.due_at > now),
        key=lambda part_id: (states[part_id].due_at, part_id),
    )
    return due + upcoming
