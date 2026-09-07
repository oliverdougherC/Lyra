# Independent solver evidence review

Review date: 2026-09-06 (America/Los_Angeles). Reviewer: independent Codex agent,
separate from the solver implementation owner. This is **agent semantic review, not actual
human acceptance**. No provider calls were made for this review.

Reviewed retained terminal answers, worked steps, verifier input, tool arguments/results,
verdicts, and stop states in:

- [Baseline](solver-baseline.json): 12 runs (five solve cases and segmentation, twice).
- [Intermediate](solver-improved.json): 12 runs (same scope).
- [History baseline](solver-history-baseline.json): two held-out conceptual runs.
- [Final](solver-final.json): six runs (statistics, biology, history, twice), tested at
  `f460dbe989abdc25ec206e5e31943ccf24bdf00e`.

Corpus and prompt hashes, configured model identity, endpoint locality, context window and
parameters are retained in each report. Baseline/intermediate reports record working-file hashes
alongside their base SHA; these are not evidence of a clean committed candidate.

## Per-case critical findings

| Case | Baseline repeat 1 | Baseline repeat 2 | Final repeat 1 | Final repeat 2 |
| --- | --- | --- | --- | --- |
| rate-units | Correct 5 m/s, conversions shown; relevant arithmetic and corrected unit checks | Same | Not run | Not run |
| stats-multipart | Correct 5 minutes, 4 minutes, median explanation; **critical verifier-input omission: requested subparts absent** | Same omission despite correct answer | Correct, complete; verifier receives all three subparts; four relevant successful checks | Same |
| biology-prose | Useful controls/confounding/replication explanation; honestly uncheckable | Same | Correct core explanation; honestly uncheckable | Same |
| rate-heldout | Correct 40 mL/s and 2.4 L/min, both requested units answered | Same | Not run | Not run |
| assumption-heldout | Earth gravity assumption explicit; 117.6 N, about 118 N; mass/weight distinguished | Same | Not run | Not run |
| history-heldout | Useful source criticism and two corroboration categories; honestly uncheckable | Same | Core criteria satisfied; honestly uncheckable | Same |
| segmentation | Five root questions and all three statistics subparts preserved | Same | Not run | Not run |

The intermediate report has correct terminal answers in both repeats for all five original solve
cases. Both segmentation runs preserve the five roots and three statistics subparts. Its statistics
verifier receives all requested subparts in both repeats, demonstrating the routing/context fix.
Do not report an improvement in statistics terminal-answer correctness: baseline answers were
already correct. The demonstrated correction is the verifier's access to the question requirements.

The final scoped six solves have no identified critical terminal-answer failure. This does not
establish final-candidate acceptance for the three numeric cases or segmentation omitted from that
run. The final integrated-candidate rerun must keep those entries explicitly not run until tested.

## Verification evidence and failed intervention

- Arithmetic and dimensional checks support the numeric results. Baseline unit-expression parsing
  produced mismatches for expressions such as `7500 m / 1500 s`; subsequent corrected expressions
  and direct final-unit checks support the result. Pump-rate consistency is supported by separate
  arithmetic conversions. These recoveries do not make the initial calls successful.
- Weight comparisons initially returned an uncertain floating-point difference near 1.4e-14;
  subsequent rational comparisons settled equality. The final answer correctly conditions weight
  on the stated local gravitational assumption.
- CAS mean/median calculations do not mechanically verify the interpretation of a typical
  observation. Experimental design, historical methodology and the mass/weight explanation remain
  semantic judgments. Production verifier prose is model self-judgment, not independent acceptance.
- **The prompt attempt to eliminate irrelevant conceptual tool calls failed.** Final biology repeat
  1 calls `cas_evaluate("nothing_to_check")` twice; repeat 2 calls `cas_evaluate("1")` twice.
  Final history calls `cas_evaluate("1")` once in each repeat. These successful tool executions
  provide no evidence for the conceptual claims. All four final conceptual verdicts remain honestly
  `uncheckable`; the retained issue is wasted calls/latency, not false mathematical proof.
- Final statistics repeats each use two successful relevant CAS comparisons and two successful
  relevant unit checks. All final verifier loops stop as `completed`; completion API stop reasons
  remain unavailable and are recorded as such.

Noncritical precision concerns: statistics calls 10 minutes an "outlier" without a formal outlier
criterion, though its higher value and effect on the mean are correctly explained. History loosely
includes wage ledgers among "independent" records; actual independence depends on provenance and
must not be inferred solely from document type. These examples support bounded basic usefulness,
not universal disciplinary acceptance.

## Latency and acceptance boundaries

For the matched six statistics/biology/history runs, baseline mean latency is **18.256 seconds**
and median **16.732 seconds**, versus final mean **16.898 seconds** and median **17.075 seconds**.
The small sample and differing direction of mean/median do not support a strong latency-win claim.

Deterministic arithmetic/tool results, production model self-judgment, this independent-agent
semantic review, and actual human review are distinct evidence layers. **Actual human review is
not run.** Endpoint model identity is configured identity, not a retained server identity assertion.
The reports use an empty retrieval class and synthetic extracted text: source-grounding, ingestion,
OCR, embeddings and installed-app behavior are outside this evaluation. Segmentation and solving
are separately exercised; the solve harness writes canonical corpus problems rather than solving
the segmentation output. Other provider/configuration acceptance is not established.
