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
This memory is bounded to 4,096 characters, keeps whole fronts, and is included in the existing
source-budget and full-prompt chunk-admission checks. It adds no model calls and never changes
the requested count or publishes incomplete output. Oversized fronts and larger decks may
exceed this memory horizon, so it reduces duplicates without claiming a semantic guarantee.
