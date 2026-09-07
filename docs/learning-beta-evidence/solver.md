# PLA-150 solver quality evidence

This is candidate evidence, not provider or human acceptance. The production path is
`solver.run_segmentation` (stops at the review gate) and `solver.run_solve`, including
retrieval, structured solution parsing, persistence, computation tools and verification.
The runner creates synthetic extracted documents and an empty retrieval class; its reviewed
problem lists are fixed corpus entries. It does not test PDF ingestion, OCR, embedding,
selected-source retrieval quality, or human review of the segmentation screen.

## Implemented behavior

A joint multipart solution's verifier now receives every requested child question as well
as the stem. Previously, generation saw the child questions but verification only saw the
stem and whatever answers the solver happened to produce. A missing answer could therefore
be invisible to verification. Existing separately solved part behavior is preserved.

The baseline statistics transcript demonstrates the context omission on both repeats.
A failing-then-passing deterministic regression protects the contract independently of
model behavior. The normal baseline answers themselves were correct: this is a correction
to what verification can establish, not a claim of an observed arithmetic accuracy gain.

The verifier prompt also requires meaningful checks tied to a claim, prohibits placeholder
arithmetic/capability probes, distinguishes step numbering from mathematical claims, and
makes completeness part of agreement. Before this change, biology prose triggered two
`cas_evaluate("1")` calls on each repeat; the frozen held-out history question triggered
three and one. These calls did not verify any claim even though the final uncheckable
verdict was honest.

## Reproduce

Use a fresh isolated configuration database with an explicitly authorized endpoint and
remote consent. This runner's tested configuration is unauthenticated; it deliberately
uses the null Keychain backend and never reads a student's credentials. Authenticated
provider configurations need an independently authorized isolated credential setup and
are not supported by this runner as currently measured.

```bash
PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring uv run python scripts/eval_solver_beta.py \
  --config-db /path/to/isolated-authorized-config/lyra.db \
  --workspace /path/to/fresh-synthetic-run \
  --output /path/to/privacy-safe-evidence.json \
  --repeat 2

uv run pytest backend/tests/test_solving.py backend/tests/test_solver.py
uv run ruff check backend/core/solver.py backend/tests/test_solving.py scripts/eval_solver_beta.py
```

The workspace must not exist. Runs are sequential, limited to at most three repeats;
`--cases` selects a bounded subset and `--skip-segmentation` isolates follow-up solving.
The JSON stores app/corpus/rubric/model versions, source and prompt hashes, configured
context window, locality/capability qualifications, generation temperature, terminal
answers, verification transcripts, actual tool arguments/results, loop stop reasons,
per-case latency, and pending semantic review. The completion API does not expose its
raw finish reason: that is explicitly unmeasured, never inferred from valid JSON.
Generation output limits omitted by production are reported as provider defaults.

Critical criteria are listed in `scripts/eval_corpora/solver_beta.json`. Check every
repeat against its expected result and retain every critical failure. A transport success,
a correct final number without units, a model's own verdict, or an average score does not
satisfy this rubric. Production CAS/unit-tool results are deterministic evidence for the
particular calculation only; selecting relevant checks and interpreting prose are model
work. Implementation-owner review, independent-agent review and actual human review must
remain separate.

## Limits and final rerun

Baseline production SHA is `e96bf4886977c648f9e7905c7807c806b1ae7a80`; intermediate candidate evidence
records source hashes because the multipart fix was uncommitted during that run. Final
representative runs use implementation SHA `f460dbe989abdc25ec206e5e31943ccf24bdf00e`.
The release owner must rerun this command at the final integrated candidate SHA. These
results do not establish broader model support or replace signed desktop smoke/launch,
provider acceptance, source-grounding checks on ingested courses, or human subject review.

## Retained results

- [Baseline](solver-baseline.json): 10 solves and two segmentation passes, corpus v1.
- [Multipart fix intermediate](solver-improved.json): same 10 solves and two segmentation
  passes, before verifier-prompt calibration.
- [Held-out conceptual baseline](solver-history-baseline.json): two history solves before
  prompt calibration, corpus v2.
- [Final committed representative run](solver-final.json): statistics, biology and held-out
  history, two repeats each at `f460dbe989abdc25ec206e5e31943ccf24bdf00e`.
- [Per-run implementation-owner review](solver-review.json): separate from independent-agent
  and human review; includes every latency, tool failure, stop reason and context failure.

All retained terminal answers were correct for their explicit expected results in owner
review. This is a small synthetic corpus, not a general accuracy estimate. The critical
verifier-context failure is present in both baseline statistics runs (zero of three
questions visible), and absent in both intermediate and both final runs (three of three).
Four original segmentation passes preserved the five questions and three dependent subparts.
Final segmentation and the other three quantitative cases were **not rerun at the final
committed SHA**; the earlier repeated measurements remain versioned separately.

**Failed intervention:** the prompt calibration did not eliminate irrelevant computation.
Final biology runs called CAS twice each (including `nothing_to_check` treated as a symbol),
and final history runs once each. All four final conceptual verdicts remained honestly
uncheckable. No elimination of placeholder calls or general latency improvement is claimed.
This is unresolved model/tool-use overhead, retained explicitly rather than averaged away.

| Case | Original baseline seconds (r1 / r2) | Multipart-fix intermediate | Final committed |
| --- | --- | --- | --- |
| rate-units | 9.936 / 9.161 | 12.740 / 13.494 | not run |
| stats-multipart | 16.715 / 16.077 | 23.307 / 26.013 | 14.317 / 13.484 |
| biology-prose | 15.780 / 16.748 | 24.585 / 26.696 | 19.656 / 17.339 |
| rate-heldout | 18.655 / 21.838 | 18.516 / 17.465 | not run |
| assumption-heldout | 24.609 / 19.894 | 24.674 / 21.075 | not run |
| history-heldout | 23.905 / 20.311 | not run | 16.811 / 19.783 |

The final representative command was:

```bash
/Users/ofhd/Developer/Lyra/.venv/bin/python scripts/eval_solver_beta.py \
  --config-db .quality-private/config/lyra.db \
  --workspace .quality-private/solver-final \
  --output .quality-private/solver-final.json --repeat 2 \
  --cases stats-multipart biology-prose history-heldout --skip-segmentation
```

Deterministic verification: `python -m pytest backend/tests/test_solving.py
backend/tests/test_solver.py -q` passed **108 tests**; five existing SWIG deprecation warnings.
Targeted Ruff checks passed. The new multipart-context test failed before the implementation
and passed afterward. Markdown link and active-reference checks passed. Signed desktop
build/launch and whole-candidate checks belong to the root integration handoff.

The history answers sometimes group wage ledgers among independent records. Ledgers may
come from the same owner; source independence must be established rather than assumed.
The answers still provide usable corroboration categories, but this precision limitation
is retained and prevents a universal claim about historical-source reasoning.
