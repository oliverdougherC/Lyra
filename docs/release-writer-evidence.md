# Writer release evidence — PLA-464–468

Baseline: `3e109a7ef1cdce7362d9f0e8a286881ebf6fa5a5`, checked out from current main in the isolated release worktree. Full current Linear descriptions and all comments for PLA-464–468 were retrieved on 2026-09-05; each comment list was empty. The defects were still relevant. Owner: writer repair lane; integration, PR, packaged acceptance and candidate identity: release owner.

| Issue | Repair | Automated evidence | Candidate status |
| --- | --- | --- | --- |
| PLA-464 | Resolve the immutable supporting snapshot, preserve old revision on replace/reuse/refresh, reject missing material, retain course evidence after upload deletion. Project saved revision/date into prompts, viewer and exports. | Changed-content replace/reuse/same-transaction refresh; same-content refresh; concurrent refresh/add; missing-material refusal; prompt/export provenance; existing writer storage tests. | Source repair verified; packaged source viewer review pending. |
| PLA-465 | Conditional active-state transitions prevent resurrection and lost cancellation; cancellation remains visible after it is settled; failed callbacks settle durable state before mirroring it; completed jobs do not execute again. | Terminal-state permutations; cancelled writer and reviewer wrappers with/without late upstream error; immediate new run after cancellation; existing writer/reviewer suites. | Source repair verified; integrated restart/long-horizon acceptance pending. |
| PLA-466 | Preflight every real `_complete` and live stream request including schema, output reserve and the shared 10% safety margin. Deterministically remove optional preceding prose, next-paragraph summary, then research context when necessary for paragraph calls. Preserve plan, assignment, source IDs/evidence and student body; refuse oversized mandatory material before inference with a recoverable explanation. Continuations/retries are checked again after assembly. | Exact estimator boundary and overflow; schema accounting; mandatory evidence retained while an oversized neighbor is removed; HTTP `/pass` creates a real durable run, rejects a long existing essay at a 2048 window without calling upstream, and preserves the body/suggestion through a new retry. Existing inline-writing and writer-chat budget paths rerun separately. | Source repair verified. Estimator is endpoint-independent, not a claim of exact provider tokenization; a large mandatory plan/evidence set is explicitly refused, not silently truncated. |
| PLA-467 | Stream cutoff and unconfirmed EOF raise an explicit `StreamCompletionError` outcome after delivering partial text; trustworthy `stop` without DONE is accepted. Existing in-band/transport errors retain their failure path. Tutor partial replies persist with an incomplete label and failed retryable attempt. Live paragraphs preserve the last batch on failure and cannot complete solely because text arrived; paragraph and final assembled length checks reject model output under 80% of its target (student-edited blocks remain authoritative). | Stop/DONE/no-DONE/length/EOF transport fixtures; existing reasoning, keepalive, usage and in-band cases; durable partial blocks for length/EOF/short output; tutor API persistence/retry/idempotency suites; durable full pass rejects a too-short review replacement before finalization. | Source repair verified; real-provider quality and packaged UI checks pending. |
| PLA-468 | One absolute monotonic run deadline surrounds each live inference operation; durable cancellation is polled while waiting, including before the first token, and the transport task is cancelled and awaited. Batched output already saved remains intact and no new model batch publishes after cancellation is observed. | Deterministic silent/continually-active operation deadlines and silent cancellation assert bounded exit and task cleanup; durable wrapper cancellation tests verify consistent state. | Source repair verified; real endpoint interruption and packaged queued-work acceptance pending. |

## Before/after verification

Initial seven regressions were run before editing production code: **6 failed, 1 passed**. The failures reproduced three incorrect supporting revision assignments, completed/cancelled run resurrection, and cancellation overwritten by failure.

A separate temporary archive of the exact baseline SHA was then tested with the new regression file and HTTP durable-budget regression: **20 failed, 3 passed**. This included the actual oversized durable request reaching the forbidden upstream stub and the cancelled writer/reviewer wrapper becoming completed. Some failures are missing-new-contract failures (the explicit stream error and bounded-operation helper do not exist in baseline), rather than independent transport measurements. No production data was used.

Focused combined verification before the final finalization-length guard: **388 passed** across:

- `test_release_writer_regressions.py`, `test_exporting.py`
- `test_api_drafts.py`, `test_api_chat.py`, `test_tutor_chat_safety.py`
- `test_writer_storage.py`, `test_writer_pipeline.py`, `test_review_pipeline.py`, `test_client.py`
- `test_inline_write_budget.py`, `test_api_writer_chat.py`

After the final guard and its durable too-short-review regression, `test_writer_pipeline.py`: **40 passed**. The preceding focused writer/regression run was **67 passed**. The release owner is running the integrated suites on final combined changes.

Ruff checks passed on all writer-owned Python files; `git diff --check` passed. Frontend `npm run typecheck` and ESLint on `source-ledger.tsx` and `types/index.ts` passed; Prettier left those files unchanged.

## Remaining acceptance boundaries

These changes are available for independent review, not a claim that a signed/notarized beta or full Linear acceptance has passed. This lane did not perform installed-app/restart/provider quality, assistive-technology, physical-input or long-horizon tests. The release owner must supply candidate build identity and actual packaged evidence. No issues were closed and no commits or external messages were made by this lane.

Input handling deliberately stops before inference if mandatory existing writing, plans or evidence cannot fit. It does not promise automatic summarization of arbitrary long assignments. Saved model blocks that fall below their requested length are retained as a failed/recoverable suggestion rather than silently retried into potentially duplicated prose.

## Independent review follow-up

Two reproduced findings were repaired before the review freeze:

- Cancellation settlement now changes the run, its own live suggestion and the artifact mirror in one transaction. The mirror and trailing worker timestamp are conditional on no newer run owning the artifact. Deterministic writer and reviewer barrier tests pause immediately after the cancellation commit, create a successor through another connection, then resume the old callback; the successor remains queued/generating with no stale completion timestamp.
- Historical excerpt associations written incorrectly by older Lyra builds are resolved against real immutable snapshots on read, without database writes. Reusing an existing excerpt repairs its stored association. If no supporting material remains, projection/export/viewer explicitly label it unavailable and reuse is rejected; the excerpt text is preserved.

The four new parameterized regressions all **failed on the exact baseline archive**. Final focused writer/source/reviewer/export verification: **118 passed**. Ruff, frontend typecheck, targeted ESLint and Prettier checks passed after these follow-ups. No source mutations remain pending in this lane.
