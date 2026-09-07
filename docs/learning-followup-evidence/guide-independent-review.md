# Guide follow-up independent review

**Final quality gate: NOT PASSED.** Both final partial-attempt answers teach a false rule about taking a constant factor outside an integral. The direct-answer verification failure and eigenvalue algebra failure observed in both fresh baselines do not recur in the final repeats. This is independent-agent review of retained terminal answers and actual tool arguments/results, not model self-judging, human review, or installed-product acceptance.

## Evidence and criteria

The original `tutor_semantic.json` 1.2.0 remains the ruler for its four cases. Correct final formulas do not erase wrong teaching or unsupported verification claims. An acknowledgement can be implicit and need not use a praise phrase, but feedback must not invalidate a correct operation. Tool execution `ok:true` is distinct from mathematical agreement. `matched`, `mismatched` and `unresolved` must be read with the actual submitted expressions and the scope of the claim.

Fresh baseline runs record clean SHA `2ee865c0da52c94c1f5aa056ea64a2e2de513e4e`. Final runs record SHA `19b94fbfeac16fda05ba8fa5459a6ef205b4ea75`, dirty. Use the per-run prompt/system/corpus hashes to establish source identity rather than interpreting that dirty SHA as an immutable full checkout. The lead identifies these as the final exact-source runs. This reviewer made no production edits or provider calls. Per-case classifications, input/answer hashes, stop reasons and latency are retained in [guide-independent-review.json](guide-independent-review.json).

## Four-case baseline versus final

Sources: [baseline1](guide-baseline/runs.json), [baseline2](guide-baseline-2/runs.json), [tool-contract intermediate](guide-tool-contract/runs.json), [final1](guide-final-1/runs.json), [final2](guide-final-2/runs.json).

| Case | Baseline1 / baseline2 | Intermediate | Final1 / final2 | Reason |
| --- | --- | --- | --- | --- |
| attempt-where-did-i-go-wrong | Concern / concern | Concern | **Fail / fail** | Baselines correctly identify lost `e^tau`, credit setup and show valid initial-interval bounds, though explicit credit for those bounds is weak. Intermediate explicitly credits those bounds and repairs two unresolved checks with successful `exp` comparisons, but says the antiderivative is correct “up to sign,” an avoidable ambiguity. Final twice states extracting `e^-t` is legal only if the entire integrand is constant in `tau`. That is false: only the extracted factor must be independent of `tau`. |
| just-tell-me-the-answer | **Fail / fail** | Pass | Pass / pass | Baseline's only comparison returns `equal:false`, with lowercase `e` treated as a symbolic variable, yet the answer claims exact derivative verification. Final submits `exp(-0.03*t)` and receives `equal:true`, zero difference and `matched`. Its narrow claim that the displayed solution satisfies the ODE is supported. Formula, units and initial condition are correct. |
| dont-ask-me-questions-teach-it | **Fail / fail** | Pass | Pass / pass | Both baselines print characteristic polynomial constant12 rather than18 for matrix `[[5,2],[1,4]]`; correct eigenvalue/vector checks do not cover that erroneous expansion. Final uses `[[1,2],[1,2]]`, correctly derives `lambda(lambda-3)`, checks eigenvalues0/3 and vectors, and recovers a `lambda` syntax error by checking the polynomial using symbol `l`. No compulsory questions. |
| show-convolution-worked | Pass / pass | Pass with concern | Pass / pass | Baseline and final have correct support, limits, factoring, signs and branches. Final tools evaluate both branch integrals and numerical examples at0.5/2; reported0.3935 and0.2325 are accurate. Intermediate's full Heaviside integral remains unevaluated and Laplace check fails, but the answer does not claim those succeeded; its two branch checks and displayed derivation suffice. |

“Concern” is **not counted as a demonstrated critical mathematical failure** in the fresh baseline attempt answers. Their setup acknowledgement and corrected interval reuse are relevant; a human may decide whether the original explicit acknowledgement criterion is met. This judgement does not soften the final failure: the final constant-factor rule is objectively wrong regardless of wording preference.

The repeated final sentence is: “Pulling e^-t out of the integral is only legal if the integrand is constant in tau; it isn't.” Counterexample using the answer's own expression:

`integral_0^t exp(-t+tau) d tau = exp(-t) integral_0^t exp(tau) d tau`.

Here `exp(tau)` varies with `tau`, and extracting `exp(-t)` is valid. The error in the student's attempt is dropping `exp(tau)`, not taking out the constant. The final tools return correct definite integrals but do not verify that false explanatory sentence. The diagnostic answer also omits values at0/1; that is secondary to the false rule.

All four final terminal answers are byte-identical across repeats. This establishes recurrence on the sampled configuration, not independent coverage of diverse generations. The final Show answer itself uses valid constant extraction, so the retained answers are inconsistent across teaching contexts.

