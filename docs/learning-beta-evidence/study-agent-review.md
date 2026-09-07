# Independent study semantic review — September 6 beta lane

Status: retained baseline, initial candidate and final deck attempt review complete. Final study quality acceptance remains blocked by the held-out ecology deck failure; its second repeat was cancelled. This is independent-agent review of retained synthetic outputs, not actual human acceptance or a provider-model self-grade. No provider calls were made by the reviewer. Deterministic schema, uniqueness and provenance flags are separate evidence and cannot establish semantic correctness.

Sources: `scripts/eval_corpora/study-beta.json` (study-beta-1 / study-critical-1), `docs/evidence/study-beta-baseline-quiz.json`, `docs/evidence/study-beta-baseline-deck.json`, and `docs/evidence/study-beta-improved.json`. Baseline records identify SHA e96bf4886977c648f9e7905c7807c806b1ae7a80, app 0.2.0-beta.0, remote Qwen3.8-27B, temperature 0, context window 262144, maximum generation 65536 tokens, concurrency 1. See source artifacts for prompt/content hashes, call transcripts and capability limitations.

Each published question/card was read with its key/back, explanation and selected synthetic source. A pass means no identified critical semantic failure in that artifact, not universal quality. Correctness, grounding, answerability and semantic duplication are hard gates; useful difficulty and breadth are also reported. Numbered items below use one-based terminal-content order.

## Baseline artifact verdicts

| Case | Kind | Repeat | Critical verdict | Latency (s) | Item review |
| --- | --- | --- | --- | ---: | --- |
| circuits | quiz | 1 | Pass | 58.993 | Q1–5 correct keys and grounded explanations; independently answerable. |
| circuits | quiz | 2 | **Fail** | 48.831 | Q1,3–5 pass; Q2 omits the circuit setup and depends on a preceding question. |
| ecology | quiz | 1 | **Fail** | 45.699 | Q1,3–5 pass; Q2 omits the density or sampling data and depends on a preceding question. |
| ecology | quiz | 2 | Pass | 61.247 | Q1–5 correct, grounded and independently answerable. |
| thin-evidence | quiz | 1 | **Fail** | 16.510 | Q1–2 factual administrative recall; Q3 and Q5 repeat their knowledge; Q4 asserts an unstated formal seminar name. No useful subject practice. |
| thin-evidence | quiz | 2 | **Fail** | 14.617 | Same Q1–5 and failures as repeat 1. |
| circuits | deck | 1 | **Fail** | 68.274 | Cards1–10 individually correct/grounded; cards6/8 duplicate the fixed-variable power comparison; card4 largely repeats card2 plus voltage drops. |
| thin-evidence | deck | 1 | **Fail** | 59.764 | Cards1–10 grounded but administrative or absence-of-content recall, heavily duplicated across topics. No useful subject practice. |

The baseline contains two artifacts without identified critical failure and six failures. These are artifact counts, not an aggregate score that offsets critical failures. No ecology deck baseline or second deck baseline repeats were retained in these files; those comparisons are not run.

## Baseline findings and concrete corrections

- Circuits quiz repeat2 Q2: “In that same series circuit, what is the voltage drop across the … resistor?” lacks the 2 Ω + 4 Ω series setup and 12 V supply. The keyed 8 V is correct only with missing prior context. Include the complete setup in the question itself.
- Ecology quiz repeat1 Q2: “Using the estimated density, how many daisies are expected in a comparable … meadow …?” lacks the density or sample counts/areas. State 2 daisies/m² or all five 2 m² quadrat counts. Its 300-daisy key and representative-sampling assumption are correct when that context is supplied.
- Thin quiz both repeats Q4: “Which of the following is not stated in the course material?” keys only “The instructor's first name”; explanation says “the seminar's name” is given. The source calls it a synthetic seminar but supplies no formal name, so that distractor is also reasonably unstated. Do not fabricate a named course, and do not turn absent teaching material into quota-filling questions.
- Thin quiz Q3 repeats Q1's Tuesday recall; Q5 combines Q1/Q2 without adding a useful skill. Deck cards1/3/5 all recall Tuesday; cards2/4/6 all recall Rowan; cards7/8 are paraphrases of absent subject matter/examples; cards9/10 split that same absence. Return honest insufficient-material status instead of a Ready subject-study artifact.
- Circuits deck cards6/8 both ask what is fixed before comparing resistor power and teach the same voltage/current contrast. Keep one and select a distinct source-supported probe. Card2 asks the exact series example's current; card4 repeats that calculation before voltage drops. A targeted voltage-drop prompt would add more distinct practice.

