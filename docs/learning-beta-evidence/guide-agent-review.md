# Guide independent-agent semantic review

Reviewed 2026-09-07. This is **independent-agent review, not human review or provider acceptance**. It reads terminal answers and available tool transcripts rather than adopting the model judge's classifications. No provider calls or production edits were made for this review.

The evaluated candidate (the `guide-final-*` runs, now superseded by a bounded remediation rerun) does **not** pass a zero-critical-semantic-failure gate. Correct first-step help, direct explanations and requested answers coexist with repeated erroneous worked reasoning and a repeated held-out science misconception. Successful requests and CAS results do not override these findings.

## Scope and interpretation

Reviewed corpus `tutor_semantic.json` 1.2.0: baseline1 (13), baseline2 (six), final1 (13), final2 (seven repeated cases). Reviewed frozen `tutor_beta_heldout.json` beta-heldout-1.0.0: heldout1 and heldout2 (six each). The lead reports the held-out corpus was frozen before tuning. Final1/final2/heldout1 record SHA `f460dbe989abdc25ec206e5e31943ccf24bdf00e`; heldout2 records `700d02f63cdec1fb18f4e30a998d58db745ab7a0`. The lead reports identical tutor code at those SHAs. Their metadata declares dirty worktrees; use recorded hashes, not SHA alone, to identify evaluated behavior. Baseline2 records `e96bf4886977c648f9e7905c7807c806b1ae7a80` dirty; baseline1 predates the SHA evidence addition.

Pass means no critical failure identified in that retained answer against its request and explicit rubric. It is not an assertion of universal correctness. Equivalent intuition counts: an echo analogy need not literally say “sliding window.” A question after delivering help is not compulsory questioning. Minor wording, extra detail and unsupported generalization concerns are retained separately. NR means not run in that subset.

Baseline1's model self-judge marked **13/13 pass** in [grades.json](guide-baseline-1/grades.json). Independent review identified **four critical failures** below. Preserve that disagreement; arithmetic aggregation of a model's judgments does not make those judgments independently correct.

## Baseline1 case review

Source: [baseline1 answers](guide-baseline-1/runs.json).

| Case | Independent result | Evidence / noncritical concern |
| --- | --- | --- |
| explain-convolution | Pass | Useful smearing model, correct formulas and numerical example. Extra properties extend beyond the core explanation. |
| what-is-a-derivative | Pass | Immediate intuition, definition and correct example. |
| why-can-they-cancel-these-terms | Pass | Explains substitution rather than cancellation; valid chain-rule reasoning. More detail than the single step requires. |
| no-idea-how-to-start | Pass | Gives both rates, units and ODE setup without unsolicited solved formula. |
| attempt-where-did-i-go-wrong | **Fail** | Says pulling out constant `e^-t` caused the error, although that factorization is valid; the lost factor is `e^tau`. Does not acknowledge the valid setup/limits on `0<=t<=1`. Correct final branches do not fix misleading diagnosis. |
| is-my-answer-correct | Pass | Immediate correct verdict and transform-pair justification. |
| just-tell-me-the-answer | **Fail: numerical correctness** | Correct requested formula, then incorrect volunteered example: `10+10e^-3` is approximately `10.498 kg`, not `10.05 kg`. |
| explain-that-more-simply | Pass | Echo example genuinely lowers abstraction without quiz or new formalism. |
| dont-ask-me-questions-teach-it | Pass | Direct teaching, correct eigenpairs, no question to the student. Optional diagonalization material is excess. |
| intuition-not-the-derivation | **Fail: correctness** | Claims omitting flip necessarily makes a delayed input produce advanced output. Counterexample: `R(t)=integral f(tau)g(tau-t) dtau`; delaying `f` by `a` gives `R(t-a)`, still a delay. Useful age intuition does not establish the asserted time-invariance distinction. |
| five-minutes-before-exam | Pass with concerns | Scannable core facts, but too many secondary items. The exponential pair needs positive decay parameter; step transform needs distribution/principal-value interpretation. |
| show-convolution-worked | **Fail: derivation correctness** | Step1 sets lower bound `max(0,t-0)`, which equals `t` for positive `t`; this contradicts the subsequent nonzero branch. Correct later formulas do not repair an unacknowledged invalid step. |
| show-what-is-a-derivative | Pass | Complete worked example, definition, interpretation and takeaway. |

