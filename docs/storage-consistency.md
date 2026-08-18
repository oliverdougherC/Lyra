# Storage Consistency

How Lyra keeps the SQLite database and the document files on disk telling the same story
when a write, a rename, a delete, the process, or the machine fails partway through.

Document lifecycle operations cross two transactional domains — SQLite and the filesystem —
and no transaction spans both. This document defines the one contract every such operation
follows, so later work (streaming uploads, backup/restore, packaging, launcher recovery)
extends this model instead of inventing a second incompatible one.

Implementation: `backend/core/storage_intents.py` (intents and reconciliation),
`backend/storage/private.py` (`publish_private_bytes`, the publication primitive),
`backend/storage/migrations/032_storage_intents.sql` (the journal table).
Fault-injection coverage: `backend/tests/test_storage_consistency.py`.

## The contract

Three rules, composed:

### 1. Final names are published, never written

A file that other code trusts on the strength of its existence — a stored upload, an
extracted-text file, a rendered page or figure — never appears under its final name until
it is whole. `private.publish_private_bytes` writes the bytes to a writer-private staging
name beside the target (`<name>.<pid>.<tid>.partial`), through the same no-follow `0o600`
descriptor as any private write, then renames it into place. The rename is atomic within
the directory, so a crash at any point leaves the previous complete file, a clearly-marked
`*.partial` leftover that nothing reads, or the new complete file — never a truncated file
under a trusted name.

**This is the publication contract for any future final-file producer.** A streaming
upload (PLA-165) should stream into a staging name (its own, or `private.partial_path`)
and publish with one rename; it must not write final names incrementally.

### 2. Owed filesystem work is recorded before it is owed

An operation that must touch the filesystem *after* a database commit records a
`storage_intents` row **in the same transaction** as the database mutation:

| Kind | Recorded with | Owed work |
| --- | --- | --- |
| `move_document` | the row's new `class_id`/`stored_path` | rename source → destination |
| `delete_document` | the document row's deletion | unlink upload, extracted text, page/figure cache |
| `delete_class` | the class row's deletion (documents cascade) | remove the class upload directory, every document's text and page/figure cache |

The intent is deleted (and that deletion committed) only after the filesystem work has
completed. So at every instant, `storage_intents` names exactly the filesystem work still
owed; nothing about pending cleanup lives only in process memory. The table deliberately
has no foreign keys: a delete intent must outlive the rows whose files it cleans up.

Upload needs no intent. It orders its effects so the committed state is always safe: the
row is inserted uncommitted, the file is *published* (rule 1), and only then is the row
pointed at the file and committed. A committed document row therefore never points at a
missing or torn upload. The one state a crash can leak — a whole published file whose
insert rolled back — is garbage with no row, and the sweep (rule 3) removes it.

### 3. Startup reconciliation converges, idempotently

`storage_intents.reconcile_storage` runs in the app lifespan, after migrations and before
the ingestion queue is rebuilt. It settles every surviving intent, then sweeps orphans:

- **`move_document`** rolls the operation forward or recognizes it as done, driven by
  where the file actually is: destination present → done; source present → perform the
  rename now; neither present → mark the document `failed` with an honest "file went
  missing" message rather than leaving a `pending` row pointing at nothing. A row that no
  longer matches the intent (the document was deleted, or a compensation restored the
  source location) means the rename is no longer owed.
- **`delete_document` / `delete_class`** re-run their cleanup. Missing files are the goal
  state, so re-running is idempotent.
- An intent whose filesystem work *still* fails (a permission error, say) is kept, logged,
  and retried at the next startup — never silently dropped.
- **The orphan sweep** then removes `*.partial` staging files (at startup, every writer is
  dead) and stored uploads / extracted text / page caches whose document id has no row.
  Only entries whose id provably has no row are removed; anything belonging to a live row
  is never touched by the sweep, which is why intents settle first — a wedged move's
  source file has a live row and gets renamed, not swept.

Reconciliation never follows a symlink and never acts on a recorded path outside the
current data tree; a planted or stale path is logged and skipped. The crash-consistency
machinery cannot be used to weaken the private-storage/no-follow contract it protects.

## Per-operation walkthroughs

### Upload

1. Insert the document row (uncommitted) to allocate its id.
2. Publish the file at `uploads/<class>/<id>-<name>` (rule 1).
3. Point the row at the file; commit.

Crash after 1: insert rolls back; nothing on disk. Crash during 2: insert rolls back; a
`*.partial` is swept at startup. Crash between 2 and 3: insert rolls back; a whole file
with no row is swept at startup. After 3: consistent.

### Move

1. Refuse if the source is not a real regular file (absent or a symlink): a missing
   source is an honest 409, never a "successful" move to a fictional destination.
2. One transaction: invalidate chunks and document-scoped profile evidence, update the row
   (new class, new path, `pending`), record the `move_document` intent; commit.
3. Rename the file.
4. Delete the intent; commit. Queue the re-ingest.

If step 3 fails in-process, the compensation runs instead: one commit restores the row to
the source class and path and withdraws the intent, and the document re-indexes in place
(its chunks are already gone). If the process dies at any point after step 2, the intent
survives and reconciliation converges as described above. The database can briefly point
at the destination while the file is still at the source — but never without the intent
that records exactly that, which is the difference between a recoverable state and
split-brain.

### Delete (document and class)

1. One transaction: invalidate derived rows, record the delete intent carrying the stored
   path(s), delete the row(s); commit. From here the UI honestly reports the thing gone.
2. Remove the files (idempotent; missing files are fine).
3. Delete the intent; commit.

A cleanup failure after step 1 does not fail the already-committed delete: it logs, keeps
the intent, and startup retries. Private coursework is never left orphaned behind a UI
that says it was deleted, and the pointer needed to finish the cleanup is never lost.

### Caches

Rendered pages and figure crops are disposable derived state: they are published
atomically (rule 1) so a cache hit is always a whole file, and they are removed by the
delete intents and the orphan sweep, but losing one costs a re-render, not data. Extracted
text is also derived, but readers treat it as the complete transcript of the document, so
it gets the full publication contract and is covered by the delete intents.

## What this deliberately does not do

- **No fsync discipline.** SQLite's own durability applies to the database; file contents
  are made *atomic* (whole-or-absent) but not flushed to platters before commit. A power
  loss can lose a just-published file's bytes on some filesystems; the reconciliation
  above still converges (the row's insert would also not have committed, or the intent
  survives). Full write-barrier durability is out of scope for a single-user local app.
- **No cross-process coordination.** One backend process owns the data tree at a time —
  already true of the in-memory ingestion queue and the launcher's ownership model. The
  startup sweep assumes any `*.partial` writer is dead.
- **Backup/restore and the launcher** should treat `storage_intents` as part of the
  database state it already captures: restoring a database with surviving intents is safe,
  because the next startup reconciles them against whatever files the restored tree holds.
