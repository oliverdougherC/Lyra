"""The draft workspace's background worker: one queue, one thread, jobs by type.

The same background shape as ingestion, solving, and study: an in-memory queue, one
worker thread, state on the artifact row. What runs here is registered by type - the
draft pass in `core/writer_pipeline.py` is the resident - and everything registered
shares the one thread on purpose: the tutor endpoint serves one request at a time, and
two workers would be two callers.

The original resident was the one-shot whole-document suggestion, replaced by the
pipeline's section-scoped passes once documents outgrew one prompt. What survives it
here is the shape it proved: a failed or interrupted run costs the run, never the
draft, because background work lands either as revisions or as a pending edit and the
document the student wrote is not touched by anything else.
"""

import logging
import queue
import sqlite3
import threading
from collections.abc import Callable

from backend.core import artifacts, writer_runs

logger = logging.getLogger(__name__)

# "Steps" rather than a job-specific noun, because the artifact row is all this module
# sees and two residents count differently: a draft pass counts sections, a review
# counts lenses. What both promise is the same - the work that finished is kept.
INTERRUPTED_DETAIL = "The pass was interrupted by a restart. The draft is unchanged."
_INTERRUPTED_PARTIAL_DETAIL = (
    "The pass was interrupted by a restart; {done} of {total} steps finished."
)

_queue: queue.Queue[object] = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False

# Job type -> runner. Populated at import time by the modules whose jobs run here;
# dispatch is by exact type, and an unregistered job is a bug worth a loud log, not a
# dead worker.
_RUNNERS: dict[type, Callable[[object], None]] = {}


def register_runner(job_type: type, runner: Callable[[object], None]) -> None:
    """Declare who runs jobs of one type. Called at import time by the job's module."""
    _RUNNERS[job_type] = runner


def enqueue(job: object) -> None:
    """Queue one background job for the draft workspace's worker."""
    if type(job) not in _RUNNERS:
        raise ValueError(f"No runner is registered for {type(job).__name__}.")
    _queue.put(job)


def start_worker() -> None:
    """Start the single drafting worker, once per process."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_drain_queue, name="lyra-drafting", daemon=True).start()


def _drain_queue() -> None:
    """Run jobs until the process exits. The worker must never die."""
    while True:
        job = _queue.get()
        try:
            runner = _RUNNERS.get(type(job))
            if runner is None:
                # enqueue() refuses these, so reaching here means a registration was
                # torn down mid-flight. The job is dropped; the worker survives.
                logger.error("No runner for queued job %r", job)
            else:
                runner(job)
        except Exception:
            logger.exception("Draft worker job failed: %r", job)
        finally:
            _queue.task_done()


class UpstreamTolerance:
    """One failure is weather; two in a row is the endpoint being down.

    The student's llama-server flakes under long runs - the first live review died to a
    one-off 500 nine minutes in, taking two whole lenses with it. A run that aborts on
    the first failure loses everything after it for no reason; a run that never aborts
    burns every remaining call to settle a partial result as if it were whole. Both
    background residents make many calls in a row, so both need this same middle.

    Not thread-safe, and does not need to be: one worker thread runs everything here.
    """

    def __init__(self, limit: int = 2) -> None:
        self._limit = limit
        self._consecutive = 0

    def failed(self) -> bool:
        """Record a failure. True when the run should give up."""
        self._consecutive += 1
        return self._consecutive >= self._limit

    def succeeded(self) -> None:
        """Record anything that was not a failure, which resets the streak."""
        self._consecutive = 0


def reconcile_interrupted(conn: sqlite3.Connection) -> tuple[int, int]:
    """Requeue durable runs and return legacy drafts caught mid-run to `ready`.

    Persisted runs survive a restart as queued jobs rebuilt from `writer_runs`. Drafts
    without a durable run row are legacy interrupted work and fall back to the older
    honest contract: the draft is intact, only the run died, and `stage_detail` says so.

    `pending` counts as caught: the queue is in memory, and jobs are marked pending in
    the request that queues them, so a restart between the queueing and the worker taking
    the job leaves an artifact waiting on a job that no longer exists. Development reloads
    this file often enough that the difference is not theoretical.
    """
    requeued = 0
    active_run_artifacts: set[int] = set()
    for run in writer_runs.recoverable_runs(conn):
        active_run_artifacts.add(int(run["artifact_id"]))
        message = (
            "This run resumed after a restart from the last completed boundary."
            if run["status"] != writer_runs.QUEUED
            else "This queued run resumed after a restart."
        )
        requeued_run = writer_runs.queue_for_restart(conn, int(run["id"]), message)
        enqueue(writer_runs.build_job(requeued_run))
        requeued += 1

    rows = conn.execute(
        "select id, state, problems_total, problems_done from artifacts "
        "where state in (?, ?) and kind = ?",
        (artifacts.PENDING, artifacts.GENERATING, artifacts.KIND_DRAFT),
    ).fetchall()
    recovered = 0
    for row in rows:
        if int(row["id"]) in active_run_artifacts:
            continue
        # Only a run that actually started can report progress. A pending one never
        # cleared its counters, so reading them would report the *previous* pass's
        # sections as this one's - "5 of 5 steps finished" for a job that never began.
        total = row["problems_total"] if row["state"] == artifacts.GENERATING else None
        detail = (
            _INTERRUPTED_PARTIAL_DETAIL.format(done=int(row["problems_done"]), total=int(total))
            if total
            else INTERRUPTED_DETAIL
        )
        conn.execute(
            "update artifacts set state = ?, stage_detail = ?, updated_at = datetime('now') "
            "where id = ?",
            (artifacts.READY, detail, int(row["id"])),
        )
        recovered += 1
    conn.commit()
    return requeued, recovered
