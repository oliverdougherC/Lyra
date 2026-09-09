# Study reliability contracts

These are the maintained selected-source, cancellation, scheduling, and review recovery contracts.
The dated integration evidence below records the original work; it does not state current PR or
release status. Use the [release ledger](release-evidence.md) for current acceptance gates.

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
malformed recovery storage blocks recording explicitly rather than silently discarding a pending key.
The student can retry storage access in place, or study without recording reviews while the raw
recovery evidence remains untouched.
Recovery is scoped to the current browser/webview session; it does not promise persistence after
tab destruction. A removed card preserves its pending operation as an explicitly unresolved record; it is never
counted as confirmed. The student can continue valid remaining cards. An absent review log after
deletion cannot establish whether an earlier request committed, because card deletion cascades
to that log. Previously unkeyed review-log entries remain compatible.

Every mounted session owns a per-deck lease. Writes compare the stored serialized revision, and
post-await continuations check ownership before acknowledgement. Unmount releases the lease; a
new route instance supersedes it. A delayed acknowledgement from the old route therefore cannot
replace the newer queue, counters, states, or operation. Cancellation is not used as commit evidence.

Restoration reads the complete authoritative deck, bypassing the ordinary query-cache freshness
window. Membership, content, and scheduling state are reconciled without expanding the saved
queue to newly added cards. Missing cards with no pending operation are excluded without ratings.
A missing pending card needs explicit continuation that preserves its operation as unresolved.
Cards absent only from the limited session endpoint remain eligible. Editing/removal callbacks
and session restart persist coherent snapshots; restart excludes retained unresolved card IDs.

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

## Historical recovery follow-up evidence

> The remaining sections preserve the original PR #70/#71 integration history. Both PRs later
> merged; branch states and issue statuses below apply only to those recorded revisions.

The reviewed product head was `0e8d522284206d174c2af48282bcf8f2908fbd52`.
The new `study-recovery.spec.ts` runs production components against the actual card/review routes:

- **Late continuation, failing before:** A commits with its response held; real SPA navigation
  unmounts the run; the new run restores/replays A using its original payload; B commits with its
  response lost. B's saved pending key is asserted before releasing A. Releasing the original A
  response changes that operation from `{ id: <B key>, rating: good }` to `null` on the old product.
  **Passing after:** the entire B snapshot remains equal, reload replays B's original payload,
  and direct database reads show exactly one intended operation for each card.
- **Stale cards, failing before:** after a saved session leaves the study route, the API deletes
  its next card and corrects the following card. On returning through the actual SPA route,
  the old product keeps the deleted head and never displays the corrected content.
  **Passing after:** the complete deck read removes the deleted card, displays corrected content
  and authoritative scheduling state, preserves confirmed counters, and survives another reload.
- The eight production cases also cover deletion with an unresolved operation both before and
  after commit, an existing pending card omitted from the limited 20-card response of a 25-card
  deck, storage read failure, acknowledgement-write failure, and malformed storage. Unknown
  deleted outcomes remain retained and uncounted; storage retry replays the original key.

The baseline used the exact reviewed component/API, with only the safe acceptance ownership
harness substituted. Initial selector/interceptor setup failures were discarded as product
proof. A run using the old baseline teardown actually terminated neighboring acceptance
backends; those affected results were invalidated. The final baseline and fixed runs use the
combined ownership harness. The corrected teardown has real-process regressions for replacement
backends, neighboring process/state survival, absent birth-token refusal, and strict failure-ledger
reporting. No global command or state-file sweep remains. Helper fixtures also receive a
per-run ephemeral port (optional `ACCEPTANCE_HELPER_PORT` override), retained through restart.
All three backend harness cleanup routes require captured process identity instead of killing
whatever occupies a port. An actual-route regression keeps an independent listener alive while
all three cleanup attempts fail explicitly, then proves cleanup succeeds for a captured fixture.

