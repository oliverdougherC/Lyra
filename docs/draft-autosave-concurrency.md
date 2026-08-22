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
- The engine tracks the newest **desired** editor body independently of the body it last
  confirmed (`lastSaved`) and of the body a write is currently carrying. Reverting the
  editor to the previously-saved text while a different write is in flight therefore cannot
  make the engine believe nothing is owed: it still owes a corrective write of the desired
  body once the in-flight one lands, and it never reports `saved` while editor and server
  differ. (The old engine cleared its pending work when `schedule(S)` saw `S === lastSaved`,
  then let an in-flight write of `A` land and report `Saved` over an editor showing `S`.)
- `flush()` (tab hide, unmount, before a pass/review/export/snapshot) joins the in-flight
  write and drives the newest pending body to the server; it does not open a second
  pipeline. It **returns a verdict** - `{ ok, status: 'saved' | 'error' | 'conflict', version }`
  - so an explicit caller can prove the newest local body is authoritative before it acts on
  it. A `void`ed flush (tab hide) ignores the verdict; a body-dependent action must not.
- A failed write leaves the newest body dirty and retryable; a later successful retry does
  not regress state.
- `saved` is reported only when the server has confirmed the newest known body at its
  version. A refused stale write shows `Changed elsewhere`, never `Saved`.
- **Lost-response adoption.** A stale `409` whose `server_body` is byte-for-byte the body the
  write was carrying is treated as a lost successful response: the engine adopts
  `current_version` as confirmed rather than raising a conflict dialog over identical text.
  Provably safe - the bytes match, so nothing is lost.

## Body-dependent actions prove the save first

Every action that reads the body server-side - start/continue a pass, start a review, export
a PDF, take a snapshot - calls `flush()` first and **only proceeds when the verdict is `ok`**.
A save failure leaves the action unstarted with the actionable save state shown; a conflict
leaves it unstarted with the reconciliation dialog open. None of them runs against stale
server text.

## Reconciling the editor after a server operation (`syncEditorFromServer`)

When an AI pass, an accepted or **rejected** suggestion, or a restore settles, the workspace
pulls the new body back into the editor - but never over unresolved local work. The whole
decision is `decideServerSync`, and it consults unresolved-write ownership *first*, before any
equality shortcut:

- **skip** when a write can still commit (in flight, a debounce about to fire, or a conflict
  already open): adopt *nothing*, not even a freshly-read baseline whose bytes currently equal
  the editor. That write carries the pre-operation version, so the server's CAS refuses it and
  the engine raises the conflict itself once it settles; the editor and pipeline are left
  untouched. Adopting here would bump the write epoch and could report `Saved` while the
  request is still free to commit a *different* body server-side - the epoch only suppresses
  that write's stale *response*, it cannot revoke the server *mutation*. That was the exact
  silent editor/server divergence PLA-289 exists to eliminate; equality is not an exception to
  it. (Concrete reachable path: `S@v0` → type `A` → autosave `A` in flight → undo to `S` →
  reject a pending suggestion, which is body-neutral and syncs → refetch still reads `S@v0`
  and the editor shows `S`, so the bytes are equal → the sync must still skip, let `A` commit
  `A@v1`, and let the desired-body pipeline write the corrective `S` on top at `v1`.)
- **adopt** once no write can move the server: either the editor already shows the server body
  (only the version base moved) or there is no unsaved local divergence. Reset the editor to
  the server body and move the version base forward. The editor is reset only when it is not
  already showing that body, so a matching editor never churns.
- **conflict** when the student has unsaved local text the operation moved under and the
  editor is not already showing the server body: raise a reconciliation and keep their words.
  `noteSaved`/editor reset is reached only on the safe paths - a settling operation can never
  discard unresolved local text, and a body-neutral sync can never falsely confirm one.

## Conflict recovery

On a stale-version `409` the workspace keeps the local editor text and opens a
reconciliation dialog showing the version saved elsewhere. The student chooses:

