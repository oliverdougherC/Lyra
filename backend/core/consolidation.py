"""The class-scope pass that turns per-document observations into one readable profile.

`backend.core.profiles` merges what differs only by formatting, because that merge is free
and cannot be wrong. What is left over differs by wording: `Time Shift` against `Time-Shift
Property`, `Convolution` against `Continuous-Time Graphical Convolution`. Deciding those
takes judgment about the subject, which is what this module asks the tutor model for.

It also catches what the extraction prompt asked the model not to report and got anyway. A
small local model reading its ninth problem set will name the assignment and restate the
course code however firmly it was told not to, so the profile cannot rely on that instruction
being obeyed. This pass is where the profile earns its quality independently of how well the
per-document extractor behaved.

Two properties make it safe to run automatically:

- It only groups and demotes. It never renames, never invents, and never touches a fact the
  user confirmed, rejected, or corrected.
- It is not load-bearing. If the model fails or answers unusably, the profile is still the
  deterministically merged one, which is already the improvement.

It sends fact labels rather than document text, but to the same endpoint, so it asks
`extraction_allowed` first for the same reason `extract_facts` does.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Mapping, Sequence

from backend.core.app_settings import extraction_allowed, resolve_tutor_config
from backend.llm import client, replies
from backend.llm.prompts import CONSOLIDATION_SCHEMA, build_consolidation_prompt

logger = logging.getLogger(__name__)

# The kinds worth consolidating. A deadline and a grading weight are already identified by a
# label the document chose, and asking a model whether two exams are one exam risks losing a
# date the student needs. The noise lives in the three open-ended kinds.
CONSOLIDATED_KINDS = ("topic", "prerequisite", "note")

# Below this there is nothing a consolidation pass could usefully find, and the round trip is
# a cost with no return.
MIN_ENTRIES = 6

# Above this the prompt stops being cheap. The newest entries are kept, which are the ones
# the pass has not seen, and the truncation is logged rather than silently applied.
MAX_ENTRIES = 120

# The placeholders are generated from a module constant and every value is bound.
_SELECT_CANDIDATES = f"""
select id, kind, label, value, confidence, consolidated
from profile_facts
where class_id = ? and rejected = 0 and confirmed = 0 and edited = 0
  and kind in ({", ".join("?" for _ in CONSOLIDATED_KINDS)})
