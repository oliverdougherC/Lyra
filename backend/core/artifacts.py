"""Artifacts: things Lyra produced that the user keeps, edits, and returns to.

Through Phase 1 Lyra held inputs (`documents`), a derived index (`chunks`), a transcript
(`messages`), and claims about a class (`profile_facts`). None of those is a work product.
`profile_facts` is the closest: generated content with a source document, a confidence, a
confirmed flag, and inline correction. This module generalizes that pattern rather than
inventing a second one, and the propose-and-confirm posture carries over unchanged.

Two rules this module exists to hold, because they are the ones that would otherwise drift:

- **Content and history move together.** Every non-empty content write goes through
  `set_part_content`, which writes the revision in the same transaction as the update. A
  part whose content has no matching revision would make the history lie.
- **A part belongs to exactly one artifact.** A child's `artifact_id` must match its
  parent's. `list_parts` walks the tree from the artifact's roots, so a mismatched child
  would exist in the table and be invisible to every reader.

Nothing here knows about prompts, models, or solving. `backend/core/solver.py` drives the
job; this is the store it writes to. See docs/solver-phase-2.md.
"""

import json
import sqlite3
from dataclasses import dataclass

from backend.core.errors import NotFoundError

KIND_SOLUTION_SET = "solution_set"
ARTIFACT_KINDS: tuple[str, ...] = (KIND_SOLUTION_SET,)

PENDING = "pending"
SEGMENTING = "segmenting"
AWAITING_REVIEW = "awaiting_review"
SOLVING = "solving"
READY = "ready"
FAILED = "failed"
CANCELLED = "cancelled"

ARTIFACT_STATES: tuple[str, ...] = (
    PENDING,
    SEGMENTING,
    AWAITING_REVIEW,
    SOLVING,
    READY,
    FAILED,
    CANCELLED,
)

# States a restart has to reconcile. `awaiting_review` is deliberately absent: it was not
# working, it was waiting, and a restart does not change that.
RUNNING_STATES: tuple[str, ...] = (PENDING, SEGMENTING, SOLVING)

PROBLEM = "problem"
STEP = "step"
ANSWER = "answer"
FIGURE = "figure"
PART_KINDS: tuple[str, ...] = (PROBLEM, STEP, ANSWER, FIGURE)

MARKDOWN = "markdown"
IMAGE = "image"
CONTENT_TYPES: tuple[str, ...] = (MARKDOWN, IMAGE)

PART_PENDING = "pending"
PART_SOLVING = "solving"
PART_VERIFYING = "verifying"
PART_COMPLETE = "complete"
PART_FAILED = "failed"
PART_STATUSES: tuple[str, ...] = (
    PART_PENDING,
    PART_SOLVING,
    PART_VERIFYING,
    PART_COMPLETE,
    PART_FAILED,
)

GENERATED = "generated"
REGENERATED = "regenerated"
USER_CORRECTED = "user_corrected"
ORIGINS: tuple[str, ...] = (GENERATED, REGENERATED, USER_CORRECTED)

UNCHECKED = "unchecked"
VERIFIED = "verified"
REFUTED = "refuted"
UNCHECKABLE = "uncheckable"
VERDICTS: tuple[str, ...] = (UNCHECKED, VERIFIED, REFUTED, UNCHECKABLE)

PROBLEM_SET = "problem_set"
REFERENCE_SOLUTIONS = "reference_solutions"
SOURCE_ROLES: tuple[str, ...] = (PROBLEM_SET, REFERENCE_SOLUTIONS)

_ARTIFACT_COLUMNS = (
    "id, class_id, kind, title, state, stage_detail, problems_total, problems_done, "
    "error_message, created_at, updated_at"
)

_PART_COLUMNS = (
    "id, artifact_id, parent_part_id, kind, ordinal, label, content, content_type, "
    "status, origin, verdict, verdict_detail, error_message, created_at, updated_at"
)

