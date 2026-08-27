"""The brief: what a draft is, kept where every writer prompt can reach it.

One row per draft. The assistant proposes a brief - discerned from the title and body,
or cross-referenced against an assignment handout it found in the class documents - and
the student confirms or edits it. A proposed brief is usable (the assistant just says it
is working from a guess); a confirmed one is settled. Either way it is the single
highest-leverage block of context the writer gets, which is why it lives in its own
table instead of inside a prompt somewhere.

The lifecycle honors propose-never-assert: nothing here writes a confirmed brief except
on the student's say-so, and a re-proposal over a confirmed brief demotes it back to
proposed, because a brief the assistant changed is a brief the student has not agreed to.
"""

import re
import sqlite3

from backend.core import artifacts
from backend.core.errors import NotFoundError

PROPOSED = "proposed"
CONFIRMED = "confirmed"

# One page of double-spaced 12pt prose is nearer 250 words; one single-spaced page is
# nearer 500. Student assignments are quoted in pages far more often than in words and
# are usually double-spaced, but a target the writer overshoots costs the student a cut
# and one it undershoots costs them a rewrite, so this sits above the double-spaced
# figure. Adjust here: every stage divides this same number.
WORDS_PER_PAGE = 400
_DOUBLE_SPACED_PAGE = 250

_PAGES = re.compile(r"\bpages?\b", re.IGNORECASE)
_WORDS = re.compile(r"\bwords?\b", re.IGNORECASE)
_DOUBLE_SPACED = re.compile(r"double[\s-]*spaced", re.IGNORECASE)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Students write "a five page essay" at least as often as "5 pages", and a length target
# that only reads digits misses the phrasing people actually use. Only the range worth
# spelling out: past twenty, nobody writes it in words.
_SPELLED = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
}
_SPELLED_WORDS = re.compile(rf"\b({'|'.join(_SPELLED)})\b", re.IGNORECASE)


def length_target_words(target: str | None, *, require_unit: bool = False) -> int | None:
    """A brief's free-text length target as a word count, or None when it says nothing.

    The column is prose the student or the assistant wrote - "5 pages", "1,500 words",
    "5-7 pages, double-spaced" - and every drafting stage needs a number out of it: how
    many sections to plan, how long each one should run. A range takes its midpoint,
    because writing to the bottom of a range is how a five-page paper becomes three.

    Returns None rather than guessing when there is no unit to read. A caller that gets
    None writes exactly as it did before any of this existed.

    Args:
        target: The text to read a length out of.
        require_unit: Refuse a bare number. Set when reading arbitrary prose rather than
            the brief's dedicated field: "tighten section 2" states no length at all, and
            reading its 2 as a page count would silently rewrite the whole pass.
    """
    if not target:
        return None
    text = target.strip()
    if not text:
        return None
    if require_unit and not (_WORDS.search(text) or _PAGES.search(text)):
        return None
    numbers = [float(match.group().replace(",", "")) for match in _NUMBER.finditer(text)]
    numbers = [value for value in numbers if value > 0]
    if not numbers:
        numbers = [
            float(_SPELLED[match.group().lower()]) for match in _SPELLED_WORDS.finditer(text)
        ]
    if not numbers:
        return None
    # A range ("5-7 pages", "1500 to 2000 words") is written to its middle. Anything
    # longer than a pair is not a range anyone wrote - take the first number and stop.
    magnitude = sum(numbers[:2]) / 2 if len(numbers) == 2 else numbers[0]

    if _WORDS.search(text):
        return int(round(magnitude))
    if _PAGES.search(text):
        per_page = _DOUBLE_SPACED_PAGE if _DOUBLE_SPACED.search(text) else WORDS_PER_PAGE
        return int(round(magnitude * per_page))
    # A bare number is only ever a word count in practice - nobody writes "5" for five
    # pages - but a small one is far more likely to be a page count typed without its
    # unit, and reading "5" as five words would silently cap the document at nothing.
    return int(round(magnitude * WORDS_PER_PAGE)) if magnitude <= 30 else int(round(magnitude))


