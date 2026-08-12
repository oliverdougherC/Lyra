"""The background solve job: segment, wait for review, solve, check, and re-solve.

Ingestion-shaped, not chat-shaped. A full problem set with verification passes can run for
tens of minutes on local hardware, which is well past what an open connection should be
trusted with, so this is a queue drained by a worker thread with a polled status endpoint,
following the pattern `core/ingestion.py` already proves.

It is a *second* worker rather than another consumer of the ingestion queue. Ingestion is
throughput against a single local embedding server; solving is a long chain of tutor-model
calls. Sharing one worker would let a thirty-minute solve block every upload behind it.

The state machine:

    pending -> segmenting -> awaiting_review -> solving -> ready

`awaiting_review` is where the run stops and waits, indefinitely, for the student to
confirm the problem list. It is a state rather than a flag because a restart has to tell
"was working and died" from "was waiting and still is", and only a state can carry that.

This module is the orchestration: state transitions, ordering, and what survives a
failure. The model passes live next door in `core/segmentation.py`, `core/solving.py`, and
`core/verification.py`, so that reading this file tells you what happens when, and reading
those tells you what is asked and how the reply is read.

Two rules run through every path here:

- **Results land as they are produced.** Every problem is written when it completes, never
  buffered until the end, so a student who closes the laptop mid-solve comes back to
  finished work.
- **A failed problem never fails the artifact - but a failed endpoint does.** One problem
  that could not be solved carries its own error and the run continues; the set lands
  `ready` with a gap in it, which is worth more than nothing. What that rule must not be
  allowed to mean is grinding twelve problems through a 600-second timeout each against a
  dead endpoint and then calling the result ready: several failures *in a row* are an
  endpoint fact rather than a problem fact and stop the run, and a set in which nothing
  at all was solved is `failed`, because "ready" with zero solutions is a lie.
"""

import asyncio
import json
import logging
import queue
import sqlite3
import threading
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path

from backend.config import settings
from backend.core import artifacts, figures, solving, verification
from backend.core.app_settings import (
    NO_ENDPOINT,
    REMOTE_UNACKNOWLEDGED,
    TutorConfig,
    get_settings_row,
    resolve_tutor_access,
    update_settings_row,
)
from backend.core.artifacts import ProvenanceEntry
from backend.core.errors import LyraError
from backend.core.segmentation import SegmentedProblem, propose_problems
from backend.core.solving import SolvedProblem, SolveInput
from backend.llm import client
from backend.llm.prompts import SOLVE_SCHEMA
from backend.rag import locate
from backend.rag.parse import PDF_MIME
from backend.storage.database import connect

logger = logging.getLogger("lyra.solver")

NO_SOURCES = "The documents this solution set was built from are no longer there."
NO_PROBLEMS = "There are no problems to solve. Add one at the review step first."
NOT_A_UNIT = (
    "This part is solved as part of its problem, so the whole problem has to be solved again."
)
INTERRUPTED_MESSAGE = "Interrupted, please retry"
_DEFAULT_FAILURE = "Something went wrong while reading this problem set."
_SOLVE_FAILURE = "Something went wrong while solving this problem set."
_PROBLEM_FAILURE = "Lyra could not solve this problem."
_EMPTY_REPLY = "The tutor model returned nothing for this problem."
_NOT_SOLVED_DETAIL = "This problem was not solved, so there was nothing to check."

# How many problems in a row may fail before the run gives up on the rest, mirroring
# `recognition.MAX_CONSECUTIVE_FAILURES` for the same reason. One problem failing is
# ordinary; every problem failing in sequence is the endpoint being down, and each attempt
# against a dead endpoint costs the student the full background timeout. The problems
# never reached stay `pending`, which is the truth, and `Solve the rest` picks them up.
MAX_CONSECUTIVE_FAILURES = 3
ENDPOINT_SUSPECT_MESSAGE = (
    "Several problems in a row could not be solved, so the run stopped. A streak like "
    "that is almost always the tutor endpoint being down or unreachable, not the problems "
    "themselves. Check the endpoint in Settings, then solve again."
)
NOTHING_SOLVED_MESSAGE = (
    "None of the problems in this set could be solved. Check the tutor endpoint in "
    "Settings, then solve again."
)

# Why document text may not be sent, said in the words the student needs to act on. The
# rule itself lives in `app_settings.document_text_allowed`; these are its consequences
# for solving, which cannot happen at all without sending the problem statement.
BLOCKED_MESSAGES = {
    NO_ENDPOINT: "No tutor endpoint is configured. Add one in Settings, then solve.",
    REMOTE_UNACKNOWLEDGED: (
        "Your tutor endpoint is not on this machine, and solving has to send it your "
        "problem statements. Allow that in Settings, then solve."
    ),
}

SEGMENT = "segment"
SOLVE = "solve"
REGENERATE = "regenerate"


@dataclass(frozen=True)
class _Job:
    """One unit of work for the solve worker.

    Carries ids rather than rows or connections: the worker opens its own connection per
    run, so nothing request-scoped can cross the thread boundary.
    """

    kind: str
    artifact_id: int
    part_id: int | None = None
    correction: str = ""


_queue: queue.Queue[_Job] = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False


def enqueue(artifact_id: int) -> None:
    """Hand an artifact to the worker for segmentation. Returns immediately."""
    _queue.put(_Job(kind=SEGMENT, artifact_id=artifact_id))


def enqueue_solve(artifact_id: int) -> None:
    """Hand a confirmed problem list to the worker for solving. Returns immediately."""
    _queue.put(_Job(kind=SOLVE, artifact_id=artifact_id))


def enqueue_regenerate(artifact_id: int, part_id: int, correction: str = "") -> None:
    """Ask the worker to solve one problem again, optionally with a correction."""
    _queue.put(
        _Job(kind=REGENERATE, artifact_id=artifact_id, part_id=part_id, correction=correction)
    )


def start_worker() -> None:
    """Start the single solve worker, once per process.

    Called from the app lifespan, and idempotent so a reload cannot end up with two.
    One worker, because two solves running at once against a local model would halve
    each other's speed while doubling the memory, and neither would finish sooner.
    """
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        thread = threading.Thread(target=_drain_queue, name="lyra-solver", daemon=True)
        thread.start()
        _worker_started = True