# Parts are a tree of arbitrary depth, so document order is a walk rather than a sort. The
# path is zero-padded at each level, which makes string ordering agree with numeric
# ordering without a second column to maintain.
_LIST_PARTS_SQL = """
with recursive ordered as (
  select p.id, p.artifact_id, p.parent_part_id, p.kind, p.ordinal, p.label, p.content,
         p.content_type, p.status, p.origin, p.verdict, p.verdict_detail, p.error_message,
         p.created_at, p.updated_at, printf('%08d', p.ordinal) as path
  from artifact_parts p
  where p.artifact_id = ? and p.parent_part_id is null
  union all
  select c.id, c.artifact_id, c.parent_part_id, c.kind, c.ordinal, c.label, c.content,
         c.content_type, c.status, c.origin, c.verdict, c.verdict_detail, c.error_message,
         c.created_at, c.updated_at, o.path || '.' || printf('%08d', c.ordinal)
  from artifact_parts c
  join ordered o on c.parent_part_id = o.id
)
select id, artifact_id, parent_part_id, kind, ordinal, label, content, content_type,
       status, origin, verdict, verdict_detail, error_message, created_at, updated_at
from ordered
order by path, id
"""

_INSERT_PART_SQL = """
insert into artifact_parts (
  artifact_id, parent_part_id, kind, ordinal, label, content, content_type, status, origin
) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@dataclass(frozen=True)
class SourceSpec:
    """One document an artifact is produced from, and in what capacity.

    Attributes:
        document_id: A document belonging to the same class as the artifact.
        role: `problem_set` for material to work on, `reference_solutions` for material
            to imitate the notation and method of.
    """

    document_id: int
    role: str = PROBLEM_SET


@dataclass(frozen=True)
class CheckEntry:
    """One tool call the verifier made while checking a part.

    Attributes:
        tool: Registry name of the tool, such as `cas_integrate`.
        arguments: The arguments as JSON text, printed by the interface rather than
            queried, which is why they are not parsed into columns.
        ok: Whether the tool ran. A refused or malformed call is still recorded: what the
            verifier tried is part of the audit trail even when it did not work.
        result: What came back, as JSON text.
    """

    tool: str
    arguments: str = "{}"
    ok: bool = False
    result: str = "{}"


@dataclass(frozen=True)
class ProvenanceEntry:
    """Where one part's content came from.

    Every field is optional because provenance degrades rather than disappears: a chunk
    that has been re-ingested may be gone while its document and page are still known.
    """

    chunk_id: int | None = None
    document_id: int | None = None
    page_number: int | None = None
    label: str | None = None
    # Where on the page this starts, as fractions of the page box. An empty tuple records
    # that the marker was searched for and not found, which is not the same as not having
    # looked; None means nothing has looked yet.
    bbox: tuple[float, ...] | None = None


def _require(value: str, allowed: tuple[str, ...], field: str) -> str:
    """Check a value against its allowed set, naming it when it is wrong.

    The columns carry the same check constraints. Failing here instead reports which
    value was rejected, which an `IntegrityError` does not.
    """
    if value not in allowed:
        raise ValueError(f"Unknown {field}: {value}")
    return value


def _touch_artifact(conn: sqlite3.Connection, artifact_id: int) -> None:
    """Mark an artifact as changed now. The caller commits."""
    conn.execute("update artifacts set updated_at = datetime('now') where id = ?", (artifact_id,))


def get_artifact(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object]:
    """One artifact.

    Raises:
        NotFoundError: when no artifact carries that id. Every other function here routes
            its lookup through this one, so the message is written once.
    """
    row = conn.execute(
        f"select {_ARTIFACT_COLUMNS} from artifacts where id = ?",  # noqa: S608
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("That solution set does not exist.")
    return dict(row)


def list_artifacts(conn: sqlite3.Connection, class_id: int) -> list[dict[str, object]]:
    """Every artifact in a class, most recently changed first.

    Ordered by `updated_at` rather than `created_at` because a part landing counts as the
    artifact changing, which is what a student looking for their in-progress run expects.
    """
    rows = conn.execute(
        f"select {_ARTIFACT_COLUMNS} from artifacts where class_id = ? "  # noqa: S608
        "order by updated_at desc, id desc",
        (class_id,),
    )
    return [dict(row) for row in rows]


def create_artifact(
    conn: sqlite3.Connection,
    class_id: int,
    title: str,
    sources: list[SourceSpec],
    kind: str = KIND_SOLUTION_SET,
) -> dict[str, object]:
    """Create an artifact in state `pending` with its source documents attached.

    Args:
        conn: Open database connection.
        class_id: Class the artifact belongs to.
        title: User-facing name. Trimmed, and may not be blank.
        sources: Documents to produce from, in the order they should be read. At least
            one must carry role `problem_set`: an artifact with only reference material
            has nothing to work on.
        kind: Artifact kind.

    Returns:
        The new artifact row.

    Raises:
        NotFoundError: when a source document does not exist, or belongs to another
            class. Retrieval is class-partitioned, and a source from another class would
            quietly break that.
        ValueError: on a blank title, an unknown kind or role, no problem-set source, or
            the same document supplied twice.
    """
    _require(kind, ARTIFACT_KINDS, "artifact kind")
    cleaned = title.strip()
    if not cleaned:
        raise ValueError("Artifact title cannot be blank.")
    # Every role is checked before the problem-set test, so a bad role on a later source
    # is still reported rather than short-circuited past by an earlier good one.
    roles = [_require(spec.role, SOURCE_ROLES, "source role") for spec in sources]
    if PROBLEM_SET not in roles:
        raise ValueError("An artifact needs at least one problem-set document.")

    seen: set[int] = set()
    for spec in sources:
        if spec.document_id in seen:
            # The primary key would reject this anyway. Saying which document is repeated
            # is more useful than an IntegrityError, and a document is one role per run.
            raise ValueError(f"Document {spec.document_id} is listed twice.")
        seen.add(spec.document_id)
        row = conn.execute(
            "select class_id from documents where id = ?", (spec.document_id,)
        ).fetchone()
        if row is None or int(row["class_id"]) != class_id:
            raise NotFoundError("That document does not exist in this class.")

    artifact_id = int(
        conn.execute(
            "insert into artifacts (class_id, kind, title, state) values (?, ?, ?, ?)",
            (class_id, kind, cleaned, PENDING),
        ).lastrowid
        or 0
    )
    conn.executemany(
        "insert into artifact_sources (artifact_id, document_id, role, ordinal) "
        "values (?, ?, ?, ?)",
        [
            (artifact_id, spec.document_id, spec.role, ordinal)
            for ordinal, spec in enumerate(sources)
        ],
    )
    conn.commit()
    return get_artifact(conn, artifact_id)


def delete_artifact(conn: sqlite3.Connection, artifact_id: int) -> None:
    """Delete an artifact and everything it owns.

    Raises:
        NotFoundError: when no artifact carries that id.
    """
    get_artifact(conn, artifact_id)
    # sources, parts, and through them revisions and provenance all cascade from here.
    conn.execute("delete from artifacts where id = ?", (artifact_id,))
    conn.commit()


def list_sources(
    conn: sqlite3.Connection, artifact_id: int, role: str | None = None
) -> list[dict[str, object]]:
    """An artifact's source documents in reading order, with their filenames.

    Args:
        conn: Open database connection.
        artifact_id: Artifact to read.
        role: Restrict to one role, or None for every source.

    Returns:
        One dict per source carrying `document_id`, `role`, `ordinal`, and `filename`.
        A source whose document has been deleted is gone from this list, not present with
        a null filename: the row cascades away with the document.
    """
    sql = (
        "select s.document_id, s.role, s.ordinal, d.filename "
        "from artifact_sources s join documents d on d.id = s.document_id "
        "where s.artifact_id = ?"
    )
    parameters: list[object] = [artifact_id]
    if role is not None:
        sql += " and s.role = ?"
        parameters.append(_require(role, SOURCE_ROLES, "source role"))
    sql += " order by s.ordinal, s.document_id"
    return [dict(row) for row in conn.execute(sql, parameters)]


def set_artifact_state(
    conn: sqlite3.Connection,
    artifact_id: int,
    state: str,
    stage_detail: str | None = None,
) -> None:
    """Move an artifact to the next state and commit.

    Committed at every transition rather than at the end of a run, because the polled
    status endpoint is the only way progress is visible.

    Raises:
        NotFoundError: when no artifact carries that id.
        ValueError: when `state` is outside the allowed set.
    """
    get_artifact(conn, artifact_id)
    _require(state, ARTIFACT_STATES, "artifact state")
    conn.execute(
        "update artifacts set state = ?, stage_detail = ?, updated_at = datetime('now') "
        "where id = ?",
        (state, stage_detail, artifact_id),
    )
    conn.commit()


def set_problems_total(conn: sqlite3.Connection, artifact_id: int, total: int) -> None:
    """Publish the problem count once segmentation knows it.

    Until this is called the column is null, which is how the interface tells "not counted
    yet" from "counted, and there are none".
    """
    get_artifact(conn, artifact_id)
    conn.execute(
        "update artifacts set problems_total = ?, updated_at = datetime('now') where id = ?",
        (total, artifact_id),
    )
    conn.commit()


def set_problems_done(conn: sqlite3.Connection, artifact_id: int, done: int) -> None:
    """Set the finished count outright.

    Used at the start of a run to establish the baseline, which is not always zero: a
    cancelled set resumed with `Solve the rest` already holds finished problems, and
    counting from zero would report work as undone that the student can read on screen.
    """
    get_artifact(conn, artifact_id)
    conn.execute(
        "update artifacts set problems_done = ?, updated_at = datetime('now') where id = ?",
        (max(0, done), artifact_id),
    )
    conn.commit()


def increment_problems_done(conn: sqlite3.Connection, artifact_id: int) -> int:
    """Count one more finished problem and return the new total.

    Incremented when a problem's verification finishes, never on a timer and never
    estimated: the interface renders this number directly.
    """
    get_artifact(conn, artifact_id)
    conn.execute(
        "update artifacts set problems_done = problems_done + 1, "
        "updated_at = datetime('now') where id = ?",
        (artifact_id,),
    )
    conn.commit()
    return int(get_artifact(conn, artifact_id)["problems_done"])


def mark_artifact_failed(
    conn: sqlite3.Connection, artifact_id: int, stage: str, message: str
) -> None:
    """Record a failure with the stage it happened in and a message written for the user."""
    get_artifact(conn, artifact_id)
    conn.execute(
        "update artifacts set state = ?, stage_detail = ?, error_message = ?, "
        "updated_at = datetime('now') where id = ?",
        (FAILED, stage, message, artifact_id),
    )
    conn.commit()


def get_part(conn: sqlite3.Connection, part_id: int) -> dict[str, object]:
    """One part.

    Raises:
        NotFoundError: when no part carries that id.
    """
    row = conn.execute(
        f"select {_PART_COLUMNS} from artifact_parts where id = ?",  # noqa: S608
        (part_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("That part of the solution does not exist.")
    return dict(row)


def list_parts(conn: sqlite3.Connection, artifact_id: int) -> list[dict[str, object]]:
    """Every part of an artifact in document order, parents before their children.

    Flat rather than nested, so a caller that only wants the problems can filter on
    `kind` without walking anything. The order is a depth-first walk, so building the tree
    from this list is a single pass.
    """
    return [dict(row) for row in conn.execute(_LIST_PARTS_SQL, (artifact_id,))]


def list_child_parts(conn: sqlite3.Connection, part_id: int) -> list[dict[str, object]]:
    """One part's direct children in order, which for a problem is its steps."""
    rows = conn.execute(
        f"select {_PART_COLUMNS} from artifact_parts "  # noqa: S608
        "where parent_part_id = ? order by ordinal, id",
        (part_id,),
    )
    return [dict(row) for row in rows]


