"""Benchmark for the batched study-list deck counts (PLA-507).

Measures the old per-deck counting path (one `card_states` query plus a Python pass per
deck, as `list_study` does today) against the new one-statement class-scoped aggregate
(`deck_counts_for_class`), on a temporary SQLite database filled with synthetic
semester-sized fixtures. It never opens, reads, or writes user data: the database lives
in a fresh temporary directory and is deleted on exit.

What it records (repeatable - no randomness, pinned clock, fixed fixture):

- SQL statement counts via a trace callback: the old path grows with the deck count, the
  new path is one statement.
- Wall time (median/min over a fixed number of iterations) for each path.
- Peak memory allocated (tracemalloc) by each path, run in isolation.
- `EXPLAIN QUERY PLAN` output for both statements, to show the joins ride existing
  indexes.
- An equivalence gate: the two paths must produce identical counts or the run fails.

Run from the repository root:

    uv run python scripts/benchmark_deck_counts.py
    uv run python scripts/benchmark_deck_counts.py --json
    uv run python scripts/benchmark_deck_counts.py --decks 20 --iterations 100
"""

import argparse
import contextlib
import json
import shutil
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path

from backend.api import routes_study
from backend.config import settings
from backend.core import artifacts, deck_counts
from backend.storage.database import connect, migrate

# A pinned horizon, in the card_states storage format; the old and new paths compare
# against the same string, exactly as the route passes one `now` to every deck.
NOW = "2026-09-08 12:00:00"
DUE_SOON = "2026-09-08 12:00:00"
DUE_LATER = "2026-09-15 08:00:00"

# The per-deck statement the route still runs (copied verbatim from
# `routes_study._deck_counts` for the EXPLAIN QUERY PLAN only; the timed runs call the
# real function).
OLD_PER_DECK_SQL = (
    "select cs.* from card_states cs "
    "join artifact_parts p on p.id = cs.part_id where p.artifact_id = ?"
)


