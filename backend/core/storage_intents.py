"""Crash-consistent document storage: durable intents and startup reconciliation.

Document lifecycle operations mutate two transactional domains - SQLite and the
filesystem - and no transaction spans both. The contract that keeps them consistent
(docs/storage-consistency.md) has three parts, and this module is where all of it lives:

1. **Publication.** A stored file appears under its final name only through
   `private.publish_private_bytes`: staged bytes, then one atomic rename. A file that
   exists under a final name is therefore whole, and a crash mid-write leaves only a
   `*.partial` staging name that nothing ever reads.
2. **Intents.** An operation that owes filesystem work after a database commit - a move's
   rename, a delete's unlinks - records a `storage_intents` row in the *same transaction*
   as the database mutation, performs the filesystem work, and only then deletes the
   intent. After any interruption, the surviving intents name exactly the work still owed;
   nothing about the cleanup lives only in process memory.
3. **Reconciliation.** `reconcile_storage` runs at startup, before the ingestion queue is
   rebuilt: it settles every surviving intent idempotently (rolling a move forward,
   re-running a delete's cleanup), then sweeps crash orphans - staged `*.partial` files
   and stored files whose document row never committed. An intent that cannot be settled
   is always kept: retried next startup when the failure is transient, or durably marked
   blocked (`blocked_reason`) when its recorded work is unsafe to perform at all.

Everything here is idempotent by construction: settling an intent twice, or sweeping
twice, converges on the same state. Recovery never follows a symlink and never acts on a
path outside the current data tree - such an intent is blocked, not obeyed and not
silently settled - so the crash-consistency machinery cannot be turned against the
private-storage contract it exists to protect.

The live-request half of the story - the process-wide lifecycle mutex and the
identity-checked publication barrier that keep two concurrent requests, or a request and
the ingestion worker, from interleaving destructively - lives in `backend.core.ownership`.
"""

import contextlib
import json
import logging
import sqlite3
import stat
from pathlib import Path

from backend.config import settings
from backend.rag import render
from backend.storage import private

logger = logging.getLogger(__name__)

MOVE_DOCUMENT = "move_document"
DELETE_DOCUMENT = "delete_document"
DELETE_CLASS = "delete_class"

# Recovery found a move intent whose file exists at neither end. The bytes are gone in a
# way Lyra cannot repair, and the honest outcome is a failed row that says so, not a
# `pending` row that would fail cryptically in the parser or a "moved" row pointing at
# nothing.
FILE_LOST_MESSAGE = "The stored file went missing while it was being moved. Upload it again."


class CleanupIncompleteError(Exception):
    """Durable cleanup ran but could not reach its goal state.

    Raised when a delete's filesystem cleanup left something behind: an entry that could
    not be removed, an unexpected subdirectory that is never recursed into, a directory
    that would not go away. Retryable by design, exactly like a transient `OSError`: the
    caller keeps the storage intent so the work is retried at the next startup, instead
    of settling an intent whose recorded cleanup was silently skipped - which would
    discard the only durable record that files are still owed removal.
    """


class IntentBlockedError(Exception):
    """An intent whose recorded work cannot be performed safely, now or by retrying.

    Raised when settling would require acting on a path outside the current data tree,
    or when the payload cannot be read at all. Distinct from a transient filesystem
    failure (an `OSError` that a retry can fix): a blocked intent is kept with a durable
    classification in `blocked_reason` so its evidence - the payload - survives for
    manual handling, it is re-validated (cheaply, destructively never) at each startup in
    case the environment changed back, and it is never silently settled with its cleanup
    skipped.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def text_path(document_id: int) -> Path:
    """Where ingestion stores this document's extracted text."""
    return settings.text_dir / f"{document_id}.txt"


def record_intent(
    conn: sqlite3.Connection,
    kind: str,
    *,
    document_id: int | None = None,
    class_id: int | None = None,
    payload: dict[str, object] | None = None,
) -> int:
    """Insert one intent row inside the caller's open transaction.

    Deliberately does not commit: the intent must become durable in the same commit as
    the database mutation it belongs to, or a crash between the two would leave either a
    mutation with no recorded cleanup or a cleanup order for a mutation that never
    happened.
    """
    cursor = conn.execute(
        "insert into storage_intents (kind, document_id, class_id, payload) values (?, ?, ?, ?)",
        (kind, document_id, class_id, json.dumps(payload or {})),
    )
    return int(cursor.lastrowid or 0)


