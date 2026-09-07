# PLA-150: conservative verdict replay

The current verdict rule downgrades **4 of 18 previously verified runs** to uncheckable in
28 retained solver runs. All four are correct weight answers whose verifier recovered from
floating-point residue with a successful rational-arithmetic retry. This is a measured cost
of the conservative policy, not an accuracy improvement or four newly discovered wrong answers.
The other 24 verdicts are unchanged.

[Exact replay results and raw provenance](solver-verdict-replay.json) retain source-file
hashes and metadata, terminal answers, full model/tool transcripts, original verdicts,
replayed verdicts, and call indexes. No model, provider, CAS tool, or solver generation ran.
The replay invokes `verification.judge` on the exact retained `ToolLoopResult` fields.
Each solved run contains one verification loop and one top-level problem, so matching the
stored artifact verdict to its terminal transcript is unambiguous. Four segmentation-only
entries are excluded.

| Verdict | Retained original | Current deterministic replay |
| --- | ---: | ---: |
| Verified | 18 | 14 |
| Uncheckable | 10 | 14 |
| Total solved runs | 28 | 28 |

## Changed cases

| Source packet | Case | Repeat | Previous | Replayed |
| --- | --- | ---: | --- | --- |
| solver-baseline | assumption-heldout | 1 | verified | uncheckable |
| solver-baseline | assumption-heldout | 2 | verified | uncheckable |
| solver-improved | assumption-heldout | 1 | verified | uncheckable |
| solver-improved | assumption-heldout | 2 | verified | uncheckable |

Each asks for the weight of a 12 kg box with the gravitational assumption stated. The
terminal answers use 9.8 m/s² and correctly obtain 117.6 N. The initial comparison
`12*9.8` against `117.6` returns `equal:false`, `certain:false`, and a difference of
`1.42108547152020e-14`. Baseline repeat 1 also checks the reverse direction and retains the
negative residue. Every affected run then uses `12*98/10` and obtains `equal:true`,
`certain:true`, difference zero against `117.6` or `1176/10`. Exact offline rational
arithmetic independently confirms `Fraction("9.8") * 12 == Fraction("117.6")`.

The original model explicitly explains the rational correction. The new rule nevertheless
keeps the run uncheckable because an earlier unresolved comparison remains in its transcript.
It does not infer which successful call supersedes which earlier comparison. The numeric
answer is not refuted; a fresh coherent verification can establish the corrected result.
This policy costs useful verification coverage on these four retained runs. The 4/18 ratio
only describes this small historical corpus and is not an estimated general failure rate.

One distinct execution-error recovery remains verified: baseline `rate-heldout`, repeat 2,
attempts `40 mL/s * 60 s` in the CAS, receives a syntax error with `ok:false`, and subsequently
completes valid checks. A failed execution is not a mathematical contradiction, so the
rule preserves that retry behavior.

No retained solver comparison has `equal:false,certain:true`, and no solver packet retains
a lowercase-e-to-E notation correction. Therefore this replay cannot establish coverage
for genuine algebraic mismatches or corrected Euler notation; those belong to the separate
deterministic regressions and Guide evidence. All six final representative solver verdicts
from the previous candidate remain unchanged under replay.

## Reproduce and provenance

Replay ran from Git HEAD `19b94fbfeac16fda05ba8fa5459a6ef205b4ea75` with a dirty worktree.
The exact `backend/core/verification.py` SHA-256 was
`5c1d1936f76bd3fcd0e7feb9b9bed8119aa61834476e477d6d90622923a5985e`.
The JSON preserves this distinction rather than claiming HEAD alone identifies the tested
source. This compact command reproduces the verdict comparison from the existing packets:

```bash
/Users/ofhd/Developer/Lyra/.venv/bin/python - <<'PY'
import json
from collections import Counter
from pathlib import Path
from backend.core import verification
from backend.llm import tools

counts = Counter()
for name in ("solver-baseline", "solver-improved", "solver-history-baseline", "solver-final"):
    data = json.loads(Path(f"docs/learning-beta-evidence/{name}.json").read_text())
    for run in data["runs"]:
        if run["kind"] != "solve":
            continue
        loops = [t for t in run["transcript"] if t["kind"] == "tool_loop"]
        parents = [p for p in run["parts"]
                   if p["kind"] == "problem" and p["parent_part_id"] is None]
        assert len(loops) == len(parents) == 1
        t = loops[0]
        result = tools.ToolLoopResult(
            content=t["answer"], stopped=t["stopped"], detail=t["detail"],
            calls=tuple(tools.RecordedCall(**c) for c in t["calls"]),
        )
        before, after = parents[0]["verdict"], verification.judge(result).verdict
        counts[(before, after)] += 1
        if before != after:
            print(name, run["case_id"], run["repeat"], before, "->", after)
print(counts)
PY
```

Deterministic replay and arithmetic checks are separate from model judging, independent-agent
review and actual human review; those reviews were not performed by this replay. No existing
raw evidence, production code, corpus, rubric or acceptance criterion was modified. Current
provider behavior and a final integrated-candidate solver rerun remain unmeasured here.
