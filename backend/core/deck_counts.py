"""Bounded batch counting of flashcard-deck review states, for the study list.

The study hub lists a class's decks and quizzes; each deck entry carries its card
counts (total, bucket split, due count) so the panel can show "3 due" without opening
the deck. The list endpoint polls every 1.5 seconds while generation runs, and the old
path computed these counts with one `card_states` query per deck plus a Python pass
building the scheduler's value object for every card - the cost grew with the number of
decks, so a semester-sized class paid it on every poll.

This helper collapses that into one class-scoped aggregate operation: a single
join/group over the class's flashcard-deck artifacts and their card states, returning
exactly what the per-deck counter produced, for every deck in the class including
decks with no cards yet.

The semantics are the existing scheduler's, expressed in the aggregate rather than
reimplemented:

- A card is `new` when it has never been reviewed (`reps = 0`) or still carries the
  `new` state; `mastered` when it is in the long-term `review` state with stability at
  or above `scheduler.MASTERED_STABILITY_DAYS` (a `reps = 0` card is new even in
  `review`); everything else is `learning`. That is `scheduler.bucket` minus the
  datetime fields, which never enter the bucket decision.
- A card is due when its stored `due_at` is lexicographically at or before `now`. The
  storage format is chronological, so this is the same comparison the per-deck counter
  makes, with `now` in the same storage format the route already computes.

`now` arrives as a storage-format string (the caller owns the clock, as it does for the
per-deck counter), and the returned mapping is keyed by artifact id with the same entry
shape the list endpoint already builds.
"""

import sqlite3

from backend.core import artifacts, scheduler

# One class-scoped aggregate: every flashcard-deck artifact in the class (any state,
# including decks with no parts yet) with its card totals. The joins ride existing
# indexes - the class on artifacts, the artifact id on parts, the part id on card
# states - so the statement count does not grow with the number of decks.
_QUERY = """
select a.id as artifact_id,
       count(cs.part_id) as cards_total,
       sum(case when cs.reps = 0 or cs.state = 'new' then 1 else 0 end) as new_count,
       sum(case when cs.state = 'review' and cs.stability >= ? and cs.reps > 0
             then 1 else 0 end) as mastered_count,
       sum(case when cs.due_at <= ? then 1 else 0 end) as due_count
from artifacts a
left join artifact_parts p on p.artifact_id = a.id
left join card_states cs on cs.part_id = p.id
where a.class_id = ? and a.kind = ?
group by a.id
"""


def deck_counts_for_class(
    conn: sqlite3.Connection, class_id: int, now: str
) -> dict[int, dict[str, object]]:
    """Per-deck card counts for every flashcard deck in a class, in one aggregate.

    Args:
        conn: Open database connection.
        class_id: The class whose decks are counted; other classes never appear.
        now: The "due horizon" in the card_states storage format (see
            `scheduler.to_storage`); a card is due when its `due_at` is at or before it.

    Returns:
        One entry per flashcard-deck artifact in the class - including decks with no
        cards yet - keyed by artifact id: `{"cards_total": int, "buckets": {"new",
        "learning", "mastered": int}, "due_count": int}`, the same shape the per-deck
        counter returns. Bucket membership and the due comparison follow the scheduler's
        existing rules (see the module docstring).
    """
    rows = conn.execute(
        _QUERY,
        (scheduler.MASTERED_STABILITY_DAYS, now, class_id, artifacts.KIND_FLASHCARD_DECK),
    ).fetchall()

    counts: dict[int, dict[str, object]] = {}
    for row in rows:
        total = int(row["cards_total"])
        new = int(row["new_count"] or 0)
        mastered = int(row["mastered_count"] or 0)
        counts[int(row["artifact_id"])] = {
            "cards_total": total,
            "buckets": {"new": new, "learning": total - new - mastered, "mastered": mastered},
            "due_count": int(row["due_count"] or 0),
        }
    return counts
