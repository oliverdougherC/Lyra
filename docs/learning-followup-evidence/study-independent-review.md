# Independent ecology study review

**Both follow-up decks pass the critical semantic gate with noncritical qualification notes.** All 28 published card instances were read against the two synthetic source documents. They contain 14 distinct cards: the published contents are identical across the two repeats. Ready status was not used as a semantic verdict.

This is an independent-agent review, separate from deterministic checks and generator self-judging. The reviewer made no provider calls. Actual human review is **not run**. Detailed per-card verdicts and the reviewed evidence hash are in [study-independent-review.json](study-independent-review.json).

## Scope and versions

Reviewed [study-ecology-decks.json](study-ecology-decks.json), source SHA `19b94fbfeac16fda05ba8fa5459a6ef205b4ea75`, app `0.2.0-beta.0`, corpus `study-beta-1`, rubric `study-critical-1`. Corpus SHA256 is `aeba5901143e1f2f254263f9e46a3c68c067eda363fa93711c8b2a524d4e4e4b`; study prompt SHA256 is `98baea534fa8aebc33647462af5afca632a23855419cbaa94c26643ecbf2061f`.

The retained configuration is remote `Qwen3.8-27B`, temperature 0, configured context 262,144, flashcard output limit 8,192, topic/quiz limit 65,536, concurrency 1, and real local nomic retrieval helper. Exact accepted schema format is unobserved in transport. No universal provider, context-capability or student-learning claim follows from these runs. Ecology remains labelled held-out in the original corpus, but this follow-up uses a known failing case and is **not a new blind holdout**.

## Per-repeat outcomes

| Run | Published semantic result | Cards | Latency | Internal generation failures |
| --- | --- | ---: | ---: | --- |
| Follow-up repeat 1 | Pass with qualification notes | 14 | 139.185 s | Call 7 ended `length` at 8,192 tokens; bounded retry recovered |
| Follow-up repeat 2 | Pass with qualification notes | 14 | 138.228 s | Call 7 ended `length` at 8,192 tokens; bounded retry recovered |
| Historical final ecology repeat 1 | Failed useful-deck delivery | 0 | 483.488 s | Incomplete JSON/whitespace reached `length`; unpublished explanation also had wrong algebra |
| Historical final ecology repeat 2 | Cancelled; not evaluable | 0 | 164.000 s | No retained completed call transcript |

Both follow-up runs contain nine provider calls, with stop sequence `stop, stop, stop, stop, stop, stop, length, stop, stop`. The failed call has no terminal answer; usage records all 8,192 completion tokens as reasoning tokens. The following retry supplies valid cards 11/12. The repeated failure remains a cost and latency limitation; it is not silently converted into nine clean requests. Two successful deliveries do not establish a low failure rate on broader material.

Historical [final-deck evidence](../evidence/study-beta-final-deck.json) and [earlier independent review](../learning-beta-evidence/study-agent-review.md) remain unchanged. The old unpublished explanation divided `MC` by `R/C`. Follow-up card 11 explicitly derives the correct `N ≈ M/(R/C) = MC/R`.

## Individual card review

The following applies to **each repeat**, since every published front/back is identical. All fronts include the quantities needed to answer independently; card 2 explicitly supplies its density rather than depending on card 1.

