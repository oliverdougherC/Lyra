"""Draft suggestion runs: whole-document AI revisions, proposed for review.

The same background shape as ingestion, solving, and study: an in-memory queue, one
worker thread, state on the artifact row. A suggestion run reads the draft body, grounds
the instruction in the class's material, asks for the complete revised document, and
lands it as a pending edit (`core/suggestions.py`) - never directly into the document.
The student reviews the diff hunk by hunk.

A failed or interrupted run costs the suggestion, not the draft: the artifact goes back
to `ready` either way, because the document the student wrote was never touched.
"""

import asyncio
import logging
import queue
import sqlite3
import threading
from dataclasses import dataclass

from backend.core import artifacts, suggestions
from backend.core.app_settings import (
    NO_ENDPOINT,
    REMOTE_UNACKNOWLEDGED,
    document_text_allowed,
    resolve_tutor_config,
)
from backend.core.errors import LyraError, NotFoundError
from backend.core.profiles import select_active_facts
from backend.llm import client, prompts
from backend.rag.retrieve import retrieve
from backend.storage.database import connect

logger = logging.getLogger(__name__)

# Retrieval budget for grounding a revision, in estimated tokens. A revision instruction
# is a question about the draft, and the course material answers it.
SUGGEST_RETRIEVAL_BUDGET = 2_500

BLOCKED_MESSAGES = {
    NO_ENDPOINT: "No tutor endpoint is configured. Add one in Settings, then suggest.",
    REMOTE_UNACKNOWLEDGED: (
        "Your tutor endpoint is not on this machine, and a suggestion has to send it "
        "your draft. Allow that in Settings, then suggest."
    ),
}

NO_CHANGES_DETAIL = "no changes suggested"
INTERRUPTED_DETAIL = "The suggestion was interrupted by a restart. The draft is unchanged."


@dataclass(frozen=True)
class _Job:
    """What a queued suggestion needs. Ids and the instruction only."""

    artifact_id: int
    instruction: str


_queue: queue.Queue[_Job] = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False


def enqueue(job: _Job) -> None:
    """Queue a suggestion run for a draft."""
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
            run_suggestion(job)
        except Exception:
            logger.exception("Suggestion run failed for draft %s", job.artifact_id)
        finally:
            _queue.task_done()


def reconcile_interrupted(conn: sqlite3.Connection) -> int:
    """Return drafts caught mid-suggestion to `ready`. Returns how many.

    The draft itself is intact - a suggestion run never writes the document - so this is
    not a failure: only the run died, and `stage_detail` says so.
    """
    cursor = conn.execute(
        "update artifacts set state = ?, stage_detail = ?, updated_at = datetime('now') "
        "where state = ? and kind = ?",
        (artifacts.READY, INTERRUPTED_DETAIL, artifacts.GENERATING, artifacts.KIND_DRAFT),
    )
    conn.commit()
    return cursor.rowcount


def run_suggestion(job: _Job) -> None:
    """Run one suggestion. The worker calls this; tests call it directly."""
    conn = connect()
    try:
        _suggest(conn, job)
    except NotFoundError:
        # Deleted between enqueue and run: the de-facto cancel, as in ingestion.
        logger.info("Draft %s vanished before its suggestion ran", job.artifact_id)
    except Exception as exc:
        conn.rollback()
        _settle_failed(conn, job.artifact_id, exc)
    finally:
        conn.close()


def _settle_failed(conn: sqlite3.Connection, artifact_id: int, exc: Exception) -> None:
    """Back to ready with the reason: the run failed, the draft is intact."""
    row = conn.execute("select id from artifacts where id = ?", (artifact_id,)).fetchone()
    if row is None:
        return
    message = exc.message if isinstance(exc, LyraError) else str(exc)
    artifacts.set_artifact_state(conn, artifact_id, artifacts.READY)
    conn.execute("update artifacts set error_message = ? where id = ?", (message, artifact_id))
    conn.commit()


def _suggest(conn: sqlite3.Connection, job: _Job) -> None:
    """Read the draft, ground the instruction, and propose the revised document."""
    artifact = artifacts.get_artifact(conn, job.artifact_id)
    class_id = int(artifact["class_id"])
    part = _body_part(conn, job.artifact_id)
    blocked = document_text_allowed(conn)
    if blocked is not None:
        raise LyraError(BLOCKED_MESSAGES.get(blocked, BLOCKED_MESSAGES[NO_ENDPOINT]))
    config = resolve_tutor_config(conn)

    artifacts.set_artifact_state(conn, job.artifact_id, artifacts.GENERATING, "Revising the draft")
    result = retrieve(conn, class_id, job.instruction, SUGGEST_RETRIEVAL_BUDGET)
    context_block = prompts.format_context_block([vars(chunk) for chunk in result.chunks])
    facts_block = prompts.format_facts_block(select_active_facts(conn, class_id))
    reply = asyncio.run(
        client.complete(
            config.endpoint_url,
            config.api_key,
            config.model,
            prompts.build_suggest_prompt(
                str(part["content"]), job.instruction, context_block, facts_block
            ),
            request_timeout=client.BACKGROUND_TIMEOUT,
        )
    )

    proposed = reply.strip()
    # Both sides are stripped for the comparison: a reply that differs only in edge
    # whitespace is "no changes", not a suggestion to review.
    if not proposed or proposed == str(part["content"]).strip():
        artifacts.set_artifact_state(conn, job.artifact_id, artifacts.READY, NO_CHANGES_DETAIL)
        return
    suggestions.propose(conn, int(part["id"]), proposed, job.instruction)
    artifacts.set_artifact_state(conn, job.artifact_id, artifacts.READY)


def _body_part(conn: sqlite3.Connection, artifact_id: int) -> dict[str, object]:
    """The draft's one body part."""
    for part in artifacts.list_parts(conn, artifact_id):
        if part["kind"] == artifacts.DRAFT_BODY:
            return part
    raise NotFoundError("That draft has no body.")
