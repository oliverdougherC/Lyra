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
from backend.core.errors import ConflictError, LyraError
from backend.core.segmentation import SegmentedPart, SegmentedProblem
from backend.storage.database import get_db

router = APIRouter(prefix="/api", tags=["solutions"])

DbConn = Annotated[sqlite3.Connection, Depends(get_db)]

MAX_PROBLEMS = 200

NOT_READY_MESSAGE = "{filename} has not finished processing yet."
NOT_AT_GATE_MESSAGE = "This solution set is not waiting for review."
NOT_RUNNING_MESSAGE = "This solution set is not running."
TOO_MANY_PROBLEMS = f"A solution set can hold at most {MAX_PROBLEMS} problems."
DEFAULT_TITLE = "Solution set"

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
    error_message: str | None
    provenance: list[ProvenanceRead]


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
        {**part, "provenance": artifacts.list_provenance(conn, int(part["id"]))}
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