def _drain_queue() -> None:
    """Run queued jobs forever, surviving anything one artifact does."""
    while True:
        job = _queue.get()
        try:
            _run(job)
        except Exception:
            # The thread is the whole solving capability of the process. One bad problem
            # set must never be able to take it down.
            logger.exception("Solve job %s crashed for artifact %s", job.kind, job.artifact_id)
        finally:
            _queue.task_done()


def _run(job: _Job) -> None:
    """Dispatch one job to its arm."""
    if job.kind == SEGMENT:
        run_segmentation(job.artifact_id)
    elif job.kind == SOLVE:
        run_solve(job.artifact_id)
    elif job.kind == REGENERATE and job.part_id is not None:
        run_regeneration(job.artifact_id, job.part_id, job.correction)
    else:
        logger.warning("Ignoring unknown solve job kind: %s", job.kind)


def run_segmentation(artifact_id: int) -> None:
    """Take one artifact from `pending` to `awaiting_review`.

    Takes only an id and opens its own connection, so the worker thread never touches a
    request-scoped connection and tests can call it directly without the queue.

    Args:
        artifact_id: Row id in `artifacts`. An artifact deleted since it was queued is
            skipped rather than treated as an error.
    """
    conn = connect()
    try:
        _segment(conn, artifact_id)
    except Exception as exc:
        stage = _current_state(conn, artifact_id)
        if stage is None:
            logger.warning("Segmentation skipped: artifact %s no longer exists", artifact_id)
            return
        logger.exception("Segmentation failed for artifact %s during %s", artifact_id, stage)
        artifacts.mark_artifact_failed(conn, artifact_id, stage, _failure_message(exc))
    finally:
        conn.close()


def _segment(conn: sqlite3.Connection, artifact_id: int) -> None:
    """Propose the problem list, then stop and wait for a person to confirm it."""
    if _current_state(conn, artifact_id) is None:
        logger.warning("Segmentation skipped: artifact %s no longer exists", artifact_id)
        return

    artifacts.set_artifact_state(conn, artifact_id, artifacts.SEGMENTING)
    sources = artifacts.list_sources(conn, artifact_id, artifacts.PROBLEM_SET)
    if not sources:
        # Every problem-set document was deleted between creating this artifact and
        # reaching it. Nothing to read, and nothing the student can fix at the gate.
        raise LyraError(NO_SOURCES)

    proposed: list[SegmentedProblem] = []
    for source in sources:
        document_id = int(source["document_id"])
        text = _document_text(document_id)
        if not text:
            # A document whose extraction is missing contributes nothing. The others
            # still segment, and the gate is where the student notices the gap.
            logger.warning("No extracted text for document %s", document_id)
            continue
        proposed.extend(propose_problems(conn, document_id, str(source["filename"]), text))

    if _current_state(conn, artifact_id) == artifacts.CANCELLED:
        # The student stopped the run while the model was reading. Landing them at the
        # review gate they just walked away from would be the opposite of what they asked,
        # so the proposal is dropped rather than written.
        logger.info("Segmentation of artifact %s discarded: cancelled", artifact_id)
        return

    # Replaced wholesale rather than merged: this runs on a re-segmentation too, and
    # merge and split are not expressible as per-row edits.
    artifacts.delete_parts(conn, artifact_id)
    write_problems(conn, artifact_id, proposed)
    artifacts.set_problems_total(conn, artifact_id, len(proposed))
    artifacts.set_problems_done(conn, artifact_id, 0)
    artifacts.set_artifact_state(conn, artifact_id, artifacts.AWAITING_REVIEW)


def write_problems(
    conn: sqlite3.Connection, artifact_id: int, problems: list[SegmentedProblem]
) -> list[int]:
    """Write a problem list as parts, with sub-parts nested under their problem.

    A sub-part is stored as a `problem` under a `problem`, not as a `step`. It is
    something to be solved, which is what a problem is; a step is a line of the solution.

    Whether a problem's sub-parts are then solved with it or one at a time is
    `solve_parts`, written here and read by `_solve_units`. Both readings put the same
    rows in the same shape; what changes is where the steps and the answer hang.

    Returns:
        The ids of the top-level problem parts, in order.
    """
    # Every problem's position, found before any of them are written, because a marker is
    # only unambiguous beside the other markers on its page.
    positions = _locate_all(
        conn, [(problem.document_id, problem.page_number, problem.label) for problem in problems]
    )

    written: list[int] = []
    for ordinal, problem in enumerate(problems):
        part_id = artifacts.create_part(
            conn,
            artifact_id,
            artifacts.PROBLEM,
            ordinal,
            label=problem.label,
            content=problem.statement,
            origin=problem.origin,
            solve_parts=(
                artifacts.SEPARATELY
                if problem.separate_parts and problem.parts
                else artifacts.TOGETHER
            ),
        )
        # A problem the student typed in at the gate belongs to no file, so it gets no
        # provenance rather than a row pointing at document zero. `document_id or None`
        # is what keeps a deleted source from becoming a foreign key that does not resolve.
        if problem.document_id and (problem.chunk_ids or problem.page_number is not None):
            bbox = positions[ordinal]
            artifacts.set_provenance(
                conn,
                part_id,
                [
                    ProvenanceEntry(
                        chunk_id=chunk_id,
                        document_id=problem.document_id or None,
                        page_number=problem.page_number,
                        bbox=bbox,
                    )
                    for chunk_id in (problem.chunk_ids or (None,))
                ],
            )
        for index, part in enumerate(problem.parts):
            artifacts.create_part(
                conn,
                artifact_id,
                artifacts.PROBLEM,
                index,
                label=part.label,
                content=part.statement,
                parent_part_id=part_id,
                origin=problem.origin,
            )
        written.append(part_id)

    _write_figures(conn, artifact_id, problems, written, positions)
    return written


