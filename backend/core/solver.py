"""The background solve job: segment, wait for review, then solve.

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
"""

import logging
import queue
import sqlite3
import threading
from pathlib import Path

from backend.config import settings
from backend.core import artifacts
from backend.core.artifacts import ProvenanceEntry
from backend.core.errors import LyraError
from backend.core.segmentation import SegmentedProblem, propose_problems
from backend.storage.database import connect

logger = logging.getLogger("lyra.solver")

NO_SOURCES = "The documents this solution set was built from are no longer there."
INTERRUPTED_MESSAGE = "Interrupted, please retry"
_DEFAULT_FAILURE = "Something went wrong while reading this problem set."

_queue: queue.Queue[int] = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False


def enqueue(artifact_id: int) -> None:
    """Hand an artifact to the solve worker. Returns immediately."""
    _queue.put(artifact_id)


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
    """Run queued segmentations forever, surviving anything one artifact does."""
    while True:
        artifact_id = _queue.get()
        try:
            run_segmentation(artifact_id)
        except Exception:
            # The thread is the whole solving capability of the process. One bad problem
            # set must never be able to take it down.
            logger.exception("Segmentation crashed for artifact %s", artifact_id)
        finally:
            _queue.task_done()


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
            artifacts.set_provenance(
                conn,
                part_id,
                [
                    ProvenanceEntry(
                        chunk_id=chunk_id,
                        document_id=problem.document_id or None,
                        page_number=problem.page_number,
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


def reconcile_interrupted(conn: sqlite3.Connection) -> int:
    """Fail every artifact left mid-flight by a shutdown. Returns the row count.

    The queue lives in memory, so an artifact caught in `segmenting` when the process
    stopped would otherwise sit there forever claiming to be working.

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
    conn.commit()
    return cursor.rowcount


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


def _failure_message(exc: Exception) -> str:
    """A user-facing reason, never carrying a path or a traceback."""
    if isinstance(exc, LyraError):
        return exc.message
    return _DEFAULT_FAILURE
