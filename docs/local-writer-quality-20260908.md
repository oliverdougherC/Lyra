# PLA-153 local writer quality: grounded full drafts and length adherence

> Dated local workstream evidence, 2026-09-08. Baseline source is current
> `origin/main` at `07ad4511ea8eed287fd950dfadccf43af775827a` (the prior prompt/transport/
> recovery fixes are already integrated). This packet does not claim universal quality
> or human acceptance; the human decision table for the writer rubric remains entirely
> unchecked.

## Remaining defects (from [writer-semantic-followup.md](writer-semantic-followup.md))

Full-draft (live Draft) runs can (a) put claims into the draft that the sources do not
establish and (b) overshoot the requested length — the latest frozen full-draft examples
ran 420–503 words against a 320-word request and settled `completed`. Existing accepted
student prose must remain exact.

## Diagnosis: two general causal weaknesses in the writer live pipeline

**1. The drafting stage has no scope discipline for its claims.** The live paragraph
drafting call (`backend/llm/prompts.py`, `build_paragraph_draft_prompt`) told the model
to cite only the listed sources, but not to keep claims inside what those sources
establish. The review-side prompts carry that discipline ("a missing measurement does
not establish the absence of an effect or change in the population…"), the drafting
side did not — so imprecise and population-extension claims were produced first and
left to a small-window model judgment to catch. (The judgment rubric for what the
baseline prose actually contains is corrected in the [claim read below](#unsupported-claims-semantic-read-of-the-retained-prose):
the baseline's flagged items are one imprecise framing and one legitimate proposal,
not a positive unsupported assertion and a fabricated method.)

**2. The length contract is one-sided and unenforced.** Each paragraph prompt says
"write about N words" (approximate), the output ceiling tolerates 1.5× a paragraph
budget, and the live finalize step checked only a per-block *minimum* (80%). Nothing
measured the assembled document against the student's explicit total. The baseline run
delivered 514 words as `completed` against an explicit 320-word request.

## Repair (small, writer-only)

Three changes, all in owned files (`writer_pipeline.py`, one code constant in the
writer-specific `writer_runs.py`, and the writer-only drafting prompt):

1. **Drafting-side scope contract** (`backend/llm/prompts.py`,
   `build_paragraph_draft_prompt` — the writer-only drafting prompt; only call site is
   the live pipeline's `_live_paragraph_prompt`): historical and factual claims must
   come from the supplied sources — no extending a source's sample, group, or
   measurements to a broader population than the source describes, no presenting an
   unmeasured effect or population as already measured, and a missing measurement is
   not evidence that an effect or population is absent. Clearly labeled proposals
   (a next step, a method to try, a survey to run) are the student's own
   recommendations and stay permitted, as does the student's own stated experience and
   stance from the notes — the only prohibition is inventing personal experience. An
   earlier draft of this fix was overbroad (it also banned proposed methods and
   unstated personal detail) and was revised; the live reruns below confirm legitimate
   proposals survive.
2. **Budget bound** (same prompt): the per-paragraph instruction now reads "Write about
   N words, staying within 10 percent of that budget." The budget is a bound, not a
   suggestion; this matches the deterministic band below. Residual limitation: on this
   model the bound is still routinely exceeded — see the claim/length reads below.
3. **Durable total-length warning** (`backend/core/writer_pipeline.py`,
   `_run_live_pipeline`, at finalize, using the existing `writer_runs` warning API):
   when the student set an explicit length (`_target_words` from the instruction or
   brief), the **whole assembled draft** — every block, student edits included — is
   measured against it. Beyond `LIVE_TOTAL_LENGTH_TOLERANCE = 1.10` the run still
   completes and the complete reviewable proposal is still delivered as a pending edit;
   the run keeps a durable `length_overshoot` warning stating actual and requested
   words and asking the student to review or shorten before accepting
   (`add_warning(..., replace=True)` on re-passes, `clear_warning` on the in-range and
   no-explicit-target paths so a stale warning cannot survive as a false alarm). The
   per-block thin-paragraph floor is unchanged, and no new abstraction or automatic
   rewriting loop was added. A draft with no explicit student length carries no length
   contract: plan budgets are model-chosen planning numbers, not a requirement.
   (The first revision of this lane instead settled the run `failed` and withheld the
   pending edit; that design turned a usable over-length proposal into a failed run
   and was replaced after review. Its receipts are retained as the `intermediate/`
   artifacts, not overwritten.)

**Preserved contracts (verified by the reruns and the suite):** source scope and
ledger (citations restricted to listed sources), exact preexisting student prose
(`body_unchanged` in every rerun), exact student block edits preserved in the
delivered proposal (behavior-tested), same-cap recovery (untouched: one same-cap retry
in the two assessment stages), cancellation (untouched), and the scoped-revision path
(legacy section pipeline — the live warning never applies to filtered passes).

## Tests

- `test_paragraph_prompt_binds_the_budget_and_scope_to_the_sources`
  (`backend/tests/test_prompts.py`): locks the bound sentence and the scope contract in
  the drafting prompt — historical/factual claims bound to the sources, proposals and
  the student's stated experience explicitly permitted, invented personal experience
  prohibited — with the target number wired through.
- `test_a_live_draft_over_the_requested_length_finalizes_with_a_durable_length_warning`
  (`backend/tests/test_writer_pipeline.py`): a durable live pass whose model output
  exceeds the explicit 700-word request beyond the band still **completes**: the full
  proposal is delivered as a reviewable pending edit, the run keeps exactly one durable
  `length_overshoot` warning with the exact actual/requested words
  ("The assembled draft runs 2100 words against a requested 700. Review or shorten it
  before accepting."), and the student document is untouched.
- `test_a_live_draft_that_fits_the_requested_length_finalizes_without_a_length_warning`:
  within the band the draft finalizes with no length warning attached.
- `test_a_live_draft_without_a_requested_length_finalizes_regardless_of_words`: with
  no explicit length the warning stays inert even for a long draft, and a stale
  warning from an earlier pass is cleared on the no-target path.
- `test_a_user_edited_block_is_preserved_and_counts_toward_the_length_warning`: a
  mid-run student edit of a block survives **exactly** in the delivered proposal, and
  the warning measures the whole assembled draft (model plus student words), not only
  the model's half.
- The existing `test_a_durable_full_pass_uses_fixed_paragraph_stages_and_never_writes_
  the_document` stub now writes ≈target words (a model obeying the budget) instead of
  3× target, and its non-failure branch additionally asserts no length warning; its
  fixed-stage/landing/thin-paragraph-failure assertions are unchanged.

These tests exercise behavior (finalization, warning persistence, preservation,
clearance), not prompt-string presence. The live runs below are the model-quality side
of the same contracts.

## Evidence

[manifest.json](local-writer-quality-20260908/manifest.json) records identities: model
label `Qwen3.8-27B`, locality class `non_loopback` (endpoint URL never recorded),
context window 262144, corpus SHAs, and the backend source fingerprint of the baseline
versus the changed source. Six live runs, one case at a time in this lane, each on a
fresh isolated profile and its own desktop-backend bootstrap; 630 s per-case timeout
(the existing control, unchanged — slow reasoning is not treated as a hang).

### Requested vs actual length (deterministic measurement)

| Run | Requested | Assembled whole draft | Outcome |
|---|---|---|---|
| baseline · `full_live_draft_from_student_notes` | 320 words (brief) | 514 (161%) | `completed` — overshoot silently delivered |
| intermediate · `full_live_draft_failed_guard` (first revision) | 320 words (brief) | 445 model-owned / 449 with headings (138–140%) | `failed` at finalize by the hard guard; partial kept; no pending edit — retained as an intermediate artifact |
| after · `full_live_draft_from_student_notes` | 320 words (brief) | 535 (167%) | `completed` **with durable `length_overshoot` warning** (535 against 320) and a reviewable 539-word pending edit |
| baseline / intermediate / after · `holdout_museum_label_revision` (scoped revision) | 360 words (brief; not a completion condition for a scoped edit) | 306 / 307 / 306-word document | `completed`, no warnings |

Per-block (full-draft runs; the plan allocates four 80-word paragraph jobs): baseline
blocks ran 129/120/143/118 words (150–179% of budget); intermediate 106/101/135/103
(126–169%); after 135/116/146/138 (145–183%). The prompt-side repair reduced the
overshoot on the first revision (514 → 445); the final source's model output is still
over budget per paragraph, and the durable warning is what makes that visible — the
student sees the actual against the requested number and a complete, reviewable draft
instead of either a silent `completed` or a failed run.

### Unsupported claims (semantic read of the retained prose)

Judgment rubric, applied proportionately: (a) an actual unsupported population or
causal assertion, or invented personal experience, is a real defect; (b) clearly
labeled proposed future work is a legitimate student recommendation, not a source
claim; (c) the student's own stated experience from their notes is legitimate material;
(d) an epistemic limit framed imprecisely (e.g. phrased as a property of a population)
is imprecise framing, judged as such — not a positive unsupported assertion.

Baseline 514-word candidate (re-judged under this rubric):
- No positive unsupported population or causal assertion, and no invented personal
  experience: "I have stood at the east gate wondering whether to wait or walk" is the
  student's own note.
- Imprecise framing (one instance): "cannot establish how much of the broader student
  population **rejected the service** or why they chose to walk" — the sentence is an
  epistemic limit (what the data cannot establish), not a claim that a population
  rejected anything; the "rejected" framing is the sources' silence dressed up as a
  population property. Proportionate: imprecise, not fabricated.
- Legitimate proposal (previously misjudged as a fabricated method): "a feasible next
  step is to pair the boarding count with a brief, anonymous check-in at the east gate"
  — a clearly labeled proposed future method, the student's own recommendation, which
  the sources neither state nor contradict.
- Legitimate scoping: "We should not invent cost or emissions figures that were not
  collected" — a methodological boundary the student sets on the memo, grounded in the
  sources' actual gap.

After 535-word candidate (final source):
- No unsupported population or causal assertion; no invented personal experience (the
  student's own gate observation is retained and correctly framed as not substituting
  for institutional data).
- Epistemic limits framed as limits: "cannot generalize the findings to all students",
  "leaving demand among the broader student population unmeasured".
- Proposals labeled as proposals: the one-term extension is "a conditional step to
  close this evidence gap"; "I propose we distribute a short survey to a broader group,
  including those who do not currently use the shuttle".
- Cost/emissions stated as not collected, not claimed. The imprecise "rejected"
  framing from the baseline does not recur.

The same-model score is supporting evidence only; an independent or human read of the
prose in `docs/local-writer-quality-20260908/` is the intended next check.

### Preservation control (scoped rewrite)

`holdout_museum_label_revision` (baseline, intermediate, and after): the unsupported
"improved understanding by 75 percent" claim is corrected in all three; the 27-of-36
result and the 30-second task are retained accurately; protected passages 3/3
preserved in every run; no forbidden literal hits; no excerpt support violations; the
student body is byte-identical (`body_unchanged: true`). The writer-side repair does
not perturb scoped-revision behavior — the live warning never applies to the legacy
section pipeline.

### Deterministic vs model quality

- Deterministic (code, verified by tests): the whole-draft length warning fires above
  110% of an explicit request, is inert without one, clears on stale paths, and never
  withholds the reviewable pending edit; the thin-paragraph floor, same-cap assessment
  recovery, cancellation, citation-to-listed-sources normalization, and the pending-edit
  creation path are untouched and re-verified by the suite.
- Real-model quality (not deterministic): on the final source the draft still
  overshoots each paragraph budget (145–183% of the 80-word targets; 535 words against
  a 320-word request), so the 320-word request produces a completed run with a visible
  warning and a reviewable over-length draft rather than an in-band deliverable. This
  is a useful bounded improvement — the overshoot is now a stated, reviewable fact
  attached to a usable draft — not a fix of the model's length discipline, which is
  disclosed rather than papered over.

## Checks executed

- `uv run --no-sync pytest backend/tests/test_writer_*.py backend/tests/test_live_drafts.py
  backend/tests/test_release_writer_regressions.py backend/tests/test_prompts.py`
  (22 writer-specific files) — 405 passed.
- Full suite on the pristine `07ad451` baseline tree — 3508 passed, 1 skipped
  (independently re-run on the baseline source; note that the 3379-passed count cited
  in [writer-semantic-followup.md](writer-semantic-followup.md) is the historical count
  of the earlier `e2857f6` candidate, not of current main).
- Full suite on the changed tree — 3513 passed, 1 skipped (3508 plus 5 net-new tests:
  one prompt-contract test and four live-length behavior tests).
- Six live bounded runs on the isolated endpoint (two per source state: baseline,
  intermediate, after), one at a time in this lane; per-case timeouts 630 s; all
  recorded, none timed out.
- `uv run --no-sync ruff check` / `ruff format` clean on the changed files;
  `scripts/check_docs.py` and `scripts/check_active_references.py` pass.

## Raw and compact evidence

- Compact sanitized evidence (this directory):
  [local-writer-quality-20260908/](local-writer-quality-20260908/manifest.json) — manifest
  plus one compact JSON per run (requested/actual length, per-block words, preservation
  metrics, run identity, and the **full assembled proposal text for every attempt**,
  faithfully derived from the preserved blocks — heading dedupe plus the pipeline's
  mathnorm, identical to the pending proposal where one exists). The intermediate
  directory holds the first revision's failed-guard run with its retained partial
  intact, so the history is preserved rather than replaced.
- Raw transient receipts (outside the repository):
  `/Users/ofhd/Developer/lyra-local-quality-evidence-20260908/{baseline,
  baseline-heldout,after-full,after-heldout,after-full-v2,after-heldout-v2}/` — full
  `eval_writer.py` case JSONs and `report.json` (`after-full`/`after-heldout` are the
  intermediate first-revision runs). The evaluation config DB (endpoint settings only)
  is in the same directory under `config/`, mode 0600; no endpoint URL or credential
  appears anywhere in the retained evidence — locality class only.

## Parent independent review

Codex read the full final proposal against the synthetic sources. The warning and
reviewable pending edit are useful software improvements; **PLA-153 remains open for
semantic quality and length adherence**. The final proposal still needs substantive
revision:

- "higher volume rather than a growth in ridership" implies unique ridership did not
  grow. Trip counts cannot establish whether unique ridership grew or stayed constant.
- Missing cost/emissions measurements are offered as a reason the service cannot be
  assumed to solve attendance. Those measurements do not answer the attendance question.
- The proposed wider survey is a legitimate recommendation, but it does not by itself
  ensure decisions reflect actual usage patterns; the survey describes intended barriers
  and preferences. Its benefit should be stated conditionally.

These findings supersede any agent implication that the final run demonstrates resolved
factual grounding. The warning reports paragraph words (535); the pending Markdown
proposal including its headings has 539 whitespace-delimited words. Neither meets the
320-word request. Preserved student text and successful delivery are separate successes.

## Remaining limitations

- This model still overshoots per-paragraph budgets on the full-draft case, so an
  explicit 320-word request currently yields a completed run with a durable warning
  and a reviewable 535-word draft, not an in-band draft. The prompt keeps the budget
  bound ("within 10 percent") as guidance; a bounded tightening step for over-length
  blocks would be a future pass (out of scope here — no automatic rewriting loop was
  added).
- Independent review still found unsupported inferences and methodological reasoning
  in the final proposal, listed above. No overall semantic-quality pass is established.
- One full-draft case and one held-out case per source state: this is regression
  measurement, not quality across disciplines or student populations, and human review
  remains not run.

## Not done (out of scope for this lane)

No commits; no desktop app build/launch/quit (Codex owns integration); no rubric,
corpus, or fixture changes; no frontend/release/credential/transport changes;
Open PRs #89/#90/#91 not touched.
