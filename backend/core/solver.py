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
- **A failed problem never fails the artifact.** One problem that could not be solved
  carries its own error and the run continues; the set lands `ready` with a gap in it,
  which is worth more than nothing.
"""

import asyncio
import json
import logging
import queue
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from backend.config import settings
from backend.core import artifacts, solving, verification
from backend.core.app_settings import (
    NO_ENDPOINT,
    REMOTE_UNACKNOWLEDGED,
    TutorConfig,
    document_text_allowed,
    get_settings_row,
    resolve_tutor_config,
    update_settings_row,
)
from backend.core.artifacts import ProvenanceEntry
from backend.core.errors import LyraError
from backend.core.segmentation import SegmentedProblem, propose_problems
from backend.core.solving import SolvedProblem, SolveInput
from backend.llm import client
from backend.rag import locate
from backend.rag.parse import PDF_MIME
from backend.storage.database import connect

logger = logging.getLogger("lyra.solver")

NO_SOURCES = "The documents this solution set was built from are no longer there."
NO_PROBLEMS = "There are no problems to solve. Add one at the review step first."
INTERRUPTED_MESSAGE = "Interrupted, please retry"
_DEFAULT_FAILURE = "Something went wrong while reading this problem set."
_SOLVE_FAILURE = "Something went wrong while solving this problem set."
_PROBLEM_FAILURE = "Lyra could not solve this problem."
_EMPTY_REPLY = "The tutor model returned nothing for this problem."
_NOT_SOLVED_DETAIL = "This problem was not solved, so there was nothing to check."

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
    Solving works on top-level problems, so a problem and its sub-parts are solved
    together, which is also what the model wants: the parts share context.

    Returns:
        The ids of the top-level problem parts, in order.
    """
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
        )
        # A problem the student typed in at the gate belongs to no file, so it gets no
        # provenance rather than a row pointing at document zero. `document_id or None`
        # is what keeps a deleted source from becoming a foreign key that does not resolve.
        if problem.document_id and (problem.chunk_ids or problem.page_number is not None):
            bbox = _locate(conn, problem.document_id, problem.page_number, problem.label)
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
    return written


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
    blocked = document_text_allowed(conn)
    if blocked is not None:
        raise LyraError(BLOCKED_MESSAGES.get(blocked, BLOCKED_MESSAGES[NO_ENDPOINT]))
    config = resolve_tutor_config(conn)

    problems = _top_level_problems(conn, artifact_id)
    if not problems:
        raise LyraError(NO_PROBLEMS)

    artifacts.set_artifact_state(conn, artifact_id, artifacts.SOLVING)
    artifacts.set_problems_total(conn, artifact_id, len(problems))
    # A resumed run already holds finished problems. Counting from zero would report work
    # as undone that the student can read on screen right now.
    done = sum(1 for problem in problems if problem["status"] == artifacts.PART_COMPLETE)
    artifacts.set_problems_done(conn, artifact_id, done)

    for problem in problems:
        if _current_state(conn, artifact_id) == artifacts.CANCELLED:
            logger.info("Solve of artifact %s stopped: cancelled", artifact_id)
            return
        if problem["status"] == artifacts.PART_COMPLETE:
            continue
        part_id = int(problem["id"])
        artifacts.set_artifact_state(
            conn, artifact_id, artifacts.SOLVING, stage_detail=str(problem["label"] or "")
        )
        _solve_one(conn, artifact_id, class_id, config, part_id)
        artifacts.increment_problems_done(conn, artifact_id)

    if _current_state(conn, artifact_id) == artifacts.CANCELLED:
        return
    artifacts.set_artifact_state(conn, artifact_id, artifacts.READY)


def _solve_one(
    conn: sqlite3.Connection,
    artifact_id: int,
    class_id: int,
    config: TutorConfig,
    part_id: int,
    correction: str = "",
) -> None:
    """Solve one problem and check it, recording a failure on the problem itself.

    A problem that cannot be solved carries its own error and its own `failed` status. The
    artifact keeps going: a set with one gap in it is worth more than a set that stopped.
    """
    problem = artifacts.get_part(conn, part_id)
    label = str(problem["label"] or "This problem")
    try:
        artifacts.set_part_status(conn, part_id, artifacts.PART_SOLVING)
        solved, provenance = _generate(conn, artifact_id, class_id, config, problem, correction)
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
        return

    _check(conn, artifact_id, class_id, config, part_id, label, solved)
    artifacts.set_part_status(conn, part_id, artifacts.PART_COMPLETE)


