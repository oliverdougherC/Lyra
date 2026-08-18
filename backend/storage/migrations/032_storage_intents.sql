-- Durable storage intents: the crash-consistency journal for operations that must
-- mutate both SQLite and the filesystem (docs/storage-consistency.md).
--
-- One row is a promise that filesystem work is owed: a document move whose rename may
-- not have happened yet, or a delete whose files may not have been removed yet. The row
-- is inserted in the same transaction as the database mutation it belongs to and deleted
-- only after the filesystem work has completed, so after any interruption the survivors
-- name exactly the work still owed. Startup reconciliation replays them idempotently.
--
-- Deliberately no foreign keys: a delete intent must outlive the document/class row
-- whose files it exists to clean up.

create table storage_intents (
  id integer primary key autoincrement,
  kind text not null check (kind in ('move_document', 'delete_document', 'delete_class')),
  document_id integer,
  class_id integer,
  payload text not null default '{}',
  created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
