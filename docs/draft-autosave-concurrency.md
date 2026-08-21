# Draft autosave concurrency (PLA-289)

The draft workspace autosaves the body as the student types. Before this contract, a
slower older write could land after a newer one and restore stale text, and the save
indicator could still read `Saved` over a body the server no longer held. This document
records the invariants that make the single-user writing flow lossless and truthful.

## The version token

Every artifact part carries a monotonic `content_version` (migration `034`). It starts at
0 and every body write increments it by one, in the same transaction as the write. Reads
expose it (`GET /api/drafts/{id}` returns `body_version`), and the autosave states the
version it expects.

`content_version` tracks the body, not the revision history: an autosave records no
revision but still moves the version, so the token changes on every write regardless of
whether that write is a history point.

## Server contract

`PATCH /api/drafts/{id}/body` requires `expected_version` and writes through a
compare-and-swap (`artifacts.compare_and_set_part_content`):

- The version check and the write share one `begin immediate` transaction, so two writers
  racing the same body serialize into one winner and one conflict.
- A matching version writes the body, bumps the version, and returns the new `version`.
- A stale version returns `409` with `{code: "stale_body_version", current_version,
  server_body}` and mutates nothing - not the body, not the revisions, not the timestamps.

`begin immediate` is deliberate: a plain read-then-write could read a stale version and
then overwrite the newer body between the read and the write. The immediate lock removes
that window.

## Client contract (`save-engine.ts`)

- At most one write owns the pipeline at a time. A change arriving during a write is
  coalesced into the newest pending body and written next, never as a competing request,
  so an older write can never land after a newer one from the same editor.
- `flush()` (tab hide, unmount, before a pass/review/export) joins the in-flight write and
  drives the newest pending body to the server; it does not open a second pipeline.
- A failed write leaves the newest body dirty and retryable; a later successful retry does
  not regress state.
- `saved` is reported only when the server has confirmed the newest known body at its
  version. A refused stale write shows `Changed elsewhere`, never `Saved`.

## Conflict recovery

On a stale-version `409` the workspace keeps the local editor text and opens a
reconciliation dialog showing the version saved elsewhere. The student chooses:

- **Keep what I wrote** rebases onto the server's version and writes the local text over
  it (local wins, deliberately).
- **Use the other version** adopts the server's body and version and loads it into the
  editor (server wins, deliberately).

Neither path silently reloads server text over newer local writing.

## Causal rules for other body-mutating operations

Every operation that replaces the body goes through `set_part_content`, which increments
`content_version`. This is what coordinates autosave with the rest of the writer:

- **AI pass** writes sections directly (into empty/untouched sections) and moves the
  version. While a pass runs, the editor is `inert` - the pass owns the document. When the
  pass settles, the workspace re-seeds the editor and the version from the server, so the
  editor follows the pass and the next autosave expects the version the pass produced.
- **Accepted suggestion** and **restore** write the body and move the version. The
  workspace re-syncs the editor and the version afterward (`syncEditorFromServer`).
- **A stale editor tab** that missed one of the above no longer overwrites it: its next
  autosave carries a version the body has moved past, so it conflicts and reconciles
  rather than clobbering the AI result or the restored revision.
- **Snapshot** flushes the pipeline first, then writes at the current version; a race with
  another tab surfaces as a truthful "not saved" rather than a silent overwrite.

## Compatibility

- The migration is forward-only and atomic; existing rows default to version 0, a valid
  starting version. Every released-version upgrade path is covered by `test_migrations.py`.
- Backup/restore copies the SQLite file whole, so the body and its version survive a
  round-trip (`test_lyra_launcher.py`).
- Nothing here touches the loopback-only or private-storage boundaries; the conflict
  response returns the draft's own body, which the client already holds a version of.
