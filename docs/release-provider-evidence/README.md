# Real-service release evidence — September 6, 2026

**Current result: the retained quality gate is not passed.** The last bounded
repair (Guide `99383ff7`, class-agent `32d2036e`) passes only **4/6** critical
cases under the unchanged same-model rubric. First-step scope and explicit
no-question obedience now pass, including correct eigenvector arithmetic.
Remaining failures concern explaining the requested overlap/sliding picture and
acknowledging the correct portions of a student's partial attempt. The Show
answer also contains an inconsistent displayed general integration limit even
though its final piecewise result passes. No full-corpus pass, independent human
acceptance or immutable packaged-candidate quality pass is claimed for this
latest revision. Its exact results are in `guide-scope-critical/` and `summary.json`.


The installed settings snapshot advertises **Qwen3.8-27B**, a user-configured remote
OpenAI-compatible endpoint, with configured context window **262144**. A read-only
`/models` preflight returned HTTP 200 and included this model. Tutor and Exa
credentials were available. Endpoint URLs and credential values are not retained.
The settings database was opened with SQLite `mode=ro`; credential access used
read-only Keychain/file retrieval. No settings, credentials or coursework were
changed by these evaluation runs.

The Guide evaluation invokes the existing `scripts/eval_tutor.py` production
`class_chat` planner and terminal tool loop with corpus `tutor_semantic.json`
version **1.1.0**, containing only synthetic public mathematical examples.
Configuration is retained in memory from the read-only installed snapshot;
all writable database/data/model/cache/log paths are disposable. No real class,
student source document or attached workspace is planned against. The existing
harness injects the corpus's synthetic retrieval examples and keeps real model
inference/tool dispatch. Each model case has a 180-second external safety bound.
Results are source-working-tree evidence; individual metadata identifies the
base revision and diff hash. They are not the final immutable packaged candidate.

A same-model grading run, where present, is labeled explicitly as supporting
model-judge evidence. It is not independent human review or acceptance of the
supported endpoint configuration. Critical semantic cases require the written
human rubric and independent review in PLA-461; broader study/writer/solver
quality, their representative corpora and repeated packaged runs remain open.

## Exa

`exa-smoke.json` records an actual production `ExaClient` search with the synthetic
public query “OpenStax calculus derivative definition,” one returned public
source, validated provenance, and successful bounded content retrieval. Only the
public source URL and status metadata are retained, not fetched copyrighted text.
Search/content completed in 1.1 seconds. No course documents or private queries
were sent. This tests the live client boundary, not packaged research UI/source
capture, provider-outage injection or the full installed-app soak.

## Retained baseline and intermediate results

| Run | Prompt SHA-256 prefix | Terminal cases | Same-model judge | Scope |
| --- | --- | --- | --- | --- |
| `guide-run-1` | `d8566f770af8` | 13/13 | 11/13 | Original full corpus |
| `guide-run-2` | `d8566f770af8` | 5/5 | 4/5 | Critical repeat |
| `guide-fixed-critical` | `8474bbc233c1` | 5/5 | 4/5 | Intermediate clarification; superseded by budget-preserving revision |
| `guide-compact-critical` | `92c8e5f20f15` | 5/5 | 4/5 | Compact clarification preserves existing input-budget tests |
| `guide-compact-full` | `92c8e5f20f15` | 13/13 | 11/13 | Full unchanged corpus; explicit no-question regression found |
| `guide-compact-heldout` | `92c8e5f20f15` | 2/2 | 2/2 | New dilution/probability cases |
| `guide-final-critical` | `a997cf7dfe16` | 6/6 | 4/6 | Rejected attempted-final revision: first-step regression and factual error |
| `guide-scope-critical` | `99383ff71f3d` | 6/6 | 4/6 | Latest bounded repair; remaining rubric/quality limitations retained |

Both original runs hand over the full solved mixing ODE when the student asks
only how to start. Their simplification answers are byte-identical, yet the same
model judges the first a failure and the repeat a pass. This demonstrates judge
variance; repeated grading alone cannot manufacture human acceptance.

The intermediate clarification stops at the useful inflow/outflow setup and
passes that first-step criterion. Its simpler echo analogy receives top scores
on all seven dimensions but fails the existing corpus item specifically requiring
an overlap/sliding-window picture. The rubric and original failure artifacts are
preserved. The intermediate prompt also exceeds existing small-context prompt
budgets, so it is not the final candidate prompt.

`heldout-corpus.json` contains two additional synthetic cases (dilution first move
and plain-language conditional probability), authored after the first generic
clarification was frozen. They are separate from the unchanged original corpus
and use its unchanged grading rules.

The compact clarification retains the corrected first-step behavior and restores
the existing prompt-budget tests. Its critical run also scores 4/5: the unchanged
strict simplification item fails for the missing overlap/sliding-window picture,
while all seven scored dimensions receive 2. This is reported without changing
the rubric or inserting topic-specific keywords into the general prompt.

The compact full run found an additional explicit-instruction failure: after
“Don't ask me questions; teach it,” the model gives a correct eigenvalue
explanation but ends with a question offering another walkthrough. This is an
actual no-question request violation, not only a judge preference. The first-step
fix is preserved while the general no-question rule receives a targeted repair.
Both held-out cases passed on the compact prompt.

Evidence detail: the existing harness retains terminal responses, timestamps,
model round counts and tool names/success flags. It does not retain full tool
arguments/results or every raw provider stop field. These are explicit limits
for a future immutable packaged-candidate evaluation, not claimed transcripts.

The attempted-final `a997` revision fixes the strict simplification picture and
omits closing questions, but reintroduces a full solved ODE after the first-step
request. Its eigenvalue explanation also asserts the false equality
`(3, -3) = 2(1, -1)` for a worked eigenvector. The same-model judge assigns that
case correctness 0 and the critical run passes only 4/6. The subsequent full run
was interrupted as it began, after those critical results were complete; its
empty partial output is labeled aborted. No full-corpus score is claimed for
this rejected revision, and earlier results are not used as its substitute.

The latest bounded repair also changes the class-agent instruction to respect
the latest user-request scope after tool results and limit verification claims
to actual checks. Both source hashes are recorded in its metadata. It passes the
first-step and no-question cases with correct worked eigenvectors. Its partial-
attempt response correctly diagnoses the integrand and produces the right
piecewise solution, but does not acknowledge the student's correct work: the
0-to-t integration range is valid for the initial interval. Its simplification
still uses an echo picture rather than the strict retained overlap/sliding
criterion. The full 13 and held-out 2 results above belong to predecessor 92c8;
they are not substituted for a full run on the latest source.

The last critical gate failed, so no further full run or prompt tuning was
performed in this bounded pass. Preserve PLA-461 as open. The remaining exact
cases need resolution/review and an independent human decision before treating
this provider configuration as release-qualified. The source harness's tool-
trace limits and the broader solver/study/writer/installed-app gates remain.