def _write_figures(
    conn: sqlite3.Connection,
    artifact_id: int,
    problems: list[SegmentedProblem],
    part_ids: list[int],
    positions: list[tuple[float, ...]],
) -> None:
    """Pull the figures a problem refers to into the solution.

    Three rules, and all three are exact. A figure is attached when the problem's statement
    names it - "the system in Figure 3" - or when the problem is the only one on its page,
    where "the figures on this page" is not a guess at all, or when the page's diagrams and
    problem markers alternate, where the pairing is forced by which of the two the page puts
    first. Anything else gets nothing.

    That is deliberately less than it could be, and the reason is worth stating because the
    alternatives were tried. Attaching every figure on a page to every problem on it was the
    first version, and on the acceptance homework it gave twenty-one attachments of which
    twelve were wrong: four Fourier-series problems each received three block diagrams
    belonging to other questions. Distance does not fix it either: the list markers on that
    page sit *below* their diagrams, so "nearest preceding marker" is off by one, and
    "nearest marker by distance" gets figure two wrong by three thousandths of a page.

    The alternation rule is not a distance guess, which is why it is allowed where those
    were not. It reads no gap and no threshold; it asks whether the page has the shape of a
    list, one diagram to one question, and refuses the moment it does not. It needs every
    problem on the page to have a marker of its own, which is what `locate.find_labels`
    resolving a page in one walk now gives it, and which is why this could not be built
    until that was.

    A page where any figure is named by any problem is left to naming alone. A caption the
    text refers to is better evidence than the page's shape, and running both would let one
    diagram be filed under two different problems.

    So the figures of a page that satisfies none of the three are still extracted, served,
    and shown on the page image beside the solution, but they are not filed under a problem.
    A student sees the diagram; Lyra does not claim to know which question it answers.

    The part carries the figure's id as its content. Nothing here copies the image: the
    figure belongs to the document, and a solution referring to it should follow the source
    rather than freeze a crop taken on the day it was solved.
    """
    # The problems on each page, in document order, which is what decides both whether "the
    # figures on this page" identifies anything and whether the page alternates. A problem
    # whose page never resolved cannot be placed in it, and its document is remembered for
    # exactly that reason: the census over that document is incomplete, so "the only
    # problem on its page" is not a fact the census can state. A page really holding
    # problems 3 and 4, where only 4's page survived segmentation, would otherwise compute
    # 4 as alone and hand it every figure on the page, including 3's.
    pages: dict[tuple[int, int], list[int]] = {}
    unplaced: set[int] = set()
    for index, problem in enumerate(problems):
        if problem.document_id and problem.page_number is not None:
            pages.setdefault((problem.document_id, problem.page_number), []).append(index)
        elif problem.document_id:
            unplaced.add(problem.document_id)

    for (document_id, page_number), indexes in pages.items():
        available = figures.list_figures(conn, document_id, [page_number])
        named = {
            index: [
                figure
                for figure in available
                if isinstance(figure["label"], str)
                and _mentions(_problem_text(problems[index]), figure["label"])
            ]
            for index in indexes
        }
        anything_named = any(named[index] for index in indexes)
        paired = (
            {}
            if anything_named
            else _pair_by_alternation([positions[index] for index in indexes], available)
        )
        # The unambiguous-shortcut precondition: this problem is alone on its page *and*
        # the census that says so is complete. With any problem of this document unplaced,
        # the shortcut falls through to naming and alternation, which refuse rather than
        # guess.
        census_complete = document_id not in unplaced

        for position, index in enumerate(indexes):
            chosen = named[index] or (
                available
                if len(indexes) == 1 and census_complete
                else _figures_at(paired, position)
            )
            _attach_figures(conn, artifact_id, part_ids[index], problems[index], chosen)


def _figures_at(paired: dict[int, dict[str, object]], position: int) -> list[dict[str, object]]:
    """The one figure paired with the problem at `position`, as a list, or none."""
    figure = paired.get(position)
    return [] if figure is None else [figure]


def _top_of(bbox: object) -> float | None:
    """The top edge of a rectangle stored as four fractions of the page box."""
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        return float(bbox[1])
    return None


# The fewest figures that make a page a list rather than an illustration.
#
# One diagram among several problems is the ambiguous case, not the easy one: it may belong
# to the question above it, the question below it, or to all of them, and its neighbour
# being a marker says nothing, because on a page of questions everything's neighbour is a
# marker. Two diagrams each with their own marker is a repetition, and a repetition is
# evidence of a layout. This is the guard that keeps a lone figure unattached.
MIN_ALTERNATING_FIGURES = 2

_FIGURE, _MARKER = 0, 1


def _pair_by_alternation(
    positions: list[tuple[float, ...]], available: list[dict[str, object]]
) -> dict[int, dict[str, object]]:
    """One figure per problem where a page reads as a list, and nothing where it does not.

    Every figure must have a problem marker immediately beside it, all of them on the same
    side, and no two may want the same marker. That is the whole test, and each part of it
    is doing work. *Immediately beside* is what makes this structural rather than a distance
    guess: the question is whether anything sits between a diagram and the marker, not how
    far apart they are. *All on the same side* is what settles the layout the acceptance
    homework has, where the list markers sit below their diagrams rather than above them -
    if both readings work, the page has not said which it is and gets neither. And *no two
    to the same marker* is what refuses two diagrams sharing a question.

    Markers left over once the figures run out are not a problem and are what the real page
    looks like: three diagrams at the top of a sheet of seven questions pair with the first
    three, and the other four get nothing.

    Args:
        positions: Where each problem's marker sits, in document order. A problem whose
            marker was not found has an empty rectangle, and one of those is enough to
            refuse the whole page: an unplaced problem cannot take part in an ordering.
        available: The page's figures, in reading order.

    Returns:
        Position in `positions` to figure, for the problems that pair, or an empty mapping
        when the page does not read as a list. Empty is the common answer and is a refusal
        rather than a failure.
    """
    if len(available) < MIN_ALTERNATING_FIGURES or not positions:
        return {}

    markers = [(_top_of(bbox), index) for index, bbox in enumerate(positions)]
    diagrams = [(_top_of(figure["bbox"]), index) for index, figure in enumerate(available)]
    if any(top is None for top, _ in markers + diagrams):
        return {}

    # Down the page, with a figure ahead of a marker at the same height. A marker level with
    # a diagram is a caption beside it rather than above or below it, and putting the figure
    # first there is what makes the tie readable as one layout instead of neither.
    ordered = sorted(
        [(top, _FIGURE, index) for top, index in diagrams]
        + [(top, _MARKER, index) for top, index in markers]
    )

    below = _pair_in_direction(ordered, 1)
    above = _pair_in_direction(ordered, -1)
    if (below is None) == (above is None):
        # Neither reading works, or both do and the page has not said which.
        return {}
    pairing = below if above is None else above
    return {marker: available[figure] for figure, marker in (pairing or {}).items()}


def _pair_in_direction(ordered: list[tuple[float, int, int]], step: int) -> dict[int, int] | None:
    """Each figure to the marker `step` places from it, or None if that does not hold."""
    pairs: dict[int, int] = {}
    for place, (_, kind, index) in enumerate(ordered):
        if kind != _FIGURE:
            continue
        beside = place + step
        if not 0 <= beside < len(ordered) or ordered[beside][1] != _MARKER:
            return None
        pairs[index] = ordered[beside][2]
    # Two diagrams reaching for the same question is not a list.
    return None if len(set(pairs.values())) != len(pairs) else pairs


