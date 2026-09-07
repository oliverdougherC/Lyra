# Writer quality and durable recovery corpus

The versioned corpus is [writer_quality.v1.json](../scripts/eval_corpora/writer_quality.v1.json).
It contains invented course documents and student prose, not student data or evaluation results.
Freeze its version and SHA before taking a baseline. Keep results from that baseline even after
fixing a defect; never replace a failed observation with the improved run's result.

The nine cases cover targeted policy revision, review with a standing comment, adversarial
empty/off-target replacements, changed and deleted evidence, multi-section recovery, a long draft
under a tight context limit, literary review with invented dialogue, and a complete two-section live Draft proposal from student notes. The existing prose gives
the assistant an argument and a recognizable voice to preserve. This compact corpus is a beta
regression instrument; it does not establish quality across disciplines or student populations.

## Fixture contract

Each case has an `id`, `operation` (`pass` or `review`), a literal Markdown `body`, a confirmed
`brief` matching the HTTP brief payload, synthetic `sources`, and a `plan`. `pass_payload` and
`review_payload` use the existing HTTP contracts. The repeated top-level `instruction` is a
readable evaluation intent; the review endpoint currently accepts only depth, so that instruction
must not be represented as an injected HTTP reviewer instruction.

Resolve each source's stable fixture `key` to the fresh profile's numeric source ID. Resolve
`plan.sections[].source_keys` to the API's `sources` array; do not send `source_keys` as if it were
an API field. Seed each excerpt only after its supporting content is stored. For source history
cases, capture the original source revision and excerpt association before applying the specified
mutation. The deletion case seeds an uploaded course document, then deletes it through the HTTP document
endpoint (`delete_document`). Its retained course snapshot is explicitly unversioned. The separate
replacement case tests immutable web-source revisions. Neither case deletes a writer-source ledger
row directly. A fixture revision of 1 identifies the first seeded version, not an assumption about
an arbitrary existing database's numeric revision. Use a fresh isolated profile for each case.

Expand `body_repeat` by appending `\n\n## {section_title}\n\n` to `body`, followed by
`repeat_count` paragraphs separated by blank lines. Replace `{index}` with consecutive decimal
integers starting at `index_start`. Include the complete expanded body in the evidence receipt and
hash it. The 160 observations are deliberately synthetic repetition for a deterministic budget
stress test, not 160 independent writing-quality examples.

`initial_comments` describes already-open findings. `adversarial_provider_outputs` describes
fault fixtures for the controlled endpoint; these must never be substituted into a real-model
quality run and then reported as that model's behavior. `recovery_scenarios` is required coverage,
not proof the harness implemented or executed each scenario. Unsupported scenarios must be marked
`not-run` with a reason.

The targeted policy cases use parsed child section references `1.1`, `1.2`, and `1.3` beneath
the document H1. Their HTTP requests exercise the targeted pass path.
`full_live_draft_from_student_notes` instead starts the complete durable live Draft pipeline with
an empty section filter and a two-section proposal plan; it preserves the accepted notes while
producing a separate proposal. Its `original_notes_body_only` expectation applies to the accepted
body, not to requiring verbatim notes inside the proposal. The long-draft case also uses a full
pass so its mandatory-body budget check covers all existing prose.

## Deterministic invariants

Compute invariants from persisted body, suggestions, blocks, comments, run records, and source
snapshots, not solely from the closing assistant message. Preserve raw observations alongside the
verdict so someone else can reproduce it.

| Measure | Required evidence and interpretation |
| --- | --- |
| Student body preservation | For review, hash the whole body before and after. For revision, compare literal protected passages and out-of-scope sections in both body and proposed candidate. A suggestion can violate scope even when the accepted body is unchanged. |
| Unrelated edits | Count changed protected sections and changed characters outside allowed sections. Report the diff; zero occurrence of a marker alone cannot prove preservation. |
| Empty/unsupported rewrite | Record empty or rejected replacements and candidate prose containing a seeded unsupported assertion. The assertion quoted in a review comment is not an unsupported rewrite. Semantic new unsupported claims require separate review. |
| Duplicate comments | Compare existing and newly persisted comments by canonical anchor, severity, and finding. Exact duplicates are deterministic; paraphrased duplicates require adjudication and must be reported separately. |
| Duplicate paragraphs | Compare normalized exact paragraph text and durable block identities against pre-interruption settled output. Natural repetition already in the fixture is not a retry-created duplicate. |
| Historical citation support | Resolve the excerpt to immutable content containing the exact excerpt, retain the captured revision/date, and verify refresh/deletion never relabels it as current support. An excerpt's existence alone does not establish that a rewritten claim follows from it. |
| Recovery ownership | Record actual HTTP-created run IDs, successor IDs, checkpoint and event order. Stale callbacks must leave successor state and timestamps unchanged. |
| Honest completion | Interrupted, malformed, cancelled, deadline-limited, or insufficient-length work must keep useful partials without falsely reporting completion. A completed status is insufficient without persisted output evidence. |
| Mandatory budget | For the tight-window case, confirm refusal before provider inference when mandatory content cannot fit. Preserve the entire original body, plan, and evidence; no truncation to manufacture success. |

