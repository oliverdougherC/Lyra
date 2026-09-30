# Adaptive education chat: bounded production-path evaluation

**Status: quality gate not met.** This is exploratory evidence for PLA-574/PLA-461,
not acceptance of the candidate. The original v3 failures, R5 outputs and R3 prompt
experiments remain separate. The R3 candidate failed repeated direct reading and its
prompt/source changes were reverted; the R5 source contract 4 remains in the PR. The
owner has not reviewed these outputs. Historical Guide/Show evidence remains in its
original files.

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
equality in a multi-turn polynomial proof. The R5 application source was
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

## Selected-page delivery correction

The R5 application source was `d3eae5b3a1fa5dde3f733a9b4a0f67dbe6691a0d`.
It repairs two failures found on the reconciled PR #104 head at an 8,192-token window:
an explicitly named text page spent context room on an unnecessary image and then
could not carry a successful page-read tool reply (HTTP 503, `context_overflow`);
a requested matrix image reached the model while its short native text was absent
from the system context. Instrumented baseline planning charged 1,823 tool-schema
tokens, 1,640 system tokens and an attached image, leaving zero retrieval room.
The correction retains images for scans, figures and layout requests, omits only
optional response-shape examples and condenses unavailable-capability labels on
attached-image turns, reads an explicitly named physical text page when its indexed
chunk starts on another page, and reserves up to 1,024 tokens for a text-page tool
continuation. The selected document ID still bounds both direct text and tool reads.
An inventory-provided display label supplies the page citation, so an effective
nickname can be used after PLA-576 integration.

