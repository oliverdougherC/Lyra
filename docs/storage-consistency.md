# Storage Consistency

How Lyra keeps the SQLite database and the document files on disk telling the same story
when a write, a rename, a delete, a concurrent request, or the process fails partway
through.

Document lifecycle operations cross two transactional domains — SQLite and the filesystem —
and no transaction spans both. This document defines the one contract every such operation
follows, so later work (streaming uploads, backup/restore, packaging, launcher recovery)
extends this model instead of inventing a second incompatible one.

Implementation: `backend/core/storage_intents.py` (durable intents and startup
reconciliation), `backend/core/ownership.py` (the live-request half: lifecycle mutex and
guarded publication), `backend/storage/private.py` (`publish_private_bytes`, the
publication primitive), `backend/storage/migrations/032_storage_intents.sql` and
`033_storage_intent_blocked.sql` (the journal table).
Fault-injection and race coverage: `backend/tests/test_storage_consistency.py`,
`backend/tests/test_render.py`.

## The guarantee, stated precisely

Everything below is scoped to **process crash or interruption**: the Lyra process dying,
being killed, or failing at any instruction boundary. Within that scope:

- A committed document row never points at a missing or torn upload.
- A stored file, extracted text, rendered page, or figure crop that exists under its
  final name is whole.
- After any interruption, the surviving `storage_intents` rows name exactly the
  filesystem work still owed, and the next startup converges every reachable state
  (tables at the end of this document).
- A delete that has settled cannot have its files resurrected by a writer that started
  before it.
- Recovery and cleanup never follow a symlink and never act on a path outside the current
  data tree.

**What is deliberately not promised: durability across OS crash or power loss.** There is
no `fsync` discipline on file data, on the staging rename, or on the directories involved.
`os.replace` gives namespace atomicity, not persistence ordering: after a power loss, a
rename that "happened" may be gone, a published file's bytes may be lost while the rename
survived (an empty or stale file), and SQLite's commit durability (its own WAL fsync
policy) is a separate domain from file-data durability — neither implies the other, and no
ordering between them is guaranteed. What *is* designed to hold after power loss is
best-effort convergence: reconciliation re-derives what is owed from whatever intents and
files actually survived, treats a missing file as honestly missing (a failed row with a
"file went missing" message, never a fabricated success), and never trusts a final-name
file to be anything more than what the filesystem preserved. For a single-user local app
this is the chosen trade; if a future product invariant needs real power-loss durability,
the change is fsync of staged data before rename plus directory fsync after, behind
`publish_private_bytes`, and an explicit revision of this section.

## The contract

Four rules, composed:

### 1. Final names are published, never written

A file that other code trusts on the strength of its existence — a stored upload, an
extracted-text file, a rendered page or figure — never appears under its final name until
it is whole. `private.publish_private_bytes` writes the bytes to a writer-private staging
name beside the target (`<name>.<pid>.<tid>.partial`), through the same no-follow `0o600`
descriptor as any private write, then renames it into place. The rename is atomic within
the directory, so a process crash at any point leaves the previous complete file, a
clearly-marked `*.partial` leftover that nothing reads, or the new complete file — never a
truncated file under a trusted name.

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
insert rolled back — is garbage with no row, and the sweep (rule 4) removes it.

### 3. Live requests own what they observed

Intents make crashes recoverable; they cannot referee two *legitimate concurrent
requests*, because both are real operations and both would settle their intents believing
they finished. That is the job of `backend/core/ownership.py`, in two layers:

**The lifecycle mutex.** Move, document delete, class delete, re-ingest, and recognize
each run their whole read–decide–commit–act sequence under one process-wide lock. Lyra is
a single-process application (the in-memory ingestion queue and the launcher's ownership
model already assume it), so this fully serializes lifecycle operations for live
requests. The lock is never held across parsing or rasterization — only across the quick
DB statements and the renames/unlinks — and it is irrelevant to crash recovery:
reconciliation runs single-threaded at startup before any request or worker exists. This
is deliberately **not** a SQLite transaction held across filesystem work.

**Conditional transitions.** The mutex makes interleavings impossible; the database
writes are shaped so that even a code path that failed to take it could not corrupt
state:

- A move's committed transition is a compare-and-swap: `update … where id = ? and
  class_id = ? and stored_path = ? and state = ?` against the exact values it observed.
  Zero rows updated means something advanced the document first; the whole transaction
  (chunk invalidation, evidence, intent) rolls back and the request gets a 409.
- A move's *compensation* (rename failed in-process) is CAS-guarded the same way: it
  restores the source location only if the row is still exactly what this move committed,
  and otherwise only withdraws its own intent — stale compensation can never overwrite a
  newer lifecycle mutation, and in particular can never resurrect a document a concurrent
  delete removed.
- A delete re-reads `stored_path` *after* its first write statement — that is, under
  SQLite's write lock, when no other connection can commit — so the delete intent records
  where the file actually is, not where a stale read said it was.
- Re-ingest and recognize update their row conditionally on the state they observed.

**Guarded publication.** Derived state (extracted text, rendered pages, figure crops) is
produced by writers that read the document long before they publish — the ingestion
worker, a page request racing a delete. Every such publication goes through
`ownership.publish_current_document`, which re-verifies **under the lifecycle mutex, on a
fresh connection** that the document row still exists with the `created_at` identity the
writer started from, immediately before the atomic rename. A delete holds the mutex
across its commit *and* its cleanup, so a late writer either publishes entirely before
the delete began (its file is then removed by the cleanup) or checks after it settled
(and refuses, publishing nothing). There is no window between check and publication. The
`created_at` comparison also covers delete-then-reupload under a reused id: the old run's
output cannot land on the new document.

### 4. Startup reconciliation converges, idempotently

`storage_intents.reconcile_storage` runs in the app lifespan, after migrations and before
the ingestion queue is rebuilt. It settles every surviving intent, then sweeps orphans:

- **`move_document`** rolls the operation forward or recognizes it as done, driven by
  where the file actually is: destination present → done; source present → perform the
  rename now; neither present → mark the document `failed` with an honest "file went
  missing" message rather than leaving a `pending` row pointing at nothing. A row that no
  longer matches the intent (the document was deleted, or a compensation restored the
  source location) means the rename is no longer owed.
- **`delete_document` / `delete_class`** re-run their cleanup. Missing files are the goal
  state, so re-running is idempotent — and completion is explicit, never inferred from
  "the cleanup returned": a page cache that could not be fully emptied, an entry that
  could not be inspected or removed, an unexpected subdirectory (skipped, never recursed
  into), or a cache directory that would not go away raises `CleanupIncompleteError`, so
  the intent is kept and retried rather than settled with files still on disk. Payloads
  are validated per kind before any work or any "nothing to do" early exit: a delete
  intent must carry its `stored_path` key holding a non-empty path or an explicit null
  (the one legitimate "no stored upload" shape), a class delete its `document_ids` list,
  a move its `source` and `destination` — anything else is blocked with its evidence
  kept, never settled as if there were nothing to remove.
- An intent whose filesystem work *still* fails for an environmental reason (a permission
  error, a symlink planted inside the tree) is kept unclassified, logged, and retried at
  the next startup — never silently dropped.
- An intent whose recorded work is **unsafe to perform at all** — an unreadable payload,
  a recorded path outside the current data tree, an unknown kind — is kept and durably
  marked `blocked_reason`. It is never acted on, and never settled with its work skipped:
  settling would discard the only durable pointer to a file the operation still owes.
  Blocked intents are re-validated at every startup (cheap, never destructive), so an
  intent blocked by a temporarily relocated data directory settles by itself once the
  environment is back, while a genuinely malformed one stays visibly blocked instead of
  being retried as though it might start working. The log names the intent and the
  classification, not the outside path; the payload row is the evidence.
- **No intent can abort startup.** Every failure mode above is contained per-intent
  (including unanticipated exceptions, which are logged and retried), and later intents
  settle regardless of what earlier ones did.
- **The orphan sweep** then removes `*.partial` staging files (at startup, every writer is
  dead) and stored uploads / extracted text / page caches whose document id has no row.
  Only entries whose id provably has no row are removed; anything belonging to a live row
  is never touched by the sweep, which is why intents settle first — a wedged move's
  source file has a live row and gets renamed, not swept.

Reconciliation and cleanup never follow a symlink — in *any* path component, not only
the final one. A lexical "inside the data directory" check cannot see that `uploads/5`
has been replaced by a link to an outside directory, so every destructive operation
(unlink, rename, cache clearing) descends from the data root one component at a time
with `O_NOFOLLOW` and acts through the directory descriptor that descent validated
(`private.unlink_in_tree`, `private.replace_in_tree`, `private.clear_owned_dir`) — the
same openat machinery `secure_mkdir` uses for creation, with the same documented
best-effort `lstat` fallback on platforms without it. A symlink planted where an owned
component belongs raises `PrivacyContractError`: the intent is kept and retried, and the
link's target is never entered. That includes the page cache: `render.discard_pages`
reaches the cache directory through that descent, removes a planted symlink at the
directory's own name as a link (never entering its target), skips unexpected
subdirectories, unlinks only regular files and links matching its own naming patterns,
and reports whether the directory is provably gone. The live move path is held to the
same contract before it mutates anything: a row whose `stored_path` is outside the
uploads tree, or reachable only through a planted link, refuses with a clean 409 before
chunk invalidation, the commit, the intent, or the rename.

## Per-operation walkthroughs

### Upload

1. Insert the document row (uncommitted) to allocate its id.
2. Publish the file at `uploads/<class>/<id>-<name>` (rule 1).
3. Point the row at the file; commit.

Crash after 1: insert rolls back; nothing on disk. Crash during 2: insert rolls back; a
`*.partial` is swept at startup. Crash between 2 and 3: insert rolls back; a whole file
with no row is swept at startup. After 3: consistent.

### Move

All under the lifecycle mutex:

1. Refuse if the source is not a real regular file (absent or a symlink): a missing
   source is an honest 409, never a "successful" move to a fictional destination.
2. One transaction: invalidate chunks and document-scoped profile evidence, record the
   `move_document` intent, CAS-update the row (new class, new path, `pending`); zero rows
   → roll everything back, 409.
3. Rename the file.
4. Delete the intent; commit. Queue the re-ingest.

If step 3 fails in-process, the CAS-guarded compensation runs instead: one commit
restores the row to the source class and path (only if still owned) and withdraws the
intent, and the document re-indexes in place. If the process dies at any point after
step 2, the intent survives and reconciliation converges as described above. The database
can briefly point at the destination while the file is still at the source — but never
without the intent that records exactly that, which is the difference between a
recoverable state and split-brain.

### Delete (document and class)

All under the lifecycle mutex:

1. One transaction: invalidate derived rows, re-read the stored path under the write
   lock, record the delete intent carrying it, delete the row(s); commit. From here the
   UI honestly reports the thing gone.
2. Remove the files (idempotent; missing files are fine; never through a symlink).
3. Delete the intent; commit.

A cleanup failure after step 1 does not fail the already-committed delete: it logs, keeps
the intent, and startup retries (or blocks it, if the recorded work turns out unsafe).
Private coursework is never left orphaned behind a UI that says it was deleted, and the
pointer needed to finish the cleanup is never lost. Because the whole block holds the
mutex, no late writer can publish derived state between the commit and the cleanup and
have it survive.

### Caches

Rendered pages and figure crops are disposable derived state: they are published
atomically (rule 1) and only through the identity guard (rule 3), and they are removed by
the delete intents and the orphan sweep; losing one costs a re-render, not data.
Extracted text is also derived, but readers treat it as the complete transcript of the
document, so it gets the full publication contract, the identity guard, and coverage by
the delete intents.

## Reachable persisted states

The states a process crash can leave, and what converges each. "Intent" means a surviving
`storage_intents` row; the sweep is the startup orphan sweep. Live-request interleavings
are excluded by the mutex and CAS (rule 3) and therefore do not appear as persisted
states.

### Upload (row insert → publish → point-and-commit)

| Row committed | File at final name | How it converges |
| --- | --- | --- |
| no | no (maybe `*.partial`) | nothing owed; partial swept |
| no | yes | file with no row: swept |
| yes | yes | valid (a committed row implies the publish happened first) |
| yes | no | **unreachable by crash**: the commit is ordered after the publish. Reachable only by outside interference or power loss; the parser then fails the document honestly on first read |

### Move (CAS commit with intent → rename → settle)

| Row points at | Source file | Destination file | Intent | How it converges |
| --- | --- | --- | --- | --- |
| source | present | — | none | valid (before commit, or after compensation) |
| destination | present | absent | present | roll forward: rename now, settle |
| destination | absent | present | present | done: settle |
| destination | absent | absent | present | fail the document honestly (`FILE_LOST_MESSAGE`), settle |
| destination | absent | present | none | valid (settled before crash) |
| source (restored) | present | — | present | compensation committed but crash before its intent delete — cannot happen: restore and intent-withdrawal are one commit. If ever seen (outside interference), recovery sees row ≠ destination and settles without touching files |
| row deleted | any | any | present | a delete won after the move wedged; move recovery settles with no action, the delete intent and sweep own the files |

### Delete (intent + row delete in one commit → cleanup → settle)

| Row | Files | Intent | Old writer active | How it converges |
| --- | --- | --- | --- | --- |
| present | present | none | any | valid (delete not yet committed) |
| absent | present | present | no | startup re-runs cleanup, settles |
| absent | present | present | yes (next process) | at startup every writer is dead; identical to the row above |
| absent | absent | present | no | cleanup done, crash before settle: re-run is a no-op, settle |
| absent | absent | none | no | fully settled: valid |
| absent | absent | none | yes (same process) | the guard refuses the late publication: row absent → nothing appears |
| absent | partial derived state reappears | none | — | **unreachable**: publication is checked under the same mutex the delete holds through cleanup |
| absent | present (outside-root recorded path) | present, blocked | — | never acted on, never settled; durable evidence kept for manual handling |

Class delete is the document-delete row repeated per document plus the class directory;
its intent carries the id list, and each converges identically.

## What this deliberately does not do

- **No fsync/write-barrier discipline** — see "The guarantee, stated precisely" above for
  exactly what that scopes out.
- **No cross-process coordination.** One backend process owns the data tree at a time —
  already true of the in-memory ingestion queue and the launcher's ownership model. The
  lifecycle mutex is process-memory; the startup sweep assumes any `*.partial` writer is
  dead.
- **Backup/restore and the launcher** should treat `storage_intents` as part of the
  database state it already captures: restoring a database with surviving intents is
  safe, because the next startup reconciles them against whatever files the restored tree
  holds — including refusing, as blocked, any intent whose recorded paths do not belong
  to the restored tree.
