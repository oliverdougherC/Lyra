# Study beta quality evaluation

This is the bounded PLA-151 production-generation evaluation. It measures generated content,
not only successful requests. It complements the deterministic reliability contracts in
[study reliability](study-reliability.md); neither replaces final integrated-candidate or human
review. No new provider or human acceptance is asserted by this document.

## Corpus and acceptance

`scripts/eval_corpora/study-beta.json` version `study-beta-1` supplies synthetic multi-document
circuits and ecology courses with known source pages, an excluded contradictory answer key,
and an administrative-only insufficient-evidence case. Circuits is the development domain;
ecology is a held-out subject variation. Both domains use explicit formulas, units, worked
examples, assumptions, and conceptual distinctions. The ecology baseline was inspected to
diagnose a general standalone-question failure; it is not an untouched blind test afterward.
No corpus-specific matching rule is added to production prompts.

Critical failures are reported individually: unsupported answers, missing conditions that
make a question unanswerable alone, ambiguous keys, useless or repeated practice, fabricated
Ready content on insufficient material, and excluded-source provenance. A mechanically valid
artifact is not a semantic pass. Basic/intermediate/exam difficulty is assessed against the
requested reasoning burden, with source-supported transfer permitted rather than only verbatim
recall. Exact duplicate stems are measured mechanically; semantic duplicates require review.

## Reproduction

Use a **new, isolated, separately authorized settings database** containing the desired endpoint,
model, context window, locality permission and measured tool capability. The runner copies only
those settings into a fresh workspace. It does not discover student profiles or use Keychain.
Authenticated endpoints are unsupported by this runner until an explicitly isolated credential
adapter is provided. Never pass a student's profile as the evaluation profile.

```bash
PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring uv run python scripts/eval_study.py \
  --profile /path/to/isolated-config/lyra.db \
  --workspace /path/to/new-private-study-run \
  --models-dir /path/to/existing-model-assets --llama-port 18441 \
  --output /path/to/study-evidence.json --repeat 2
```

For quiz-only evaluation without local embeddings add `--kinds quiz`. Deck evaluation uses
real local nomic embeddings, real selected-source retrieval and the production generation worker.
No generated answers or embeddings are mocked. Existing model assets are required for the
recorded runs; model downloads are not evidence of a working configuration. The supplied helper
port must be unused. The runner stops only its own managed helpers on exit.

The output retains synthetic source text, model-call prompts and terminal answers, reported stop
reasons, per-call and per-artifact latency, content/provenance, app version, Git SHA, source and
prompt hashes, corpus/rubric versions, locality class, model identity, context and generation
parameters. It omits endpoint URLs and secrets. The transport still follows its production schema
fallback; a schema request alone is not proof the provider enforced it. Source/backend hashes
identify uncommitted changes when the tree differs from the recorded SHA.

Ready artifacts additionally exercise production route handlers directly: two quiz attempts,
alternating versus correct responses, persisted resume and idempotent finish; and 100 genuine
same-day Easy reviews with identical-operation retries plus conflicting-rating rejection. This
is real persistence and scheduling, but does not simulate lost HTTP responses or UI recovery.
Those remain covered separately by the existing integrated acceptance tests. Topic scores reflect
the recorded selections against generated keys, whose factual quality is reviewed separately.

## Implemented changes

The baseline required every quiz to reach its count even when only administrative facts were
provided; it produced padded questions and an unsupported claim about a seminar's name. Topic
mapping also split that material into artificial topics and produced ten cards that repeatedly
asked two administrative facts. Prompts now allow fewer grounded topics/questions or empty
results, and exclude administrative details from subject practice. Existing truthful completion
continues to fail an incomplete request without publishing partial content.

A baseline ecology question referred to “the estimated density” without giving the density,
depending on a previous question. Quiz prompts now require all necessary conditions and quantities
in each stem, distinct knowledge probes, appropriate difficulty, and justified answer keys.
Baseline cards strayed from the requested topic when retrieval supplied a broader passage,
creating repeated power questions in two topics. Card prompts explicitly stay on their named
topic and require distinct, self-contained recall tasks.

