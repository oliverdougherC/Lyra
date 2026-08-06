"""Spaced repetition for flashcards: a simplified FSRS/SM-2 hybrid.

Pure functions, no I/O, and no ambient clock: every function takes `now` from the caller,
so a test pins time and the API layer owns the wall clock. Scheduling state lives on
`card_states` rows (migration 016), one per card part, and is rewritten in full on every
review; there is no history here because the review log is the history.

The model, in one paragraph. `stability` is estimated memory strength in days: a card
with stability S should still be recallable about S days after its last success. It only
grows on success and only shrinks, never below a positive floor, on a lapse. `difficulty`
is a 1-10 ease knob kept for display; scheduling keys off the rating directly. Intervals
equal the new stability, which is what makes the schedule expand as recall proves out.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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

# Applied before scaling, so a fresh card never multiplies zero: the first success on a
# new card schedules it a day out rather than never.
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

    difficulty = min(
        MAX_DIFFICULTY, max(MIN_DIFFICULTY, card.difficulty + DIFFICULTY_DELTAS[rating])
    )

    if rating == "again":
        # A card that never graduated has nothing to lapse out of, so it stays in
        # learning; only a card that reached review (or was already relearning) enters
        # relearning.
        state = RELEARNING if card.state in (REVIEW, RELEARNING) else LEARNING
        return CardState(
            due_at=now + RELEARN_INTERVAL,
            stability=max(card.stability * LAPSE_DECAY, LAPSE_FLOOR_DAYS),
            difficulty=difficulty,
            reps=card.reps + 1,
            lapses=card.lapses + 1,
            state=state,
            last_review_at=now,
        )

    stability = max(card.stability, STABILITY_SEED_FLOOR) * STABILITY_FACTORS[rating]
    # A new card rated `easy` is one the learner already knows, so it fast-tracks to
    # review; any other first success still wants a near-term second look.
    state = REVIEW if rating == "easy" or card.state != NEW else LEARNING
    return CardState(
        due_at=now + timedelta(days=stability),
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