def settle_intent(conn: sqlite3.Connection, intent_id: int) -> None:
    """Delete a completed intent and commit: the owed filesystem work is done."""
    conn.execute("delete from storage_intents where id = ?", (intent_id,))
    conn.commit()


def source_file_present(path: Path) -> bool:
    """Whether a real regular file sits at `path`, without following any symlink.

    The question a move asks before promising a destination. Anything that is not a plain
    regular file - absent, a symlink, a directory - counts as not present: a move must
    never relocate a link (its target could be anywhere), and converting "the source is a
    symlink" into a successful move would be the same lie as inventing a destination for a
    missing file. The file is reached by no-follow descent from the data root, so an
    intermediate component turned symlink cannot make an outside file answer for the
    recorded path; callers validate `is_within` before asking.

    Raises:
        private.PrivacyContractError: a symlink blocks an owned component on the way.
    """
    try:
        info = private.stat_in_tree(path, root=settings.data_dir)
    except OSError:
        return False
    return info is not None and stat.S_ISREG(info.st_mode)


def perform_move(source: Path, destination: Path) -> None:
    """Rename a stored upload into its destination class directory.

    The one filesystem step of a move, shared by the request path and recovery so both
    create the destination directory under the same owned-tree, no-symlink contract. The
    rename is atomic within the uploads tree; it either happens or it does not, which is
    what lets the intent protocol treat "did the rename land?" as a question with a
    trustworthy answer. Both ends are reached by no-follow descent from the data root,
    so a symlinked intermediate directory can neither supply the file being renamed nor
    receive it outside the tree.
    """
    private.secure_mkdir(destination.parent, root=settings.data_dir)
    private.replace_in_tree(source, destination, root=settings.data_dir)


def run_document_cleanup(document_id: int, stored_path: object) -> None:
    """Remove one deleted document's files: upload, extracted text, rendered pages.

    Idempotent - a missing file is the goal state, not an error - so the request path and
    startup recovery share it and either may run after the other. A real failure
    propagates so the caller keeps the intent and the work is retried at the next
    startup instead of being forgotten: a permission error as `OSError`, a page cache
    that could not be fully removed as `CleanupIncompleteError` - completion is only
    ever declared when every file this delete owes removal is provably gone. The derived
    files go first and the stored upload last: its recorded path is the one part of the
    payload that could be stale or out-of-root (`IntentBlockedError`), and refusing it
    must not stop the parts whose locations are derived from settings and the id.
    """
    private.unlink_in_tree(text_path(document_id), root=settings.data_dir)
    pages_clean = render.discard_pages(document_id)
    if stored_path:
        _unlink_owned(Path(str(stored_path)))
    if not pages_clean:
        raise CleanupIncompleteError(
            f"the page cache for document {document_id} could not be fully removed"
        )


def run_class_cleanup(class_id: int, document_ids: list[int]) -> None:
    """Remove a deleted class's upload directory and every document's derived files.

    Idempotent for the same reason as `run_document_cleanup`, and completion-honest in
    the same way: the class upload directory and every document's page cache must be
    provably gone, or `CleanupIncompleteError` keeps the intent for retry. The upload
    directory is emptied entry by entry through the no-follow tree primitive rather
    than recursively: Lyra stores uploads flat, so anything nested in there was not
    Lyra's doing, is never entered, and keeps the cleanup visibly incomplete instead of
    being force-removed or silently abandoned. A symlink planted where the directory
    belongs is removed as a link - never its target - which is all of Lyra's state that
    exists here. Every document's cleanup is attempted before the failure is raised, so
    one obstruction does not shelter the rest of the class's files.
    """
    directory = settings.uploads_dir / str(class_id)
    complete = private.clear_owned_dir(directory, root=settings.data_dir, patterns=("*",))
    for document_id in document_ids:
        private.unlink_in_tree(text_path(int(document_id)), root=settings.data_dir)
        if not render.discard_pages(int(document_id)):
            complete = False
    if not complete:
        raise CleanupIncompleteError(
            f"the stored files for class {class_id} could not be fully removed"
        )


