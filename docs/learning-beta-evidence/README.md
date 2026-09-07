# Learning beta candidate evidence — September 6–7, 2026

**The tested Qwen3.8-27B configuration has not passed the learning beta gate.** PR #80
implements demonstrated fixes and retains improvements and repeated failures. No model,
human, or final integrated-candidate acceptance is asserted.

Baseline: current main `e96bf4886977c648f9e7905c7807c806b1ae7a80`, including merged
#73/#74/#75/#77/#78. Implementation revisions are `f460dbe` (initial root fixes), `700d02f`
(cross-topic card memory), and `49d78ae` (complete final tutor answers and checked derivations), then `3f9c84f` (provisional output cap), and `b6246d8` (scope that cap to flashcards), and `519c874` (preserve evidence before optional memory).
Evidence commits after these revisions do not retroactively retest earlier runs.

## Outcomes and issue matrix

| Issue | Implemented | Verified | Failed / blocked | Not run |
| --- | --- | --- | --- | --- |
| PLA-461 | Valid-work diagnosis; scoped verification; self-contained terminal answers; preserve conditions when simplifying | Production class_chat planner/tool loop; baseline + repeated remediation + fresh transfer; both repaired Show cases pass twice | False verification claim twice, osmosis misconception twice, eigenvalue polynomial error once; attempt-feedback criterion remains unmet twice (pedagogical judgment). Configuration gate **not passed** | Human acceptance; other providers; full latest integrated packaged-candidate corpus |
| PLA-150 | Include all joint subquestions in verification; meaningful-check instructions | 28 live solves, four segmentation passes; all three subquestions visible in both final repeats versus none at baseline; correct retained terminal answers in independent review | Conceptual placeholder tool calls persist in all four final conceptual runs; human acceptance open | Authenticated providers; OCR/course retrieval; composing segmentation output into solves; full final integrated-candidate corpus |
| PLA-151 | Standalone/source-grounded practice; evidence sufficiency; strict card strings; bounded cross-topic avoidance context; actual quiz-resume metadata | Real generation/retrieval, source provenance, saved attempts and repeated reviews; final per-artifact results in study review | Final ecology deck failed after 483.488 s with no published cards; second repeat cancelled under the evaluation budget. Human/integrated UI acceptance open | Other providers; packaged generation/source-opening UI; final merged-candidate review |

Read the detailed reviews rather than treating terminal success or a mean score as acceptance:

- [Guide independent-agent review](guide-agent-review.md), [per-case final review](guide-final-review.json),
  and [complete run index with latency](guide-run-index.json).
- [Solver methods, evidence and commands](solver.md), [independent-agent review](solver-agent-review.md),
  and [implementation-owner review](solver-review.json).
- [Study workflow and commands](../study-beta-quality.md), [independent-agent review](study-agent-review.md),
  and raw [final deck](../evidence/study-beta-final-deck.json), [baseline quiz](../evidence/study-beta-baseline-quiz.json),
  [baseline deck](../evidence/study-beta-baseline-deck.json), and
  [intermediate candidate](../evidence/study-beta-improved.json) evidence.
- [Signed desktop evidence](desktop.json). A signed frozen smoke pass is not packaged semantic acceptance.

## Critical rubric v1 and review separation

Every critical case must pass on every retained repeat. A failure remains visible by case and
run; an aggregate or model judge cannot override it. Critical failures include wrong facts,
incorrect worked equalities, unsupported verification claims, omitted requested work, evasive
or disproportionate tutoring, unanswerable/ambiguous practice, unsupported source claims,
semantic duplication, and fabricated Ready content from insufficient material.

Deterministic tests prove their specific contracts. The tutor's model self-judge marked the
first baseline **13/13 pass**, while independent agent review found **four critical failures**.
That disagreement is retained in baseline grades and the independent review. Final judgments
are independent-agent review, not human review. Actual human review remains **not run**.

Tutor corpus 1.2.0 corrects an old rubric error: `exp(-t)` may be factored outside an integral
over tau; discarding `exp(tau)` is the error. Both baseline and candidate use that corrected
criterion. This rubric repair is not a production quality gain. The initial six-case held-out
set was frozen before tuning. Osmosis later exposed a failure and became a regression case;
it is no longer called an untouched test. A fresh three-case transfer corpus was then frozen
and passed both repeats. No fixture-specific production rule was added.

## Configuration and privacy

The explicitly authorized endpoint advertised `Qwen3.8-27B` from its model-list operation and
accepted unauthenticated requests. Only a remote/non-loopback locality class is retained;
URLs, keys and student data are absent. Its configured window is **262144 tokens**, not an
independently measured maximum. The old source settings row recorded tool support, but the
isolated modern settings copy lacked a bound probe result, so the production resolver treated
support as **unknown**. Actual successful tool dispatches are retained; no capability was
silently certified. Authenticated configurations are unsupported by the new synthetic
solver/study runners until an isolated credential adapter is provided.