- **Keep what I wrote** adopts the conflict's `serverBody`/`serverVersion` as the
  authoritative baseline - a conflict is proof the pre-conflict `lastSaved` is *not* what the
  server holds any more - and then writes the local text over it whenever the two differ. It
  reports `Saved` only after that compare-and-swap lands; it adopts without a write only when
  the local bytes already equal `serverBody`. It never decides "nothing to write" by
  comparing against the stale pre-conflict `lastSaved`, which is what let a resolution clear a
  conflict and report `Saved` while the server still held the other tab's body.
- **Use the other version** adopts the server's body and version and loads it into the
  editor (server wins, deliberately).

Neither path silently reloads server text over newer local writing. Both bump an internal
write epoch, so a stale response from a write that was in flight when the conflict was raised
or resolved is ignored rather than allowed to resurrect the conflict or roll the confirmed
body backward.

## Causal rules for other body-mutating operations

Every operation that replaces the body goes through `set_part_content` /
`apply_part_content`, which increments `content_version`. That alone makes a *later* stale
autosave conflict, but it is not enough to coordinate an operation with *unsaved or in-flight*
editor state - so each body-replacing operation also carries the version it acted on:

- **AI pass** writes sections directly (into empty/untouched sections) and moves the
  version. While a pass runs, the editor is `inert` - the pass owns the document. When the
  pass settles, the workspace reconciles via `decideServerSync` above: it follows the pass
  only when there is no unresolved local work, and otherwise raises a conflict instead of
  discarding local text.
- **Accepted suggestion.** The workspace lands and confirms the student's own writing before
  the suggestion replaces the body (a `flush()` barrier), and the accept carries
  `expected_body_version` - the version the student reviewed against. That token is
  **required** server-side: `POST /api/pending-edits/{id}/accept` only ever touches a draft
  body (`_require_draft_edit`), so `AcceptRequest.expected_body_version` is a required field
  and a request without it is a `422` before anything is written - a stale bundle or a direct
  caller cannot force-replace a draft body versionlessly. `suggestions.accept` runs the whole
  read-refresh-write inside one `begin immediate` transaction and refuses a body that moved
  past that version (`StaleContentError` -> `409`) - **including a force-replace**, which is a
  choice to overwrite the version the student *saw*, not whatever landed after they looked. A
  stale accept is fed into the same reconciliation dialog.
- **Restore.** Draft-body restore is version-aware and the version is **required** for a
  draft. The shared restore endpoint enforces this by part kind, not by trusting the caller:
  a `DRAFT_BODY` part with no `expected_version` is refused with a deterministic `400` and
  nothing is mutated, while a solution part (which has no version token) restores
  unconditionally as before. A draft restore runs through
  `artifacts.compare_and_restore_part_content`, one `begin immediate` transaction that (1)
  refuses a stale `expected_version` without mutating anything, (2) records the *outgoing*
  body as a revision unless it is already the newest recorded one, then (3) records and writes
  the target and advances the version. The current text is flushed first, so the body the
  transaction preserves is the student's latest writing. This is what makes the guarantee
  real: an autosave records **no** revision (`PATCH /drafts/{id}/body` with `snapshot:false`),
  so without step (2) a manually-autosaved body would be replaced by an older revision and
  lost. Now it becomes a history point and restoring is genuinely undoable.
- **A stale editor tab** that missed one of the above no longer overwrites it: its next
  autosave carries a version the body has moved past, so it conflicts and reconciles
  rather than clobbering the AI result or the restored revision.
- **Snapshot** flushes the pipeline first (and aborts if that flush is not `ok`), then writes
  at the current version. A race with another tab loses the snapshot's CAS; that conflict is
  fed into the save engine (`forceConflict`), so the local text is kept, the version saved
  elsewhere is exposed, and the indicator is never left saying `Saved` - the student
  reconciles it the same way as any other conflict.

## Compatibility

- The migration is forward-only and atomic; existing rows default to version 0, a valid
  starting version. Every released-version upgrade path is covered by `test_migrations.py`.
- Backup/restore copies the SQLite file whole, so the body and its version survive a
  round-trip (`test_lyra_launcher.py`).
- Nothing here touches the loopback-only or private-storage boundaries; the conflict
  response returns the draft's own body, which the client already holds a version of.