def _generate(
    conn: sqlite3.Connection,
    artifact_id: int,
    class_id: int,
    config: TutorConfig,
    problem: dict[str, object],
    correction: str,
) -> tuple[SolvedProblem, list[list[ProvenanceEntry]]]:
    """Gather this problem's evidence, ask the model, and read the reply.

    Returns:
        The solution, and one provenance list per step in the same order. The provenance
        is returned rather than stored on `SolvedProblem` because it is resolved from
        this run's retrieval, not part of what the model said.

    Raises:
        LyraError: The reply held nothing usable at all. Every softer failure, including
            a model that ignored the structure entirely, is handled by the parser.
    """
    statement = str(problem["content"])
    sub_parts = tuple(
        (str(child["label"] or ""), str(child["content"]))
        for child in artifacts.list_child_parts(conn, int(problem["id"]))
        if child["kind"] == artifacts.PROBLEM
    )
    retrieval_budget, reference_budget = solving.plan_budgets(config)
    retrieval = solving.retrieve_for(conn, class_id, statement, retrieval_budget)
    references = solving.reference_documents(conn, artifact_id, reference_budget)
    messages = solving.build_prompt(
        SolveInput(
            statement=statement,
            label=str(problem["label"] or "Problem"),
            sub_parts=sub_parts,
            correction=correction,
        ),
        retrieval,
        references,
    )

    # The worker is a plain thread with no event loop, and `complete` is async. Owning a
    # loop for the length of the call keeps this function synchronous.
    content = asyncio.run(
        client.complete(config.endpoint_url, config.api_key, config.model, messages)
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
                        label=chunk.section_title,
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
    part_id: int,
    label: str,
    solved: SolvedProblem,
) -> None:
    """Check a finished solution, re-deriving once if a check disagrees.

    Exactly once. A problem re-run until it passes is a problem nobody checked, so a
    second refutation is recorded as the verdict rather than triggering a third attempt.
    """
    if not _tool_support(conn, config):
        artifacts.record_checks(conn, part_id, [])
        artifacts.set_part_verdict(
            conn, part_id, artifacts.UNCHECKED, verification.NO_TOOL_SUPPORT_DETAIL
        )
        return

    problem = artifacts.get_part(conn, part_id)
    statement = str(problem["content"])
    artifacts.set_part_status(conn, part_id, artifacts.PART_VERIFYING)
    outcome = verification.verify(config, statement, label, solved.as_markdown())

    if outcome.refuted:
        logger.info("Check disagreed with part %s, re-deriving once", part_id)
        try:
            redone, provenance = _generate(
                conn, artifact_id, class_id, config, problem, outcome.detail
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
        blocked = document_text_allowed(conn)
        if blocked is not None:
            raise LyraError(BLOCKED_MESSAGES.get(blocked, BLOCKED_MESSAGES[NO_ENDPOINT]))
        config = resolve_tutor_config(conn)
        _solve_one(
            conn, artifact_id, int(artifact["class_id"]), config, part_id, correction=correction
        )
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
    """Fail every artifact left mid-flight by a shutdown. Returns the row count.

    The queue lives in memory, so an artifact caught in `segmenting` or `solving` when the
    process stopped would otherwise sit there forever claiming to be working. Parts left
    mid-flight are reconciled too, so a problem does not keep a spinner across a restart.

    An artifact in `awaiting_review` is deliberately left alone. It was not working, it
    was waiting, and a restart does not change what it is waiting for.
    """
    placeholders = ", ".join("?" for _ in artifacts.RUNNING_STATES)
    cursor = conn.execute(
        # The placeholders are generated from a module constant and every value is bound.
        # `stage_detail` reads the pre-update row, so it keeps the lost stage.
        f"update artifacts set stage_detail = state, state = '{artifacts.FAILED}', "  # noqa: S608
        f"error_message = ?, updated_at = datetime('now') where state in ({placeholders})",
        (INTERRUPTED_MESSAGE, *artifacts.RUNNING_STATES),
    )
    conn.execute(
        # A part that was solving when the process died is `pending` again rather than
        # `failed`: nothing is wrong with it, it simply never ran, and the retry that
        # follows a failed artifact should pick it up as unsolved work.
        f"update artifact_parts set status = '{artifacts.PART_PENDING}', "  # noqa: S608
        f"updated_at = datetime('now') where status in "
        f"('{artifacts.PART_SOLVING}', '{artifacts.PART_VERIFYING}')"
    )
    conn.commit()
    return cursor.rowcount


def _top_level_problems(conn: sqlite3.Connection, artifact_id: int) -> list[dict[str, object]]:
    """The problems a solve run walks, in document order."""
    return [
        part
        for part in artifacts.list_parts(conn, artifact_id)
        if part["parent_part_id"] is None and part["kind"] == artifacts.PROBLEM
    ]


def _locate(
    conn: sqlite3.Connection, document_id: int, page_number: int | None, label: str | None
) -> tuple[float, ...]:
    """Where a problem's marker sits on its page, empty when it could not be found.

    Empty rather than None on a miss, so the backfill knows this has already been looked
    for and does not reopen the same PDF on every start.
    """
    if not document_id or page_number is None or not label:
        return ()
    row = conn.execute(
        "select stored_path, mime from documents where id = ?", (document_id,)
    ).fetchone()
    if row is None or str(row["mime"]) != PDF_MIME:
        return ()
    return locate.find_label(Path(str(row["stored_path"])), page_number, label) or ()


def backfill_problem_locations(conn: sqlite3.Connection) -> int:
    """Find the page position of problems written before positions were recorded.

    Solution sets are worth keeping across a version of the app that learns something new
    about them, and re-segmenting them to pick this up would throw away every correction
    the student made at the review gate. Runs at startup, does nothing on the second run,
    and never raises: this drives a click target on a page image.
    """
    rows = conn.execute(
        "select v.id, v.document_id, v.page_number, p.label from artifact_provenance v "
        "join artifact_parts p on p.id = v.part_id "
        "where v.bbox is null and p.kind = ? and p.parent_part_id is null "
        "and v.document_id is not null and v.page_number is not null",
        (artifacts.PROBLEM,),
    ).fetchall()

    located = 0
    for row in rows:
        found = _locate(
            conn,
            int(row["document_id"]),
            int(row["page_number"]),
            str(row["label"] or ""),
        )
        conn.execute(
            "update artifact_provenance set bbox = ? where id = ?",
            (json.dumps(list(found)), int(row["id"])),
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
