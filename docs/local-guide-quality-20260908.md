# PLA-461 local Guide quality pass — September 8, 2026

> Dated workstream evidence, local implementation lane. The change is a **candidate**
> two-bullet wording repair in the Guide mode prompt (`backend/llm/prompts.py`) retained for
> parent integration review — the parent has not integrated, built, or released it.
> All live measurements ran on the production `class_chat` planner/tool loop
> (`scripts/eval_tutor.py --surface class_chat`) against the authorized Qwen3.8-27B endpoint.
> **All evidence here is single-configuration, exploratory, and qualified. PLA-461 stays
> open.** No model, human, packaged-candidate, or issue completion is asserted; the parent's
> independent reading of the retained terminal text is recorded in
> [Parent independent review](#parent-independent-review) and differs from the same-model
> scores.

## Scope and ownership

- Owned edits: `backend/llm/prompts.py` (Guide mode section only), `backend/tests/test_prompts.py`
  (tutor prompt-surface tests). Candidate evidence: this document, `docs/local-guide-quality-20260908/`
  (sanitized `summary.json`, full sanitized `transcripts.json`, versioned held-out fixture
  `heldout-variant.json`).
- Read-only for this pass: writer/solver/study/LLM transport/frontend modules, the class-agent
  capability layer in `backend/api/routes_agent_chat.py`, all existing corpora, rubrics, and
  historical evidence. No tool or shared safety logic was touched; no new dependencies; no
  keyword answer filtering, canned answers, rubric change, or contract-version bump
  (mode semantics did not move; corpus 1.2.0 and the held-out corpora are unchanged and still
  grade against contract version 2).
- Raw per-run workspaces (terminal text, tool traces, grades) are retained under the
  gitignored `data/local-guide-eval/` of the `Lyra-local-guide` worktree
  (`baseline`, `baseline-heldout`, `fixed-1`, `fixed-2`, `fixed-3`, `fixed-heldout`,
  `controls-heldout`, `controls-show`). Checked-in sanitized evidence:
  [summary.json](local-guide-quality-20260908/summary.json) (compact verdicts + provenance)
  and [transcripts.json](local-guide-quality-20260908/transcripts.json) (full terminal text,
  synthetic case context, tool arguments/results, and grading for every run, plus the
  independent-review block). The original raw reports and the rubric are not rewritten.

## Runtime and configuration identity (sanitized)

- Model `Qwen3.8-27B` (advertised by a read-only `/models` preflight, HTTP 200, on 2026-09-08),
  configured context window 262144 (configured value, not a measured maximum), tool support
  `True` per the settings row. Endpoint locality class: **remote (non-loopback)**; the URL and
  any credential are never retained in evidence.
- Settings were read from a snapshot copy of the installed settings database taken into the
  eval directory; the live app and its database were never opened, modified, or launched.
- Every run used the deterministic disposable eval environment (one fixed class/session, no
  user data, no workspace, no web grant) with an explicit `--eval-db` per run; provider calls
  were strictly serial (one lane).

## Fresh baseline on current main (`07ad451`, pristine tree)

Same-model judge verdicts (supporting only — see the independent review below).

| Case | Terminal | Same-model verdict | What the terminal text showed |
| --- | --- | --- | --- |
| `attempt-where-did-i-go-wrong` | ok, 2 rounds, `cas_integrate` ×2 (both succeeded) | **fail** | Names the lost `τ` dependence and the corrected integrals, but states the retained false rule: "Pulling `e^{-t}` out of the integral is only legal if the integrand is constant in `τ`; it isn't." — must#1 unmet (does not say the integrand retains `e^τ` nor that pulling `e^{-t}` is legal); correctness 1, pedagogical_usefulness 1 |
| `explain-that-more-simply` | ok, 1 round, no tools | **fail** | Echo/fingerprint mechanism ("copies of the fingerprint", "smear each input value"), no overlap/sliding-window picture, no concrete example — must#1 unmet |
| `show-convolution-worked` (Show control) | ok, 2 rounds, `cas_integrate` ×4 | pass (all dimensions 2) | Complete piecewise result, every step named, CAS-verified, window-overlap takeaway |
| `attempt-sum-constant` (new held-out variant, finite sums) | ok, 5 rounds, `cas_evaluate` ×4 | pass (all dimensions 2) | States the correct rule ("the constant comes out as `n`, not `1`"), keeps the student's valid linearity split, CAS checks incl. numeric `n=5` |

The held-out variant (`docs/local-guide-quality-20260908/heldout-variant.json`, frozen before
any edit, never used to choose wording) passed at baseline with the correct constant rule in a
different domain. From this limited sample that is a **plausible diagnosis** — not a causal
proof — that the retained failure is a misattribution (blaming a legal factoring step with an
invented stricter rule) rather than missing rule knowledge.

## Diagnosis

1. **Attempted-solution diagnosis.** The prior bullet asked the model to "state the conditions
   under which a partly correct step is valid". The model did state conditions — invented
   ones. Nothing in the prompt required checking those conditions against the student's
   actual expressions. In the intermediate `fixed-1` run the model's first two
   `cas_integrate` calls (sent with LaTeX-style notation `e^{-(t-tau)}`) were rejected by the
   calculator ("expression contains characters that are not mathematical notation") and the
   reply still carried an invented rule; the repair must make checking the student's step a
   precondition of naming it wrong.
2. **Simpler explanation.** "Explain the same concrete mechanism in plain words, not a new
   analogy" did not stop the model from re-presenting the mechanism as a different, more
   familiar picture (echo) with no example. The corpus item asks for the overlap/
   sliding-window picture "ideally with one concrete small example"; a concrete example of
   the mechanism is what produced the sliding/overlap computation in the fixed runs.
3. **Proportionality/first-step.** No full-solution leak was observed in this pass's cases or
   controls (`start-quadratic` stops at the factor-ready setup; `guide-to-worked` gives the
   full solution because it was explicitly requested).

## The candidate repair (`backend/llm/prompts.py`, Guide mode only)

Two bullet rewrites, no topic names, no examples, no filters — **a candidate for parent
integration review, not yet integrated or built**:

- Simpler: `… Explain the same concrete mechanism in plain words, not a new analogy, and
  show it in one small concrete example. …` (added clause)
- Attempt diagnosis: `… Before naming a step wrong, check what that step actually does to
  the expression: name the part of the student's move that is valid and the part that
  changes the value, and never explain an error by inventing a stricter rule than the
  operation allows. …` (replaces "Preserve valid operations; distinguish an operation from an
  incorrectly applied version of it. State the conditions under which a partly correct step
  is valid.")

The Guide block grows 1,647 → 1,786 characters (+139 ≈ +35 tokens); the small-window budget
tests derive their boundaries from the current prompt and pass. One intermediate wording
(v1, SHA-256 `55f1ab5a…`) asked the model to state the operation's rule from memory; it
improved the simpler case but left the invented rule in the attempt case (retained in
`fixed-1`), so the retained candidate wording (v2, SHA-256 `0f671ce6…`, working diff
`c2479a23…`) is the check-before-diagnosing form. Both are recorded in
[summary.json](local-guide-quality-20260908/summary.json).

Prompt-surface regression tests in `backend/tests/test_prompts.py`:
`test_guide_attempt_diagnosis_checks_the_step_before_naming_it_wrong` (new) and an extended
`test_guide_bounds_start_help_and_reduces_abstraction_for_simplification` (the example
clause). They pin the instructions, not model behavior; the semantic behavior is the live
measurement below.

## Live results on the authorized endpoint

Same-model judge verdicts only (supporting evidence, never acceptance). Full terminal text,
tool arguments/results, and per-item judgments for every run are in
[transcripts.json](local-guide-quality-20260908/transcripts.json).

| Run (source) | Case | Same-model verdict |
| --- | --- | --- |
| fixed-1 (candidate v1) | `attempt-where-did-i-go-wrong` | **fail** — invented rule rephrased ("the `τ` in the exponent means it's not constant with respect to `τ` — it can't come out"); all seven dimensions 2, must#1 unmet. Retained, not reclassified. |
| fixed-1 (candidate v1) | `explain-that-more-simply` | pass — sliding-window averaging example (blip → triangle), CAS-verified overlap areas; all dimensions 2 |
| fixed-1 (candidate v1) | `show-convolution-worked` | pass — byte-identical terminal text to baseline |
| fixed-2 (candidate v2) | `attempt-where-did-i-go-wrong` | pass — **not a reliable independent pass** (see Parent independent review: false unit-impulse claim) |
| fixed-2 (candidate v2) | `explain-that-more-simply` | pass — sliding last-second window with ramp example; correctness 1 |
| fixed-2 (candidate v2) | `show-convolution-worked` | pass — byte-identical to baseline |
| fixed-3 (candidate v2, critical repeat) | `attempt-where-did-i-go-wrong` | pass — third distinct formulation, no invented rule, conditional limits acknowledged; correctness 1 |
| fixed-3 (candidate v2) | `explain-that-more-simply` | pass — byte-identical to fixed-2 |
| fixed-heldout (candidate v2) | `attempt-sum-constant` | pass — "Your split was correct; only the evaluation of `sum 1` was off"; no over-allowing introduced |
| controls (candidate v2) | `start-quadratic` (start-help) | pass — stops at the factor-ready setup, no roots |
| controls (candidate v2) | `attempt-distribution` (attempted solution) | pass — first invalid step (distribution), CAS mismatch + solve + plug-in check |
| controls (candidate v2) | `simpler-osmosis` (conceptual) | pass — **qualified by the parent review** (amount vs. concentration; counterpressure omitted) |
| controls (candidate v2) | `guide-to-worked` (explicit full solution) | pass — full solution with check, honoring the explicit request |
| controls (candidate v2) | `transfer-show-probability` (Show) | pass — 3/10, conditional 2/4, combinations cross-check, CAS match |

Same-model movement on the formerly failing pair: baseline 0/2 → 2/2 on the candidate v2
wording, with the attempt case graded pass in fixed-2 and fixed-3 and the simpler case in
fixed-1/2/3. That is supporting evidence of the candidate's direction, **not** an independent
confirmation that the pair is resolved (below).

## Parent independent review

Independent reading of the retained terminal text (parent, Codex). This section records the
parent's findings; the same-model scores above are listed separately as self-judging.

- **fixed-2 `attempt-where-did-i-go-wrong`: FAIL (independent).** The reply ends "Your
  $t e^{-t}$ would be the answer if $x(t)$ were a unit impulse at the origin, not a unit
  pulse" — mathematically false (δ ∗ h = h = e⁻ᵗu(t), not t e⁻ᵗ) — and it leads with "the
  error is in the upper limit" instead of identifying the dropped `τ` dependence first.
  The same-model pass on this answer is self-judging only and is not an independent pass.
- **fixed-3 `attempt-where-did-i-go-wrong`: improved, with caveats.** The false unit-impulse
  claim is gone and the diagnosis is better (the `τ` in the exponent is named; the `[0, t]`
  limits are acknowledged as valid only while 0 < t < 1). Residual caveats: the verification
  wording ("I checked both integrals" over a calculator that treats `e` as an unresolved
  symbol) and the aside "as a convolution of continuous signals should" (the inputs are
  step-discontinuous).
- **`explain-that-more-simply` (fixed-1/2/3): same-model pass, independent correctness
  concern.** "Older ones contribute less" is not true for arbitrary impulse responses; the
  weight is just the impulse response.
- **`simpler-osmosis` control: qualified pass.** The explanation confuses amount with
  concentration ("more water outside than inside") and omits counterpressure; it is not an
  unqualified correctness pass.
- **Robust controls (independent reading finds them sound):** the Show cases
  (`show-convolution-worked`, byte-stable across runs; `transfer-show-probability`), the
  finite-sum held-out variant (baseline and candidate v2), `attempt-distribution`, and the
  explicit full-solution control `guide-to-worked`.
- **Overall:** the formerly failing pair is **not independently resolved** — there is no
  claim of two reliable consecutive passes or of issue completion. The candidate prompt
  change and its tests are retained for parent integration review. **PLA-461 stays open.**
  All evidence is single-configuration exploratory/qualified.

## Residual limitations (disclosed, not waived)

- **False unit-impulse claim (fixed-2 attempt case).** Independent finding; the same-model
  judge's pass on that answer is self-judging only.
- **Verification-claim looseness (attempt case, fixed-2/3).** The "I checked both integrals"
  claim rests on `cas_integrate` outputs where the calculator treats `e` as an unresolved
  symbol (piecewise outputs with `1/log(e)`); the displayed final expressions are correct,
  but the judge docks correctness 1 in both v2 runs. Tightening the calculator's `e`
  handling lives in tool descriptions (read-only for this pass) — flagged as the next
  candidate.
- **Over-generalizations (simpler case).** "Older ones contribute less" and the
  fixed-3 "continuous signals" aside; correctness 1, no `must` item affected.
- **Osmosis control wording.** Amount/conflation and missing counterpressure (above).
- **Model-variance exposure.** The overlap/sliding picture arrives via the concrete example;
  it is model behavior, not a mechanical guarantee, and a different model or endpoint could
  substitute another picture and fail the strict corpus item.
- **Single configuration.** No other authorized endpoint/model was available; results are
  exploratory evidence about this configured Qwen3.8-27B, not a universal quality verdict.
- **Self-judging.** Same-model verdicts are supporting evidence; the parent's independent
  reading (above) is the review of record for the failed/qualified answers.

## Executed checks

- `uv run --no-sync python -m pytest backend/tests -q` — **3509 passed, 1 skipped** (pristine
  main collects 3509 items; the candidate diff adds exactly one test, and the full suite with
  the diff passes 3509 with 1 skip).
- `uv run --no-sync python -m pytest backend/tests/test_prompts.py backend/tests/test_eval_tutor.py backend/tests/test_tutor_chat_safety.py backend/tests/test_api_agent_chat.py backend/tests/test_api_chat.py -q` — **275 passed** (prompt/eval/tutor/API suites).
- `ruff check` + `ruff format --check` clean on the changed Python files.
- `scripts/check_docs.py` (97+ files) and `scripts/check_active_references.py` clean,
  including after this evidence/documentation revision.
- Live evaluation: 8 runs, 18 case executions, strictly serial, isolated eval databases and
  an isolated settings snapshot (raw workspaces above). No further inference was run during
  this evidence-only revision.

## Changed paths

- `backend/llm/prompts.py` — candidate Guide-mode change: Simpler bullet (concrete-example
  clause) and attempt-diagnosis bullet (check-the-step-before-naming-it-wrong + no
  invented-stricter-rule). Retained for parent integration review.
- `backend/tests/test_prompts.py` — one new prompt-surface test, one extended assertion.
- `docs/local-guide-quality-20260908.md` (this document), `docs/local-guide-quality-20260908/summary.json`,
  `docs/local-guide-quality-20260908/transcripts.json` (full sanitized terminal text, case
  context, tool arguments/results, grading, and the independent-review block),
  `docs/local-guide-quality-20260908/heldout-variant.json` (versioned synthetic fixture).
- One-line index additions: `docs/README.md`, `docs/tutor-prompt-contract.md` (follow-up
  pointer; contract version unchanged).

**Status:** PLA-461 remains open. The candidate change, its tests, and this evidence are
ready for parent integration review; final build, verification, and issue tracking are owned
by the parent.