def _problem_text(problem: SegmentedProblem) -> str:
    """Everything a problem says, sub-parts included, for the naming rule to search.

    "See Figure 2" sits inside part (b) at least as often as in the stem, and a reference
    is a reference wherever the sheet chose to print it. Searching the stem alone let the
    single-problem shortcut hand out figures the parts had already named for themselves.
    """
    return " ".join([problem.statement, *(part.statement for part in problem.parts)])


def _mentions(statement: str, label: str) -> bool:
    """Whether a problem's text refers to a figure by its caption's label.

    Whitespace-insensitive, because `Figure 3` in a caption and `figure 3` in a sentence
    are the same reference, and a PDF's text layer is not reliable about either.
    """
    flat = " ".join(statement.split()).casefold()
    return " ".join(label.split()).casefold() in flat


def _attach_figures(
    conn: sqlite3.Connection,
    artifact_id: int,
    part_id: int,
    problem: SegmentedProblem,
    chosen: list[dict[str, object]],
) -> None:
    """Write one figure part per chosen figure, each citing the page it came from."""
    for index, figure in enumerate(chosen):
        figure_id = artifacts.create_part(
            conn,
            artifact_id,
            artifacts.FIGURE,
            index,
            label=str(figure["name"]),
            content=str(figure["id"]),
            content_type=artifacts.IMAGE,
            parent_part_id=part_id,
            status=artifacts.PART_COMPLETE,
            origin=problem.origin,
        )
        artifacts.set_provenance(
            conn,
            figure_id,
            [
                ProvenanceEntry(
                    document_id=problem.document_id,
                    page_number=int(figure["page_number"]),  # type: ignore[arg-type]
                    label=str(figure["name"]),
                    bbox=tuple(float(value) for value in figure["bbox"]),  # type: ignore[union-attr]
                )
            ],
        )


@dataclass(frozen=True)
class _Unit:
    """One thing to solve: a problem, or one part of a problem that splits.

    Attributes:
        part: The row the steps, the answer, and the verdict hang off. For a split
            problem this is the sub-part, not the problem above it.
        parent: The problem the part sits under, or None when the unit *is* the problem.
            Carried because the part's statement means nothing without the stem above it,
            and because the parent's status is settled once its last part is done.
    """

    part: dict[str, object]
    parent: dict[str, object] | None = None

    @property
    def id(self) -> int:
        return int(self.part["id"])

    @property
    def label(self) -> str:
        """What to call this unit, in the sheet's own words.

        A part's own label is `(b)`, which says nothing on its own -- in the progress
        line, in a failure message, or as the heading the model is asked to solve under.
        Under its problem it becomes `Properties of LTI Systems (b)`, which is what the
        student would call it if you asked them.
        """
        own = str(self.part["label"] or "")
        if self.parent is None:
            return own or "This problem"
        above = str(self.parent["label"] or "")
        return f"{above} {own}".strip() or "This problem"

    @property
    def preamble(self) -> str:
        """The stem this part sits under. Empty when the unit is a whole problem."""
        return "" if self.parent is None else str(self.parent["content"])


def _solve_units(conn: sqlite3.Connection, artifact_id: int) -> list[_Unit]:
    """Everything this run has to solve, in document order.

    A problem whose parts are one solution is one unit and its parts are context, which
    is what solving has always done. A problem whose parts are separate questions is one
    unit *per part*: each gets its own retrieval, its own turn, its own answer, and its
    own checked verdict, because each of those things was per-question all along and only
    ever had one place to go.

    A problem marked `separately` that has no parts left -- the student deleted them at
    the gate -- is solved as itself. The marking describes parts; with none, there is
    nothing it can mean.
    """
    units: list[_Unit] = []
    for problem in _top_level_problems(conn, artifact_id):
        parts = [
            child
            for child in artifacts.list_child_parts(conn, int(problem["id"]))
            if child["kind"] == artifacts.PROBLEM
        ]
        if problem["solve_parts"] == artifacts.SEPARATELY and parts:
            units.extend(_Unit(part=part, parent=problem) for part in parts)
        else:
            units.append(_Unit(part=problem))
    return units


def _unit_for(conn: sqlite3.Connection, part_id: int) -> _Unit:
    """Rebuild one unit from the part it hangs its work off.

    What the whole run knows from `_solve_units`, a re-solve of one part has to work out
    from the part alone: whether this is a problem, or one part of a problem that splits.
    Read the same way both times -- from `solve_parts` on the row above -- so a re-solve
    asks the model exactly what the first pass asked it.
    """
    part = artifacts.get_part(conn, part_id)
    parent_id = part["parent_part_id"]
    if parent_id is None:
        return _Unit(part=part)
    parent = artifacts.get_part(conn, int(parent_id))
    if parent["solve_parts"] != artifacts.SEPARATELY:
        # A sub-part of a problem solved as a whole is not a unit at all. The API refuses
        # to target one, and this is the same refusal a step further down.
        raise LyraError(NOT_A_UNIT)
    return _Unit(part=part, parent=parent)


def run_solve(artifact_id: int) -> None:
    """Take one artifact from a confirmed problem list to `ready`.

    Opens its own connection, so tests can call it directly without the queue.
    """
    conn = connect()
    try:
        _solve(conn, artifact_id)
    except Exception as exc:
        stage = _current_state(conn, artifact_id)
        if stage is None:
            logger.warning("Solve skipped: artifact %s no longer exists", artifact_id)
            return
        logger.exception("Solve failed for artifact %s during %s", artifact_id, stage)
        artifacts.mark_artifact_failed(
            conn, artifact_id, stage, _failure_message(exc, _SOLVE_FAILURE)
        )
    finally:
        conn.close()