Tool loops use the production deterministic temperature, tool budget and reserved output
ceiling; each record retains the parameters it observes. Unspecified sampling settings are
provider defaults, not guessed numeric values. The final flashcard caller caps output at 8192 tokens,
never above the existing context reserve; quiz/topic requests retain their original reserve
(successful quizzes exceeded 8192). Earlier large-window flashcard runs allowed 65536. Input
reservation and truncated-JSON rejection remain intact. App version is `0.2.0-beta.0` (Python
`0.2.0b0`), build `3.0.1`. Source/corpus/rubric hashes and exact assembled prompt hashes are
retained where available. The first baseline predates expanded transcript instrumentation;
its tool evidence contains names/success flags only, honestly identified in review.

All sources and conversations are synthetic. Profiles, caches, helper state and credentials
are isolated; Keychain is disabled in model evaluation. Runners execute serially internally,
with overlapping bounded work lanes (up to five learning requests during baseline/grading
and implementation work, plus separately coordinated writer traffic). Latency is observed
under shared service contention; it does not establish a causal speed improvement.

## Reproduce tutor behavior

Use a separately authorized **isolated settings database**, not a student profile. Set unique
`LYRA_DATA_DIR`, `LYRA_CACHE_DIR`, `LYRA_LOGS_DIR`, `LYRA_MODELS_DIR`, and
`PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring`. The configured endpoint must be usable
without production credentials for these retained runs. Use a fresh output directory per
repeat to preserve rather than overwrite evidence.

```bash
uv sync --python 3.12 --extra dev --extra packaging
uv run python scripts/eval_tutor.py run --surface class_chat \
  --source-db "$EVAL_CONFIG_DB" --workspace "$EVAL_OUTPUT/tutor-full-1"
uv run python scripts/eval_tutor.py run --surface class_chat \
  --source-db "$EVAL_CONFIG_DB" --workspace "$EVAL_OUTPUT/tutor-transfer-1" \
  --corpus scripts/eval_corpora/tutor_beta_transfer.json
uv run python scripts/eval_tutor.py run --surface class_chat \
  --source-db "$EVAL_CONFIG_DB" --workspace "$EVAL_OUTPUT/osmosis-1" \
  --corpus scripts/eval_corpora/tutor_beta_heldout.json --case simpler-osmosis
# Repeat into new *-2 directories, retaining every failure.
# Optional supporting model self-judge, never the acceptance gate:
uv run python scripts/eval_tutor.py grade \
  --source-db "$EVAL_CONFIG_DB" --workspace "$EVAL_OUTPUT/tutor-full-1"
uv run python scripts/eval_tutor.py report --workspace "$EVAL_OUTPUT/tutor-full-1"
```

The final remediation ran seven cases twice: `no-idea-how-to-start`,
`attempt-where-did-i-go-wrong`, `just-tell-me-the-answer`, `explain-that-more-simply`,
`dont-ask-me-questions-teach-it`, `show-convolution-worked`, and `show-what-is-a-derivative`.
Each can be selected with repeated `--case` flags. It also ran osmosis twice and all three
fresh transfer cases twice. Earlier complete and intermediate runs remain in the run index.

For the release owner's immutable integrated candidate, rerun the complete tutor corpus twice,
all regression/transfer cases twice, the solver corpus with repeated segmentation, and all
study domains/kinds twice, using the same authorized configuration and separate workspaces.
Then conduct actual human review and integrated source-opening/resume/cancellation checks.
Do not waive the currently failed semantic gates based on this PR's software checks.

## Software verification

- Final production `519c874`: `uv run python -m pytest backend/tests -q` — **3183 passed, 1 skipped**.
  The earlier `700d02f` run passed 3176 tests with one skip.
  The initial run exposed two obsolete fixed-size test assumptions; dynamic boundary fixtures
  now retain the same context/output limits and assert actual boundary behavior.
- `49d78ae`: `uv run python -m pytest backend/tests/test_api_agent_chat.py
  backend/tests/test_agent_chat_streaming.py backend/tests/test_prompts.py
  backend/tests/test_api_chat.py -q` — **196 passed** after the final tutor instructions.
- Evaluation evidence tests: **27 passed** after adding exact prompt/grading hashes.
- Frontend unchanged: `pnpm test` — **1014 passed**; `pnpm typecheck`, `pnpm lint`, `pnpm build` passed.
- `uv run ruff check backend scripts`, `uv run ruff format --check backend scripts`,
  `uv run python scripts/check_docs.py`, and `uv run python scripts/check_active_references.py` passed.
- Final study budget/source-priority changes: **269 study tests passed**, including failed-before
  large-window output and small-window evidence-admission regressions. Controlled replay of the captured malformed call is reported separately;
  it cannot establish a complete ecology deck pass.
- Desktop final production `519c874`: PyInstaller rebuild, staged sidecar, Tauri app bundle, stable development
  signing, native architecture/deployment verification and signed frozen smoke passed. Native
  startup showed the empty Classes screen; app/backend environment selectors matched the isolated
  profile, SQLite integrity was clean, and both processes exited after normal Cmd-Q.

[Local deployment](../local-deployment.md) gives the packaging commands. Invoke pnpm from
`frontend/`; running `pnpm --dir frontend` from the root selected a mismatched Corepack version
on this host and was corrected without changing dependencies.