_NOT_A_DRAFT_MESSAGE = "That draft does not exist."
_NO_BRIEF_MESSAGE = "That draft has no brief."
_NO_DOCUMENT_MESSAGE = "That document does not exist."

_COLUMNS = (
    "artifact_id, assignment_type, summary, audience, length_target, "
    "source_document_id, status, created_at, updated_at"
)


def _require_draft(conn: sqlite3.Connection, artifact_id: int) -> None:
    """404 unless the artifact exists and is a draft, same reading as the routes."""
    artifact = artifacts.get_artifact(conn, artifact_id)
    if artifact["kind"] != artifacts.KIND_DRAFT:
        raise NotFoundError(_NOT_A_DRAFT_MESSAGE)


def get_brief(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object] | None:
    """The draft's brief, or None when none has been proposed yet."""
    row = conn.execute(
        f"select {_COLUMNS} from draft_briefs where artifact_id = ?",  # noqa: S608
        (artifact_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def save_brief(
    conn: sqlite3.Connection,
    artifact_id: int,
    *,
    assignment_type: str = "",
    summary: str = "",
    audience: str = "",
    length_target: str = "",
    source_document_id: int | None = None,
    status: str = PROPOSED,
    commit: bool = True,
) -> dict[str, object]:
    """Write the draft's brief, replacing any prior one, and return it.

    The default status is `proposed` even over a previously confirmed brief: a changed
    brief is a new proposal. `status=CONFIRMED` is for the student's own edits, where
    saving and agreeing are the same gesture.

    When ``commit=False`` the caller owns the transaction boundary (PLA-310 atomicity).

    Raises:
        NotFoundError: when the artifact is not a draft, or `source_document_id` names
            a document that does not exist.
        ValueError: on a status outside the lifecycle. That is a caller bug, not model
            or student input, so it raises rather than travelling back as a result.
    """
    _require_draft(conn, artifact_id)
    if status not in (PROPOSED, CONFIRMED):
        raise ValueError(f"Not a brief status: {status}")
    if source_document_id is not None:
        row = conn.execute(
            "select id from documents where id = ?", (source_document_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(_NO_DOCUMENT_MESSAGE)

    conn.execute(
        "insert into draft_briefs "
        "(artifact_id, assignment_type, summary, audience, length_target, "
        " source_document_id, status) "
        "values (?, ?, ?, ?, ?, ?, ?) "
        "on conflict (artifact_id) do update set "
        "assignment_type = excluded.assignment_type, summary = excluded.summary, "
        "audience = excluded.audience, length_target = excluded.length_target, "
        "source_document_id = excluded.source_document_id, status = excluded.status, "
        "updated_at = datetime('now')",
        (
            artifact_id,
            assignment_type.strip(),
            summary.strip(),
            audience.strip(),
            length_target.strip(),
            source_document_id,
            status,
        ),
    )
    if commit:
        conn.commit()
    brief = get_brief(conn, artifact_id)
    assert brief is not None  # noqa: S101 - just written above, same connection.
    return brief


def confirm_brief(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object]:
    """Flip the brief to confirmed - the student's gesture, never the assistant's.

    Raises:
        NotFoundError: when the draft has no brief to confirm.
    """
    _require_draft(conn, artifact_id)
    cursor = conn.execute(
        "update draft_briefs set status = ?, updated_at = datetime('now') where artifact_id = ?",
        (CONFIRMED, artifact_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        raise NotFoundError(_NO_BRIEF_MESSAGE)
    brief = get_brief(conn, artifact_id)
    assert brief is not None  # noqa: S101 - the update just matched this row.
    return brief


def delete_brief(conn: sqlite3.Connection, artifact_id: int) -> None:
    """Drop the brief, if there is one. Deleting a guess is not an error."""
    _require_draft(conn, artifact_id)
    conn.execute("delete from draft_briefs where artifact_id = ?", (artifact_id,))
    conn.commit()
