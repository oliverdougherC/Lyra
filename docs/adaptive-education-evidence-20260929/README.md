# Adaptive education chat: bounded production-path evaluation

**Status: quality gate failed.** This is exploratory evidence for PLA-574/PLA-461,
not acceptance of the candidate. The exact vector-space question remains far too
long; first-step help still proceeds through the calculation; some worked replies contain
false intermediate claims. The owner has not reviewed these outputs. Historical
Guide/Show evidence remains in its original files.

## Setup and retained outputs

The baseline is clean main `c52ac698c62f10cfaf70face1aba3305bb8240ae`.
The three candidate runs use the isolated `codex/pla574-461-chat` worktree; their
source hashes are in the metadata. V1 is a diagnostic revision; v2 asks more explicitly
for brevity; v3 compacts the same contract to preserve context room for document and
tool continuation. All turns used `scripts/eval_tutor.py run --surface
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
answer hit a stop limit. All four runs used the same endpoint and settings.

| Run | Full terminal answers, stop reasons, tool calls | Configuration and hashes |
|---|---|---|
| Baseline, six cases | [baseline-runs.json](baseline-runs.json) | [baseline-meta.json](baseline-meta.json) |
| Candidate v1, ten cases | [candidate-v1-runs.json](candidate-v1-runs.json) | [candidate-v1-meta.json](candidate-v1-meta.json) |
| Candidate v2, ten cases | [candidate-v2-runs.json](candidate-v2-runs.json) | [candidate-v2-meta.json](candidate-v2-meta.json) |
| Candidate v3, five critical cases | [candidate-v3-runs.json](candidate-v3-runs.json) | [candidate-v3-meta.json](candidate-v3-meta.json) |

The baseline used [baseline-corpus.json](baseline-corpus.json), a copy of the
new synthetic cases with the legacy prompt-contract version set to 2. Candidate v1
used [candidate-corpus-run.json](candidate-corpus-run.json); one rubric wording was
corrected afterward, so that exact snapshot is retained. The final corpus is
`scripts/eval_corpora/tutor_semantic.json` (2.0.0, contract 3). V3's recorded
corpus, prompt, route-prompt and harness hashes match the branch files.

## Direct reading against the rubric

I read terminal answers and tool traces, starting with mathematical correctness,
then exact request, useful next step, proportionality, and source claims. Word counts
are supporting measurements. This is the implementer's review; independent review
is still required before any acceptance claim.

| Case | Baseline | V1 | V2 | V3 | Assessment |
|---|---:|---:|---:|---:|---|
| Exact vector-space definition | 590 words | 428 | 603 | 615 | **Fail.** V3 avoids irrelevant document searches, but gives every axiom, qualifications, examples, and subspace advice after a conversational request. |
| Polynomial vector-space “how do I start?” | 472 | 329 | 272 | 162 | **Fail.** V3 is shorter but carries out addition closure and sketches the remaining checks instead of stopping after one move. |
| Held-out plain-English eigenvector | 364 | 182 | 228 | — | **V2 fails proportionality.** The core definition is sound, but adds notation, edge cases and multiple examples. Not rerun after final compaction. |
| Formal vector-space definition | 532 | 415 | 477 | 533 | **Fail correctness.** V3 says $\mathbb{C}$ over $\mathbb{R}$ has dimension 1; it has dimension 2. V2 falsely distinguishes vector spaces from unital modules by the scalar-identity axiom. |
| Convolution full solution | 581 | 255 | 229 | 314 | **Pass on the reviewed result.** V3 derives the correct three branches and honors the explicit step-by-step request, with some extra checks. |
| Convolution attempt diagnosis | 281 | 211 | 242 | 203 | **Fail correctness.** V3 identifies the dropped $e^{\tau}$ but presents $\max(0,\cdot)$ as an integration lower bound, an unusable formula; V2 gave the wrong bound $\max(0,t-1)$. The correct final branches do not repair the intermediate step. |
| Scalar-multiplication follow-up | — | 247 | 195 | — | **V2 fails scope.** It re-lists axioms and adds a field/dimension tangent. Not rerun after final compaction. |
| Multi-turn narrow zero-polynomial follow-up | — | 173 | 199 | — | **V2 fails scope.** Several paragraphs follow “Answer just that point.” Not rerun after final compaction. |
| Held-out attempted algebra step | — | 112 | 88 | — | **V2 useful partial pass.** It diagnoses the undivided right side and gets $x=7$. Not rerun after final compaction. |
| Explicit request for depth on negative eigenvalues | — | 806 | 905 | — | **V2 core derivation appears correct; scope concern.** The requested depth permits length, but the reply branches into less relevant topics. V1 has a malformed norm equation. Not rerun after final compaction. |

## Limits and next gate

The key quality criterion has not passed, so no further prompt tuning or live runs
were launched after v3. The retained runs are single observations per
case, not repeat distributions. Source-scoped document tools were offered, but this
corpus had no synthetic uploaded document; the local full-stack acceptance tests for
later-page direct grounding, document-tool continuation, and native matrix text/image
passed after the compact prompt. Nickname behavior must be evaluated after PLA-576
integration. A live tool-less endpoint run was not
performed; offline route tests cover its fallback and shared contract. No native
acceptance or independent human review was performed in this worktree. PLA-461 stays
open until the critical failures are repaired and independently reviewed on the
production path.