A separate deterministic regression found that card faces supplied as booleans, numbers, arrays
or objects were stringified and published. Card validation now requires actual nonblank strings;
invalid proposals use the existing bounded recovery. Eight regressions failed before this change
and passed afterward. Selected-source, cancellation, context/output limits, retries and atomic
Ready publication remain in the production path.

## Review separation and remaining gates

The retained model outputs require item-by-item review, with failures visible per case and repeat.
The generation model does not judge itself in this runner. Implementation-owner review is distinct
from independent-agent review, and both are distinct from actual human review. Human review,
other provider configurations, signed packaged-candidate generation, source-opening UI and final
merged-candidate repeats remain release-owner gates. Deterministic safety tests establish only their
specific software contracts, not universal model correctness.

The first improved run still repeated conceptually identical questions across related deck
topics. That intermediate failure is retained in `study-beta-improved.json`; it is not counted
as a passing final deck. A follow-up supplies later topic calls and retries with the already
accepted question fronts, instructing them to choose a different supported knowledge probe.
This memory is bounded to 4,096 characters and keeps whole fronts. Evidence is retrieved and
admitted first; memory uses only the remaining input room. It adds no model calls and never changes
the requested count or publishes incomplete output. Oversized fronts and larger decks may
exceed this memory horizon, so it reduces duplicates without claiming a semantic guarantee.

The runner checkpoints each model call before dispatch and on return, including an `in_progress`
record. Interrupting a future run therefore retains completed calls and the current request even
when no artifact was published. Older retained runs predate this checkpoint change; do not infer
missing in-flight transcripts or completed answers for an interrupted older run. Runner hashes are
recorded for new runs so this recording change is distinguishable from production behavior.

The full-deck follow-up at `700d02f` fixed the repeated circuit-card problem, but the first final ecology
deck failed after 483.488 seconds. Its last topic produced one card, then its bounded retry hit
`finish_reason: length` at 65,536 output tokens after 337.3 seconds of largely whitespace JSON.
No cards were published. The remaining final ecology repeat was cancelled through the production
cancellation handler and the owned evaluation process was interrupted under the runtime/cost
budget. It is not a semantic pass. These failures remain in the evidence.

The general generation reserve scales with the configured context window; at 262,144 tokens it
allowed this small study task 65,536 output tokens. A further flashcard-only fix caps standard
`max_tokens` at `min(8192, generation_reserve(context_window))`; topic and quiz calls retain
the existing generation reserve. The existing more conservative
input ceiling, bounded retry and fatal truncation behavior stay intact. No nonstandard thinking
flag is injected into providers. A single captured-prompt replay evaluates this bound; it does not
constitute a successful full ecology-deck evaluation or replace the final-candidate/human gates.

Reproduce the single offending-call replay (its default index `-1` selects the last call of the
first matching case with a transcript):

```bash
PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring uv run python scripts/eval_study.py \
  --profile /path/to/isolated-config/lyra.db --workspace /path/to/new-private-replay \
  --output /path/to/study-cap-replay.json --kinds quiz \
  --replay-report docs/evidence/study-beta-final-deck.json --replay-case ecology --replay-index=-1
```

`--kinds quiz` avoids starting local embeddings for this captured flashcard-prompt replay; the
replay still invokes the actual study JSON call with the flashcard schema. Inspect its retained
stop reason and output rather than interpreting a zero runner exit status as semantic success.

## Recorded outcome and release handoff

The flashcard cap replay on `3f9c84f93de6902aa1e0e60cebafeb3c18a3787b` still returned malformed,
whitespace-padded JSON and `finish_reason: length`. It raised `UpstreamError` after 44.221 seconds
at 8,192 output tokens, versus 337.3 seconds and 65,536 output tokens for the original captured
call. This verifies the narrower cost/runtime bound and continued refusal of truncated content.
It does **not** repair or pass the full ecology-deck quality gate. No further provider run was made.

