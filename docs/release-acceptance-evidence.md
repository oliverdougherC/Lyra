# Release acceptance evidence — September 6, 2026

This audit read the complete current Linear PLA-150–153, PLA-461, PLA-147,
PLA-160, PLA-329 and PLA-404 records/comments, plus the live remaining Lyra
backlog. Baseline: `3e109a7ef1cdce7362d9f0e8a286881ebf6fa5a5`. Candidate build,
signing, installer hashes and integrated checks belong in the release ledger;
the bounded checks below are not candidate certification.

## Preserved completed baseline

PRs #70/#71 are merged and independently reconciled. The current issue evidence
records all 11 hosted lanes green on [main CI 33997723622](https://github.com/oliverdougherC/Lyra/actions/runs/33997723622):
2,881 backend tests/one existing skip, 932 frontend tests, 131 deterministic
full-stack tests, 34 native Rust tests, 31 Chromium and six WebKit tests. The
full-stack ledger had zero unconsumed backend failures or owned survivors.
These are retained hosted results, not fresh execution by this audit.

39 PLA-404 software children and study PLA-469/470/472/474/476/477 are Done.
Preserve late-save/finalize barriers, accessible card faces/actions, recovery,
source-scoping, scheduling/retry correctness and native no-overwrite saving.
Historical unmerged notes inside issues do not reopen that completed work.

## Exact remaining provider and quality gates

All five quality issues below remain Todo. Every retained run needs exact app
SHA/version, corpus/prompt/rubric versions, remote versus loopback locality,
model/capabilities/context/generation settings, timestamps and repetitions.
Never retain keys or credential-bearing/private endpoint URLs. Keep the same
model/configuration when attributing differences to packaging or recovery.

| Gate | Required evidence still missing | Available starting point |
| --- | --- | --- |
| [PLA-150](https://linear.app/platinum-labs/issue/PLA-150) | Two materially different course/problem styles beyond engineering; equations, prose, multipart and segmentation stress; repeated segmentation; separate parsing/reasoning/verification/final-answer scores with unmeasured preserved; per-course and aggregate report | `scripts/eval_solver.py` drives production ingestion/solver in a disposable workspace; current corpus layout assumes Homework/Homework Solutions |
| [PLA-151](https://linear.app/platinum-labs/issue/PLA-151) | Versioned representative multidocument corpus, repeated supported-provider terminal flashcards/quizzes, human-reviewed factual/math keys, grounding/coverage/duplicate metrics, scheduling and weakness metrics; selected A+C versus excluded B/reindexing, malformed generation, cancellations, 100 same-day ratings, committed-response-loss recovery and actual citation opening | Production study routes and deterministic `study-source-contract`, `study-recovery`, `study` and restart acceptance; these are fixtures, not real-model quality |
| [PLA-152](https://linear.app/platinum-labs/issue/PLA-152) | Versioned briefs/sources/drafts/plans/comments with existing student prose, unsupported claims and seeded empty/unrelated rewrites/duplicate comments; independent planning/evidence/citation/revision/comment/instruction rubrics and current-main baseline | `backend/tests/evals/writer_turn_contract.v1.json` only measures routing/tool intent; it is not a full writer-quality benchmark |
| [PLA-153](https://linear.app/platinum-labs/issue/PLA-153) | PLA-152 corpus through packaged durable Draft/reviewer paths; interrupt between sections/during calls/review boundaries, restart/retry/rate limit/disconnect/malformed streams; compare settled prose, duplication, missed issues, unsupported/unrelated rewrites against uninterrupted runs | Deterministic writer/restart acceptance and durable jobs; full real-provider comparison is outstanding |
| [PLA-461](https://linear.app/platinum-labs/issue/PLA-461) | Resolve critical first-step/proportionality failures across agreed supported configurations; held-out start-help/partial attempt/checking/concept/urgent/full-solution/multiturn Guide–Show cases, transcripts/terminal answers/stop reasons, repeat distributions and independent human spot review | `scripts/eval_tutor.py run --surface class_chat`, then `grade` and `report`; production planner/tool loop is supported. Prior Qwen class-chat 12/13 is historical and failed first-step guidance; self-judging is insufficient |

The subsequent bounded real-service run used the configured remote Qwen3.8-27B
endpoint and the production class-chat harness with synthetic material. The first
13-case run reached terminal output but its same-model judge passed only 11/13;
Guide first-step and simplification failures remain. A real Exa search/content
smoke also passed. See [retained provider evidence](release-provider-evidence/README.md).
These are source-working-tree results, not an immutable packaged-candidate pass
or independent human judgment. Existing ignored local outputs remain historical.

## Physical and installed-app gates

PLA-404 stays In Review with these 11 exact outstanding child checks:

| Issue | Actual owner-run observation required |
| --- | --- |
| PLA-407 | Packaged WebKit physical CJK candidate confirmation does not send; subsequent Enter sends once, Shift+Enter inserts newline |
| PLA-418 | Screen reader reads active question/answer including math and excludes hidden card face |
| PLA-425 | Screen-reader MCQ/fill-blank grading and question/results focus journey |
| PLA-428 | Screen-reader traversal of equivalent extracted-text source route |
| PLA-442 | Spoken Added/Removed/Unchanged diff distinctions |
| PLA-445 | Spoken Work filter pattern and pressed-state announcements |
| PLA-446 | First-time nonprogrammer explains primary choices |
| PLA-447 | Touch-only tester discovers Rename/Delete without invisible hit regions |
| PLA-448 | First-time tester finds and moves among Plan/Sources/History |
| PLA-450 | Actual 200% zoom, both faces/long answers/initial and maximum scroll: ratings unobscured |
| PLA-452 | New tester immediately identifies next setup step with no files/no endpoint |

DOM/ARIA/viewport/keyboard tests do not certify these observations. No new human,
assistive-technology, physical input or native zoom pass is claimed here.

[PLA-329](https://linear.app/platinum-labs/issue/PLA-329) requires a clean **8 GB
Apple Silicon Mac**: installed bundle <=500 MB, usable shell <=5 s after initial
migration, aggregate settled idle RSS <=500 MB after 60 s, quiescent CPU, no idle
helper/forbidden service, and investigation of unexplained >150 MB post-eviction
growth. Record complete Rust/WebKit/Python/helper tree, cold/warm launch, navigation,
ingestion, real chat/Exa, writing, rerank/OCR, eviction, quit/relaunch and repeated
sessions; include disk/cache/files/threads, sleep/wake and memory pressure. This
host reports **24 GiB** (`sysctl -n hw.memsize`: 25769803776); it cannot establish
an 8 GB pass.

[PLA-147](https://linear.app/platinum-labs/issue/PLA-147) is still In Progress.
`scripts/packaged_soak_harness.py` prepares/records a manual scenario; it does not
execute packaged UI work or certify its recorded outcomes. Run sustained mixed
classes/ingestion/retrieval/tutor/Exa/solutions/study/writing with outage/retry,
background/sleep/quit/crash/relaunch, helper eviction, migration and backup/restore.
Finish with SQLite integrity, durable jobs/artifacts, settled storage intents,
files/publications, process ownership, privacy and resource assertions. Retain
failure artifacts and a human go/no-go on the immutable candidate.

[PLA-160](https://linear.app/platinum-labs/issue/PLA-160) additionally needs the
exact DMG downloaded/mounted/dragged to Applications/Finder-launched on a clean
supported account without developer tools or warm models; Developer ID,
notarization/stapling/Gatekeeper; first-use download interruption/retry versus
warm-cache offline; real tutor/Exa and privacy/disclosure; every advertised export,
backup/restore and migration route without undeclared Pandoc/Typst installations;
actual populated-data N→N+1 signed update and recovery/rollback/Keychain continuity.
Development signing and frozen startup are useful local evidence, not these passes.

## New bounded audit fix: PLA-480

The original soak plan emitted `LYRA_DESKTOP_PROFILE_DIR`,
`LYRA_DESKTOP_EVIDENCE_DIR` and `LYRA_DESKTOP_LOG_DIR`, which the shell/backend do
not consume. Following it could open the real student profile during nominally
disposable destructive tests. [PLA-480](https://linear.app/platinum-labs/issue/PLA-480)
tracks the correction and blocks PLA-147/160 until integrated/reviewed.

The plan now emits absolute supported `LYRA_DATA_DIR`, `LYRA_DB_PATH`,
`LYRA_CACHE_DIR`, `LYRA_LOGS_DIR` and `LYRA_MODELS_DIR` paths. Explicit DB override
prevents an ambient source-checkout DB override defeating isolation. The local
execution plan contains private absolute paths: redact it before publication.
Filesystem overrides do not isolate macOS Keychain: credential-mutating acceptance
must use a disposable macOS account and test credentials.

Executed regression first failed because real packaged `Settings` resolved the
normal macOS Application Support location. After correction it resolves all
mutable paths beneath the disposable run, even after changing working directory
and with an ambient DB override. No actual student files/database were opened.

Audit verification command:

```sh
python -m pytest backend/tests/test_packaged_soak_harness.py backend/tests/test_desktop_resource_report.py backend/tests/test_desktop_runtime_report.py backend/tests/test_eval_solver.py backend/tests/test_eval_tutor.py -q
```

Result: **45 passed** (existing SWIG deprecation warnings). Ruff lint and format
checks passed for the changed harness and test. Integration owner owns final
candidate rebuild and complete required gates; no installer or real provider pass
is inferred from these tests.