order by id
"""  # noqa: S608

_DUPLICATES_KEY = "duplicates"
_NOISE_KEY = "not_about_the_course"


def _entry_text(row: sqlite3.Row) -> str:
    """One candidate as the model sees it: its kind, then the subject that identifies it."""
    kind = str(row["kind"])
    label = str(row["label"]).strip()
    value = str(row["value"]).strip()
    if label and value and label.casefold() != kind.casefold():
        return f"[{kind}] {label}: {value}"
    return f"[{kind}] {value or label}"


def _numbers(payload: Mapping[str, object], key: str) -> list[object]:
    """One top-level list from the reply, or an empty list when the field is missing or wrong."""
    value = payload.get(key)
    return value if isinstance(value, list) else []


def _resolve(candidate: object, count: int) -> int | None:
    """One reply number as a zero-based index, or None when it is not one that was sent.

    Every number has to be checked. A model that answered with an index it invented would
    otherwise merge away a fact nobody mentioned, and the whole safety of this pass is that
    it can only act on entries it was given.
    """
    if isinstance(candidate, bool) or not isinstance(candidate, int | float | str):
        return None
    try:
        number = int(candidate)
    except (TypeError, ValueError):
        return None
    return number - 1 if 1 <= number <= count else None


def _merge_groups(
    payload: Mapping[str, object], rows: Sequence[sqlite3.Row]
) -> list[tuple[int, list[int]]]:
    """The reply's duplicate groups as `(winner_id, loser_ids)`, everything unusable dropped.

    A fact may only lose once. A model that put the same entry in two groups would otherwise
    have its evidence moved twice and its row deleted twice, so the first group claims it.
    """
    claimed: set[int] = set()
    groups: list[tuple[int, list[int]]] = []
    for raw in _numbers(payload, _DUPLICATES_KEY):
        if not isinstance(raw, list):
            continue
        indexes = [index for member in raw if (index := _resolve(member, len(rows))) is not None]
        unique = list(dict.fromkeys(indexes))
        if len(unique) < 2 or any(index in claimed for index in unique):
            continue
        claimed.update(unique)
        winner, *losers = unique
        groups.append((int(rows[winner]["id"]), [int(rows[index]["id"]) for index in losers]))
    return groups


def _apply_merges(conn: sqlite3.Connection, groups: Sequence[tuple[int, list[int]]]) -> int:
    """Fold each group's losers into its winner. Returns the number of facts removed.

    Evidence moves before the row goes. A loser attested by four documents is four documents
    that attest the winner, and losing that count would cost the winner its ordering and its
    corroboration in one step.
    """
    removed = 0
    for winner_id, loser_ids in groups:
        for loser_id in loser_ids:
            conn.execute(
                "insert or ignore into profile_fact_sources (fact_id, document_id) "
                "select ?, document_id from profile_fact_sources where fact_id = ?",
                (winner_id, loser_id),
            )
            conn.execute("delete from profile_facts where id = ?", (loser_id,))
            removed += 1
    return removed


def _apply_demotions(
    conn: sqlite3.Connection, payload: Mapping[str, object], rows: Sequence[sqlite3.Row]
) -> int:
    """Demote what the model called document metadata. Returns the number demoted.

    Demotion rather than deletion, because `low` already means exactly this in Lyra: stored,
    visible in the class profile, out of every prompt, waiting for a person. A wrong call
    here costs the student one click, and the row is still there to be confirmed.
    """
    demoted = 0
    for member in _numbers(payload, _NOISE_KEY):
        index = _resolve(member, len(rows))
        if index is None or rows[index]["confidence"] == "low":
            continue
        conn.execute(
            "update profile_facts set confidence = 'low' where id = ?", (rows[index]["id"],)
        )
        demoted += 1
    return demoted


def consolidate_class(conn: sqlite3.Connection, class_id: int) -> None:
    """Merge the wording variants in one class profile and set its file metadata aside.

    Does nothing when every candidate has already been through a pass, so a re-upload that
    proposes nothing new costs no model call at all. That is what the `consolidated` column
    records, and it is set on every entry sent whatever the reply turned out to be: a fact
    the model declined to act on has been considered, and asking again would ask the same
    question of the same list.

    Args:
        conn: Open database connection.
        class_id: The class whose profile is being consolidated.

    Raises:
        UpstreamError: The tutor endpoint failed. The ingestion job catches this: a profile
            that did not get tidied is still a profile.
    """
    if extraction_allowed(conn) is not None:
        return

    rows = list(conn.execute(_SELECT_CANDIDATES, (class_id, *CONSOLIDATED_KINDS)))
    if not any(row["consolidated"] == 0 for row in rows):
        return
    if len(rows) < MIN_ENTRIES:
        _mark_consolidated(conn, rows)
        return
    if len(rows) > MAX_ENTRIES:
        logger.info(
            "Consolidating the newest %s of %s profile facts for class %s",
            MAX_ENTRIES,
            len(rows),
            class_id,
        )
        rows = rows[-MAX_ENTRIES:]

    config = resolve_tutor_config(conn)
    messages = build_consolidation_prompt([_entry_text(row) for row in rows])
    # Sync for the same reason `extract_facts` is: the ingestion worker is a plain thread
    # with no event loop, and owning one for the call keeps it free of async plumbing.
    content = asyncio.run(
        client.complete(
            config.endpoint_url,
            config.api_key,
            config.model,
            messages,
            temperature=client.DETERMINISTIC_TEMPERATURE,
            schema=CONSOLIDATION_SCHEMA,
            request_timeout=client.BACKGROUND_TIMEOUT,
        )
    )

    payload = replies.loads_object(content)
    if payload is None:
        # Not a fault worth surfacing. The profile is the deterministically merged one and
        # the next upload will ask again.
        logger.warning("Profile consolidation returned a reply that is not a JSON object")
        _mark_consolidated(conn, rows)
        return

    # One transaction from the first merge to the commit inside `_mark_consolidated`. An
    # exception in between - and the shared worker connection it would leave holding
    # uncommitted deletes - must roll everything back, or the next unrelated commit on
    # that connection would silently make a half-applied merge real.
    try:
        removed = _apply_merges(conn, _merge_groups(payload, rows))
        demoted = _apply_demotions(conn, payload, rows)
        _mark_consolidated(conn, rows)
    except Exception:
        conn.rollback()
        raise
    logger.info(
        "Consolidated class %s: %s facts merged away, %s set aside as document metadata",
        class_id,
        removed,
        demoted,
    )


def _mark_consolidated(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row]) -> None:
    """Record that these facts have been through a pass, and commit."""
    conn.executemany(
        "update profile_facts set consolidated = 1 where id = ?",
        [(int(row["id"]),) for row in rows],
    )
    conn.commit()