## Osmosis and held-out cases

Sources: [osmosis baseline1](osmosis-baseline/runs.json), [baseline2](osmosis-baseline-2/runs.json), [intermediate candidate](osmosis-candidate/runs.json), [final1](osmosis-final-1/runs.json), [final2](osmosis-final-2/runs.json), [heldout1](heldout-final-1/runs.json), [heldout2](heldout-final-2/runs.json).

| Case / stage | Result | Reason and boundary |
| --- | --- | --- |
| Osmosis fresh baseline1/2 | No critical failure identified; condition concern | “Until the two sides balance out” is vague, but does not explicitly guarantee equal concentrations. Direction and membrane example are appropriate under ordinary equal-pressure conditions. |
| Osmosis intermediate candidate | **Fail** | Explicitly says water flows “until the concentrations even out.” Osmotic equilibrium can instead balance a pressure difference; this volunteered universal endpoint is wrong. Preserve this failed intervention. |
| Osmosis final1/2 | No critical failure identified; condition concern | Correct direction and bag-swelling example; “until the pull balances out” does not make the previous concentration-equality claim. Explanation still leaves pressure implicit. Both final answers are identical. |
| division-loses-zero, final1/2 | Pass / pass | Explains division by `x` assumes nonzero `x`, restores0 and3 by factoring, and `cas_solve` returns both values. It teaches the relevant condition and supports its verification claim. |
| verify-false-identity, final1/2 | Pass / pass | CAS returns `equal:false`, `difference:2*x`, `mismatched`; answer correctly says equality is not an identity and holds only at0. It checks a concrete counterexample without presenting execution success as mathematical agreement. |

Osmosis's final wording is better than the failed intermediate response, but fresh baseline repeats also avoid the explicit erroneous guarantee. An unrelated schema/request-context change and small samples **do not establish a causal scientific repair**. Earlier osmosis failures retained in [the previous review](../learning-beta-evidence/guide-agent-review.md) remain historical failures, not deleted or retroactively passed.

The pressure qualification is documented by [OpenStax Biology 2e, Passive Transport](https://openstax.org/books/biology-2e/pages/5-2-passive-transport). A simple example need not teach chemical potential, but it should not invent a universal equilibrium endpoint. No human has accepted the final explanation in this review.

## Latency and limits

| Four-case run | Mean seconds | Median seconds |
| --- | ---: | ---: |
| Fresh baseline1 | 16.06 | 16.06 |
| Fresh baseline2 | 16.07 | 16.10 |
| Tool-contract intermediate | 13.71 | 12.89 |
| Final1 | 16.89 | 18.84 |
| Final2 | 16.81 | 18.83 |

No overall latency improvement is established. Final direct answer is about6s; attempt about16.4s; eigenvalue answer about24s; Show convolution about21s. Osmosis finals take1.63/1.64s; the two held-out cases average4.02s in each run. Differences can include model variation and tool recovery. One model/endpoint configuration with repeated identical answers cannot establish universal robustness, statistical independence or human acceptance.

## Four-item human review packet — pending, not acceptance

The reviewer should record name/date, accepted or rejected, and a short rationale for each item. All decisions below remain **not run**. Open the linked complete answer and transcript rather than reviewing the excerpt alone.

| Item | Evidence to inspect | Human decision requested | Agent finding, not human verdict |
| --- | --- | --- | --- |
| 1. Baseline false verification | [baseline1](guide-baseline/runs.json), `just-tell-me-the-answer` | Does the answer truthfully describe what its computation established? Compare the exact claim with the sole `equal:false` result. | Reject verification claim even though intended formula is correct. |
| 2. Final partial-attempt help | [final1](guide-final-1/runs.json), `attempt-where-did-i-go-wrong` | Does the feedback distinguish valid constant extraction from dropping a variable factor, and appropriately credit valid work? | Critical false rule; repeated final2. |
| 3. Final simpler osmosis | [osmosis final1](osmosis-final-1/runs.json), `simpler-osmosis` | Is “pull balances out” accurate enough for this beginner request without implying concentration equality or ignoring relevant pressure conditions? | No critical claim identified; human judgement on simplification still required. |
| 4. Corrected worked derivation | [final1](guide-final-1/runs.json), `show-convolution-worked` | Check support intervals, both antiderivatives, boundaries and numerical examples. Is the explanation self-contained and useful? | Pass; actual tool results support both branches and spot checks. |

The implemented tool-contract improvements have useful observed effects, especially truthful comparison handling, but the final repeated attempt misconception keeps the semantic gate open. Deterministic tests, this independent-agent review, any model self-judging and the pending human packet are separate evidence categories.