Difficulty/breadth observations: baseline circuits quizzes repeat the same numerical series example for three of five questions and do not explicitly test the fixed-voltage versus fixed-current trap. All correct calculations retain appropriate units. Ecology questions correctly distinguish random sampling variation, systematic bias and correlation; however, formula substitution and factual edge-case recall constitute limited “exam” difficulty. Repeat2 keys all five answers at option0, an avoidable guessing cue. Deck explanations are useful for self-checking, although several cards combine multiple recall targets. Selected documents support the answers; no excluded-source factual contamination was found. Per-item provenance can include both selected circuit documents even where only one is relevant, so selected-source validity is not precise claim attribution.

## Initial candidate (intermediate; not final acceptance)

| Case | Kind | Repeat | Critical verdict | Latency (s) | Item review |
| --- | --- | --- | --- | ---: | --- |
| circuits | quiz | 1 | Pass | 48.387 | Q1–5 correct and self-contained; voltage/current variation, parallel total current, explicit fixed-voltage power contrast and instrument misconception provide broader useful practice. |
| circuits | quiz | 2 | Pass | 47.733 | Q1–5 correct and self-contained; keys/explanations match. Q5 combines meter placement with Ohm's law. |
| circuits | deck | 1 | **Fail** | 46.996 | Cards1–12 individually grounded and correct, but cards2/4 ask the same full series current-and-voltage task; cards7/8 further repeat equivalent-resistance examples already taught in earlier cards. |

Initial candidate circuits deck repeat2 (68.405 s): **Fail** semantic duplication. Cards1–12 remain correct and grounded. Cards3/7 both ask the same series-current condition and equivalent-resistance sum; card7 adds derivation only in its back, while the front still probes the same facts. Cards5/8 repeat the parallel formula, with an added justification request in card8. Cards2/4 now target current and voltage drops separately, which is a useful distinction. This is a repeated-run diversity failure, not a correctness failure.

Initial candidate deck card2: “Two resistors of … are connected in series to a … source. Use Ohm's law to find the circuit current and the voltage drop across each resistor.” Card4: “Two resistors, … are connected in series across an ideal … DC source. What is the circuit current, and what are the voltage drops across …?” Both use 2 Ω, 4 Ω and 12 V with the same 2 A / 4 V / 8 V answer. Different topic labels and wording do not make distinct knowledge probes. Keep one, then coordinate generation across topics using already-covered concepts rather than only exact-front uniqueness. Grounding is sound; semantic diversity remains a critical failure.

Initial candidate ecology quizzes both pass the critical gate. Repeat1 (102.642 s), Q1–5: 15 daisies / 6 m² × 120 m² = 300; zero recaptures undefined; trap-shy marked animals reduce R and bias N high; quadrats estimate sessile-plant density while mark-recapture estimates mobile-animal population; temperature may confound the hypothetical association. Repeat2 (68.781 s), Q1–5: 15 / 3 m² × 120 m² = 600 with dense-patch selection bias; effective marked count 60 yields 300; trap-happy animals increase R and bias N low; mobile beetles favor mark-recapture under the supplied material; association alone does not prove causation. New, explicitly supplied scenarios and misconception/bias reasoning are a useful increase in difficulty from baseline recall, with self-contained stems and distinct targets.

A noncritical ecology repeat2 Q5 concern remains: it describes soil moisture higher in wet years, then calls moisture a possible confounder affecting rainfall and abundance. The scenario supports covariance, not that causal direction; soil moisture could instead mediate a rainfall effect. “May” avoids a definite false assertion, but a cleaner explanation would leave causal alternatives unresolved or supply a plausible common-cause scenario. Do not teach that any correlated third variable is automatically a confounder.

Initial candidate ecology decks repeat1 (123.325 s) and repeat2 (111.358 s): **Fail** semantic duplication. All fourteen fronts/backs in each artifact were inspected. The core keys are correct and grounded. Cards1/3/5 repeat the same density/extrapolation example (five 2 m² quadrats, 20 daisies, 2/m², 300); cards2/4/6/7 repeat the sample-size versus placement/bias contrast; cards10/11 ask the identical four mark-recapture assumptions. Cards8/9/12 add bias direction, formula calculation and mechanism respectively; cards13/14 overlap but distinguish causal reasoning and terminology. No zero-recapture probe is included. Card4's “systematically rather than randomly” wording could conflate systematic sampling with systematic bias; its back narrows the warning to unrepresentative areas, so this is a noncritical wording concern.

Initial candidate thin-evidence quiz repeats1/2 (3.957 / 3.968 s) and deck repeats1/2 (0.888 / 0.782 s): **Pass honest-insufficiency gate**. All four have failed state, zero published items, and provider `stop` with empty `questions` or `topics` arrays. No subject practice was fabricated or advertised Ready. These are successful protection outcomes, not successful practice generation. The deterministic `selected_provenance_only: false` on empty content is not evidence of excluded-source use; there are no published provenance rows to judge.