An initial simultaneous full-suite run exposed the former shared helper port 19500: one helper
case failed in each neighboring suite. Those runs were invalidated and the final acceptance
runs use isolated helper ports. Strict assertions and failure accounting were retained.

The component/helper suite additionally covers storage changing between render and ownership
claim, external revision changes, failed edit/remove persistence followed by restoration,
read-only study returning to the preserved operation, and per-deck recovery controls.

## Exact integration base and acceptance limits

This follow-up integrates PR #70 at `8e462cbd098a7baa91cf56eaf61759425de0aba0`
(`codex/pla-404-recovered`). PR #70 remains unmerged; PR #71 is stacked on that exact base.
The initially overlapping card and harness changes were resolved semantically before the
new targeted #70 follow-up was merged. The resulting tree retains both sets of study tests,
accessible card faces, focus and shortcut ownership, edit/remove confirmation, narrow layout,
error retry, session-versus-deck counts, and the selected-source/quiz/cancellation/scheduler work.

Final aggregate CI, signed-bundle and launch results are attached to PR #71 and PLA-477.
Independent green PR heads are not combined evidence. PLA-477 remains In Review; PLA-151
remains open for real-provider quality and release-candidate evaluation. Actual assistive
technology, physical CJK/native zoom, notarization, and the clean 8 GB sustained-soak gates
remain release acceptance work. Retained deleted outcomes cannot be reconstructed from a
cascaded review log, and malformed storage supports unrecorded study until repaired. These
limitations are explicit rather than fabricated acknowledgements or claims of release readiness.

## Accepted recovery review and native-save base refresh