Baseline1 keeps only tool names/success flags, not arguments/results. Its verification claims therefore cannot be fully audited from this record.

## Repeated baseline and final candidate

Sources: [baseline2](guide-baseline-2/runs.json), [final1](guide-final-1/runs.json), [final2](guide-final-2/runs.json).

| Case | Baseline2 | Final1 | Final2 | Concrete assessment |
| --- | --- | --- | --- | --- |
| explain-convolution | NR | Pass | NR | Direct weighted-overlap explanation and correct width-one moving average. “Center” should be origin in the general shift description; not a critical computation error. |
| what-is-a-derivative | NR | Pass | NR | Direct intuition and actual limit definition. |
| why-can-they-cancel-these-terms | NR | Pass with concerns | NR | Correct requested substitution and CAS derivative. Final “only works when the derivative ... is already sitting in the integrand” is an overly restrictive general rule; substitutions can require rewriting. Long for the requested step. |
| no-idea-how-to-start | Pass | Pass | Pass | Repeatedly supplies rates/setup with no unsolicited full solution. |
| attempt-where-did-i-go-wrong | Pass with concerns | **Fail: partial-attempt criterion** | Pass with concerns | Baseline2 correctly distinguishes constant `e^-t` from lost `e^tau` and recognizes limited validity “in form,” though setup acknowledgement is weak. Final1 acknowledges the setup but broadly rejects `0..t` without crediting its valid interval; this still misses the explicit partial-attempt requirement. Its “can't pull it out as e^-t” is ambiguous whole-expression wording, not itself proof that it prohibits valid factorization. Final2 explicitly factors `e^-t e^tau` and credits bounds on `0<t<1`: a real improvement. Final answers omit boundary values at 0/1; noncritical for diagnosis, but incomplete as full piecewise solutions. |
| is-my-answer-correct | NR | Pass | NR | Immediate verdict and correct mathematical reason. |
| just-tell-me-the-answer | Pass | Pass | Pass | Correct compact formula; incorrect baseline1 numerical aside does not recur. Final CAS compares the derivative to the ODE right side and returns equality. Baseline2's initial `e` symbol mismatch is repaired with `exp`; its simple initial-condition check is directly verifiable although not a separate recorded call. |
| explain-that-more-simply | Pass | Pass with concerns | Pass with concerns | Lower-level echo/drum intuition, no compulsory quiz. “For any system” overgeneralizes convolution's input-output application beyond linear time-invariant systems; the fixed-response setup limits the intended example. |
| dont-ask-me-questions-teach-it | Pass | Pass with concerns | Pass with concerns | Correct core examples, no questions. Final closing calls eigenvalues “directions”/“natural frequencies”; that loose summary confuses eigenvalue with eigenvector and should be tightened. Tools verify example eigenvalues/arithmetic, not the general summary. |
| intuition-not-the-derivation | NR | Pass with concerns | Pass with concerns | Correct age and mirrored-kernel picture; false baseline1 delayed/advanced claim absent. Too long and adds integral/consequences despite request for intuition; not a full formal derivation. |
| five-minutes-before-exam | NR | Pass with concerns | NR | Useful but overbroad sheet (roughly 20 properties/pairs). “Even signal implies real spectrum” needs a real-valued assumption; distributional formulas need their interpretation. Does not establish compactness improvement. |
| show-convolution-worked | Pass with concerns | **Fail: derivation correctness** | **Fail: same derivation error** | Both final runs give antiderivative `-exp(-(t-tau))` and assert `-1+exp(-t)=1-exp(-t)`. Correct antiderivative is positive `exp(-(t-tau))`. This is a repeated wrong step, not merely a different explanation. Baseline2 uses the correct antiderivative. |
| show-what-is-a-derivative | NR | **Fail: retained terminal completeness** | NR | Retained terminal answer starts “The symbolic check agrees” and contains only a derivative example/result, applications and takeaway; it lacks the requested complete definition/limit explanation. Pre-tool prose is not retained here. This proves a terminal-evidence/contract gap, not that the live UI necessarily lost pre-tool streamed explanation; that requires separate interface verification. |