def create_part(
    conn: sqlite3.Connection,
    artifact_id: int,
    kind: str,
    ordinal: int,
    *,
    label: str | None = None,
    content: str = "",
    content_type: str = MARKDOWN,
    parent_part_id: int | None = None,
    status: str = PART_PENDING,
    origin: str = GENERATED,
    note: str | None = None,
) -> int:
    """Create one part and return its id.

    Non-empty `content` is recorded as revision 1, so a part's history covers its content
    from the first write rather than from the first edit.

    Args:
        conn: Open database connection.
        artifact_id: Artifact the part belongs to.
        kind: `problem`, `step`, `answer`, or `figure`.
        ordinal: Position among its siblings.
        label: What the student calls it, such as `Problem 4`.
        content: Initial content. A problem's content is its statement; a step's is its
            working. Parts are commonly created empty and filled as solving reaches them.
        content_type: `markdown` or `image`.
        parent_part_id: Owning part, or None for a root part such as a problem.
        status: Lifecycle status. Defaults to `pending`.
        origin: Who wrote the content. Defaults to `generated`.
        note: Why this content exists, recorded on revision 1. Carries the student's
            correction on a re-solve, so the history sheet says what prompted the rewrite
            rather than showing two versions with no reason between them.

    Returns:
        The id of the new part.

    Raises:
        NotFoundError: when the artifact or the parent part does not exist.
        ValueError: on an unknown kind, content type, status, or origin, or when the
            parent belongs to a different artifact.
    """
    get_artifact(conn, artifact_id)
    _require(kind, PART_KINDS, "part kind")
    _require(content_type, CONTENT_TYPES, "content type")
    _require(status, PART_STATUSES, "part status")
    _require(origin, ORIGINS, "part origin")

    if parent_part_id is not None:
        parent = get_part(conn, parent_part_id)
        if int(parent["artifact_id"]) != artifact_id:
            # `list_parts` walks down from the artifact's roots, so a child hung off
            # another artifact's part would exist in the table and be invisible to it.
            raise ValueError("A part cannot hang off another artifact's part.")

    part_id = int(
        conn.execute(
            _INSERT_PART_SQL,
            (
                artifact_id,
                parent_part_id,
                kind,
                ordinal,
                label,
                content,
                content_type,
                status,
                origin,
            ),
        ).lastrowid
        or 0
    )
    if content:
        _insert_revision(conn, part_id, content, origin, note)
    _touch_artifact(conn, artifact_id)
    conn.commit()
    return part_id


