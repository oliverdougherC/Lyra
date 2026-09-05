# Study reliability contracts

This change addresses PLA-469, PLA-470, PLA-472, PLA-474, PLA-476, and PLA-477. Work began
from fetched `origin/main` at `ae5957720d6ba5b4be387f7f9cfaa23d9546eea2`, in an isolated
worktree so the unpublished PLA-404 UI work remained intact. No model prompts or dependencies
were added.

## Selected material and quiz publication

Flashcard topic retrieval carries the accepted document set through vector, lexical, and
section-reference retrieval before ranking or limiting. Each attempt revalidates source readiness;
completion revalidates selected sources and chunk identity inside its persistence transaction.
A selected lecture therefore cannot be crowded out by a stronger matching excluded answer key.

Quiz proposals must have a requested type, actual nonblank question/explanation/topic strings,
and usable answer options. Multiple choice requires four distinct choices after surrounding
whitespace/case normalization, with an integer index in range. True/false requires the exact
`["True", "False"]` answer convention. Fill-blank requires one nonblank answer at index zero
and a blank in the question. Validation never reorders answers or changes the requested format.
The existing single retry remains bounded; a shortfall after recovery fails without Ready content.

Source gathering reads one chunk's metadata per document turn using a keyset cursor, fetching
text only when it can fit the per-document and total budgets. A deque replaces front-of-list
shifting. Limits are 256 examined chunks per document and 4096 metadata reads overall;
oversized and no-fit inputs cannot force unbounded Python materialization or scanning.
Chunks beyond that horizon can be omitted, a deliberate bounded-work policy. Original chunk
boundaries and round-robin fairness are retained within the scan horizon. A byte-length guard
also prevents embedded NUL characters from bypassing SQLite character-length budgeting.
The regression comparing 2,000 and 16,000 chunks returns the same 8,198-character prompt with
peak traced Python retention below 150 KB. This is not an installed-app RSS or 8 GB claim.

## Cancellation

Pending and Generating are live states. Ready, Failed, and Cancelled are settled states.
Stage and progress writes require a live state. Cancellation, failure cleanup, restart cleanup,
and publication serialize through short database transactions. No write lock spans inference
or retrieval. All parts, revisions, scheduling state, provenance, and Ready commit together.

If cancellation wins, no subsequent worker write resurrects the artifact. If completion wins,
a late cancel returns 409 explaining Ready and leaves content intact. Repeated cancellation
returns the existing Cancelled result. Deletion stops late workers. Shared artifact helpers
keep their prior defaults; study publication explicitly owns the transaction.

## Scheduling policy (PLA-476)

| Situation | Successful rating | Again |
| --- | --- | --- |
| New card | Seed interval: Hard 1.2, Good 2, Easy 2.8 days | Learning, due in 10 minutes |
| Early, not yet due | Preserve stability, state, and deadline | Decay stability; due in 10 minutes |
| Due with at least 24 hours since last rating | Multiply by Hard 1.2, Good 2, Easy 2.8 | Decay stability; due in 10 minutes |
| Due without 24 hours of evidence | Keep strength with a one-day floor; graduate relearning | Decay stability; due in 10 minutes |

Every genuine rating still increments reps and records its own operation. Early practice updates
last-review time, so growth requires a subsequent uninterrupted 24-hour interval as well as being
due. Missing/future review history does not earn growth. Stability and intervals cap at 365 days;
legacy finite inflated values clamp before calculations, nonfinite stability resets to one day,
and nonfinite difficulty resets to five. Date arithmetic also respects the shared supported
calendar ceiling. Python and TypeScript consume the same 13-scenario parity fixture.

This modest policy preserves useful practice without treating seconds-apart clicks as evidence
of months of retention. Study again uses fresh operation IDs. Again retains coherent lapse and
relearning behavior; due short-interval relearning can graduate without a multiplier.

## Review retry and recovery (PLA-477)

Before sending, the client stores the queue, counters, scheduling snapshots, and immutable
operation ID/rating in tab-scoped session storage. It persists the acknowledged snapshot before
advancing the UI. The API rejects a different rating under an already committed operation key
with 409; same-key/same-rating replay returns the original result without another log entry.

| Event | Result |
| --- | --- |
| Failure before commit | Retry the saved rating/key; exactly one review is eventually committed |
| Response lost after commit | Retry the saved rating/key; replay the original committed result |
| Different rating while unresolved | Buttons and keyboard cannot submit it; confirm the original first |
| Reload/navigation in the same tab | Restore the pending card and operation, even if the server queue reordered it |
| Acknowledgement succeeds | Persist and display the committed rating, scheduling state, and count together |
| Study again | Start a fresh session and mint a fresh key for each genuine review |

Unresolved interval buttons show confirmation status, not a speculative interval. Unavailable or
malformed recovery storage blocks review explicitly rather than silently discarding a pending key.
Recovery is scoped to the current browser/webview session; it does not promise persistence after
tab destruction. A terminal deck/card conflict preserves the pending key and surfaces the API
failure. Previously unkeyed review-log entries remain compatible.

## Verification and release-quality scope

New deterministic coverage lives in `backend/tests/test_study_scope_contracts.py`,
`backend/tests/test_study_cancellation.py`, the study/API tests, the shared scheduler fixture,
and frontend scheduler/session tests. Cancellation tests use two connections and controlled
barriers around stage/progress writes, final persistence, failure cleanup, cancellation,
deletion, duplicate work, and restart. They also prove partial publication is invisible.

Real-stack Playwright coverage in `study-source-contract.spec.ts` exercises the source picker,
excluded answer-key evidence, retry provenance, requested MCQ recovery, and terminal malformed
output. `study.spec.ts` forwards a review to the real API and drops its response after commit,
then exercises a competing rating, reload, replay, persisted intervals and truthful counters.
It also tests failure before commit, 100 distinct same-day operations, and queued cancellation.
The restart acceptance suite checks recovery and durable completed artifacts.

These deterministic provider fixtures establish software contracts, not factual quality across
real providers. PLA-151 stays open for a versioned representative corpus, human-reviewed answers,
recorded model/endpoint configuration, and repeats on the merged release candidate. Add the
selected-source, malformed-format, cancellation, bounded-library, early/due-practice, and lost-ack
cases above to that gate. The adjacent malformed-topic diagnostic gap is tracked as PLA-478.

PLA-404 PR #70 was published during final verification at
`532f5581ed655f939f13a3fa63f0f918caff901e`. Its study diff was inspected. A non-mutating
`git merge-tree` check finds content conflicts in `deck-session.tsx` and
`deck-session.test.tsx`; neither branch was merged. Rebase after that work reaches main.
Keep its semantic card faces, focus management, CardActions, deck-wide progress, and layout.
Combine its in-memory retry binding with this durable operation/snapshot contract using one
review guard. Editing/removing a card must update the persisted snapshot too; do not let those
actions discard an unresolved rating. Rerun session, source-picker, and acknowledgement-loss
acceptance against the resolved integration. This branch does not claim that combined gate.

The local review app was rebuilt with the frozen backend, signed with the existing Apple
Development identity, and all 81 native code objects passed verification. The signed bundle's
frozen smoke reported authenticated ephemeral-loopback startup; native launch loaded existing
classes and the backend process from this worktree's bundle. The signing helper was the existing
local `scripts/sign_local_app.py` from the parallel checkout, kept outside this PR's code scope.