[Review comment 5554631516](https://github.com/oliverdougherC/Lyra/pull/71#issuecomment-5554631516)
accepted the recovery follow-up at `8a0d6c5867b84f8da09fc3e1c034dc89e3335762`.
The next integration incorporates PR #70's exact native failed-save correction at
`8e462cbd098a7baa91cf56eaf61759425de0aba0`. Study implementation, immutable operations,
recovery ownership, authoritative reconciliation, accessible controls and the isolated
acceptance harness are unchanged from the accepted review.

The incoming native helper writes and syncs a private sibling, then publishes atomically only
to an absent destination. Existing and racing entries are never overwritten; post-publication
cleanup/durability failures report that bytes were saved. Combined verification must include
its production-helper fault-point tests, source-pane outcome tests and the real-stack
original-document/study-recovery regressions. Current exact-head results are recorded in
PR #71 and PLA-477. This refresh does not claim a new interactive native Save-dialog run.

## Quiz continuation metadata

The class study list includes `active_attempt_id` and `answered_count` for each quiz.
These describe a ready quiz’s unfinished, non-abandoned attempt against its current question
snapshot. No valid active attempt returns `null` and `0`. The count reflects saved student
answers, separately from generated-question progress or a finished score. Queries are batched
across the class so the interface can offer continuation without fetching every quiz separately.

[Study beta quality evaluation](study-beta-quality.md) describes the bounded synthetic source
corpus and production generation/review replay commands. Its model evidence is separate from
these deterministic reliability contracts and from final merged-candidate human acceptance.

## Answer grading (PLA-496)

A submitted answer is the pair `(selected_index, response_text)`. For a choice question the
index is the graded answer; `response_text` is optional for legacy clients and the server
records the chosen option's text itself. For a fill-blank the index carries its `-1` miss
marker and `response_text` is the student's typed words — a fill-blank submitted without words
is rejected (422), not stored as a miss. The raw response persists beside the verdict, and the
verdict itself is one of `correct`, `incorrect`, or `uncertain` — a recorded "not confidently
right, not confidently wrong", never an irreversible wrong.

The layered pass in `backend/core/grading.py` runs cheapest settled layer first, and every
layer that cannot decide hands to the next rather than calling the student wrong. The rubric's
`answer_kind` gates which typed layers may settle at all — an untyped legacy question runs
every layer, `text` questions run only the text layers and the judge, and each typed layer is
conservative about what it accepts:

1. Trivial equivalence (always): exact match after stripping, or casefold equality, but only
   when neither side contains a digit or a mathematical token — `x` against `X` is not the
   text layer's to decide.
2. Rubric alternatives (always): acceptable forms the question generator wrote into its hidden
   grading contract, compared by the same equivalence.
3. Numeric (numeric/symbolic/set kinds): magnitudes in base units (units, percentages,
   scientific notation) with a relative tolerance — 1% by default, up to 5% when the rubric
   says so. The strict `math.isclose` check uses zero absolute tolerance. A failed comparison
   may receive boundary credit only when its excess is within four ULPs (representable-value
   spacings) at the larger operand's magnitude, and that rounding band is no more than
   `1e-8` of the rubric's permitted error. This is a deliberate rounding policy, not exact
   recovery of decimal intent: a few representation steps outside the computed cutoff are
   boundary-equivalent. There is no universal physical-unit allowance.
   Zero/nonzero, opposite-sign, and clearly outside-band mismatches remain incorrect. If a
   would-be boundary credit has an underflowed error limit or a band too large for the rubric,
   the result is precision uncertainty. That uncertainty remains terminal through scalar,
   set, and alternative paths; later algebra or semantic judgment cannot turn it into a
   confident wrong. Ordinary prose or missing-unit abstention still reaches the judge.
   A unit-scale mismatch is a settled wrong (`1 mW` against `1 MW`), not an abstention;
   a bare value against a unit-bearing one still abstains to the judge.
4. Sets and lists (set kind, or untyped): a declared unordered set may accept exact/trivial
   prose matches in any order. An unmatched prose member abstains to the semantic judge so
   synonyms such as “cell membrane” and “plasma membrane” cannot become a confident false
   negative. Deterministically decidable numeric membership can still settle a mismatch;
   unavailable or malformed semantic judgment remains uncertain and retryable. A legacy
   prose list that merely reorders also abstains to the judge.
5. Symbolic (symbolic kind, or untyped): both sides normalized to plain notation and compared
   in the bounded algebra subprocess. The runner's `certain` is the contract: equal-and-settled
   is correct, different-and-shown is incorrect, not shown abstains. Only forms that carry a
   digit or an operator in the raw text ever reach the algebra — a product of bare words is
   prose, not a formula.
6. Constrained judge: one small schema-enforced JSON call to the configured tutor endpoint
   carrying the question, its hidden grading contract, the reference answer, and the student's
   words. The provider's confidence must be a finite number in `[0, 1]`; anything else —
   missing, non-numeric, out of range — is a malformed reply and the verdict is `uncertain`,
   never clamped to a default. The five provider grades map onto the flow's three outcomes
   conservatively: `correct`/`mostly_correct` are credit; `partially_correct` is credit only
   when the rubric's `partial_understanding_accepted` allows it; an `incorrect` below the
   confidence floor is `uncertain`. Keyword overlap alone is never enough to be right, and an
   answer containing a stated contradiction is wrong no matter what else it says.

Two hazards are gated before the expensive layers. Numeric parsing first rejects any power
expression whose base or exponent is not a bare literal — `2**(1000*1000*1000)` and
`2**1000**1000` are refused, so a computed or chained power can never blow up the unit parser's
allocation. And the algebra only sees text that already looks mathematical, so prose can never
settle as an incorrect formula.

No endpoint configured, an endpoint refusal, an unreadable reply, or a pass no layer settles
all land on `uncertain`. Unresolved answers are reported, never scored: the finish tally
carries an `unresolved` count beside `score`/`total`, per-topic breakdowns separate settled
answers from unresolved ones, and the interface offers a recheck for an unsettled fill-blank
instead of a confident wrong. The results screen follows the same rule: with every answer
settled it reports `You scored S out of T`, but as soon as any answer is unresolved it makes
no score claim at all and reports how much was answered, `You answered A of T`, with the
unresolved tally beneath — `total` stays the true question count either way.

The raw response survives reload, retry, and attempt history: a re-posted identical submission
replays its stored result instead of charging for another judgment, a different response
regrades and updates the row, and legacy pre-grading rows (`grading_version` 0, null response)
always regrade — a legacy `-1` is never read as a recoverable original response. Stored results
from an older grading contract also regrade on resubmission. Version 5 adds the bounded
machine-rounding policy, so a stored version-4 decimal or unit-boundary false negative can be
corrected when submitted again. Historical attempts are not automatically bulk-regraded.
Replay is locked to the question it graded: the stored digest of the question content must still match,
so a regenerated question regrades even an identical submission rather than reviving a
judgment about different words. An `uncertain` verdict never replays — retrying an unsettled
answer is exactly how a failed provider judgment recovers, and it re-grades in place without a
new attempt.

Concurrent duplicates are bounded to one judgment. A claim marks the row in flight, and the
marker is stamped with a unique claim token that the publish transaction must still match:
the slow A→B→A ordering (two different answers, then the first re-arrived) can never write
the older judgment into the row the newer one claimed. A second identical submission waits
for the first judgment rather than charging it again, and a late arrival for an *older*
answer is refused rather than overwriting a newer one. A duplicate that read the row as
absent just before the first judgment published is re-checked inside the claim transaction:
it replays the settled result it missed, not a fresh regrade, and it shares a terminal
result even when that terminal result is `uncertain`. A claim older than the longest
judgment has lost its owner and is reclaimed, so a restart cannot strand an answer forever.

The wait is prompt in both directions. It returns the moment any terminal state for this
exact submission is visible — settled *or* uncertain, once the row's stored digest still
matches the question's current content — and it stops immediately, without polling to a
deadline, when the attempt dies or is abandoned, the question's content changes, a newer
distinct answer takes the row, or a replay would surface against a no-longer-live attempt.
Grading runs outside any write transaction (the algebra subprocess and the judge may each
take many seconds), and the publish transaction revalidates the attempt and the question
content before writing the answer row; a late result after a restart or regeneration is
refused with a conflict, so a judgment in flight can never contaminate a new attempt.

A reload resumes at the earliest answer that still needs the student, not merely the first
unanswered one: an answer that was recorded but left unresolved carries its raw words and
its retry, so a reload with an unsettled first question and an unanswered second returns to
the first, restores the recorded response into the neutral reveal, and keeps the recheck
available.

Generation writes the hidden grading contract — `answer_kind`, `tolerance`, `units`,
`acceptable_alternatives`, `required_ideas`, `common_misconceptions`, `contradictions`,
`partial_understanding_accepted` — into fill-blank questions through the quiz schema, whose
`grading` property is a complete JSON Schema (typed object, required fields, no additions)
that mirrors the store-side validation applied when the question is written. Instruction,
schema, and validator agree on one nullability shape: the nullable scalars
(`answer_kind`, `tolerance`, `units`) are `null` and the four lists are empty where the
question type does not use them — a multiple-choice or true/false question grades a chosen
option, so its contract says exactly that — and `partial_understanding_accepted` is the
boolean `false`, never absent. The contract is stripped from every public read of a
question; the student sees the verdict, the reference answer, and the explanation only.

Honest limits: the algebra's "shown different" rests on sampling the difference at fixed
rational points, so a difference that vanishes at all sample points, or one over six free
symbols, is not settled and falls to the judge. The numeric layer abstains on a bare value
against a unit-bearing one — `4.2` against `4.2 Hz` is the judge's call, not a confident wrong.
Without a configured tutor endpoint no conceptual answer ever settles beyond the typed layers,
and that is recorded as `uncertain`.
