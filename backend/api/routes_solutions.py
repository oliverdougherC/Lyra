"""Solution-set endpoints: creation, the polled status, and the segmentation gate.

Creation answers `202` and does no work beyond writing the rows, because reading a problem
set is a model pass that takes a minute on local hardware. The interface polls `/status`
from there, exactly as it already does for ingestion.

Handlers are sync `def`: `sqlite3` and file reads block, and FastAPI runs sync handlers in
a threadpool, which is where blocking work belongs.

The route prefix is `/api/solutions` while the table is `artifacts`, and that is
deliberate. The model is general; this is the solver's view of it. When Phase 4 produces
artifacts of its own kind they get their own route rather than overloading this one with a
`kind` query parameter.
"""

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, field_validator

from backend.core import artifacts, solver
from backend.core.classes import get_class, touch_class
from backend.core.errors import ConflictError, LyraError, NotFoundError
from backend.core.segmentation import SegmentedPart, SegmentedProblem
from backend.storage.database import get_db

router = APIRouter(prefix="/api", tags=["solutions"])

DbConn = Annotated[sqlite3.Connection, Depends(get_db)]

MAX_PROBLEMS = 200

# Long enough for a paragraph of "here is what is wrong with step 3", short enough that
# nothing pathological reaches the prompt builder.
MAX_CORRECTION_CHARS = 4000

NOT_READY_MESSAGE = "{filename} has not finished processing yet."
NOT_AT_GATE_MESSAGE = "This solution set is not waiting for review."
NOT_RUNNING_MESSAGE = "This solution set is not running."
ALREADY_RUNNING_MESSAGE = "This solution set is already running."
NO_PROBLEMS_MESSAGE = "There are no problems to solve. Add one first."
NOT_THIS_SET_MESSAGE = "That part does not belong to this solution set."
NOT_A_PROBLEM_MESSAGE = "Only a whole problem can be solved again."
TOO_MANY_PROBLEMS = f"A solution set can hold at most {MAX_PROBLEMS} problems."
DEFAULT_TITLE = "Solution set"
EDITED_SINCE_CHECK = "This was edited after it was checked, so the earlier check no longer applies."
RESTORED_NOTE = "Restored version {revision}."

# States a confirmed problem list may be started from. `cancelled` and `ready` are here on
# purpose: `Solve the rest` after a stop, and re-running a set whose sources changed, are
# the same act as starting it, and solving skips problems that are already complete.
STARTABLE_STATES = (
    artifacts.AWAITING_REVIEW,
    artifacts.READY,
    artifacts.CANCELLED,
    artifacts.FAILED,
)

SourceRole = Literal["problem_set", "reference_solutions"]


class SourceCreate(BaseModel):
    """One document to build from, and in what capacity."""

    document_id: int
    role: SourceRole = "problem_set"


class SolutionCreate(BaseModel):
    """Body of `POST /api/classes/{class_id}/solutions`."""

    sources: list[SourceCreate] = Field(min_length=1)
    title: str | None = None


class PartUpdate(BaseModel):
    """One sub-part in a corrected problem list."""

    label: str | None = None
    statement: str = Field(min_length=1)


