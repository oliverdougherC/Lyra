# PLA-507: batched flashcard-deck counts for the study list

The study hub lists a class's decks and quizzes, and the panel polls
`GET /classes/{id}/study` every 1.5 s while generation runs. Until now each deck's
counts (total cards, bucket split, due count) were computed by
[`_deck_counts`](/backend/api/routes_study.py): one `card_states` query per deck,
each loading every card row and rebuilding the scheduler's value object in Python.
The cost grew with the number of decks, so a semester-sized class paid it on every
poll.

This doc covers the new bounded batched primitive, [`deck_counts_for_class`](/backend/core/deck_counts.py),
the evidence that it is equivalent and bounded, and its integration into the study-list route.

## The helper

```python
def deck_counts_for_class(
    conn: sqlite3.Connection, class_id: int, now: str
) -> dict[int, dict[str, object]]
```

One class-scoped aggregate operation: a single join/group over the class's
flashcard-deck artifacts and their card states. It returns one entry per
flashcard-deck artifact in the class - **including decks with no cards yet** - keyed by
artifact id, each shaped exactly like the per-deck counter's return:

```json
{"cards_total": 12, "buckets": {"new": 4, "learning": 5, "mastered": 3}, "due_count": 6}
```

`now` arrives as a storage-format string (the caller owns the clock, as
`list_study` already does via `scheduler.to_storage`); there is no ambient clock in the
helper. Other classes' decks, and non-deck artifacts (quizzes, solution sets, drafts),
never appear.

## Why the aggregate preserves the existing semantics exactly

The bucket and due rules are the scheduler's, expressed in the aggregate rather than
reimplemented:

- **new** when `reps = 0` or the state is `new` - a card that never graduated is new even
  if its row says `review` (the scheduler checks reviews before state).
- **mastered** when the state is `review`, the card has been reviewed (`reps != 0`), and
  `stability >= scheduler.MASTERED_STABILITY_DAYS` (21 days), bound as a SQL parameter so
  the threshold stays defined once.
- **learning** for everything else (`relearning` and under-threshold `review` included).

That is [`scheduler.bucket`](/backend/core/scheduler.py) minus the datetime fields, which
never enter the bucket decision. The mastered comparison uses the same IEEE-754 doubles
on both sides (the column is `real`, the parameter is the same Python float), so a
stability of exactly 21.0, or the largest double just under/over it, buckets identically.

A card is **due** when its stored `due_at` is lexicographically at or before `now`. The
storage format is chronological, so this is byte-for-byte the comparison the per-deck
counter makes (`str(row["due_at"]) <= now`), with second precision included: a card whose
`due_at` equals `now` is due.

Decks are pulled from `artifacts` (any artifact state - generating, ready, failed,
pending all appear in the list) with `LEFT JOIN`s through `artifact_parts` to
`card_states`, so a deck with no parts, or parts with no state rows yet, still gets its
zero entry.

## Query plan: existing indexes, no new ones

`EXPLAIN QUERY PLAN` on the benchmark database (SQLite):

Old per-deck statement, executed once per deck:

```text
 3  0 SEARCH p USING COVERING INDEX idx_parts_artifact (artifact_id=?)
 9  0 SEARCH cs USING INTEGER PRIMARY KEY (rowid=?)
```

New batched statement:

```text
 9  0 SEARCH a USING INDEX idx_artifacts_class (class_id=?)
18  0 SEARCH p USING COVERING INDEX idx_parts_artifact (artifact_id=?) LEFT-JOIN
24  0 SEARCH cs USING INTEGER PRIMARY KEY (rowid=?) LEFT-JOIN
```

The aggregate rides the existing `idx_artifacts_class`, `idx_parts_artifact`, and the
`card_states` primary key. No new index was proposed or added.

## Evidence

### Equivalence tests

[`backend/tests/test_deck_counts.py`](/backend/tests/test_deck_counts.py) compares the
helper's output against the production per-deck counter (`routes_study._deck_counts`) on
synthetic fixtures:

- empty decks and decks with parts but no state rows (zero entries, present in the result);
- a mixed-state deck covering `new`, `learning`, `relearning`, under-threshold `review`,
  mastered `review`, and `review` with zero reps (which buckets as new);
- the mastered threshold at exactly 21.0 and at the largest doubles just under/over it;
- the due boundary at exactly `now`, one second early, one second late, and a day late;
- decks in every artifact lifecycle state, with quiz and solution artifacts that must not
  be counted;
- two classes that must never mix;
- a semester-shaped fixture compared end to end;
- a trace-callback check that the helper executes **one** statement while the old path
  executes one per deck, at deck counts of 1, 5, and 16.

### Benchmark

[`scripts/benchmark_deck_counts.py`](/scripts/benchmark_deck_counts.py) builds a temporary
SQLite database (fresh temp directory, deleted on exit - it never opens user data) with a
synthetic semester, pins the clock to a storage-format string, and times the old path
(calling the real `routes_study._deck_counts`) against the new helper. Run it from the
repository root:

```text
uv run python scripts/benchmark_deck_counts.py
uv run python scripts/benchmark_deck_counts.py --json
uv run python scripts/benchmark_deck_counts.py --decks 30 --cards-per-deck 150
```

Recorded results (synthetic, repeatable - no randomness, fixed fixture, median over the
stated iterations):

| Fixture | Statements (old → new) | Median (old → new) | Peak allocation (old → new) |
| --- | --- | --- | --- |
| 12 decks, 866 cards (50 runs) | 13 → 1 | 2.80 ms → 0.107 ms | 24.7 KiB → 1.9 KiB |
| 30 decks, 3700 cards (30 runs) | 31 → 1 | 12.1 ms → 0.39 ms | 43.3 KiB → 3.6 KiB |

The statement count is the load-bearing number: it is 1 for the helper at any deck count,
while the benchmark old path includes one deck-ID query plus one query per deck.
In the full route, the existing inventory query remains; deck counting adds one aggregate
instead of one query per deck on every 1.5 s poll.
The equivalence gate inside the benchmark re-checks old-vs-new on every run and fails the
process on any mismatch.

These are modest, synthetic, same-machine measurements; they show the shape of the
improvement (constant statements, no per-deck Python pass) and are not a device or
fleet claim.

## Study-list integration

[`list_study`](/backend/api/routes_study.py) uses the helper with one batch call before the
loop when its inventory contains decks:

```python
now = scheduler.to_storage(datetime.now(UTC))
deck_counts = deck_counts_for_class(conn, class_id, now)
for row in rows:
    entry = dict(row)
    if row["kind"] == artifacts.KIND_FLASHCARD_DECK:
        entry.update(deck_counts[int(row["id"])])
        decks.append(entry)
    else:
        ...
```

The helper covers the same class, kind and states as the inventory. If a deck is deleted
between inventory and aggregation, the route returns zero counts for that stale inventory
entry, matching the prior per-deck read. Quiz-only lists skip the aggregate. The existing per-deck behavior - counts for a single
deck's cards - is unchanged, and no scheduler, polling, cache, or denormalized-counter
change is part of this work.

Integration tests also compare the complete route counts at 1 and 16 decks, assert constant
statement count, preserve quiz-only progress queries, and cover concurrent deck deletion.
Legacy negative review counts retain the existing scheduler bucket behavior.