def _solve(conn: sqlite3.Connection, artifact_id: int) -> None:
    """Solve every unsolved problem in order, writing each one as it completes."""
    artifact = artifacts.get_artifact(conn, artifact_id)
    class_id = int(artifact["class_id"])

    # Solving sends the student's own problem statements to the tutor model, so it is
    # bound by the same rule profile extraction and segmentation are: document text is
    # never sent to a non-local endpoint the student has not acknowledged. Asked before
    # any statement is read, so there is no path on which it leaks. Unlike segmentation,
    # solving cannot degrade around this: without the statement there is nothing to solve.
    # One snapshot: the endpoint checked for consent is the endpoint solving is sent to.
    access = resolve_tutor_access(conn)
    if access.document_block is not None:
        raise LyraError(BLOCKED_MESSAGES.get(access.document_block, BLOCKED_MESSAGES[NO_ENDPOINT]))
    config = access.config

    # Units, not problems: a section of five independent questions is five things to
    # solve and the progress line should say so. Counting sections instead reported a
    # sheet as a fifth done when it was a fifth of the way through its first section.
    units = _solve_units(conn, artifact_id)
    if not units:
        raise LyraError(NO_PROBLEMS)

    artifacts.set_artifact_state(conn, artifact_id, artifacts.SOLVING)
    artifacts.set_problems_total(conn, artifact_id, len(units))
    # A resumed run already holds finished problems. Counting from zero would report work
    # as undone that the student can read on screen right now.
    done = sum(1 for unit in units if unit.part["status"] == artifacts.PART_COMPLETE)
    artifacts.set_problems_done(conn, artifact_id, done)

    consecutive = 0
    for unit in units:
        if _current_state(conn, artifact_id) == artifacts.CANCELLED:
            logger.info("Solve of artifact %s stopped: cancelled", artifact_id)
            return
        if unit.part["status"] == artifacts.PART_COMPLETE:
            continue
        artifacts.set_artifact_state(conn, artifact_id, artifacts.SOLVING, stage_detail=unit.label)
        solved = _solve_one(conn, artifact_id, class_id, config, unit)
        _settle_parent(conn, unit)
        if solved:
            consecutive = 0
            # Only successes count. The interface renders this as "3 of 12 solved", and a
            # tally that included failures would read "12 of 12" over a page of errors.
            artifacts.increment_problems_done(conn, artifact_id)
            continue
        consecutive += 1
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            logger.warning(
                "Stopped solving artifact %s after %s problems failed in a row",
                artifact_id,
                consecutive,
            )
            raise LyraError(ENDPOINT_SUSPECT_MESSAGE)

    if _current_state(conn, artifact_id) == artifacts.CANCELLED:
        return
    # Re-read rather than trusted from the loop, because a resumed run's `units` carry the
    # statuses they had when the run began. A set in which not one problem was solved -
    # this run or any before it - is not ready by any honest reading of the word.
    statuses = [str(artifacts.get_part(conn, unit.id)["status"]) for unit in units]
    if statuses and all(status == artifacts.PART_FAILED for status in statuses):
        raise LyraError(NOTHING_SOLVED_MESSAGE)
    artifacts.set_artifact_state(conn, artifact_id, artifacts.READY)


def _solve_one(
    conn: sqlite3.Connection,
    artifact_id: int,
    class_id: int,
    config: TutorConfig,
    unit: _Unit,
    correction: str = "",
) -> bool:
    """Solve one unit and check it, recording a failure on the unit itself.

    A problem that cannot be solved carries its own error and its own `failed` status. The
    artifact keeps going: a set with one gap in it is worth more than a set that stopped.
    On a split problem that is per part, so one part that failed leaves the other four
    readable and one part marked wrong sends one part back to the model.

    Returns:
        Whether the unit was solved. The caller's circuit breaker and its progress count
        both need the distinction: a failure is recorded here, but it is not progress.
    """
    part_id = unit.id
    try:
        artifacts.set_part_status(conn, part_id, artifacts.PART_SOLVING)
        solved, provenance = _generate(conn, artifact_id, class_id, config, unit, correction)
        origin = artifacts.REGENERATED if correction else artifacts.GENERATED
        _write_solution(
            conn, artifact_id, part_id, solved, provenance, origin, note=correction or None
        )
    except Exception as exc:
        logger.exception("Could not solve part %s", part_id)
        artifacts.set_part_status(
            conn, part_id, artifacts.PART_FAILED, _failure_message(exc, _PROBLEM_FAILURE)
        )
        artifacts.set_part_verdict(conn, part_id, artifacts.UNCHECKED, _NOT_SOLVED_DETAIL)
        return False

    _check(conn, artifact_id, class_id, config, unit, solved)
    artifacts.set_part_status(conn, part_id, artifacts.PART_COMPLETE)
    return True


def _settle_parent(conn: sqlite3.Connection, unit: _Unit) -> None:
    """Mark a split problem finished once its last part is.

    The problem above a set of separately solved parts is never solved itself, so nothing
    would ever move it off `pending` and the interface would show a section that looks
    like it is still queued while every question inside it is answered. It is complete
    when none of its parts is still waiting; a part that failed is a failure the student
    can see on that part, not a section that never finished.
    """
    if unit.parent is None:
        return
    parent_id = int(unit.parent["id"])
    unsettled = {artifacts.PART_PENDING, artifacts.PART_SOLVING, artifacts.PART_VERIFYING}
    parts = [
        child
        for child in artifacts.list_child_parts(conn, parent_id)
        if child["kind"] == artifacts.PROBLEM
    ]
    if any(child["status"] in unsettled for child in parts):
        return
    artifacts.set_part_status(conn, parent_id, artifacts.PART_COMPLETE)


def _generate(
    conn: sqlite3.Connection,
    artifact_id: int,
    class_id: int,
    config: TutorConfig,
    unit: _Unit,
    correction: str,
) -> tuple[SolvedProblem, list[list[ProvenanceEntry]]]:
    """Gather this unit's evidence, ask the model, and read the reply.

    Returns:
        The solution, and one provenance list per step in the same order. The provenance
        is returned rather than stored on `SolvedProblem` because it is resolved from
        this run's retrieval, not part of what the model said.

    Raises:
        LyraError: The reply held nothing usable at all. Every softer failure, including
            a model that ignored the structure entirely, is handled by the parser.
    """
    # A unit that is one part of a split problem answers for itself alone, so it carries
    # no sub-parts: the only rows under it are its own steps, and the parts beside it are
    # other units with their own turns.
    sub_parts = (
        ()
        if unit.parent is not None
        else tuple(
            (str(child["label"] or ""), str(child["content"]))
            for child in artifacts.list_child_parts(conn, unit.id)
            if child["kind"] == artifacts.PROBLEM
        )
    )
    problem = SolveInput(
        statement=str(unit.part["content"]),
        label=unit.label,
        preamble=unit.preamble,
        sub_parts=sub_parts,
        correction=correction,
    )
    retrieval_budget, reference_budget = solving.plan_budgets(config)
    retrieval = solving.retrieve_for(conn, class_id, problem.query(), retrieval_budget)
    references = solving.reference_documents(conn, artifact_id, reference_budget)
    messages = solving.build_prompt(problem, retrieval, references)

    # The worker is a plain thread with no event loop, and `complete` is async. Owning a
    # loop for the length of the call keeps this function synchronous.
    content = asyncio.run(
        client.complete(
            config.endpoint_url,
            config.api_key,
            config.model,
            messages,
            temperature=client.DETERMINISTIC_TEMPERATURE,
            schema=SOLVE_SCHEMA,
            request_timeout=client.BACKGROUND_TIMEOUT,
        )
    )
    solved = solving.parse_solution(content)
    if not solved.steps and not solved.answer:
        raise LyraError(_EMPTY_REPLY)
    return solved, _provenance_for(solved, retrieval, references)