def delete_part(conn: sqlite3.Connection, part_id: int) -> None:
    """Delete one part, its descendants, its revisions, and its provenance.

    Raises:
        NotFoundError: when no part carries that id.
    """
    part = get_part(conn, part_id)
    conn.execute("delete from artifact_parts where id = ?", (part_id,))
    _touch_artifact(conn, int(part["artifact_id"]))
    conn.commit()


def delete_parts(conn: sqlite3.Connection, artifact_id: int) -> None:
    """Drop every part of an artifact.

    This is what a re-segmentation does before writing the corrected problem list: merge
    and split are not expressible as per-row edits, so the list is replaced wholesale.

    Raises:
        NotFoundError: when no artifact carries that id.
    """
    get_artifact(conn, artifact_id)
    conn.execute("delete from artifact_parts where artifact_id = ?", (artifact_id,))
    _touch_artifact(conn, artifact_id)
    conn.commit()


def set_part_content(
    conn: sqlite3.Connection,
    part_id: int,
    content: str,
    origin: str,
    note: str | None = None,
) -> int:
    """Replace a part's content, recording the revision it produced.

    The revision and the update are written together, which is the whole reason content
    is not set with a bare `update`: history that can drift from content is worse than no
    history, because it is believed.

    Args:
        conn: Open database connection.
        part_id: Part to write.
        content: New content, replacing whatever is there.
        origin: `generated`, `regenerated`, or `user_corrected`.
        note: Why this revision exists. The student's correction on a re-solve, or the
            verifier's refutation. Kept so a reader of the history knows what prompted it.

    Returns:
        The new revision number, counting from 1.

    Raises:
        NotFoundError: when no part carries that id.
        ValueError: when `origin` is outside the allowed set.
    """
    part = get_part(conn, part_id)
    _require(origin, ORIGINS, "part origin")
    revision = _insert_revision(conn, part_id, content, origin, note)
    conn.execute(
        "update artifact_parts set content = ?, origin = ?, updated_at = datetime('now') "
        "where id = ?",
        (content, origin, part_id),
    )
    _touch_artifact(conn, int(part["artifact_id"]))
    conn.commit()
    return revision