def reconcile_storage(conn: sqlite3.Connection) -> tuple[int, int]:
    """Settle every intent the last shutdown left, then sweep crash orphans.

    Runs at startup before the ingestion queue is rebuilt, so a document a recovered move
    rolls forward is re-indexed from the path the recovery settled on. Each intent is
    settled and deleted in its own transaction, and every failure to settle keeps the
    intent - never the reverse - in one of two durable classifications:

    - A transient filesystem failure (the unlink or rename still fails) is logged and
      retried at the next startup.
    - Work that cannot be performed safely at all - an unreadable payload, a recorded
      path outside the current data tree, an unknown kind - marks the intent blocked
      (`blocked_reason`), preserving the payload as evidence for manual handling. Blocked
      intents are re-validated at each startup, which is cheap and never destructive, so
      an intent blocked by a temporarily relocated data directory settles by itself once
      the environment is back; a genuinely malformed one stays visibly blocked instead of
      being retried as if it might start working.

    No single intent, however malformed, can abort reconciliation or startup: each is
    isolated, and later intents settle regardless of what earlier ones did.

    Returns:
        How many intents were settled, and how many orphaned files were swept.
    """
    settled = 0
    rows = conn.execute(
        "select id, kind, document_id, class_id, payload from storage_intents order by id"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except ValueError:
            _block_intent(conn, row, "unreadable payload")
            continue
        if not isinstance(payload, dict):
            _block_intent(conn, row, "unreadable payload")
            continue
        try:
            _settle_one(conn, row, payload)
        except IntentBlockedError as exc:
            _block_intent(conn, row, exc.reason)
            continue
        except (OSError, private.PrivacyContractError, CleanupIncompleteError):
            logger.exception(
                "Storage intent %s (%s) could not be settled; it will be retried next startup",
                row["id"],
                row["kind"],
            )
            continue
        except Exception:
            # A defect this loop did not anticipate must cost this one intent a retry,
            # never the startup: everything after reconciliation - the ingestion queue,
            # the API - still has to come up.
            logger.exception(
                "Storage intent %s (%s) failed unexpectedly; it is kept and will be "
                "retried next startup",
                row["id"],
                row["kind"],
            )
            continue
        conn.execute("delete from storage_intents where id = ?", (row["id"],))
        conn.commit()
        settled += 1
    swept = _sweep_orphans(conn)
    return settled, swept


def _block_intent(conn: sqlite3.Connection, row: sqlite3.Row, reason: str) -> None:
    """Durably classify an intent as unsafe to settle, keeping it and its payload.

    The log names the intent and the classification but not the recorded path: an
    out-of-root path is by definition outside Lyra's tree, and the log should not repeat
    where it points. The payload row itself remains the durable evidence.
    """
    conn.execute("update storage_intents set blocked_reason = ? where id = ?", (reason, row["id"]))
    conn.commit()
    logger.error(
        "Storage intent %s (%s) cannot be settled safely (%s); it is kept for manual "
        "review and will be re-validated at the next startup",
        row["id"],
        row["kind"],
        reason,
    )


def _settle_one(conn: sqlite3.Connection, row: sqlite3.Row, payload: dict[str, object]) -> None:
    """Finish one intent's owed filesystem work.

    Raises:
        OSError: the work still cannot be done; the caller keeps the intent for retry.
        IntentBlockedError: the recorded work is unsafe to perform; the caller keeps the
            intent durably classified rather than retrying or - worse - settling it.
    """
    kind = str(row["kind"])
    if kind == MOVE_DOCUMENT:
        if row["document_id"] is None:
            raise IntentBlockedError("missing document id")
        _recover_move(conn, int(row["document_id"]), payload)
    elif kind == DELETE_DOCUMENT:
        if row["document_id"] is None:
            raise IntentBlockedError("missing document id")
        # The stored path is the delete's critical durable pointer, so its shape is
        # validated, not defaulted: a payload that lost the key - or holds something
        # other than the route's two legitimate shapes, a non-empty path string or an
        # explicit null for a document that never had a stored upload - must keep its
        # evidence as blocked rather than settle as if there were nothing to remove.
        if "stored_path" not in payload:
            raise IntentBlockedError("delete payload is missing its stored path")
        stored_path = payload["stored_path"]
        if stored_path is not None and (not isinstance(stored_path, str) or not stored_path):
            raise IntentBlockedError("delete payload records an unusable stored path")
        run_document_cleanup(int(row["document_id"]), stored_path)
    elif kind == DELETE_CLASS:
        if row["class_id"] is None:
            raise IntentBlockedError("missing class id")
        ids = payload.get("document_ids")
        if not isinstance(ids, list) or not all(
            isinstance(entry, int) and not isinstance(entry, bool) for entry in ids
        ):
            raise IntentBlockedError("unreadable payload")
        run_class_cleanup(int(row["class_id"]), ids)
    else:
        # An unknown kind is owed work this build does not know how to perform - a
        # downgraded install, or corruption. Settling it would discard the work; blocking
        # keeps it visible for the build that understands it.
        raise IntentBlockedError(f"unknown intent kind {kind!r}")


def _recover_move(conn: sqlite3.Connection, document_id: int, payload: dict[str, object]) -> None:
    """Converge an interrupted move on one valid state.

    The intent was committed with the row already pointing at the destination, so the only
    open question is whether the rename landed. Roll forward when the source still holds
    the file; recognize completion when the destination does; and when neither does, fail
    the document honestly - the row must not go on claiming an upload that no longer
    exists anywhere.
    """
    # The payload is validated before any "nothing to do" branch can return: a malformed
    # intent must surface as blocked evidence even when the work it described is moot,
    # never be silently dropped by an early exit that happened to come first.
    source_recorded = payload.get("source")
    destination_recorded = payload.get("destination")
    if not isinstance(source_recorded, str) or not source_recorded:
        raise IntentBlockedError("unreadable move payload")
    if not isinstance(destination_recorded, str) or not destination_recorded:
        raise IntentBlockedError("unreadable move payload")
    source = Path(source_recorded)
    destination = Path(destination_recorded)
    # Validated before anything acts on them: a stale or corrupted payload must not aim a
    # rename - or the directory creation it implies - outside the uploads tree, and the
    # `ValueError` the owned-root helper would raise for it must not reach startup.
    if not private.is_within(source, settings.uploads_dir):
        raise IntentBlockedError("recorded move source is outside the uploads directory")
    if not private.is_within(destination, settings.uploads_dir):
        raise IntentBlockedError("recorded move destination is outside the uploads directory")
    document = conn.execute(
        "select stored_path from documents where id = ?", (document_id,)
    ).fetchone()
    if document is None:
        # The document was deleted after the move wedged: its own delete intent and the
        # orphan sweep own the files now. Nothing to move.
        return
    if str(document["stored_path"]) != str(destination):
        # A later committed operation rewrote the row (the compensation path restores the
        # source location and deletes the intent atomically, so this is belt and braces).
        # The rename this intent describes is no longer owed.
        return
    if source_file_present(destination):
        return
    if source_file_present(source):
        perform_move(source, destination)
        return
    conn.execute(
        "update documents set state = 'failed', stage_detail = null, error_message = ? "
        "where id = ?",
        (FILE_LOST_MESSAGE, document_id),
    )
    # Committed by the caller together with the intent delete, so the failure mark and the
    # intent's settlement are one atomic step.


def _unlink_owned(path: Path) -> None:
    """Remove one file recorded by an intent, only ever inside the current data tree.

    A payload path outside the tree - a data directory that moved between runs, or a
    corrupted row - is refused, and the refusal is `IntentBlockedError` rather than a
    logged skip: recovery must never be the thing that deletes a file outside what Lyra
    owns, but it also must never settle an intent whose recorded cleanup it skipped -
    that would silently discard the only durable pointer to a file the delete still owes.
    The entry is reached by no-follow descent from the data root and unlinked against
    the validated directory descriptor, so neither a symlink at the final component (a
    link is removed as a link) nor one planted in an intermediate directory can steer
    the unlink at a file outside the tree.

    Raises:
        IntentBlockedError: the path is outside the current data directory.
        private.PrivacyContractError: a symlink blocks an owned component on the way;
            the intent is kept and retried, and nothing was touched.
    """
    if not private.is_within(path, settings.data_dir):
        raise IntentBlockedError("recorded path is outside the current data directory")
    private.unlink_in_tree(path, root=settings.data_dir)


def _sweep_orphans(conn: sqlite3.Connection) -> int:
    """Remove files a crash left behind that no committed row points at.

    Two kinds of garbage, both private coursework that must not linger invisibly:
    `*.partial` staging files whose writer is gone (at startup, every writer is), and
    stored uploads / extracted text / page caches whose document id has no row - an upload
    whose insert rolled back after its file was published, or a pre-intent delete that
    crashed between its commit and its unlinks. Only entries whose id provably has no row
    are removed; a file whose row exists is never touched here, whatever its path says,
    so this sweep can never race a live document. Runs after the intents settle, so a
    wedged move's source file - whose row very much exists - is renamed, not swept.
    """
    known_documents = {int(r[0]) for r in conn.execute("select id from documents")}
    known_classes = {int(r[0]) for r in conn.execute("select id from classes")}
    removed = 0
    removed += _sweep_uploads(known_documents, known_classes)
    removed += _sweep_text(known_documents)
    removed += _sweep_pages(known_documents)
    return removed


def _sweep_uploads(known_documents: set[int], known_classes: set[int]) -> int:
    removed = 0
    uploads = settings.uploads_dir
    # A symlink where Lyra's own top-level directory belongs is never iterated: the
    # sweep below unlinks what it finds, and a followed link would aim that at outside
    # files. The guard is best-effort (the destructive unlinks themselves go through the
    # no-follow tree walk); a link is left visible for the operator rather than removed.
    if uploads.is_symlink() or not uploads.is_dir():
        return 0
    for class_dir in uploads.iterdir():
        if class_dir.is_symlink() or not class_dir.is_dir() or not class_dir.name.isdigit():
            continue
        for entry in class_dir.iterdir():
            removed += _sweep_entry(entry, known_documents)
        if int(class_dir.name) not in known_classes:
            # Something surviving the sweep (an unrecognized name) keeps the directory,
            # deliberately visible rather than force-removed.
            with contextlib.suppress(OSError):
                class_dir.rmdir()
    return removed


def _sweep_entry(entry: Path, known_documents: set[int]) -> int:
    """Remove one uploads entry if it is provably garbage. Never touches a symlink."""
    if entry.is_symlink() or not entry.is_file():
        return 0
    if entry.name.endswith(private.PARTIAL_SUFFIX):
        return _swept(entry)
    prefix = entry.name.split("-", 1)[0]
    if prefix.isdigit() and int(prefix) not in known_documents:
        return _swept(entry)
    return 0


def _sweep_text(known_documents: set[int]) -> int:
    removed = 0
    directory = settings.text_dir
    if directory.is_symlink() or not directory.is_dir():
        return 0
    for entry in directory.iterdir():
        if entry.is_symlink() or not entry.is_file():
            continue
        if entry.name.endswith(private.PARTIAL_SUFFIX) or (
            entry.suffix == ".txt"
            and entry.stem.isdigit()
            and int(entry.stem) not in known_documents
        ):
            removed += _swept(entry)
    return removed


def _sweep_pages(known_documents: set[int]) -> int:
    removed = 0
    directory = settings.pages_dir
    if directory.is_symlink() or not directory.is_dir():
        return 0
    for cache_dir in directory.iterdir():
        if cache_dir.is_symlink() or not cache_dir.is_dir() or not cache_dir.name.isdigit():
            continue
        # Stale staging files are garbage even for a live document: their writer is gone.
        for entry in cache_dir.glob(f"*{private.PARTIAL_SUFFIX}"):
            if not entry.is_symlink():
                removed += _swept(entry)
        if int(cache_dir.name) not in known_documents:
            # Counted only when the cache is provably gone; an obstructed one is logged
            # and retried at the next startup, like any other orphan the sweep could
            # not remove. The sweep is best-effort by design - a failure here must not
            # abort reconciliation or startup - so unlike the delete paths there is no
            # intent to keep, only the retry the next sweep provides.
            try:
                discarded = render.discard_pages(int(cache_dir.name))
            except (OSError, private.PrivacyContractError):
                logger.warning(
                    "Could not remove the orphaned page cache %s; it will be retried next startup",
                    cache_dir.name,
                    exc_info=True,
                )
                continue
            if discarded:
                removed += 1
            else:
                logger.warning(
                    "The orphaned page cache %s could not be fully removed; it will be "
                    "retried next startup",
                    cache_dir.name,
                )
    return removed


def _swept(entry: Path) -> int:
    """Unlink one orphan through the no-follow tree walk (best-effort, logged)."""
    try:
        private.unlink_in_tree(entry, root=settings.data_dir)
    except (OSError, private.PrivacyContractError):
        logger.warning("Could not remove orphaned file %s; it will be retried next startup", entry)
        return 0
    return 1