def _provenance_for(
    solved: SolvedProblem,
    retrieval: solving.RetrievalResult,
    references: list[solving.ReferenceDocument],
) -> list[list[ProvenanceEntry]]:
    """Resolve each step's cited context numbers into provenance rows.

    The numbering is one sequence: retrieved chunks first, then the reference solutions
    attached to this run. A reference citation becomes a document-level row with no chunk
    and no page, because a reference enters the prompt whole rather than as an indexed
    passage, and inventing a page for it would be a citation nobody can follow.

    A citation outside the sequence is dropped rather than stored: a source line that
    resolves to nothing would render as grounding the step does not have.
    """
    resolved: list[list[ProvenanceEntry]] = []
    for step in solved.steps:
        entries: list[ProvenanceEntry] = []
        for number in step.sources:
            if 1 <= number <= len(retrieval.chunks):
                chunk = retrieval.chunks[number - 1]
                entries.append(
                    ProvenanceEntry(
                        chunk_id=chunk.chunk_id,
                        document_id=chunk.document_id,
                        page_number=chunk.page_number,
                        # The path where the document has an outline. A citation reading
                        # `Rn / The Cross Product` says where in the book a step came
                        # from; `The Cross Product` alone does not say which book section,
                        # and three of the reference book's titles are not unique.
                        label=chunk.section_path or chunk.section_title,
                    )
                )
                continue
            offset = number - len(retrieval.chunks) - 1
            if 0 <= offset < len(references):
                reference = references[offset]
                entries.append(
                    ProvenanceEntry(
                        chunk_id=None,
                        document_id=reference.document_id,
                        page_number=None,
                        label=None,
                    )
                )
        resolved.append(entries)
    return resolved


def _write_solution(
    conn: sqlite3.Connection,
    artifact_id: int,
    part_id: int,
    solved: SolvedProblem,
    provenance: list[list[ProvenanceEntry]],
    origin: str,
    note: str | None = None,
) -> None:
    """Write a solution that has already been generated onto a problem's parts.

    The order is the point, and it is the same rule the chat retry follows: nothing is
    replaced until the replacement exists. A re-solve that fails upstream leaves the
    student with the solution they already had rather than with nothing.

    Existing steps are **rewritten in place** rather than dropped and recreated. That is
    what gives a re-solved step a history to show instead of a single entry, and it keeps
    a step's id stable, so a Guide panel or a link anchored to step 2 still points at
    step 2 afterwards. Only surplus steps are deleted, and a problem's sub-parts are never
    touched: they are the question, not the answer.
    """
    children = artifacts.list_child_parts(conn, part_id)
    steps = [child for child in children if child["kind"] == artifacts.STEP]
    answers = [child for child in children if child["kind"] == artifacts.ANSWER]

    for index, step in enumerate(solved.steps):
        entries = provenance[index] if index < len(provenance) else []
        if index < len(steps):
            step_id = int(steps[index]["id"])
            artifacts.set_part_content(conn, step_id, step.content, origin, note)
            artifacts.set_part_position(conn, step_id, index, step.title or None)
            artifacts.set_part_status(conn, step_id, artifacts.PART_COMPLETE)
        else:
            step_id = artifacts.create_part(
                conn,
                artifact_id,
                artifacts.STEP,
                index,
                label=step.title or None,
                content=step.content,
                parent_part_id=part_id,
                status=artifacts.PART_COMPLETE,
                origin=origin,
                note=note,
            )
        # Replaced unconditionally, including with nothing: a step this run did not
        # ground must not keep the citation the previous run gave it.
        artifacts.set_provenance(conn, step_id, entries)

    for surplus in steps[len(solved.steps) :]:
        artifacts.delete_part(conn, int(surplus["id"]))

    _write_answer(conn, artifact_id, part_id, solved, answers, origin, note)


def _write_answer(
    conn: sqlite3.Connection,
    artifact_id: int,
    part_id: int,
    solved: SolvedProblem,
    answers: list[dict[str, object]],
    origin: str,
    note: str | None,
) -> None:
    """Write, rewrite, or drop the problem's final answer part."""
    if not solved.answer:
        # A solution that reached no stated answer must not keep the previous one, which
        # would read as this run's result.
        for stale in answers:
            artifacts.delete_part(conn, int(stale["id"]))
        return

    if answers:
        answer_id = int(answers[0]["id"])
        artifacts.set_part_content(conn, answer_id, solved.answer, origin, note)
        artifacts.set_part_position(conn, answer_id, len(solved.steps), "Answer")
        artifacts.set_part_status(conn, answer_id, artifacts.PART_COMPLETE)
        for stale in answers[1:]:
            artifacts.delete_part(conn, int(stale["id"]))
        return

    artifacts.create_part(
        conn,
        artifact_id,
        artifacts.ANSWER,
        len(solved.steps),
        label="Answer",
        content=solved.answer,
        parent_part_id=part_id,
        status=artifacts.PART_COMPLETE,
        origin=origin,
        note=note,
    )


