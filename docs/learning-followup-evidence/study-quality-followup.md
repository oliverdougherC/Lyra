# Study generation follow-up

This follow-up starts from reviewed PR #80 head `2ee865c0da52c94c1f5aa056ea64a2e2de513e4e`.
Older evidence in `docs/evidence/` and the earlier independent study review is unchanged.
The target is a useful complete ecology deck, with valid JSON only an intermediate check.
Ecology retains its original corpus partition, but is now a known failing case; these repair
repeats are not an untouched blind holdout.

## Current failure and isolated repair

The retained ecology retry returned the intended `front` and `back`, then generated whitespace
until `length`. Its schema also required a `topic` string even though production discards this
field: `_propose_topic_cards` consumes only the two faces and `_persist_topic_cards` assigns the
already requested topic. The prompt did not ask for this extra field.

A fresh replay through production `_call_json`, using the exact retained messages and unchanged
model/settings, reproduced the failure at the existing 8,192-token cap. Removing only the unused
schema field let the same request close its JSON object successfully. This controlled comparison
is consistent with a constrained-output conflict around an unnecessary required value; it does
not claim access to the provider's internal decoding implementation.

| Run | Changed input | Outcome | Output tokens | Latency |
| --- | --- | --- | ---: | ---: |
| Fresh baseline | None; original retained request and original schema | `length`, rejected malformed JSON | 8,192 | 43.868 s |
| Schema-only replay | Removed unused required `topic`; messages identical | `stop`, one valid front/back card | 4,400 | 26.257 s |

The returned card names a confounder and explains how it can create a rainfall–abundance
correlation without proving rainfall causes abundance. That meets the retry's one-card deficit;
it does not by itself establish full-deck acceptance.

The model was the authorized remote `Qwen3.8-27B`, configured context 262,144, temperature zero,
maximum flashcard output 8,192, one live study request at a time. No thinking extension, endpoint,
credential or transport behavior changed. The comparison JSON asserts identical retained
messages, configuration, study source hash and study prompt-constant hash. Schema definitions are
retained separately because the older evaluation recorder names schemas without copying them.

After that comparison, the flashcard prompt gained an explicit JSON `cards` array containing only
`front` and `back` strings. This keeps schema fallback instructions consistent with the consumed
payload. The app continues to persist its own requested topic in every card's public content.

The new regression failed before the schema edit and passes afterward; the full focused study
suite passed 270 tests in 8.42 seconds. Existing source-first budgeting of optional prior-question
memory is preserved, as are selected-source scope, retries, truncation rejection, cancellation,
atomic readiness and quiz output reserves. No scheduler or frontend behavior changed.

## Reproduction

Use the existing evaluator and an explicitly authorized isolated settings profile. Private profile
paths and endpoint values are intentionally absent from evidence. These commands use fresh private
workspaces; the baseline replay must use the original schema from the reviewed base.

```bash
PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring uv run python scripts/eval_study.py \
  --profile /path/to/isolated-config/lyra.db --workspace /path/to/fresh-private-replay \
  --output docs/learning-followup-evidence/study-schema-only.json --kinds quiz \
  --replay-report docs/evidence/study-beta-final-deck.json --replay-case ecology --replay-index=-1

PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring uv run python scripts/eval_study.py \
  --profile /path/to/isolated-config/lyra.db --workspace /path/to/fresh-private-ecology \
  --output docs/learning-followup-evidence/study-ecology-decks.json \
  --kinds deck --cases ecology --repeat 2 \
  --models-dir /path/to/existing-model-assets --llama-port 18541
```

Full-deck outcomes are recorded below. Independent semantic review and human
review, other provider configurations and the integrated packaged-candidate rerun are separate
gates, not implied by a successful replay or deterministic tests.

## Full production ecology decks at the fixed source

Both runs used exact source `19b94fbfeac16fda05ba8fa5459a6ef205b4ea75`, the unchanged
`study-beta-1` corpus / `study-critical-1` rubric, app `0.2.0-beta.0`, remote `Qwen3.8-27B`,
configured context 262,144 and temperature zero. Real local nomic embedding and selected-source
retrieval ran on isolated helper ports; no model, retrieval or persistence response was mocked.
The recorder preserves per-call `max_tokens` (8,192 for flashcards, the original larger topic
reserve), reported model identity, usage, stop reason, source/prompt/corpus hashes and latency.
The configured context is not a measurement at its full capacity, and requested structured
format alone does not establish the provider's exact enforcement/fallback capability.

| Run | Terminal artifact | Published cards | Model calls | Internal failures | Total latency |
| --- | --- | ---: | ---: | --- | ---: |
| Ecology 1 | Ready | 14 | 9 | Call 7 exhausted 8,192 reasoning tokens with no content; bounded retry recovered | 139.185 s |
| Ecology 2 | Ready | 14 | 9 | Same internal failure and recovery | 138.228 s |

The published cards are identical across the two repeats. This establishes repeated delivery for
this fixed request, not diversity across independent generations or a population error-rate
estimate. Both artifacts retain selected-source provenance and zero exact duplicate stems. The
cards include density with area units, population extrapolation, selection bias versus sampling
variation, the 200-animal estimate, undefined zero-recapture behavior, mixing and retained-mark
assumptions, and confounding. The earlier erroneous ratio rewrite is now explicitly correct:
`N ≈ M/(R/C) = MC/R`. Topic placement is imperfect: some mark-recapture cards appear under
broader density/sampling labels. A precision concern remains in card 7's categorical migration
bias wording compared with the source's “can bias”; keep that visible in semantic/human review.

The original 483.488-second run delivered no deck; these runs delivered complete reviewable
practice using the existing bounded recovery. The schema repair also removed the reproduced
last-topic malformed-JSON stall in the controlled comparison. It did **not** eliminate internal
model failures: two of the eighteen full-run calls exhausted their reasoning allowance. Those
failures and latency remain recorded individually, rather than hidden behind Ready status.
Truncated/empty content was rejected, no partial deck was published, and no output limit was
raised to force completion.

For each Ready deck, the actual review route handler committed 100 distinct Easy operations;
stability and deadline stayed at the initial 2.8-day schedule, exactly 100 review rows remained,
same-operation retries returned identical acknowledgements, and a conflicting rating was rejected.
These are real persistence checks, not simulated lost HTTP responses or native UI acceptance.

Evidence: [full decks and all calls](study-ecology-decks.json),
[controlled schema comparison](study-schema-comparison.json),
[fresh failure](study-schema-baseline.json), [schema-only success](study-schema-only.json).
The [six-card human review selection](study-human-review.md) includes the qualification concern
and links the full deck. **Actual human review is not run.** Independent-agent review is a separate
record owned by the integrating reviewer. Final packaged-candidate generation, other providers,
large request counts and real student learning outcomes are not established by these runs.
No further study provider calls were made after these two full repeats.

The recorded study source, complete prompts file and corpus SHA256 values were checked against
Git revision `19b94fb` and match exactly. Ruff, documentation-link and active-reference checks
passed; all new study JSON records parse and contain no URL or Bearer-token strings. The study
evaluation processes and isolated helper listeners were gone after completion.

The independent reviewer subsequently read all 28 published card instances and assessed both
decks as **passing the critical semantic gate with noncritical qualification notes**. That review
separates arithmetic, assumptions, grounding, useful reinforcement and topic placement from Ready
status, and preserves the recovered call failures. See the
[independent review](study-independent-review.md) and its per-card JSON record. This is agent
review, not actual human acceptance.