class ProblemUpdate(BaseModel):
    """One problem in a corrected problem list.

    `id` is the part this entry came from, when it came from one. It is what lets an
    edited problem keep the page and chunks it was found in. Merge and split produce
    entries with no id, which is correct: a problem assembled from two others did not come
    from either one alone.
    """

    id: int | None = None
    label: str | None = None
    statement: str = Field(min_length=1)
    parts: list[PartUpdate] = Field(default_factory=list)

    @field_validator("statement")
    @classmethod
    def _check_statement(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A problem statement cannot be blank.")
        return cleaned


class SegmentationUpdate(BaseModel):
    """Body of `PATCH /api/solutions/{artifact_id}/segmentation`.

    The whole list, not a patch of one row. Merge and split are the two corrections that
    matter most at this gate and neither is expressible as a per-row edit, so the list is
    replaced.
    """

    problems: list[ProblemUpdate]


class PartEdit(BaseModel):
    """Body of `PATCH /api/solutions/{artifact_id}/parts/{part_id}`."""

    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def _check_content(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A step cannot be empty.")
        return cleaned


class RegenerateRequest(BaseModel):
    """Body of `POST /api/solutions/{artifact_id}/parts/{part_id}/regenerate`.

    The correction is optional: `Regenerate` and `Mark wrong and re-solve` are the same
    endpoint, and the difference between them is whether the student had something to say.
    """

    correction: str = Field(default="", max_length=MAX_CORRECTION_CHARS)


class RestoreRequest(BaseModel):
    """Body of `POST /api/solutions/{artifact_id}/parts/{part_id}/restore`."""

    revision: int = Field(ge=1)


class SourceRead(BaseModel):
    """A source document as the interface sees it."""

    document_id: int
    role: SourceRole
    ordinal: int
    filename: str


class ProvenanceRead(BaseModel):
    """Where one part came from. `filename` is null once the document is gone."""

    chunk_id: int | None
    document_id: int | None
    page_number: int | None
    label: str | None
    filename: str | None


class CheckRead(BaseModel):
    """One tool call the verifier made. The audit trail behind a verdict."""

    tool: str
    arguments: str
    ok: bool
    result: str


class RevisionRead(BaseModel):
    """One stored version of a part, newest first in the list this appears in."""

    revision: int
    content: str
    origin: str
    note: str | None
    created_at: str


class PartRead(BaseModel):
    """One addressable part of a solution set."""

    id: int
    artifact_id: int
    parent_part_id: int | None
    kind: str
    ordinal: int
    label: str | None
    content: str
    content_type: str
    status: str
    origin: str
    verdict: str
    verdict_detail: str | None
    error_message: str | None
    provenance: list[ProvenanceRead]
    checks: list[CheckRead]


class SolutionRead(BaseModel):
    """A solution set and where its run has got to."""

    id: int
    class_id: int
    kind: str
    title: str
    state: str
    stage_detail: str | None
    problems_total: int | None
    problems_done: int
    error_message: str | None
    created_at: str
    updated_at: str
    sources: list[SourceRead]


class SolutionDetail(SolutionRead):
    """One solution set with every part, in document order."""

    parts: list[PartRead]


class PartStatusRead(BaseModel):
    """Just enough of a part for the progress display to render it."""

    id: int
    status: str
    verdict: str


class StatusRead(BaseModel):
    """The poll target while a solution set is being read or solved."""

    state: str
    stage_detail: str | None
    problems_total: int | None
    problems_done: int
    error_message: str | None
    parts: list[PartStatusRead]


@router.post(
    "/classes/{class_id}/solutions",
    response_model=SolutionRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_solution(class_id: int, payload: SolutionCreate, conn: DbConn) -> dict[str, object]:
    """Create a solution set and start reading its problem set.

    Returns `202`: segmentation is a model pass over a whole document, and nothing about
    it belongs on an open connection. It does not start solving. That waits for the
    student to confirm the problem list.
    """
    get_class(conn, class_id)
    for source in payload.sources:
        _require_ready(conn, source.document_id)

    created = artifacts.create_artifact(
        conn,
        class_id,
        payload.title or _default_title(conn, payload),
        [
            artifacts.SourceSpec(document_id=source.document_id, role=source.role)
            for source in payload.sources
        ],
    )
    touch_class(conn, class_id)
    solver.enqueue(int(created["id"]))
    return _with_sources(conn, created)


@router.get("/classes/{class_id}/solutions", response_model=list[SolutionRead])
def list_solutions(class_id: int, conn: DbConn) -> list[dict[str, object]]:
    # An unknown class is a 404 rather than an empty list, so a stale link is obvious.
    get_class(conn, class_id)
    return [_with_sources(conn, artifact) for artifact in artifacts.list_artifacts(conn, class_id)]


@router.get("/solutions/{artifact_id}", response_model=SolutionDetail)
def read_solution(artifact_id: int, conn: DbConn) -> dict[str, object]:
    artifact = artifacts.get_artifact(conn, artifact_id)
    parts = [
        {
            **part,
            "provenance": artifacts.list_provenance(conn, int(part["id"])),
            "checks": artifacts.list_checks(conn, int(part["id"])),
        }
        for part in artifacts.list_parts(conn, artifact_id)
    ]
    return {**_with_sources(conn, artifact), "parts": parts}


@router.get("/solutions/{artifact_id}/status", response_model=StatusRead)
def read_solution_status(artifact_id: int, conn: DbConn) -> dict[str, object]:
    """Everything the progress display needs, and nothing it does not.

    Deliberately excludes part content. This is polled every second or two while a set
    solves, and shipping every solution body on each poll would grow with the work.
    """
    artifact = artifacts.get_artifact(conn, artifact_id)
    return {
        **artifact,
        "parts": [
            {"id": part["id"], "status": part["status"], "verdict": part["verdict"]}
            for part in artifacts.list_parts(conn, artifact_id)
            if part["parent_part_id"] is None
        ],
    }


@router.patch("/solutions/{artifact_id}/segmentation", response_model=SolutionDetail)
def update_segmentation(
    artifact_id: int, payload: SegmentationUpdate, conn: DbConn
) -> dict[str, object]:
    """Replace the problem list with the student's corrected one.

    Only at the gate. Once solving has started the list is what the solutions are keyed
    to, and rewriting it underneath them would leave answers attached to problems that no
    longer exist.
    """
    artifact = artifacts.get_artifact(conn, artifact_id)
    if artifact["state"] != artifacts.AWAITING_REVIEW:
        raise ConflictError(NOT_AT_GATE_MESSAGE)
    if len(payload.problems) > MAX_PROBLEMS:
        raise LyraError(TOO_MANY_PROBLEMS)

    corrected = _to_segmented(conn, artifact_id, payload)
    artifacts.delete_parts(conn, artifact_id)
    solver.write_problems(conn, artifact_id, corrected)
    artifacts.set_problems_total(conn, artifact_id, len(corrected))
    return read_solution(artifact_id, conn)


@router.post(
    "/solutions/{artifact_id}/start",
    response_model=SolutionRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_solution(artifact_id: int, conn: DbConn) -> dict[str, object]:
    """Confirm the problem list and begin solving.

    Answers `202`: a full set with verification passes runs for tens of minutes on local
    hardware, and nothing about that belongs on an open connection.

    Also the way back into a stopped run. Solving skips problems that are already
    complete, so `Solve the rest` after a cancel is this same call rather than a second
    endpoint with its own resume logic to keep in step.
    """
    artifact = artifacts.get_artifact(conn, artifact_id)
    if artifact["state"] not in STARTABLE_STATES:
        raise ConflictError(ALREADY_RUNNING_MESSAGE)
    if not _problem_count(conn, artifact_id):
        raise LyraError(NO_PROBLEMS_MESSAGE)

    artifacts.set_artifact_state(conn, artifact_id, artifacts.PENDING)
    solver.enqueue_solve(artifact_id)
    return _with_sources(conn, artifacts.get_artifact(conn, artifact_id))


@router.patch("/solutions/{artifact_id}/parts/{part_id}", response_model=PartRead)
def update_part(
    artifact_id: int, part_id: int, payload: PartEdit, conn: DbConn
) -> dict[str, object]:
    """Store the student's edit of one step or answer.

    The edit becomes a revision, and the part's origin becomes `user_corrected`, so a
    later read knows this text is the student's rather than the model's. The verdict is
    deliberately cleared to `unchecked`: whatever checking concluded, it concluded it
    about text that is no longer there.
    """
    part = _require_part(conn, artifact_id, part_id)
    artifacts.set_part_content(conn, part_id, payload.content, artifacts.USER_CORRECTED)
    if part["verdict"] != artifacts.UNCHECKED:
        artifacts.set_part_verdict(conn, part_id, artifacts.UNCHECKED, EDITED_SINCE_CHECK)
        artifacts.record_checks(conn, part_id, [])
    return _part_response(conn, part_id)


@router.post(
    "/solutions/{artifact_id}/parts/{part_id}/regenerate",
    response_model=PartRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def regenerate_part(
    artifact_id: int, part_id: int, payload: RegenerateRequest, conn: DbConn
) -> dict[str, object]:
    """Solve one problem again, optionally carrying what the student says is wrong.

    Only a whole problem, not a single step: a step re-derived without the ones after it
    would leave a solution that no longer follows from itself.

    This does not move the artifact's state. The rest of the document stays readable while
    one problem is re-solved, and the existing solution is replaced only once the new one
    has been written.
    """
    part = _require_part(conn, artifact_id, part_id)
    if part["kind"] != artifacts.PROBLEM or part["parent_part_id"] is not None:
        raise LyraError(NOT_A_PROBLEM_MESSAGE)

    artifacts.set_part_status(conn, part_id, artifacts.PART_SOLVING)
    solver.enqueue_regenerate(artifact_id, part_id, payload.correction.strip())
    return _part_response(conn, part_id)


@router.get("/solutions/{artifact_id}/parts/{part_id}/revisions", response_model=list[RevisionRead])
def read_part_revisions(artifact_id: int, part_id: int, conn: DbConn) -> list[dict[str, object]]:
    _require_part(conn, artifact_id, part_id)
    return artifacts.list_revisions(conn, part_id)


@router.post("/solutions/{artifact_id}/parts/{part_id}/restore", response_model=PartRead)
def restore_part_revision(
    artifact_id: int, part_id: int, payload: RestoreRequest, conn: DbConn
) -> dict[str, object]:
    """Put an earlier version of a part back.

    Recorded as a new revision rather than by rewinding the history, so restoring is
    itself undoable and the sheet still shows what was there in between.
    """
    _require_part(conn, artifact_id, part_id)
    revision = artifacts.get_revision(conn, part_id, payload.revision)
    artifacts.set_part_content(
        conn,
        part_id,
        str(revision["content"]),
        artifacts.USER_CORRECTED,
        RESTORED_NOTE.format(revision=payload.revision),
    )
    artifacts.set_part_verdict(conn, part_id, artifacts.UNCHECKED, EDITED_SINCE_CHECK)
    artifacts.record_checks(conn, part_id, [])
    return _part_response(conn, part_id)


@router.post(
    "/solutions/{artifact_id}/resegment",
    response_model=SolutionRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def resegment_solution(artifact_id: int, conn: DbConn) -> dict[str, object]:
    """Read the problem set again from scratch.

    The way back from a failed run, and from a segmentation the student would rather have
    Lyra redo than fix by hand. Refused once solving has started, for the same reason the
    correction endpoint is.
    """
    artifact = artifacts.get_artifact(conn, artifact_id)
    if artifact["state"] in (artifacts.SOLVING, artifacts.SEGMENTING):
        raise ConflictError("This solution set is already running.")

    artifacts.set_artifact_state(conn, artifact_id, artifacts.PENDING)
    solver.enqueue(artifact_id)
    return _with_sources(conn, artifacts.get_artifact(conn, artifact_id))


@router.post("/solutions/{artifact_id}/cancel", response_model=SolutionRead)
def cancel_solution(artifact_id: int, conn: DbConn) -> dict[str, object]:
    """Stop a running solution set, keeping whatever it has already produced.

    A partly solved set is worth more than nothing, so completed problems stay. The
    worker checks this state before it writes, so a run cancelled mid-pass discards its
    proposal rather than landing the student back where they left.
    """
    artifact = artifacts.get_artifact(conn, artifact_id)
    if artifact["state"] not in artifacts.RUNNING_STATES:
        raise ConflictError(NOT_RUNNING_MESSAGE)

    artifacts.set_artifact_state(conn, artifact_id, artifacts.CANCELLED)
    return _with_sources(conn, artifacts.get_artifact(conn, artifact_id))


@router.delete("/solutions/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_solution(artifact_id: int, conn: DbConn) -> None:
    artifacts.delete_artifact(conn, artifact_id)


def _require_ready(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    """A document that can actually be read, or a message naming the one that cannot.

    Segmentation reads the text ingestion extracted, so a document that has not finished
    ingesting has nothing to read. Refusing here beats accepting the run and failing it a
    second later, which would look like Lyra broke rather than like the upload is not done.
    """
    row = conn.execute(
        "select id, filename, state from documents where id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise LyraError("That document does not exist.")
    if row["state"] != "ready":
        raise LyraError(NOT_READY_MESSAGE.format(filename=row["filename"]))
    return row


def _require_part(conn: sqlite3.Connection, artifact_id: int, part_id: int) -> dict[str, object]:
    """One part of this artifact, or a 404 naming which of the two was wrong.

    The artifact is checked first so a stale solution link is a 404 rather than a part
    that happens not to exist, and the ownership check is what stops one artifact's route
    from editing another's part.
    """
    artifacts.get_artifact(conn, artifact_id)
    part = artifacts.get_part(conn, part_id)
    if int(part["artifact_id"]) != artifact_id:
        raise NotFoundError(NOT_THIS_SET_MESSAGE)
    return part


def _part_response(conn: sqlite3.Connection, part_id: int) -> dict[str, object]:
    """One part in the shape `PartRead` wants."""
    return {
        **artifacts.get_part(conn, part_id),
        "provenance": artifacts.list_provenance(conn, part_id),
        "checks": artifacts.list_checks(conn, part_id),
    }


def _problem_count(conn: sqlite3.Connection, artifact_id: int) -> int:
    """How many top-level problems this set holds."""
    return sum(
        1
        for part in artifacts.list_parts(conn, artifact_id)
        if part["parent_part_id"] is None and part["kind"] == artifacts.PROBLEM
    )


def _default_title(conn: sqlite3.Connection, payload: SolutionCreate) -> str:
    """Name the set after its first problem-set document, without the extension."""
    for source in payload.sources:
        if source.role != "problem_set":
            continue
        row = conn.execute(
            "select filename from documents where id = ?", (source.document_id,)
        ).fetchone()
        if row is not None and row["filename"]:
            return str(row["filename"]).rsplit(".", 1)[0] or DEFAULT_TITLE
    return DEFAULT_TITLE


def _with_sources(conn: sqlite3.Connection, artifact: dict[str, object]) -> dict[str, object]:
    """One artifact with its source documents attached."""
    return {**artifact, "sources": artifacts.list_sources(conn, int(artifact["id"]))}


def _to_segmented(
    conn: sqlite3.Connection, artifact_id: int, payload: SegmentationUpdate
) -> list[SegmentedProblem]:
    """Turn a corrected problem list into what the writer takes, carrying provenance over.

    An entry that names an existing part inherits that part's document, page, and chunks:
    the student edited a problem's wording, not where it was found. An entry with no id is
    new, and new is honest about having no provenance.
    """
    stored = artifacts.list_parts(conn, artifact_id)
    existing = {int(part["id"]): part for part in stored if part["parent_part_id"] is None}
    children: dict[int, list[dict[str, object]]] = {}
    for part in stored:
        if part["parent_part_id"] is not None:
            children.setdefault(int(part["parent_part_id"]), []).append(part)
    problems: list[SegmentedProblem] = []
    for index, problem in enumerate(payload.problems, start=1):
        source = existing.get(problem.id) if problem.id is not None else None
        provenance = artifacts.list_provenance(conn, int(source["id"])) if source else []
        problems.append(
            SegmentedProblem(
                # An entry that still matches what Lyra proposed was not edited, whatever
                # else the student did elsewhere in the list. Marking the whole list as
                # corrected would put an `Edited` badge on every untouched problem, which
                # is exactly the wrong thing for a badge that means "not verbatim".
                origin=(
                    artifacts.GENERATED
                    if source is not None and _unchanged(problem, source, children)
                    else artifacts.USER_CORRECTED
                ),
                label=(problem.label or "").strip() or f"Problem {index}",
                number=str(index),
                statement=problem.statement,
                document_id=_document_of(provenance),
                page_number=provenance[0]["page_number"] if provenance else None,
                chunk_ids=tuple(
                    int(entry["chunk_id"]) for entry in provenance if entry["chunk_id"]
                ),
                parts=tuple(
                    SegmentedPart(
                        label=(part.label or "").strip() or f"({chr(ord('a') + position)})",
                        statement=part.statement.strip(),
                    )
                    for position, part in enumerate(problem.parts)
                ),
            )
        )
    return problems


def _unchanged(
    problem: ProblemUpdate,
    source: dict[str, object],
    children: dict[int, list[dict[str, object]]],
) -> bool:
    """Whether a submitted problem still matches the part it names, sub-parts included."""
    if problem.statement.strip() != str(source["content"]):
        return False
    if (problem.label or "").strip() != (source["label"] or ""):
        return False
    stored = children.get(int(source["id"]), [])
    if len(stored) != len(problem.parts):
        return False
    return all(
        part.statement.strip() == str(other["content"])
        and (part.label or "").strip() == (other["label"] or "")
        for part, other in zip(problem.parts, stored, strict=True)
    )


def _document_of(provenance: list[dict[str, object]]) -> int:
    """The document a part came from, or 0 when that is no longer known.

    Zero rather than None because `SegmentedProblem.document_id` is what orders a
    multi-file set, and a problem the student typed in belongs to no file. It is written
    back as provenance only when there is real provenance to write.
    """
    for entry in provenance:
        if entry.get("document_id"):
            return int(entry["document_id"])
    return 0