def set_part_position(
    conn: sqlite3.Connection, part_id: int, ordinal: int, label: str | None
) -> None:
    """Set where a part sits among its siblings and what it is called.

    Separate from `set_part_content` because moving or renaming a part is not a new
    version of its content, and writing a revision for it would fill the history sheet
    with entries nobody made.

    Raises:
        NotFoundError: when no part carries that id.
    """
    part = get_part(conn, part_id)
    conn.execute(
        "update artifact_parts set ordinal = ?, label = ?, updated_at = datetime('now') "
        "where id = ?",
        (ordinal, label, part_id),
    )
    _touch_artifact(conn, int(part["artifact_id"]))
    conn.commit()


def _insert_revision(
    conn: sqlite3.Connection, part_id: int, content: str, origin: str, note: str | None
) -> int:
    """Append one revision and return its number. The caller commits."""
    previous = conn.execute(
        "select coalesce(max(revision), 0) from artifact_part_revisions where part_id = ?",
        (part_id,),
    ).fetchone()[0]
    revision = int(previous) + 1
    conn.execute(
        "insert into artifact_part_revisions (part_id, revision, content, origin, note) "
        "values (?, ?, ?, ?, ?)",
        (part_id, revision, content, origin, note),
    )
    return revision


def list_revisions(conn: sqlite3.Connection, part_id: int) -> list[dict[str, object]]:
    """A part's revisions, newest first, which is the order the history sheet reads.

    Raises:
        NotFoundError: when no part carries that id.
    """
    get_part(conn, part_id)
    rows = conn.execute(
        "select id, part_id, revision, content, origin, note, created_at "
        "from artifact_part_revisions where part_id = ? order by revision desc",
        (part_id,),
    )
    return [dict(row) for row in rows]