| Card | Judgment | Evidence |
| --- | --- | --- |
| 1 | Pass | Total 20 over sampled 10 m² gives 2 daisies/m², correct units. |
| 2 | Pass | Supplied 2/m² × 150 m² gives 300; representative/comparable-meadow assumption retained. |
| 3 | Pass | Random placement reduces preferential site selection, with a useful mechanism. |
| 4 | Pass | Larger samples reduce variation without automatically removing systematic bias. |
| 5 | Pass | Defined M = 40, C = 50, R = 10 give 200 animals. |
| 6 | Pass | Zero recaptures make the simple estimate undefined, not proof of infinity. |
| 7 | Pass with note | Names closure and explains migration risk; categorical bias wording needs qualification below. |
| 8 | Pass | Explains how different marked/unmarked catchability distorts the recapture fraction. |
| 9 | Pass with note | Mixing supports representative recapture; outcome wording is too categorical. |
| 10 | Pass | Lost marks undercount positive R and inflate MC/R; zero-R boundary is separately covered. |
| 11 | Pass with note | Correct marked-fraction interpretation and algebra; fuller standalone assumption qualification would help. |
| 12 | Pass with note | Correct closure definition including births/deaths; changes to marked fraction are possible, not inevitable. |
| 13 | Pass | Identifies that correlation alone cannot establish causation. |
| 14 | Pass | Supplies a possible common-cause explanation, beyond merely naming the limitation. |

## Qualification and usefulness notes

These are noncritical precision/organization concerns in this corpus, not hidden failed arithmetic or answer keys:

- **Cards 7/12:** Card 7 says migration means “R/C is systematically biased and N is biased.” The source says migration **can** bias estimates. Effect depends on which animals move, whether the marked fraction changes, and the target population/time. Prefer “Migration can change the marked fraction, so the original M and observed R/C may no longer support the simple estimate.” In card 12, change “the marked fraction changes” to “can change.” The intended closure condition and risk are correctly taught; the wording overstates inevitability.
- **Card 9:** “R will not reflect the true marked proportion” is stronger than the supplied assumptions justify for unspecified sampling geometry. Prefer “may fail to represent it.” The need for mixing is correct.
- **Card 11:** Mixing and equal capture probability are named, while stable population/retained marks are taught in other cards. Add “under the standard closed-population and retained-mark assumptions” for maximal standalone precision. The displayed algebra is correct.
- **Organization and difficulty:** Cards 5/6 fall under “Density estimation and extrapolation” but test mark-recapture. Cards 7/12 and9/11 share context, yet ask a mechanism versus a broader condition or estimator derivation; this is useful reinforcement rather than the previous identical calculation repeated under multiple labels. A shorter deck could consolidate these pairs. The deck mixes basic recall and mechanism explanation with two numeric applications; it is useful review, but not evidence of consistently demanding exam practice.

Substantive duplication was assessed from the required knowledge, not exact-front string uniqueness. Density calculation versus extrapolation, random placement versus sample-size limitations, estimator substitution versus its proportional derivation, distinct assumption failures, and evidence limitation versus confounding mechanism provide genuinely distinct targets. All six expected concept families appear. No contradictory key, wrong unit, unanswerable stem or substantive repeated probe was identified at the critical threshold.

Every published provenance row points to selected `Population survey.txt` page 3 or `Mark recapture.txt` page 4, matching the synthetic sources; no excluded-guide provenance or false facts were found. Both documents are broadly attached to most cards, so this establishes selected-source scope, not precise per-claim citation attribution.

## Controlled schema comparison

The reviewer checked [fresh baseline](study-schema-baseline.json) and [schema-only replay](study-schema-only.json): retained messages and configuration are exactly equal. The [comparison](study-schema-comparison.json) records removal of unused required `topic` while retaining `front`/`back`. Baseline reaches 8,192 tokens, `length`, rejected JSON, 43.868 s; schema-only returns one valid confounder card with `stop`, 4,400 tokens, 26.257 s. The returned explanation is source-grounded and useful for that one-card retry.

This supports the bounded schema repair under the tested configuration. It does not reveal the provider's internal decoder or prove schema repair alone guarantees complete decks. The full runs additionally use an explicit front/back JSON prompt, and still need a bounded retry for another topic. The earlier cap-only replay reduced wasted generation time but remained malformed; it was not semantic acceptance.

Deterministic scheduling/retry records in the full-run evidence are separate engineering checks, not proof of student learning. Actual human review, fresh unseen courses, other configurations and final integrated packaged-candidate review remain separate gates. The unchanged historical failures and the two recovered internal failures must accompany the two passing deck outcomes in the release handoff.
