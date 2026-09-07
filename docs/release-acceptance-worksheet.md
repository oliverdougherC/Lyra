# Candidate native acceptance worksheet

Status: **not run; stable candidate pending**. This worksheet is preparation, not a request
for human review. Record pass/fail/blocked/not-run, observer, time, and a privacy-safe receipt
for each row. Follow the [release ledger](release-evidence.md),
[macOS checklist](macos-apple-silicon-release-checklist.md),
[local deployment](local-deployment.md), and [release procedure](releasing.md).

## Candidate and isolation record

| Field | Record before execution and verify again on relaunch |
| --- | --- |
| Candidate | Source SHA/tree, clean status, version/build/schema, app/backend/DMG/archive hashes, signing mode |
| Environment | Authorized disposable macOS account/device, account identifier, hardware/RAM/macOS, GUI access |
| App ownership | Installed path, compiled bundle identifier, exclusive single-instance IPC endpoint, owned process tree |
| Browser storage | Actual WebKit store location/identity and evidence it belongs only to this disposable environment |
| Mutable roots | Database, data, cache, logs, models, import/backup/recovery paths; all synthetic and isolated |
| Credentials | Synthetic credential target and Keychain identity; no credential values in receipts |
| Workload | Corpus/scenario version, provider configuration class/settings, helper/model versions, bounded request allowance |
| Timing | Agreed duration: **not declared / not run**; start/end and checkpoint times: **not run** |

Only the normal account was available during this pass. Native execution is blocked until the
already-required isolated environment is accessible. Do not launch/select/replace/reopen
production-identity Lyra on the normal account. Backend path overrides and null Keyring do not
isolate WebKit or compiled-identifier IPC. Never remove incumbent sockets or change normal data,
credentials, browser storage, unrelated processes, or host security to manufacture isolation.
A unique-identifier/nonpersistent-store variant records limited evidence, not production-byte
or real-Keychain certification.

## Observations

| Existing gate | Procedure and observable result | Result / receipt |
| --- | --- | --- |
| PLA-160, PLA-324, PLA-328 | Clean install and first launch; honest macOS warning/locality/disclosure; model acquisition interruption/retry and offline warm reuse; authenticated readiness and owned helper eviction | blocked |
| PLA-153, PLA-160 | Save synthetic writing, quit/reopen, retry interrupted work; compare exact content, job states, source attribution and duplicate effects | blocked |
| PLA-327, PLA-160 | Native import/backup/restore; reopen original sources and compare hashes; preserve retained prior data | blocked |
| PLA-160 | Actual locked/denied Keychain replacement, recovery and Forget using synthetic credentials; check after restart and update | blocked |
| PLA-159 | Installed private N→N+1 and rollback sequence below; verify preserved content and credentials | blocked |
| PLA-160 | Native Print and Save as PDF from saved writing; inspect preview/output, math and clipping | blocked |
| PLA-407 / PLA-404 | Physical CJK candidate confirmation does not send; next Enter sends once; Shift+Enter inserts newline | blocked |
| PLA-418, PLA-425 / PLA-404 | VoiceOver reads active card/math only; question, grading and results focus journey is understandable | blocked |
| PLA-428, PLA-442, PLA-445 / PLA-404 | VoiceOver source text traversal, Added/Removed/Unchanged distinctions, filter pattern and pressed state | blocked |
| PLA-450 / PLA-404 | Actual 200% zoom: both faces, long answers and initial/maximum scroll keep ratings visible | blocked |
| PLA-446, PLA-448, PLA-452 / PLA-404 | Unfamiliar user explains primary choices, moves among Plan/Sources/History, and identifies empty-state setup next step | blocked |
| PLA-447 / PLA-404 | Touch-only tester discovers Rename/Delete with usable visible targets | blocked |
| PLA-147, PLA-329, PLA-337 | Sustained workload and checkpoints below; record observed hardware separately from retained 8 GB owner waiver | blocked |

DOM assertions and independent-agent content assessment do not substitute for physical input,
assistive-technology observations or unfamiliar-user comprehension. Reconcile current issue
comments before closing any existing child; preserve completed software implementation scopes.

## Sustained workload

Use [PLA-147](https://linear.app/platinum-labs/issue/PLA-147)'s existing versioned
`packaged_soak_harness.py` plan/record protocol. Its current acceptance specifies sustained,
repeated work sufficient to reveal leaks and recovery faults; it supplies no numeric duration.
The tester must declare the agreed duration before execution; short startup samples do not pass.

Exercise multiple classes/documents; ingestion/retrieval; tutor/tools/Exa/source capture;
solutions, study generation/review and long writing/edit/review; navigation/cancellation/retry;
provider failures/recovery/offline use; background/sleep/wake, quit/relaunch, duplicate launch
and owned-process crash; interrupted synthetic import/backup/restore; embedding/rerank/OCR
and default idle eviction. Record failures as well as successful retries.

Measure complete app/WebKit/backend/helper resources at cold/warm launch, settled idle after
60 seconds, active work, helper eviction and repeated sessions: RSS/CPU, files/threads,
cache/disk growth and owned survivors. Preserve existing PLA-329 thresholds (bundle ≤500 MB,
usable shell ≤5 seconds after initial migration, settled aggregate idle RSS ≤500 MB; investigate
unexplained >150 MB post-eviction growth). A 24 GiB observation is not an 8 GB measurement.
Finish with SQLite integrity, settled storage intents, truthful durable jobs/artifacts,
source/backup hashes, no partial publications, privacy checks and no unexplained resource drift,
console errors, backend failures or forbidden/unowned processes. Retain step log and human decision.

## Private installed update and version changes

Follow [the updater contract and recovery sequence](release-updater-evidence.md) and
[immutable signing/staging rules](releasing.md). Record supported private N and N+1 versions,
builds, source/signature/archive hashes, schema compatibility and test-feed configuration before
execution. Keep normal feed configuration unchanged. The current updater pins feed/asset origins;
a test configuration must document those differences rather than claim byte equivalence.
No private-feed deployment or installed update was executed in this pass.

On the isolated installation: seed synthetic content/credentials; record hashes; explicitly
check/download/install N+1; restart; compare content/credentials and helper continuity. Exercise
interrupted/corrupt/wrong-signature downloads, wrong architecture/downgrade refusal, staging-space
failure and failed first launch. Reverify the retained prior app, restore a compatible data copy
separately when needed, relaunch and compare preserved data. Never downgrade the sole data copy.
Signature-unit tests alone cannot pass this row.

A release version change requires synchronized metadata and current-base CI, rebuilt sidecar,
frontend and native app, separate development/distribution signing, new app/backend/DMG/archive
hashes, updater signature/archive checks, read-only mounted-image equality and authenticated
frozen smoke. Repeat integration-sensitive/native acceptance against those exact bytes; never
relabel beta.0 as beta.1. The release workflow's stage operation can continue into promotion:
do not dispatch it for private testing. Release-PR merge, public tags/promotion and subsequent
anonymous production download/feed smoke remain separately authorized owner actions.

Final observer decision: **not run**. Preserve the recorded 8 GB waiver and distribution-clearance
attestation as owner decisions. Off-device updater-key recovery backup confirmation remains an
existing owner action; do not copy secrets into this worksheet.