def set_part_status(
    conn: sqlite3.Connection,
    part_id: int,
    status: str,
    error_message: str | None = None,
) -> None:
    """Move a part through its lifecycle, committing so a poller sees it.

    `error_message` is cleared on any status other than `failed`, so a problem that
    succeeds on a retry does not keep the previous attempt's error beside it.

    Raises:
        NotFoundError: when no part carries that id.
        ValueError: when `status` is outside the allowed set.
    """
    part = get_part(conn, part_id)
    _require(status, PART_STATUSES, "part status")
    conn.execute(
        "update artifact_parts set status = ?, error_message = ?, "
        "updated_at = datetime('now') where id = ?",
        (status, error_message if status == PART_FAILED else None, part_id),
    )
    _touch_artifact(conn, int(part["artifact_id"]))
    conn.commit()


def set_part_verdict(
    conn: sqlite3.Connection, part_id: int, verdict: str, detail: str | None = None
) -> None:
    """Record what checking concluded about a part, and why.

    Separate from `status` on purpose. A part can be `complete` and `unchecked`, and that
    combination is the honest reading of a solution produced against an endpoint with no
    tool support. Neither `unchecked` nor `uncheckable` may ever be rendered as a pass.

    Args:
        conn: Open database connection.
        part_id: Part to record against.
        verdict: One of `VERDICTS`.
        detail: The sentence behind the verdict. A refutation names the check that
            disagreed and what it returned; an `unchecked` names why checking did not run.
            Written on every verdict including `verified`, where None simply clears a
            stale reason from a previous attempt.

    Raises:
        NotFoundError: when no part carries that id.
        ValueError: when `verdict` is outside the allowed set.
    """
    part = get_part(conn, part_id)
    _require(verdict, VERDICTS, "verdict")
    conn.execute(
        "update artifact_parts set verdict = ?, verdict_detail = ?, "
        "updated_at = datetime('now') where id = ?",
        (verdict, detail, part_id),
    )
    _touch_artifact(conn, int(part["artifact_id"]))
    conn.commit()