Local exact-source checks: 3,930 backend tests passed / one skipped; the five focused
upload-to-chat browser acceptance cases passed, including both formerly failing
cases; TypeScript, targeted ESLint, Ruff, docs links and active-reference checks
passed. Hosted PR #104 full-stack acceptance passed on
[run 36662208691](https://github.com/oliverdougherC/Lyra/actions/runs/36662208691).
Its Rust tests passed, but the audit-tool installation job failed while the hosted
runner could not resolve `index.crates.io`; this is a separate CI infrastructure
failure, not semantic acceptance or native-app verification.

Two clean production `class_chat` reruns on that application source retained all
16 terminal answers, tools and stop reasons:
[A answers](delivery-r5-a-runs.json), [A metadata](delivery-r5-a-meta.json),
[B answers](delivery-r5-b-runs.json), [B metadata](delivery-r5-b-meta.json).
They used the same configured Qwen3.8-Flash-Next endpoint and settings as C/D.
For no-document cases, the assembled system and full first-request hashes match
the prior C/D runs despite route-source changes; these are fresh observations,
not reused v2 or v3 outputs. All cases stopped `completed`.

| Request | A / B words | Direct reading of final answers |
|---|---:|---|
| Conversational vector-space definition | 125 / 136 | Correct core definition, but both enumerate the axioms and related examples after a conversational question; brevity still varies. |
| Polynomial first move | 104 / 99 | B gives one useful addition-closure move; A also asks for scalar multiplication, beyond one move. |
| Convolution attempt | 171 / 186 | Both identify the actual lost $\tau$ and valid overlap. Both add the second limit error and more explanation than requested. |
| Explicit full convolution | 281 / 225 | Both derive the correct piecewise answer. A appends an overgeneralized “number of cases = pulse edges + 1” rule; B's continuity aside omits the integrability conditions behind its claim. Core solution is sound, auxiliary advice is not a general rule. |
| Held-out detailed negative eigenvalue | 936 / 768 | Both distinguish the discrete sign flip from continuous $e^{\lambda t}v$ decay, repairing the previous C error. Both add substantial unrequested applications; B's determinant-sign claim omits the zero-eigenvalue exception. The requested derivation and geometric example are correct on direct reading. |
| Formal vector definition / scalar follow-up | 349 / 88 (A only) | Formal answer covers the axioms. The scalar follow-up prints the wrong codomain $F\times V\to F$ before correcting itself to $F\times V\to V$, an avoidable mathematical error in a narrow answer. |
| Multi-turn full proof / narrow zero follow-up | 399 / 106 (A only) | The full polynomial proof is correct but uses a long axiom-by-axiom route; the narrow follow-up explains the identity correctly with more context than requested. |

These reruns show the page-evidence delivery repair did not solve the remaining
response-quality variance. The rubric is direct implementer review with subsequent
independent corrections to the earlier C/D findings; it is not owner or independent
human acceptance. PLA-461 remains **In Progress**. The combined synthetic
nicknamed-source live case and isolated signed-app acceptance remain separate gates.

## R3 scope correction experiments

The R5 A/B answers above are the current pre-R3 source evidence; the old v3 failures
below are historical. The working hypothesis was that a long contract repeatedly
enumerating unwanted answer shapes, together with the class agent's broad verification
cue, encouraged extra axioms and checks. Four small production-route probes changed
only the education wording and, from probe 2 onward, the class-agent's tool/verification
scope wording. Each used the same configured endpoint and synthetic environment. The
records include the exact prompt-source hashes and full answers; diagnostic source was
dirty while variants were compared, so none is claimed as exact-commit acceptance.

| Probe | Evidence | Direct reading and decision |
|---|---|---|
| 1 | [answers](r3-probe-1-runs.json), [metadata](r3-probe-1-meta.json) | Shorter positive contract alone: the concept became 327 words, first-step help 229, and the attempt became a full worked solution with checks. Rejected. |
| 2 | [answers](r3-probe-2-runs.json), [metadata](r3-probe-2-meta.json) | Added pre-tool scope decision: fewer tool calls, but the concept was 311 words and the narrow scalar reply re-listed axioms. Rejected. |
| 3 | [answers](r3-probe-3-runs.json), [metadata](r3-probe-3-meta.json) | Stronger stopping points: concept 70 words, correct scalar codomain, and a sound core convolution derivation. The concept still compresses an axiom inventory into a paragraph; attempt and full answer still add unrequested material. Selected for exact-source repeats, then rejected on that evidence. |
| 4 | [answers](r3-probe-4-runs.json), [metadata](r3-probe-4-meta.json) | Tighter wording shortened the attempt but the full solution printed an unusable $\max(0,\cdot)$ lower bound and optional checks. Rejected. |

No word count is an acceptance rule. The problem in probe 4 is a contradictory
displayed step despite a correct final piecewise result.

### Exact-source repeat and disposition

The probe-3 wording was committed as `c0b5edcd76ca82c7efeb3cc8ca11621f3486adbc`
and run twice on the production `class_chat` planner/tool loop with a disposable
synthetic class. Both [A answers](r3-final-a-runs.json) and [B answers](r3-final-b-runs.json)
completed all nine cases. [A metadata](r3-final-a-meta.json) and
[B metadata](r3-final-b-meta.json) confirm clean source, identical prompt/agent hashes,
the existing configured Qwen3.8-Flash-Next endpoint and the
[corpus 2.2.0/contract 5 candidate](r3-candidate-corpus.json). Each complete answer
was read for scope and correctness.

| Request | A / B words | Direct reading |
|---|---:|---|
| Conversational vector-space definition | 142 / 123 | Both still enumerate axioms and append unrequested applications. This is no durable gain over R5's 125 / 136 words. |
| Held-out plain-English eigenvector | 145 / 132 | Correct core concept and nonzero condition, with extra examples and applications. |
| Scalar and zero-polynomial narrow follow-ups | 131 / 82; 141 / 132 | Scalar codomain is correct, but both scalar replies re-list compatibility axioms; zero replies expand beyond the one point. |
| Polynomial first-step help | 179 / 187 | Both list a sequence of subspace checks and partially perform the setup instead of leaving one move. |
| Convolution attempt diagnosis | 238 / 208 | Both identify the dropped $e^{\tau}$ but add the second error and much of the solution. B again prints an unusable $\max(0,\cdot)$ lower bound. |
| Explicit full convolution | 352 / 285 | Both derive the correct piecewise core. Optional checks and alternate routes remain; B's claim that convolution with any $L^1$ kernel cannot jump is not justified generally. |
| Formal vector definition | 386 / 465 | The required axioms and field are present; A appends an unrelated malformed LaTeX identity. |
| Detailed negative-eigenvalue explanation | 1,096 / 926 | The negative-eigenvalue derivation and matrix examples are sound. Tangents add false or unqualified statements, including A's claim that a one-dimensional linear map has two degrees of freedom and B's universal determinant-sign claim without a zero-eigenvalue exception. |

The prompt experiment did not reliably improve the R5 behavior and introduced
new correctness defects. Its application changes were reverted before the PR handoff;
the exact probe and repeated transcripts remain as historical evidence. PLA-461 and
PLA-574 remain **In Progress**. No second model pass, hard answer cap, fixture-specific
rule or provider comparison was added. R5 A/B above are again the current-source live
observations, with their original scope and auxiliary-correctness limits.

## Original v3 comparison

The baseline used [baseline-corpus.json](baseline-corpus.json), a copy of the
new synthetic cases with the legacy prompt-contract version set to 2. Candidate v1
used [candidate-corpus-run.json](candidate-corpus-run.json); one rubric wording was
corrected afterward, so that exact snapshot is retained. The v3 corpus was
`scripts/eval_corpora/tutor_semantic.json` at version 2.0.0, contract 3. V3's
recorded corpus, prompt, route-prompt and harness hashes identify that historical
branch revision, not the current source. The current R5 corpus is 2.1.0,
contract 4. The rejected R3 experiment used corpus 2.2.0, contract 5.

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