A seeded issue is **missed** only after an evaluator checks persisted comments or revised prose
against its expected repair. `required_findings` contains seed IDs, not strings a model must emit.
Count found and missed seeds separately; include the unresolved seed text and the evaluator's
reason. Report `not-scored` rather than assigning zero misses when no semantic adjudication ran.

## Subjective dimensions

Score each applicable dimension from 0 to 4 independently. Use `not-applicable` where the workflow
does not exercise a dimension and `not-scored` where no evaluator assessed it. Every score needs
an output excerpt, a short rationale, evaluator type, and evaluator identity/version.

| Dimension | 0 | 2 | 4 |
| --- | --- | --- | --- |
| Planning | Ignores assignment or cannot form a usable plan | Plausible sections with weak claim/evidence alignment | Proportional, executable section jobs tied to the brief, evidence, and existing draft |
| Evidence/citation | Invents support or misattributes evidence | Mostly grounded with an unaddressed inference or citation gap | Claims follow from identified supporting revisions; limitations and conflicting evidence remain visible |
| Revision precision | Deletes work or changes unrelated passages | Addresses the target but makes unnecessary changes | Repairs the requested passage with minimal collateral change and clear reasoning |
| Review usefulness | Misses central seeded problems or offers misleading advice | Finds a meaningful problem but gives vague or repetitive advice | Prioritizes concrete, correctly anchored, distinct findings with actionable repairs |
| Instruction following | Violates a core scope/content requirement | Meets the main request but misses a secondary requirement | Meets all applicable scope, content, length, and evidence constraints |
| Student voice | Replaces the student's perspective with generic prose | Retains ideas but flattens cadence or uncertainty | Preserves stance, characteristic language, and productive ambiguity while improving clarity |

Use 1 and 3 for outcomes between adjacent anchors. Do not average these dimensions into an
acceptance score that hides a citation or preservation failure. A polished passage with invented
support fails the evidence dimension regardless of its fluency.

## Comparison and receipts

Start actual runs through `POST /api/drafts/{id}/pass` or `/review`, then poll the returned durable
run. A direct helper invocation or mocked provider is regression evidence only. A controlled
OpenAI-compatible endpoint can test durable orchestration; label it `deterministic-fixture`, never
real-provider acceptance.

For each case retain corpus SHA/version, source SHA, environment, profile isolation settings,
model identifier/provider type, context settings, request payloads, actual run IDs, timing and
fault injection event, pre/post body hashes, source snapshots, plan versions, persisted blocks,
comments, terminal states, errors, and each invariant/score. Never retain credentials or normal
student content. Link per-case receipts from baseline and improved reports.

Compare an uninterrupted run with interrupted runs on the **same model and settings**, including
inference, section boundaries, review/persistence boundaries, retry, cancellation and process
restart. Report transient failure, rate-limit, partial/malformed-stream, and deadline tests
individually. Compare already-settled content exactly. Future nondeterministic prose need not be
byte-identical, but must meet the same scope, evidence, and quality rubric. Model-to-model quality
comparisons belong in a separate table and cannot explain away a same-model recovery regression.

Separate four evidence classes in the report: deterministic invariants, subjective evaluator
scores, independent-agent review, and actual human review. An agent's review is never human review.
Absent real-provider access, packaged execution, an independent evaluator, or human review, mark
that evidence `blocked` or `not-run`; do not imply acceptance from synthetic endpoint results.

The release owner should rerun the same corpus against the signed packaged candidate's backend,
record its SHA and bundle identity, and compare it with the baseline before making a beta decision.
This corpus and rubric do not waive the packaged, consent, credential, or human acceptance gates.

