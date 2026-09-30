# Adaptive education chat: bounded production-path evaluation

**Status: quality gate failed.** This is exploratory evidence for PLA-574/PLA-461,
not acceptance of the candidate. The exact vector-space question remains far too
long; first-step help still proceeds through the proof; some worked replies contain
false intermediate claims. The owner has not reviewed these outputs. Historical
Guide/Show evidence remains in its original files.

## Setup and retained outputs

The baseline is clean main `c52ac698c62f10cfaf70face1aba3305bb8240ae`.
The two candidate runs use the isolated `codex/pla574-461-chat` worktree; their
source hashes are in the metadata. Candidate v1 is a diagnostic revision, followed
by the more explicit v2 prompt. All turns used `scripts/eval_tutor.py run --surface
class_chat`: the actual class-agent planner, offered tool schemas, tool loop, and
terminal answer, with the harness's disposable synthetic class and session. The
normal app was not launched or changed. The installed database was read only for
the already configured endpoint settings; no user documents entered the eval.

The configured model was `Qwen3.8-Flash-Next`; `/v1/models` returned that identity
at evaluation time. The private endpoint URL is represented by SHA-256
`0850d34c3024f015b5ff6bf4687d788f0d302b57e2f0b64119dbc02ed0fa4fb8`.
The configured context window was 262,144 tokens, not independently tested as the
server maximum. The stored tool-support verdict was unknown. The tool loop used
temperature 0, `max_tokens=65536`, max depth 24, and a 600-second deadline; no
answer hit a stop limit. All three runs used the same endpoint and settings.

| Run | Full terminal answers, stop reasons, tool calls | Configuration and hashes |
|---|---|---|
| Baseline, six cases | [baseline-runs.json](baseline-runs.json) | [baseline-meta.json](baseline-meta.json) |
| Candidate v1, ten cases | [candidate-v1-runs.json](candidate-v1-runs.json) | [candidate-v1-meta.json](candidate-v1-meta.json) |
| Candidate v2, ten cases | [candidate-v2-runs.json](candidate-v2-runs.json) | [candidate-v2-meta.json](candidate-v2-meta.json) |

The baseline used [baseline-corpus.json](baseline-corpus.json), a copy of the
new synthetic cases with the legacy prompt-contract version set to 2. Candidate v1
used [candidate-corpus-run.json](candidate-corpus-run.json); one rubric wording was
corrected afterward, so that exact snapshot is retained. The final corpus is
`scripts/eval_corpora/tutor_semantic.json` (2.0.0, contract 3). V2's recorded
corpus, prompt, route-prompt and harness hashes match the branch files.

## Direct reading against the rubric

I read terminal answers and tool traces, starting with mathematical correctness,
then exact request, useful next step, proportionality, and source claims. Word counts
are supporting measurements. This is the implementer's review; independent review
is still required before any acceptance claim.

| Case | Baseline | Candidate v1 | Candidate v2 | Assessment |
|---|---:|---:|---:|---|
| Exact vector-space definition | 590 words | 428 | 603 | **Fail.** V2 lists all axioms, qualifications, examples, and subspace advice after a conversational definition request. It also searches empty uploads. |
| Polynomial vector-space “how do I start?” | 472 | 329 | 272 | **Fail.** V2 performs the closure calculations and effectively completes the subspace test instead of stopping after one setup move. |
| Held-out plain-English eigenvector | 364 | 182 | 228 | **Fail proportionality.** The core definition is sound, but V2 adds notation, edge cases and multiple examples. |
| Formal vector-space definition | 532 | 415 | 477 | **Fail correctness.** V2 lists the axioms but falsely says the scalar-identity axiom distinguishes vector spaces from modules. Modules over unital rings also have unit action. |
| Convolution full solution | 581 | 255 | 229 | **Pass on the reviewed mathematical result.** V2 derives the correct three branches and honors the explicit step-by-step request. |
| Convolution attempt diagnosis | 281 | 211 | 242 | **Fail correctness.** V2 identifies the dropped $e^{\tau}$, then gives a wrong corrected integration lower limit $\max(0,t-1)$ in its original $\tau$ formulation. Its later piecewise result is right, so the transcript is internally inconsistent. |
| Scalar-multiplication follow-up | — | 247 | 195 | **Fail scope.** V2 re-lists axioms and adds a field/dimension tangent to a narrow clarification. |
| Multi-turn narrow zero-polynomial follow-up | — | 173 | 199 | **Fail scope.** It answers the point but adds several paragraphs after “Answer just that point.” |
| Held-out attempted algebra step | — | 112 | 88 | **Useful partial pass.** It correctly diagnoses the undivided right side and gets $x=7$; the reply could more explicitly affirm that dividing both sides by 2 was valid. |
| Explicit request for depth on negative eigenvalues | — | 806 | 905 | **Core derivation appears correct; scope concern.** The length is allowed by the request, but V2 branches into global orientation, characteristic equations and complex eigenvalues beyond the requested geometric explanation. V1 has a malformed norm equation. Thirteen tool calls did not certify all surrounding prose. |

## Limits and next gate

The key quality criterion has not passed, so no further prompt tuning or live runs
were launched in this bounded pass. The retained runs are single observations per
case, not repeat distributions. Source-scoped document tools were offered, but this
corpus had no synthetic uploaded document; grounded physical-page/nickname behavior
must be evaluated after PLA-576 integration. A live tool-less endpoint run was not
performed; offline route tests cover its fallback and shared contract. No native
acceptance or independent human review was performed in this worktree. PLA-461 stays
open until the critical failures are repaired and independently reviewed on the
production path.