def record_checks(conn: sqlite3.Connection, part_id: int, checks: list[CheckEntry]) -> None:
    """Replace the tool calls recorded against a part.

    Replace rather than append, for the same reason provenance is replaced: a re-checked
    part was judged by this pass, and showing this pass's calls alongside a previous
    pass's would make the count on the interface a sum of two different verdicts.

    Raises:
        NotFoundError: when no part carries that id.
    """
    get_part(conn, part_id)
    conn.execute("delete from artifact_checks where part_id = ?", (part_id,))
    conn.executemany(
        "insert into artifact_checks (part_id, ordinal, tool, arguments, ok, result) "
        "values (?, ?, ?, ?, ?, ?)",
        [
            (part_id, ordinal, check.tool, check.arguments, int(check.ok), check.result)
            for ordinal, check in enumerate(checks)
        ],
    )
    conn.commit()


def list_checks(conn: sqlite3.Connection, part_id: int) -> list[dict[str, object]]:
    """Every tool call recorded against a part, in the order it ran.

    Raises:
        NotFoundError: when no part carries that id.
    """
    get_part(conn, part_id)
    rows = conn.execute(
        "select id, part_id, ordinal, tool, arguments, ok, result, created_at "
        "from artifact_checks where part_id = ? order by ordinal, id",
        (part_id,),
    )
    return [dict(row) for row in rows]


def get_revision(conn: sqlite3.Connection, part_id: int, revision: int) -> dict[str, object]:
    """One numbered revision of a part.

    Raises:
        NotFoundError: when the part has no revision with that number.
    """
    row = conn.execute(
        "select id, part_id, revision, content, origin, note, created_at "
        "from artifact_part_revisions where part_id = ? and revision = ?",
        (part_id, revision),
    ).fetchone()
    if row is None:
        raise NotFoundError("That version of this part does not exist.")
    return dict(row)


def set_provenance(conn: sqlite3.Connection, part_id: int, entries: list[ProvenanceEntry]) -> None:
    """Replace a part's provenance with `entries`.

    Replace rather than append, because a regenerated part was informed by whatever this
    run retrieved, not by that plus whatever the last run did. An empty list clears it,
    which is the correct record for a step the model supplied on its own.

    Raises:
        NotFoundError: when no part carries that id.
    """
    part = get_part(conn, part_id)
    conn.execute("delete from artifact_provenance where part_id = ?", (part_id,))
    conn.executemany(
        "insert into artifact_provenance "
        "(part_id, chunk_id, document_id, page_number, label, bbox) values (?, ?, ?, ?, ?, ?)",
        [
            (
                part_id,
                entry.chunk_id,
                entry.document_id,
                entry.page_number,
                entry.label,
                None if entry.bbox is None else json.dumps(list(entry.bbox)),
            )
            for entry in entries
        ],
    )
    _touch_artifact(conn, int(part["artifact_id"]))
    conn.commit()


def list_provenance(conn: sqlite3.Connection, part_id: int) -> list[dict[str, object]]:
    """Where one part's content came from, with the source filename where it survives.

    `filename` is null when the source document has been deleted since. The provenance row
    is kept rather than cascaded away, because the page number is still true and still
    worth showing.

    Raises:
        NotFoundError: when no part carries that id.
    """
    get_part(conn, part_id)
    rows = conn.execute(
        "select p.id, p.part_id, p.chunk_id, p.document_id, p.page_number, p.label, "
        "p.bbox, d.filename from artifact_provenance p "
        "left join documents d on d.id = p.document_id "
        "where p.part_id = ? order by p.id",
        (part_id,),
    )
    return [{**dict(row), "bbox": _read_bbox(row["bbox"])} for row in rows]


def _read_bbox(raw: object) -> list[float] | None:
    """A stored rectangle, or None when there is not a usable one.

    A malformed value reads as absent rather than raising: this drives a convenience on a
    page image, and no part of a solution should fail to load over it.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, list) or len(parsed) != 4:
        return None
    return [float(value) for value in parsed] if all(_is_number(v) for v in parsed) else None


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