### Why the worked-convolution checks do not clear the failure

In both final runs, two `cas_evaluate` calls compare the two definite integrals with their correct final branch formulas; both return `equal: true`. Neither call checks the negative antiderivative printed in the explanation. The terminal claim that the integrals were checked is narrowly supported, but the student is still taught an invalid intermediate step. The final explanation also says support overlap shrinks after `t=1`: its integration interval remains `[0,1]` and the exponential weights decay. That picture should distinguish decreasing weight from decreasing overlap length.

Baseline2's Show answer had several malformed CAS requests and an unresolved symbolic expression involving lowercase `e`; later `exp` calls genuinely produce both definite integrals and total area 1. Failures were recovered rather than silently treated as checks. Tool recovery costs are visible below.

## Frozen held-out transfer cases

Sources: [heldout1](guide-heldout-1/runs.json), [heldout2](guide-heldout-2/runs.json).

| Case | Heldout1 | Heldout2 | Evidence / limitations |
| --- | --- | --- | --- |
| start-quadratic | Pass | Pass | Divides by 3, moves 5, suggests factoring; does not give roots. |
| attempt-distribution | Pass | Pass | Identifies first invalid distribution, explains both terms receive factor 2, corrects equation and checks result. Full solution is permitted by this case. |
| simpler-osmosis | **Fail: correctness** | **Fail: identical claim** | Plain-language direction/barrier description is useful, but both assert flow continues until the sides are equally concentrated. Osmotic flow can stop when hydrostatic pressure balances osmotic pressure; concentration equality is not guaranteed. The volunteered universal ending teaches a misconception. |
| direct-pvalue | Pass with concerns | Pass with concerns | Correct conditional probability and one-sided coin-tail arithmetic `11/1024`; no student-directed question or practice offer. The example defines the upper-tail event but should explicitly name the one-sided alternative rather than using general “coin may not be fair” wording. Rhetorical questions explaining the definition do not violate a ban on questioning the student. |
| guide-to-worked | Pass | Pass | Fully solves `x=6`, shows adding 2, verifies `5(6-2)=20` with supported tool result. |
| show-to-one-step | Pass within fixture assumptions | Pass within fixture assumptions | Conserved 12 mL solute, new total100mL and setup fraction; no final12%. Corpus explicitly assumes consistent volume percent/additive volumes, and history establishes that convention. Therefore “mL salt” is not a failure for this synthetic case; do not generalize it to unspecified real salt solution percentages. |

