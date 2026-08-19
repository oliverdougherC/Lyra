"""In-process coordination of document lifecycle mutations and derived-state publication.

The durable half of storage consistency lives in `storage_intents`: intents recorded with
their database mutation, and startup reconciliation that converges whatever a crash left.
This module is the live half - the coordination that keeps two *concurrent requests in the
same process* from interleaving their filesystem work, which no intent can repair because
both operations are legitimate and both settle their intents believing they finished.

Two primitives, composed (docs/storage-consistency.md):

1. **The lifecycle mutex.** Every operation that mutates a document's row *and* performs
   destructive or cross-domain filesystem work - move, delete, class delete, re-ingest,
   recognize - runs its whole read-decide-commit-act sequence under one process-wide lock.
   Lyra is a single-process application (the in-memory ingestion queue and the launcher's
   ownership model already assume it), so this lock is the complete story for live
   requests. It deliberately does not replace the durable state machine: a crash releases
   it, and reconciliation - which runs single-threaded at startup before any request or
   worker exists - never needs it. The one long-lived thread that does not take it is the
   ingestion worker, whose writes are guarded instead by the publication check below and by
   `recognition.document_replaced`.
2. **Guarded publication.** Derived state - extracted text, rendered pages, figure crops -
   is produced by workers and requests that read the document long before they publish.
   `publish_current_document` re-verifies, under the same mutex, that the document row
   still exists with the identity (`created_at`) the writer started from, immediately
   before the atomic rename that would make the file visible. A delete holds the mutex
   across its commit *and* its cleanup, so a late writer either publishes entirely before
   the delete (and the delete's cleanup removes the file) or checks after it (and refuses).
   There is no window in which "the row was there when I checked" can be stale by
   publication time.

The mutex is intentionally one lock rather than one per document. Lifecycle operations are
rare, human-initiated, and cheap (SQLite statements plus a rename or a handful of
unlinks); a class delete spans many documents and would otherwise need ordered
multi-lock acquisition to stay deadlock-free. Publication holds it only for an existence
check and one rename, never for rasterization or parsing.
"""

import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

from backend.storage import private
from backend.storage.database import connect

# Read at call time in the helpers below, so tests can substitute a reentrant lock to
# interleave two lifecycle operations deterministically on one thread.
_lifecycle_mutex = threading.Lock()


@contextlib.contextmanager
def lifecycle_mutation() -> Iterator[None]:
    """Serialize one document lifecycle operation against every other and against
    guarded publication.

    Hold this for the operation's entire read-decide-commit-act sequence: the decision
    must be made from state no concurrent operation can change before the action lands.
    The database transitions inside remain conditional (compare-and-swap `where` clauses,
    re-reads under SQLite's write lock) so that even a code path that fails to take this
    lock cannot commit a transition from state it does not own - the lock makes races
    impossible, the conditional writes make them harmless.
    """
    with _lifecycle_mutex:
        yield


def document_is_current(conn: sqlite3.Connection, document_id: int, created_at: str) -> bool:
    """Whether the document row still exists with the identity the caller started from.

    `created_at` is the row's value captured when the work began. A row that is gone was
    deleted; a row whose `created_at` differs was deleted and re-created under the same id
    by a newer upload. Either way, work derived from the old file must not land.
    """
    row = conn.execute("select created_at from documents where id = ?", (document_id,)).fetchone()
    return row is not None and str(row["created_at"]) == str(created_at)


def publish_current_document(document_id: int, created_at: str, path: Path, data: bytes) -> bool:
    """Atomically publish derived state, only if its document still exists unchanged.

    The late-writer barrier: a worker or request that parsed or rasterized the document
    seconds (or hours) ago must not make a file appear for a document whose deletion has
    already committed and cleaned up. The check runs on a fresh connection - the caller's
    connection may hold a read snapshot older than the delete's commit - and shares the
    lifecycle mutex with the delete itself, so the check and the rename are one atomic
    step relative to any lifecycle operation.

    Returns:
        True when the file was published; False when the document is gone or replaced and
        nothing was written. The staged bytes never appear under the final name on refusal.

    Lock discipline: callers must not hold an open SQLite write transaction here. A
    lifecycle operation inside the mutex may be waiting on the database's write lock, so
    a caller holding that lock while waiting on the mutex would stall both until SQLite's
    busy timeout breaks the cycle. Every current caller publishes between commits, which
    is also what the ingestion state machine already requires of its stages.
    """
    with _lifecycle_mutex:
        conn = connect()
        try:
            if not document_is_current(conn, document_id, created_at):
                return False
        finally:
            conn.close()
        private.publish_private_bytes(path, data)
    return True