def _seed_semester(db: sqlite3.Connection, class_id: int, decks: int, cards_per_deck: int) -> dict:
    """A synthetic semester: `decks` flashcard decks (mostly ready, one generating,
    one failed, one empty) plus quiz and solution artifacts that must not be counted."""
    document = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, "
            "state) values (?, ?, '/tmp/x', 'application/pdf', 1, 'ready')",
            (class_id, "lecture-1.pdf"),
        ).lastrowid
        or 0
    )
    db.commit()

    def make_deck(state: str, title: str) -> int:
        created = artifacts.create_artifact(
            db,
            class_id,
            title,
            [artifacts.SourceSpec(document_id=document, role=artifacts.STUDY_SOURCE)],
            kind=artifacts.KIND_FLASHCARD_DECK,
            commit=False,
        )
        deck_id = int(created["id"])
        if state != artifacts.PENDING:
            db.execute(
                "update artifacts set state = ?, updated_at = datetime('now') where id = ?",
                (state, deck_id),
            )
        return deck_id

    deck_ids: list[int] = []
    card_rows: list[tuple[int, str, float, int, int, str, str]] = []
    profiles: list[tuple[str, int]] = []
    for index in range(decks):
        if index == decks - 2:
            profiles.append(("failed", 0))
        elif index == decks - 1:
            profiles.append(("ready", 0))
        elif index % 5 == 4:
            profiles.append(("generating", max(8, cards_per_deck // 3)))
        else:
            profiles.append(("ready", cards_per_deck))
    for index, (state, card_count) in enumerate(profiles):
        deck_id = make_deck(state, f"Lecture deck {index + 1}")
        deck_ids.append(deck_id)
        for ordinal in range(1, card_count + 1):
            if ordinal % 7 == 0:
                state_name, reps, lapses = "new", 0, 0
            elif ordinal % 2 == 0:
                state_name, reps, lapses = "review", 1 + ordinal % 9, 0
            else:
                state_name, reps, lapses = "learning", 1 + ordinal % 5, ordinal % 3
            stability = 21.0 if ordinal % 5 == 0 else 5.0 + (ordinal % 10)
            due = DUE_SOON if ordinal % 3 == 0 else DUE_LATER
            card_rows.append((deck_id, ordinal, state_name, stability, reps, lapses, due))
    # Quiz and solution artifacts share the class; neither is a flashcard deck, so the
    # helper must not count them. The solution set needs its own problem-set source.
    artifacts.create_artifact(
        db,
        class_id,
        "Week 5 quiz",
        [artifacts.SourceSpec(document_id=document, role=artifacts.STUDY_SOURCE)],
        kind=artifacts.KIND_QUIZ,
        commit=False,
    )
    problem_doc = int(
        db.execute(
            "insert into documents (class_id, filename, stored_path, mime, byte_size, "
            "state) values (?, ?, '/tmp/x', 'application/pdf', 1, 'ready')",
            (class_id, "problem-set.pdf"),
        ).lastrowid
        or 0
    )
    artifacts.create_artifact(
        db,
        class_id,
        "Set 3",
        [artifacts.SourceSpec(document_id=problem_doc, role=artifacts.PROBLEM_SET)],
        kind=artifacts.KIND_SOLUTION_SET,
        commit=False,
    )
    db.executemany(
        "insert into artifact_parts (artifact_id, kind, ordinal, label, content, "
        "content_type, status) values (?, 'card', ?, ?, '{}', 'json', 'complete')",
        [(deck_id, ordinal, f"Card {ordinal}") for deck_id, ordinal, _, _, _, _, _ in card_rows],
    )
    # One static query (the parts are the only card parts in this synthetic class), then
    # the join to state rows happens in Python, keyed by (artifact, ordinal).
    part_by_key = {
        (int(row["artifact_id"]), int(row["ordinal"])): int(row["id"])
        for row in db.execute(
            "select artifact_id, ordinal, id from artifact_parts where kind = 'card' "
            "order by artifact_id, ordinal"
        )
    }
    db.executemany(
        "insert into card_states (part_id, due_at, stability, difficulty, reps, lapses, "
        "state, last_review_at) values (?, ?, ?, 5.0, ?, ?, ?, null)",
        [
            (
                part_by_key[(deck_id, ordinal)],
                due,
                stability,
                reps,
                lapses,
                state,
            )
            for deck_id, ordinal, state, stability, reps, lapses, due in card_rows
        ],
    )
    db.commit()
    return {"decks": deck_ids, "cards": len(card_rows)}


class _StatementCounter:
    def __init__(self) -> None:
        self.statements = 0

    def __call__(self, statement: str) -> None:
        self.statements += 1


def _count_statements(db: sqlite3.Connection, fn) -> tuple[object, int]:
    counter = _StatementCounter()
    db.set_trace_callback(counter)
    try:
        result = fn()
    finally:
        db.set_trace_callback(None)
    return result, counter.statements


def _old_path(db: sqlite3.Connection, class_id: int) -> dict[int, dict[str, object]]:
    def run() -> dict[int, dict[str, object]]:
        deck_ids = [
            int(row["id"])
            for row in db.execute(
                "select id from artifacts where class_id = ? and kind = ? order by updated_at desc",
                (class_id, artifacts.KIND_FLASHCARD_DECK),
            )
        ]
        return {deck_id: routes_study._deck_counts(db, deck_id, NOW) for deck_id in deck_ids}

    return run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decks", type=int, default=12, help="Number of decks (default 12)")
    parser.add_argument(
        "--cards-per-deck", type=int, default=100, help="Cards per ready deck (default 100)"
    )
    parser.add_argument("--iterations", type=int, default=50, help="Timed iterations (default 50)")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="lyra-deck-counts-bench-"))
    try:
        settings.data_dir = work
        settings.db_path = work / "lyra.db"
        settings.ensure_directories()
        db = connect()
        migrate(db)
        class_id = int(
            db.execute("insert into classes (name, code) values ('Signals', 'ECE 203')").lastrowid
            or 0
        )
        db.commit()
        fixture = _seed_semester(db, class_id, args.decks, args.cards_per_deck)

        old = _old_path(db, class_id)
        new = deck_counts.deck_counts_for_class(db, class_id, NOW)
        if old != new:
            for deck_id in sorted(set(old) | set(new)):
                if old.get(deck_id) != new.get(deck_id):
                    print(
                        f"EQUIVALENCE MISMATCH deck {deck_id}: "
                        f"{old.get(deck_id)} != {new.get(deck_id)}"
                    )
            raise SystemExit("The batched helper disagrees with the per-deck counter.")

        def run_old() -> None:
            _old_path(db, class_id)

        def run_new() -> None:
            deck_counts.deck_counts_for_class(db, class_id, NOW)

        _, old_stmts = _count_statements(db, run_old)
        _, new_stmts = _count_statements(db, run_new)

        def median_time(fn) -> tuple[float, float, float]:
            for _ in range(3):
                fn()
            samples: list[float] = []
            for _ in range(args.iterations):
                start = time.perf_counter_ns()
                fn()
                samples.append(time.perf_counter_ns() - start)
            median = statistics.median(samples) / 1e6
            minimum = min(samples) / 1e6
            mean = statistics.fmean(samples) / 1e6
            return median, minimum, mean

        def peak_alloc(fn) -> int:
            tracemalloc.start()
            try:
                fn()
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            return peak

        old_median, old_min, old_mean = median_time(run_old)
        new_median, new_min, new_mean = median_time(run_new)
        old_peak = peak_alloc(run_old)
        new_peak = peak_alloc(run_new)

        old_plan = db.execute("explain query plan " + OLD_PER_DECK_SQL, (0,)).fetchall()
        new_plan = db.execute(
            "explain query plan " + deck_counts._QUERY,
            (
                21.0,
                NOW,
                class_id,
                artifacts.KIND_FLASHCARD_DECK,
            ),
        ).fetchall()

        def plan_text(rows: list) -> list[str]:
            return [f"{row['id']:>2} {row['parent']:>2} {row['detail']}" for row in rows]

        result = {
            "fixture": {
                "decks": args.decks,
                "cards_per_deck": args.cards_per_deck,
                "total_decks": len(fixture["decks"]),
                "total_cards": fixture["cards"],
                "clock": NOW,
                "database": "temporary SQLite, synthetic rows only",
            },
            "statements": {"old_per_deck_path": old_stmts, "new_batched_path": new_stmts},
            "median_ms": {"old": round(old_median, 3), "new": round(new_median, 3)},
            "min_ms": {"old": round(old_min, 3), "new": round(new_min, 3)},
            "mean_ms": {"old": round(old_mean, 3), "new": round(new_mean, 3)},
            "peak_alloc_bytes": {"old": old_peak, "new": new_peak},
            "explain_query_plan": {"old": plan_text(old_plan), "new": plan_text(new_plan)},
            "equivalence": "old and new paths produced identical counts",
            "iterations": args.iterations,
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("PLA-507 deck-counts benchmark (synthetic, temporary SQLite)")
            print(
                f"  fixture: {result['fixture']['total_decks']} decks, "
                f"{result['fixture']['total_cards']} cards, clock pinned at {NOW}"
            )
            print(f"  statements (trace callback): old path {old_stmts}, new path {new_stmts}")
            print(
                f"  median: old {old_median:.3f} ms  new {new_median:.3f} ms "
                f"(over {args.iterations} runs)"
            )
            print(f"  min:    old {old_min:.3f} ms  new {new_min:.3f} ms")
            print(f"  mean:   old {old_mean:.3f} ms  new {new_mean:.3f} ms")
            print(
                f"  peak allocation: old {old_peak / 1024:.1f} KiB  new {new_peak / 1024:.1f} KiB"
            )
            print("  equivalence: old and new paths produced identical counts")
            print("  explain query plan (old, per deck):")
            for line in plan_text(old_plan):
                print(f"    {line}")
            print("  explain query plan (new, batched):")
            for line in plan_text(new_plan):
                print(f"    {line}")
        return 0
    finally:
        # `db` never exists if connection setup itself failed.
        with contextlib.suppress(NameError):
            db.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