The osmosis qualification is supported by [OpenStax Biology 2e, Passive Transport](https://openstax.org/books/biology-2e/pages/5-2-passive-transport): pressure can balance osmotic flow before concentrations equalize. A simple equal-pressure direction picture is appropriate; promising its eventual endpoint without qualification is the problem.

Four held-out terminal answers (starting quadratic, osmosis, p-value, dilution) are byte-identical across repeats; the other two vary only modestly. Repeats therefore show recurrence, not independent sampling across diverse behavior.

## Latency and execution evidence

Wall time includes planning and all model/tool rounds. These are sequential small runs on one authorized configuration, not a provider benchmark. No caching behavior or generation independence was established.

| Run | Cases | Median seconds | Mean seconds | Min–max seconds | Executed tool calls | Failed tool calls |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| Baseline1 | 13 | 10.23 | 12.64 | 1.80–35.64 | 14 | 2 |
| Baseline2 | 6 | 21.71 | 20.45 | 2.49–33.75 | 21 | 7 |
| Final1 | 13 | 14.47 | 13.58 | 4.35–28.99 | 14 | 3 |
| Final2 | 7 | 14.86 | 14.84 | 4.95–31.83 | 11 | 3 |
| Heldout1 | 6 | 5.00 | 5.80 | 1.41–12.67 | 2 | 0 |
| Heldout2 | 6 | 4.72 | 5.41 | 1.41–11.04 | 2 | 0 |

All retained requests have terminal success status, which does not establish semantic success. Baseline1 and final1 use the same13-case set: median latency increases4.24s and mean increases0.94s. Other rows contain different subsets and cannot establish aggregate latency improvement. Small sequential samples do not support confidence intervals or universal model claims.

## Review handoff

After these findings, the lead reported a bounded follow-up addressing final-response self-containment, intermediate sign/equality checks and preservation of conditions in simpler explanations. That follow-up is not evaluated by the tables above. Retain these rows as intermediate failures; new evidence must be reported separately.

The final repeated Show antiderivative failure and repeated held-out osmosis misconception remain critical. Attempt feedback improves in final2 but does not consistently satisfy the partial-attempt criterion in final1. The retained Show derivative terminal requires full-round evidence before concluding whether the issue is missing UI content, incomplete persistence, or incomplete harness capture. None of these can be cleared by the baseline self-judge's13/13 or successful tool flags.

Deterministic harness checks establish evidence integrity only; model self-judging is a separate signal; this document is independent-agent review; actual human review and integrated installed-candidate acceptance remain not run by this reviewer. The release owner must preserve the failures and require a focused rerun/human review rather than silently waiving them.

## Final bounded remediation: 49d78ae

The final bounded intervention was evaluated at `49d78ae0aa03f6c1946b7e2db0d684f7ea9cd451` (metadata: dirty). **The quality gate remains NOT PASSED for this tested configuration.** This is an implemented intervention with demonstrable improvements and residual failures, not an unevaluated proposal. No further prompt tuning is implied by this report.

The previously held-out osmosis case became an explicit regression case after its failure was inspected. A fresh three-case transfer corpus, `beta-transfer-2.0.0`, was frozen after those findings; it is not the original held-out corpus. Neither transfer success nor recovered Show behavior can clear failures elsewhere.

| Case | Remediation1 | Critical evidence or improvement |
| --- | --- | --- |
| no-idea-how-to-start | Pass | Supplies rates, units and ODE setup; no unsolicited final solution. |
| attempt-where-did-i-go-wrong | **Fail: partial-attempt criterion** | Correctly identifies dropped integration-variable dependence and credits the convolution setup, but still rejects `0..t` without acknowledging the valid `0<t<1` interval. This is the explicit acceptance failure, not a demand for a particular praise phrase. It is less misleading than baseline1's criticism of factoring out `e^-t`. |
| just-tell-me-the-answer | **Fail: unsupported verification claim** | Formula is correct, but claims exact derivative verification although the only CAS call returned `equal:false` and `3*(1-log(e))/(10*e**(3*t/100))`. Lowercase `e` was parsed as a symbol; no recorded `exp` retry repaired the check. A reviewer can verify the intended formula independently, but that does not make the assistant's claim about this tool check truthful. |
| explain-that-more-simply | Pass with concern | Plain bell-memory explanation is useful and no quiz. “Convolution is ... the past” assumes a causal response; the example provides that setting but the opening overgeneralizes it. |
| dont-ask-me-questions-teach-it | **Fail: intermediate algebra** | Correct matrix eigenvalues3/6 and eigenvectors, but writes `(5-lambda)(4-lambda)-2 = lambda^2-9lambda+12 = (lambda-3)(lambda-6)`. The constant is18, not12. CAS checks eigenvalues and vector arithmetic, not this printed polynomial. |
| show-convolution-worked | Pass | Correct support interval, positive antiderivative via factoring `e^-t`, correct branches and continuity. Exact tools support both definite integrals and total area1. Prior sign failure does not recur in this answer. |
| show-what-is-a-derivative | Pass | Retained terminal now includes intuition, limit definition, worked difference quotient, result and takeaway; CAS verifies the actual limit. “Cancelling makes the limit exist” is imprecise pedagogical wording; cancellation exposes an existing limit rather than causing it. |

The changed failures matter: no-question teaching now introduces a wrong polynomial while its example remains correct; the direct-answer case now claims a failed symbolic comparison succeeded. A final-expression check cannot validate every sentence or algebraic step generated afterward.

### Osmosis regression and fresh transfer

| Case | First run | Repeat | Evidence |
| --- | --- | --- | --- |
| simpler-osmosis | **Fail** | **Fail** | Both responses say the plain-water side “gets more diluted” while water leaves it, and promise the sides become more similar. Pure water remains pure when water is removed; the sugar/salt is stated unable to cross. The intervention changed the wording without removing the misconception. The “crowded” explanation also substitutes a simplified concentration mechanism for the pressure qualification. |
| transfer-ratio-attempt | Pass | Pass | Locates reversed ratio, keeps proportional multiplication, changes it to milk/flour `2/3`, yields `10/3cups` and checks magnitude. Reusing the student's scaling method constitutes acknowledgement; explicit praise is unnecessary. |
| transfer-simple-feedback | Pass | Pass | Plain thermostat on/off explanation, temperature “near” set point rather than perfectly constant, no student question. Assumes a thermostat controlling suitable heating/cooling; that is the stated example. |
| transfer-show-probability | Pass | Pass | Complete conditional multiplication `3/5 * 2/4 = 3/10` plus combinations check, all arithmetic tool comparisons supported. |

Fresh transfer answers are byte-identical between runs; osmosis regression answers are also identical. These are bounded repeated observations on one configuration, not independent human endorsement or evidence of broad provider robustness. Final run-level classifications, per-case latency and evidence-file hashes are in `guide-final-review.json`.

### Remediation repeat2 and final disposition

Repeat2 still fails the direct-answer verification claim: it produces the same answer and sole `equal:false` CAS result as remediation1. The attempt reply now explicitly restores `e^-t e^tau`, but does not acknowledge validity of the student bounds on the initial interval; its closing attribution of `t e^-t` to both errors also fails to distinguish that interval. The independent partial-attempt criterion remains unmet, with the algebraic diagnosis nevertheless improved.

Repeat2 removes the erroneous eigenvalue polynomial extension and passes that case. Both Show cases pass again: the convolution sign correction persists and the derivative terminal is complete. The second convolution trace returns a Heaviside expression for the full integral rather than the displayed piecewise syntax; it is equivalent on the three intervals, and the two direct branch comparisons plus area check support the worked result. A claim of byte-for-byte matching tool output would be too strong, but the mathematical verification here is supported.

| Case | Remediation1 | Remediation2 |
| --- | --- | --- |
| no-idea-how-to-start | Pass | Pass |
| attempt-where-did-i-go-wrong | Fail: partial-attempt criterion | Fail: partial-attempt criterion |
| just-tell-me-the-answer | Fail: unsupported verification | Fail: same unsupported verification |
| explain-that-more-simply | Pass with causal-scope concern | Pass with same concern |
| dont-ask-me-questions-teach-it | Fail: polynomial constant | Pass |
| show-convolution-worked | Pass | Pass |
| show-what-is-a-derivative | Pass | Pass |

The partial-attempt judgement is an explicit pedagogical acceptance finding; it should not be conflated with the more categorical false-CAS-verification and wrong-polynomial findings. The candidate fails the gate even if a human reviewer interprets the partial acknowledgement more leniently, because the repeated false verification and osmosis failures remain.

| Run | Cases | Median seconds | Mean seconds | Min–max seconds |
| --- | ---: | ---: | ---: | --- |
| guide-remediation-1 | 7 | 17.20 | 15.81 | 3.20–31.02 |
| guide-remediation-2 | 7 | 12.08 | 14.42 | 3.32–28.10 |
| guide-transfer-1 | 3 | 7.63 | 9.31 | 6.29–14.01 |
| guide-transfer-2 | 3 | 7.28 | 8.80 | 6.84–12.27 |
| guide-osmosis-remediation-1 | 1 | 3.03 | 3.03 | 3.03–3.03 |
| guide-osmosis-remediation-2 | 1 | 2.81 | 2.81 | 2.81–2.81 |

No further model calls, prompt edits, provider acceptance or human review were performed by this reviewer. The release-owner handoff is **implemented improvements verified on bounded cases; semantic quality gate NOT PASSED**.