Initial candidate total: four substantive quizzes pass, four insufficient-evidence protections pass, and **all four substantive decks fail** semantic diversity. Arithmetic/grounding improvements do not cancel those failures. This remains intermediate evidence and must not stand in for the subsequent prior-context deck repair rerun.

## Final deck repair candidate

Source: `docs/evidence/study-beta-final-deck.json`, exact SHA `700d02f63cdec1fb18f4e30a998d58db745ab7a0`, study implementation SHA256 `9378c48b5dd090b51c8a2287b187b315e7b22d97569c352dd3590b0bac58da77`, study prompt SHA256 `fae7597770402231a2274bfddcbcafd07b8152d56744dad4fb0ad2d6a9834279`. Same named model/context/generation configuration; real local nomic embedding helper. Accepted schema format remains unobserved in transport; no stronger capability claim is made.

Circuits deck repeat1 (127.448 s): **Pass**. Every card1–12 is correct, grounded and self-contained. The identical solved circuit repeated under two topics is gone. Cards3/4 test general resistance-sum and voltage-sum laws separately from card2's worked numeric application. Card7 tests network equivalent-resistance interpretation; card8 supplies the positive-resistor and at-least-two-branches conditions needed for its strict bound. Cards6/9 deliberately contrast fixed voltage and fixed current, with different correct dependencies. Card10 correctly derives total parallel power 48 W. Cards11/12 test distinct instrument connections. Card6's placement in a parallel-resistance topic remains a noncritical organizational mismatch; parallel branch-current calculation is not directly tested in this particular deck.

Circuits deck repeat2 (122.827 s): **Pass**. Every card1–12 was read independently and is correct and grounded. Card5 applies the reciprocal rule numerically, card7 recalls its general expression, and card8 derives the qualitative comparison; these are distinct skills rather than the same question with new topic labels. Card10 correctly asks and answers 24 W **per resistor**, unlike repeat1's 48 W total. The same noncritical power-topic mismatch persists.

Ecology deck repeat1 (483.488 s): **Fail — useful artifact delivery**. No terminal cards were published. The ninth provider call spent 337.300 s and ended with `length`, leaving an incomplete JSON card followed by repeated whitespace. The existing malformed-output protection withheld the deck, but substantial source material was available: this is a failure to deliver practice, not a passing insufficient-evidence response.

The valid pre-publication card batches were also read. They cover the quadrat calculation, sampling versus bias, representative extrapolation, equal catchability, mark-recapture formula, zero recaptures, closed population, mark retention, mixing, the recapture-proportion interpretation, bias direction, increasing catch size, and confounding. Direct topic-to-topic duplication is substantially reduced. **A critical generated but unpublished explanation error remains:** call7 card1 says “dividing $MC$ by an inflated $R/C$ gives a value that is too small.” The correct identity is $N=M/(R/C)=MC/R$, not $MC/(R/C)$. Replace “MC” with “M” or describe dividing MC by R. The conclusion that preferential recapture biases the estimate low is correct, but does not repair the false algebra. This error was not observed in published content because the complete deck failed. It still prevents calling the generated study content semantically accepted.

Ecology deck repeat2 (164.000 s before cancellation): **Not evaluable — cancelled**. The retained record contains no terminal content and no completed call transcripts. Cancellation is not a semantic pass or an independent second failure demonstrating the same mechanism. No repeat reliability estimate for final ecology generation can be made.

| Final deck case | Repeat | Critical result | Latency (s) | Published items |
| --- | --- | --- | ---: | ---: |
| circuits | 1 | Pass | 127.448 | 12 |
| circuits | 2 | Pass | 122.827 | 12 |
| ecology | 1 | Fail: no useful deck; malformed length stop; unpublished algebra error | 483.488 | 0 |
| ecology | 2 | Not evaluable: cancelled | 164.000 | 0 |

The measured tradeoff is explicit: final circuit decks are semantically more useful but take roughly two minutes, versus 68.274 s for the sole baseline circuit deck. The final held-out deck attempt takes over eight minutes and still fails. Faster initial quiz performance is case-specific (circuits 58.993/48.831 s → 48.387/47.733 s; ecology 45.699/61.247 s → 102.642/68.781 s). Initial quiz and thin-evidence improvements were not rerun at the final deck SHA, and final deck evidence must not imply otherwise. Require the release owner's integrated-candidate rerun and actual human review before acceptance; retain the failures rather than substituting a favorable average.

Actual human review: **not run**. Model self-judging: **not run in the retained study evidence**. Independent review is limited to this compact synthetic corpus and named configuration; other providers, local configurations, real course distributions, and actual student learning outcomes are not established.