def _check(
    conn: sqlite3.Connection,
    artifact_id: int,
    class_id: int,
    config: TutorConfig,
    unit: _Unit,
    solved: SolvedProblem,
) -> None:
    """Check a finished solution, re-deriving once if a check disagrees.

    Exactly once. A problem re-run until it passes is a problem nobody checked, so a
    second refutation is recorded as the verdict rather than triggering a third attempt.

    The checker is given the stem as well as the part, because one part of a split problem
    states a case and not a question: "is $y(t) = x^2(t)$ correct" is not something anyone
    can check, and "is it linear and time-invariant" is.
    """
    part_id = unit.id
    label = unit.label
    if not _tool_support(conn, config):
        artifacts.record_checks(conn, part_id, [])
        artifacts.set_part_verdict(
            conn, part_id, artifacts.UNCHECKED, verification.NO_TOOL_SUPPORT_DETAIL
        )
        return

    statement = solving.SolveInput(
        statement=str(unit.part["content"]), label=label, preamble=unit.preamble
    ).query()
    artifacts.set_part_status(conn, part_id, artifacts.PART_VERIFYING)
    outcome = verification.verify(config, statement, label, solved.as_markdown())

    if outcome.refuted:
        logger.info("Check disagreed with part %s, re-deriving once", part_id)
        try:
            redone, provenance = _generate(
                conn, artifact_id, class_id, config, unit, outcome.detail
            )
            _write_solution(
                conn,
                artifact_id,
                part_id,
                redone,
                provenance,
                artifacts.REGENERATED,
                note=outcome.detail,
            )
            outcome = verification.verify(
                config, statement, label, redone.as_markdown(), refutation=outcome.detail
            )
        except Exception:
            # The re-derive failed upstream. The first solution and the first refutation
            # both stand: the student keeps readable work and is told what disagreed.
            logger.exception("Could not re-derive part %s after a refutation", part_id)

    artifacts.record_checks(conn, part_id, list(outcome.checks))
    artifacts.set_part_verdict(conn, part_id, outcome.verdict, outcome.detail or None)


def _tool_support(conn: sqlite3.Connection, config: TutorConfig) -> bool:
    """Whether this endpoint can run tool calls, probing once and remembering.

    A real inference call rather than a header, because an OpenAI-compatible server
    advertises nothing about tool calling. The answer is stored on the settings row and
    cleared whenever the endpoint or model changes, so it is never carried across a
    configuration it was not measured on.
    """
    stored = get_settings_row(conn)["tools_supported"]
    if stored is not None:
        return bool(stored)
    try:
        support = asyncio.run(
            client.probe_tool_support(config.endpoint_url, config.api_key, config.model)
        )
    except Exception:
        # Unknown rather than unsupported: nothing is stored, so the next run asks again.
        logger.exception("Could not probe tool support")
        return False
    update_settings_row(
        conn, {"tools_supported": int(support.ok), "tools_message": support.message}
    )
    return support.ok


def run_regeneration(artifact_id: int, part_id: int, correction: str = "") -> None:
    """Solve one problem again, optionally carrying the student's correction.

    Deliberately does not move the artifact's state. The rest of the document is still
    readable while this one problem is re-solved, and only the initial run walks the
    artifact state machine.
    """
    conn = connect()
    try:
        artifact = artifacts.get_artifact(conn, artifact_id)
        # One snapshot: the endpoint checked for consent is the endpoint solving is sent to.
        access = resolve_tutor_access(conn)
        if access.document_block is not None:
            raise LyraError(
                BLOCKED_MESSAGES.get(access.document_block, BLOCKED_MESSAGES[NO_ENDPOINT])
            )
        config = access.config
        unit = _unit_for(conn, part_id)
        _solve_one(
            conn, artifact_id, int(artifact["class_id"]), config, unit, correction=correction
        )
        _settle_parent(conn, unit)
    except Exception as exc:
        logger.exception("Could not regenerate part %s", part_id)
        try:
            artifacts.set_part_status(
                conn, part_id, artifacts.PART_FAILED, _failure_message(exc, _PROBLEM_FAILURE)
            )
        except LyraError:
            logger.warning("Regeneration target %s no longer exists", part_id)
    finally:
        conn.close()


def reconcile_interrupted(conn: sqlite3.Connection) -> int:
    """Settle every artifact the last shutdown caught. Returns how many were failed.

    The queue lives in memory, so an artifact left non-terminal would otherwise sit there
    forever claiming to be working. What it deserves depends on what it was doing, and the
    reasoning is the same as ingestion's `reconcile_interrupted`:

    A `pending` artifact was queued and never touched: nothing was interrupted, and
    failing it was a punishment it did nothing to earn - every restart during development
    turned freshly created sets into rows the student had to retry by hand. What it was
    queued *for* is not written down, though, because both segmentation and solving pass
    through `pending`. An artifact with no problem parts can only have been waiting to
    segment and is requeued outright. One that has parts might be a confirmed list
    waiting to solve or a set the student asked to re-read, and guessing wrong either
    burns model time on a list the student rejected or - worse - re-segments over their
    gate corrections. So it goes back to the gate instead: everything it had is intact,
    and resuming is the one click that says which of the two they meant.

    An artifact caught in `segmenting` or `solving` had started when the process died and
    is failed, with its mid-flight parts reset to `pending`: nothing is wrong with those
    parts, they simply never ran, and the retry should pick them up as unsolved work.

    A mid-flight part whose artifact is *not* being failed - a regeneration caught inside
    a `ready` set - is marked `failed` instead. Resetting it to `pending` left a status no
    run would ever pick up, which on screen is a spinner that never stops; `failed` with
    the interrupted message is the truth, and the part's previous content is untouched, so
    the student keeps what they had and can regenerate.

    An artifact in `awaiting_review` is deliberately left alone. It was not working, it
    was waiting, and a restart does not change what it is waiting for.
    """
    # Scoped to solution sets: study decks, quizzes, and drafts have their own reconciles
    # with their own rules, and a shared sweep would requeue a deck into the solver.
    pending = [
        int(row[0])
        for row in conn.execute(
            "select id from artifacts where state = ? and kind = ?",
            (artifacts.PENDING, artifacts.KIND_SOLUTION_SET),
        )
    ]
    queued = [artifact_id for artifact_id in pending if not _top_level_problems(conn, artifact_id)]
    gated = [artifact_id for artifact_id in pending if artifact_id not in queued]
    for artifact_id in gated:
        artifacts.set_artifact_state(conn, artifact_id, artifacts.AWAITING_REVIEW)

    mid_flight = tuple(state for state in artifacts.RUNNING_STATES if state != artifacts.PENDING)
    placeholders = ", ".join("?" for _ in mid_flight)
    stalled = [
        int(row[0])
        for row in conn.execute(
            f"select id from artifacts where state in ({placeholders}) "  # noqa: S608
            "and kind = ?",
            (*mid_flight, artifacts.KIND_SOLUTION_SET),
        )
    ]
    conn.execute(
        # The placeholders are generated from a module constant and every value is bound.
        # `stage_detail` reads the pre-update row, so it keeps the lost stage.
        f"update artifacts set stage_detail = state, state = '{artifacts.FAILED}', "  # noqa: S608
        f"error_message = ?, updated_at = datetime('now') where state in ({placeholders}) "
        "and kind = ?",
        (INTERRUPTED_MESSAGE, *mid_flight, artifacts.KIND_SOLUTION_SET),
    )

    retried = [*stalled, *pending]
    mid_parts = f"('{artifacts.PART_SOLVING}', '{artifacts.PART_VERIFYING}')"
    if retried:
        retried_placeholders = ", ".join("?" for _ in retried)
        conn.execute(
            f"update artifact_parts set status = '{artifacts.PART_PENDING}', "  # noqa: S608
            f"updated_at = datetime('now') where status in {mid_parts} "
            f"and artifact_id in ({retried_placeholders})",
            retried,
        )
        stray_scope = f"and artifact_id not in ({retried_placeholders})"
        stray_values: tuple[object, ...] = (INTERRUPTED_MESSAGE, *retried)
    else:
        stray_scope = ""
        stray_values = (INTERRUPTED_MESSAGE,)
    conn.execute(
        f"update artifact_parts set status = '{artifacts.PART_FAILED}', "  # noqa: S608
        f"error_message = ?, updated_at = datetime('now') "
        f"where status in {mid_parts} {stray_scope}",
        stray_values,
    )
    conn.commit()

    # After the commit, so a queue that starts draining immediately cannot race the
    # writes above.
    for artifact_id in queued:
        enqueue(artifact_id)
    return len(stalled)