| Surface | Baseline | Improved measured outcome | Acceptance scope |
| --- | --- | --- | --- |
| Circuit quizzes, two repeats | Correct keys, repetitive example coverage; one repeat had a dependent stem | Both independently reviewed passing; distinct topics, explicit conditions, valid answers | Recorded provider and corpus only |
| Ecology quizzes, two repeats | Direct worked-answer recall at exam level; one dependent density stem | Both independently reviewed passing keys, standalone questions and transfer; one imprecise possible-confounder example remains noncritical | Recorded provider and corpus only |
| Administrative-only quiz/deck | Padded Ready artifacts, duplicates and an ambiguous/unsupported quiz key | Both kinds failed honestly with empty output in both repeats | Insufficiency handling, not a useful rich-source deck |
| Circuit decks | Semantic repeats across topics | First prompt-only candidate still failed; bounded prior-front follow-up passed both independent reviews, 24 cards | Full deck at `700d02f`; later output cap not a full-deck rerun |
| Ecology decks | Not run at original baseline | Prompt-only candidate repeated facts; memory follow-up produced no deck after output truncation; second repeat cancelled under budget | **Failed / incomplete. Do not approve this configuration for this corpus** |
| Scheduling and quiz weakness | Existing deterministic contracts retained | Actual route handlers: 100 genuine Easy reviews keep 2.8-day stability/deadline, 100 logs, same-key replay, changed-rating conflict; quizzes score 3/5 then 5/5 and resume/finish replay | Persistence contracts, not HTTP/UI or human validation |

Raw records are `docs/evidence/study-beta-baseline-quiz.json`,
`study-beta-baseline-deck.json`, `study-beta-improved.json`, `study-beta-final-deck.json`, and
`study-beta-cap-replay.json`. Independent findings are in
[the study agent review](learning-beta-evidence/study-agent-review.md). The prompt-only intermediate
and the failed/cancelled full-deck outcomes remain visible; none is averaged into a passing score.

The focused study suite passed **265 tests** after limiting the cap to flashcard calls:

```bash
uv run python -m pytest backend/tests/test_study.py backend/tests/test_study_beta_eval.py \
  backend/tests/test_study_scope_contracts.py backend/tests/test_study_durability.py \
  backend/tests/test_study_concurrency.py backend/tests/test_study_cancellation.py \
  backend/tests/test_api_study.py -q
```

Ruff, documentation link checking and the active-reference scan passed. The three harness
mechanical-check tests were also rerun after per-call checkpoint recording was added. Tests used synthetic
profiles and isolated credentials. Full backend, frontend, signed-bundle, frozen smoke and native
launch verification are the integrating owner's separate evidence. Human review, full generation
on the final integrated packaged candidate, the largest 30-question requests and other provider
configurations are not run here. PLA-151 remains open for those gates and the demonstrated ecology
failure. The next owner decision is the beta's supported configuration/corpus scope and whether to
resolve this provider's structured-output failure before admitting ecology-style deck generation.

A token-usage audit found that the successful pre-cap ecology quizzes consumed 16,948 and 10,697
completion tokens, including reasoning, despite short final JSON answers. The provisional blanket
8,192 study cap at `3f9c84f` therefore could not carry their semantic acceptance forward. Before
handoff, the cap was restricted to `FLASHCARDS_SCHEMA`, preserving the original quiz and topic
reserves. New regressions assert 65,536 for quiz/topic calls at a 262,144-token window, 8,192 for
flashcards, and unchanged behavior for smaller windows. The recorded one-call flashcard replay
remains applicable because its dispatch parameters are unchanged; it is not relabelled as a
new provider run. Successful final deck calls used at most 7,128 completion tokens. No provider
thinking extension or shared transport change was made.

Final code review added four failing-before regressions for a 2048-token window and an
exact-fit source, on first and retry calls. Evidence is now retrieved and admitted first;
optional whole-question memory uses only remaining context room (still capped at 4096
characters). All 269 study checks pass. Large-window memory behavior remains covered; this
software repair does not change the recorded failed ecology semantic acceptance.
