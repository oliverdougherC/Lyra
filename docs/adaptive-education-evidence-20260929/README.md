# Adaptive education chat: bounded production-path evaluation

**Status: quality gate failed.** This is exploratory evidence for PLA-574/PLA-461,
not acceptance of the candidate. The original v3 failures and the correction-pass
outputs below are retained separately. The correction makes some conversational answers
compact and preserves full solution branches, but final-source quality still varies
and several answers make false or unsupported auxiliary claims. The owner has not reviewed these
outputs. Historical Guide/Show evidence remains in its original files.

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

## Current-main correction pass

PR #104 was reconciled with main `299d0da7debb22244c484cd7fd779c423866f9c3`
after #100 merged. The first clean prompt revision was
`2a64ab22fbb6acb3d0c990e20230025a0179038e`; its repeated first-pass matrix
exposed a false optional numerical check in a full convolution solution and an invalid
equality in a multi-turn polynomial proof. The final application source is
`d5c2b6a4e3420815e26766b205d33046827e4136`, which directs explicit full answers
toward the shortest complete route while retaining source and tool rules. Contract 4
and corpus 2.1.0 cover definition → example → exercise → full proof → narrow follow-up.
The retained raw records include assembled prompt/source hashes, terminal answers,
tools, stop reasons and configuration. All 21 final-source observations completed.

| Stage | Raw answers and tool records | Metadata |
|---|---|---|
| Diagnostic probes 1–3 | [1](correction-probe-1-runs.json), [2](correction-probe-2-runs.json), [3](correction-probe-3-runs.json) | [1](correction-probe-1-meta.json), [2](correction-probe-2-meta.json), [3](correction-probe-3-meta.json) |
| Diagnostic probes 4–6 | [4](correction-probe-4-runs.json), [5](correction-probe-5-runs.json), [6](correction-probe-6-runs.json) | [4](correction-probe-4-meta.json), [5](correction-probe-5-meta.json), [6](correction-probe-6-meta.json) |
| Clean first revision, A/B | [A](correction-first-pass-a-runs.json), [B](correction-first-pass-b-runs.json) | [A](correction-first-pass-a-meta.json), [B](correction-first-pass-b-meta.json) |
| **Final source, C/D** | [C](correction-final-c-runs.json), [D](correction-final-d-runs.json) | [C](correction-final-c-meta.json), [D](correction-final-d-meta.json) |

Probe 1 changed prompt wording while its cases were running, so its per-case prompt
hashes are diagnostic only. Probes 2–6 used dirty source snapshots and are also not
final evidence. A/B and C/D each used a clean, single source SHA per pair. The already
configured Qwen3.8-Flash-Next endpoint and its hashed URL were unchanged; a separate
read-only `/v1/models` check returned that configured model before the final pass.
The 262,144-token window remained a configured value, not a measured server maximum.
Runs used the production `class_chat` planner and tool loop with a disposable synthetic
class, temperature 0, the existing generation/context guards, and no student documents.
No endpoint URL, key, or coursework is present in the retained JSON.

Direct reading of the **C/D final answers** against correctness, requested scope, and
reading burden gives this bounded assessment. Word counts describe the observations,
not a response cap or automatic pass rule.

| Request | C / D words | Assessment |
|---|---:|---|
| Conversational vector-space definition | 62 / 151 | C is compact and correct; D is correct but re-lists axioms and adds an unasked consequence. Proportionality varies. |
| Formal vector-space definition | 306 / 388 | Complete and mathematically sound in both observations; some extra remarks. |
| Polynomial “how do I start?” | 184 / 109 | Both give a useful generic setup but name addition and scalar closure; C adds a wider survey. D also says zero and inverses are simply inherited by a subset, although their membership still needs to be established. The one-move burden criterion remains unmet. |
| Convolution attempt diagnosis | 160 / 170 | Both identify the lost $\tau$ and use valid bounds or restricted ranges. Both evaluate a branch and mention a second issue beyond the first-error request. |
| Explicit full convolution solution | 311 / 252 | Both derive the correct piecewise result. C includes several unrequested checks. D adds a false general claim that convolution of any two $L^1$ functions with compact/decaying support cannot jump; the particular pulse/exponential result is continuous, but that broad assertion is unjustified. Core derivation passes; the D final answer is not wholly correct. |
| Brine-tank start help | 114 / 138 | Both give a usable opening move; D adds the next balance context. |
| Multi-turn full polynomial proof | 423 / 344 | Complete and correct on direct reading, with surplus axiom-by-axiom work and closing tangents. The prior A run's invalid equality is preserved separately. |
| Scalar-multiplication / narrow zero follow-ups | 148 / 103 (C only) | Correct core points, but both add axioms or nonemptiness discussion beyond the narrow question. |
| Held-out negative-eigenvalue depth request | 695 (C only) | **Correctness failure.** It correctly derives $Av=\lambda v$ and gives a matrix example, then falsely says a negative-eigenvalue mode of the continuous system $\dot x=Ax$ decays *through* the origin to the opposite ray. A continuous mode $e^{\lambda t}v$ retains its sign for finite $t$. |

The other C held-out cases (plain-English eigenvector, algebra-attempt diagnosis,
answer checking, simpler convolution explanation) answered their core questions
without a found critical mathematical error, though several carry avoidable prose.
Independent reading of the D outputs identified the additional continuity and
subset-membership mistakes above after the implementer's first rubric pass. The
false continuous-time claim, these auxiliary mathematical errors, and repeated scope
misses keep PLA-461 **In Progress**. This is not owner or independent human acceptance.
The synthetic nicknamed-source case, final combined signed-app behavior and native
acceptance belong to the later integrated candidate review; none is claimed here.

## Original v3 comparison

The baseline used [baseline-corpus.json](baseline-corpus.json), a copy of the
new synthetic cases with the legacy prompt-contract version set to 2. Candidate v1
used [candidate-corpus-run.json](candidate-corpus-run.json); one rubric wording was
corrected afterward, so that exact snapshot is retained. The v3 corpus was
`scripts/eval_corpora/tutor_semantic.json` at version 2.0.0, contract 3. V3's
recorded corpus, prompt, route-prompt and harness hashes identify that historical
branch revision, not the current source. The current corpus is 2.1.0, contract 4.

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

The current correction pass adds repeated critical observations and held-out/multi-turn
cases after v3, but its final-source quality gate still fails. Source-scoped document
tools were offered; this semantic corpus contains no synthetic uploaded document.
The local full-stack tests cover later-page direct grounding, document-tool
continuation, and native matrix text/image, but the combined nicknamed-source live
case remains for integration. A live tool-less endpoint run was not performed;
offline route tests cover its fallback and shared contract. No native acceptance or
independent human review was performed in this worktree. PLA-461 stays open for the
specific correctness and scope failures above. Green software checks do not waive
that result.
