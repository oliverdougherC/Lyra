"""Class workspace management.

A class is the unit everything else hangs off: documents, chunks, chat sessions, and
profile facts are all class-scoped and cascade from here. Every read returns the class
plus its `document_count`, since no screen shows a class without it.
"""

import logging
import sqlite3

from backend.core import ownership, storage_intents
from backend.core.errors import NotFoundError
from backend.storage import private

logger = logging.getLogger(__name__)

UPDATABLE_FIELDS = frozenset({"name", "code", "semester", "archived"})

_LIST_SQL = """
select c.id, c.name, c.code, c.semester, c.archived, c.created_at, c.last_active_at,
       count(d.id) as document_count
from classes c
left join documents d on d.class_id = c.id
group by c.id
order by c.last_active_at desc, c.id desc
"""

_GET_SQL = """
select c.id, c.name, c.code, c.semester, c.archived, c.created_at, c.last_active_at,
       count(d.id) as document_count
from classes c
left join documents d on d.class_id = c.id
where c.id = ?
group by c.id
"""


def list_classes(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Every class, most recently active first, each with its document count."""
    return [dict(row) for row in conn.execute(_LIST_SQL)]


def get_class(conn: sqlite3.Connection, class_id: int) -> dict[str, object]:
    """One class with its document count.

    Raises:
        NotFoundError: when no class carries that id. Every other function in this
            module routes its lookup through here, so the message is written once.
    """
    row = conn.execute(_GET_SQL, (class_id,)).fetchone()
    if row is None:
        raise NotFoundError("That class does not exist.")
    return dict(row)


def create_class(
    conn: sqlite3.Connection,
    name: str,
    code: str | None = None,
    semester: str | None = None,
) -> dict[str, object]:
    """Insert a class and return it, with `document_count` zero."""
    cursor = conn.execute(
        "insert into classes (name, code, semester) values (?, ?, ?)",
        (name, code, semester),
    )
    conn.commit()
    return get_class(conn, int(cursor.lastrowid or 0))


def update_class(
    conn: sqlite3.Connection, class_id: int, **fields: str | None
) -> dict[str, object]:
    """Apply the supplied fields and return the updated class.

    Only keys in `UPDATABLE_FIELDS` are accepted. An empty update is a no-op that still
    returns the current row, so a PATCH with nothing set behaves like a read.

    Raises:
        NotFoundError: when no class carries that id.
        ValueError: when a key outside the allowed set is supplied.
    """
    current = get_class(conn, class_id)
    unknown = set(fields) - UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"Cannot update class field(s): {', '.join(sorted(unknown))}")
    if not fields:
        return current

    assignments = ", ".join(f"{column} = ?" for column in fields)
    # Column names come from the allowlist checked above; the values stay parameterised.
    conn.execute(
        f"update classes set {assignments} where id = ?",  # noqa: S608
        (*fields.values(), class_id),
    )
    conn.commit()
    return get_class(conn, class_id)


def delete_class(conn: sqlite3.Connection, class_id: int) -> None:
    """Delete a class, everything it owns, and everything its documents left on disk.

    Raises:
        NotFoundError: when no class carries that id.
    """
    # Under the lifecycle mutex, like a document delete and for the same reasons: no
    # concurrent move may relocate one of this class's documents between the id/path
    # capture below and the cleanup, and no late publication may recreate a deleted
    # document's derived files after the cleanup has run.
    with ownership.lifecycle_mutation():
        get_class(conn, class_id)
        # Read before the rows cascade away, because the files below are named after them.
        document_ids = [
            int(row[0])
            for row in conn.execute("select id from documents where class_id = ?", (class_id,))
        ]
        # The intent commits with the row deletes, so the cascading rows are never the
        # only record of the files still to remove: after a crash between this commit and
        # the cleanup below, startup reconciliation re-runs the cleanup from the intent
        # instead of leaving the class's coursework orphaned on disk
        # (docs/storage-consistency.md).
        intent_id = storage_intents.record_intent(
            conn,
            storage_intents.DELETE_CLASS,
            class_id=class_id,
            payload={"document_ids": document_ids},
        )
        # `chunk_embeddings` is a vec0 virtual table, and a virtual table receives no
        # foreign-key cascade. Its rows have to go explicitly, before the class row does.
        conn.execute("delete from chunk_embeddings where class_id = ?", (class_id,))
        # documents, chunks, chat_sessions, messages, and profile_facts cascade from here.
        conn.execute("delete from classes where id = ?", (class_id,))
        conn.commit()
        # The uploads are only what the student handed over. Ingestion also writes the
        # text it extracted, and the reader the pages it rendered, and neither lives under
        # the upload directory: a class is every document in it, so all three go. A
        # cleanup that cannot finish keeps its intent and is retried at the next startup
        # rather than failing a delete that has already committed.
        try:
            storage_intents.run_class_cleanup(class_id, document_ids)
        except (
            OSError,
            private.PrivacyContractError,
            storage_intents.IntentBlockedError,
            storage_intents.CleanupIncompleteError,
        ):
            logger.warning(
                "Cleanup for deleted class %s is deferred to the next startup",
                class_id,
                exc_info=True,
            )
            return
        storage_intents.settle_intent(conn, intent_id)


def touch_class(conn: sqlite3.Connection, class_id: int) -> None:
    """Mark a class as active now, so the class list keeps a useful order."""
    conn.execute(
        "update classes set last_active_at = datetime('now') where id = ?",
        (class_id,),
    )
    conn.commit()