## Running the corpus

Use the locked development environment and a dedicated synthetic endpoint configuration. The
harness creates fresh per-case profiles, forces NullKeyring and overrides data/cache/log/model
paths for every backend subprocess. It starts the production desktop bootstrap with an inherited
loopback socket and authenticates each HTTP request. Source ledger seeding uses production storage
functions because the app has no public endpoint for arbitrary synthetic source ingestion.

```bash
uv run python scripts/eval_writer.py --fault-provider --output /tmp/writer-fixtures
uv run python scripts/eval_writer.py --config-db /path/to/isolated-config.db \
  --allow-remote --output /tmp/writer-baseline --source-root /path/to/baseline-checkout
uv run python scripts/eval_writer.py --config-db /path/to/isolated-config.db \
  --allow-remote --output /tmp/writer-candidate \
  --backend-executable /path/to/Lyra.app/Contents/Resources/resources/lyra-backend/lyra-backend
```

The configuration database must be a dedicated, nonsecret evaluation handoff, not a student
profile. Only endpoint/model/context settings are read. Supply an evaluation credential through
`LYRA_EVAL_API_KEY` only if required. Retained reports redact that credential and the private
endpoint. `--allow-remote` is explicit consent to send this synthetic corpus to a non-loopback
endpoint. Model configuration and locality are recorded without its private address.

Use `--case` to select fixtures, `--scenario` for interruption/fault comparisons and
`--generate-plan` to omit the fixture plan and measure actual model planning. Keep generated-plan
runs separate from seeded-plan runs. A requested scenario that never reaches its boundary is
not executed coverage. A harness timeout also differs from the product's own inference deadline.

## Recovery behavior under evaluation

Completed review lenses and live paragraph reviews are durable boundaries. Failed, malformed or
incomplete assessments retain useful prose/comments and fail the run; they do not count as a
clean review. Empty revisions keep the previous paragraph and require recovery. Revision calls
include the current passage as mandatory input so the model can correct it rather than regenerate
from a topic summary. The existing context preflight may refuse this material if it cannot fit.

Model comment and live-block writes check run ownership while holding SQLite's writer lock.
Cancellation and terminal settlement fence late callbacks. Failure mirrors, and the review's
closing summary/completion, commit atomically. Exact unanchored findings deduplicate on retry;
semantic paraphrase duplication remains an evaluation concern. Live suggestion publication reads
student edits under the same lock used to create the pending proposal. The student's document
still changes only through the established targeted-write or proposal-acceptance contracts.

Reviewers can inspect saved source pages with `read_source`, scoped to the current class and an
explicit immutable revision when supplied. Pages are bounded and disclose omitted content; a
missing historical snapshot never falls forward to newer evidence. Selected excerpts are not a
complete source, so an uninspected passage must not be treated as absent. Courses containing only
saved ledger evidence no longer start an embedding runtime to search an empty indexed scope.

Structured writer outputs are capped at 4,096 tokens (or the smaller existing reserve); wide
input windows do not imply unbounded planning output. The research schema uses integer source
IDs to match the saved ledger. Structured calls request direct output through the existing
optional thinking control. If a compatible endpoint explicitly rejects that field with HTTP 400,
the same captured request retries once without it; other failures retain their prior semantics.
The schema and source-validation contract are not downgraded by that field negotiation.

A saved plan does not replace the assignment or student prose: both are mandatory live-generation
context and are preflighted before research. Targeted critiques read the current nonstale proposal
and receive the original student requirements; later corrections cannot overrule those requirements.
Research can inspect bounded saved-source context with explicit omission and revision labels.
Model-owned live blocks normalize known numeric citation shorthand to the application's marker;
unknown references stop publication. User-edited text is not silently reformatted.

A matching failed or cancelled full-pass retry retains the previous live blocks, user revisions,
and reviewed-work checkpoint in its current successor suggestion. This is conservative: changed
request, depth, original body, plan, brief, provider configuration, or source state starts fresh
instead of mixing incompatible work. Prior rows are retained; no terminal run is resurrected.

`--retry-failures` retries an observed injected failure once in the same synthetic profile, so
rate-limit, transient-error and incomplete-stream recovery can be compared without replacing the
profile. Resumed paragraph calls retain saved text and remove demonstrable echoed prefixes before
publication; a second incomplete stream still fails. Exact original receipts remain available.