def _top_level_problems(conn: sqlite3.Connection, artifact_id: int) -> list[dict[str, object]]:
    """The problems a solve run walks, in document order."""
    return [
        part
        for part in artifacts.list_parts(conn, artifact_id)
        if part["parent_part_id"] is None and part["kind"] == artifacts.PROBLEM
    ]


def _pdf_path(conn: sqlite3.Connection, document_id: int) -> Path | None:
    """Where a document is stored, when it is a PDF and still exists as a row."""
    if not document_id:
        return None
    row = conn.execute(
        "select stored_path, mime from documents where id = ?", (document_id,)
    ).fetchone()
    if row is None or str(row["mime"]) != PDF_MIME:
        return None
    return Path(str(row["stored_path"]))


def _locate_all(
    conn: sqlite3.Connection, wanted: list[tuple[int, int | None, str | None]]
) -> list[tuple[float, ...]]:
    """Where each problem's marker sits, resolved a page at a time and in document order.

    Grouped by page rather than asked one problem at a time, because that is what lets two
    problems numbered the same take different markers: `locate.find_labels` walks a page
    once and never goes back up it. Asking per problem gave every `1.` on a sheet the first
    one, which drew the source pane's highlight band at the wrong place and left figures
    with no distinct position to be paired against.

    Args:
        wanted: One `(document_id, page_number, label)` per problem, in document order.

    Returns:
        One rectangle per entry, in the same order. Empty rather than None on a miss, so
        the backfill knows this has already been looked for and does not reopen the same
        PDF on every start.
    """
    found: list[tuple[float, ...]] = [() for _ in wanted]
    pages: dict[tuple[int, int], list[int]] = {}
    for index, (document_id, page_number, label) in enumerate(wanted):
        if document_id and page_number is not None and label:
            pages.setdefault((document_id, page_number), []).append(index)

    for (document_id, page_number), indexes in pages.items():
        path = _pdf_path(conn, document_id)
        if path is None:
            continue
        located = locate.find_labels(
            path, page_number, [str(wanted[index][2]) for index in indexes]
        )
        for index, rect in zip(indexes, located, strict=True):
            found[index] = rect or ()
    return found


def backfill_problem_locations(conn: sqlite3.Connection) -> int:
    """Find the page position of problems written before positions were recorded.

    Solution sets are worth keeping across a version of the app that learns something new
    about them, and re-segmenting them to pick this up would throw away every correction
    the student made at the review gate. Runs at startup, does nothing on the second run,
    and never raises: this drives a click target on a page image.
    """
    # One row per problem, in the order the sheet puts them in, which is what `_locate_all`
    # needs to walk a page. `min(v.id)` collapses the several provenance rows a problem with
    # several chunks carries; they all describe the same marker and all take the same box.
    rows = conn.execute(
        "select p.artifact_id, p.id as part_id, v.document_id, v.page_number, p.label "
        "from artifact_provenance v join artifact_parts p on p.id = v.part_id "
        "where v.bbox is null and p.kind = ? and p.parent_part_id is null "
        "and v.document_id is not null and v.page_number is not null "
        "group by p.id order by p.artifact_id, p.ordinal, min(v.id)",
        (artifacts.PROBLEM,),
    ).fetchall()

    located = 0
    # One artifact at a time. Two solution sets over the same sheet hold the same problems
    # on the same page, and walking both as one sequence would run the second set off the
    # bottom of the page it shares with the first.
    for _, group in groupby(rows, key=lambda row: int(row["artifact_id"])):
        batch = list(group)
        positions = _locate_all(
            conn,
            [
                (int(row["document_id"]), int(row["page_number"]), str(row["label"] or ""))
                for row in batch
            ],
        )
        for row, found in zip(batch, positions, strict=True):
            conn.execute(
                "update artifact_provenance set bbox = ? where part_id = ? and bbox is null",
                (json.dumps(list(found)), int(row["part_id"])),
            )
            if found:
                located += 1
    conn.commit()
    return located


def _document_text(document_id: int) -> str:
    """The text ingestion extracted, which is what segmentation reads.

    Read from disk rather than reassembled from chunks: chunk overlap would repeat
    paragraphs, and a problem statement the student compares against their own sheet must
    not have a duplicated line in it.
    """
    path = settings.text_dir / f"{document_id}.txt"
    if not path.exists():
        return ""
    return Path(path).read_text(encoding="utf-8")


def _current_state(conn: sqlite3.Connection, artifact_id: int) -> str | None:
    """The artifact's state right now, or None if it has been deleted."""
    row = conn.execute("select state from artifacts where id = ?", (artifact_id,)).fetchone()
    return None if row is None else str(row["state"])


def _failure_message(exc: Exception, fallback: str = _DEFAULT_FAILURE) -> str:
    """A user-facing reason, never carrying a path or a traceback."""
    if isinstance(exc, LyraError):
        return exc.message
    return fallback
