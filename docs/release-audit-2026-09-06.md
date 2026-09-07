# Release configuration audit — September 6, 2026

Decision: **no-go for public promotion**. This is a read-only source/configuration audit and
isolated test receipt, not a new installed-candidate receipt. Source inspected:
`e96bf4886977c648f9e7905c7807c806b1ae7a80`, version `0.2.0-beta.0`, build `3.0.1`.
Hardware used for source tests: Mac16,8, 24 GiB, macOS 27.0 (26A5416b). It cannot supply the
required supported 8 GB hardware evidence. No app was installed/launched, production key
rotated, environment changed, release published, or PR merged by this audit.

Read current AGENTS.md/CONTRIBUTING.md, the [September 6 execution plan](https://linear.app/platinum-labs/document/beta-execution-plan-and-parallel-agent-briefs-2026-09-06-17d2a50ad819),
PLA-324/337/159/479/160 and relevant comments, merged PRs #73/#74/#77, and open release PR #79.
Historical receipts in [release-evidence.md](release-evidence.md) remain historical.

## Owner follow-up after the audit

The owner confirmed “You have clearance” in this task and stated that no 8 GB Mac is available,
asking to ignore that hardware gate. Record distribution clearance as owner-attested; this is
not an independent legal finding about the PyMuPDF licensing basis. The 8 GB check is
**blocked / owner-waived for this handoff**, not passed, and no performance claim follows.
The pre-confirmation observations below are retained as the audit receipt. Encrypted off-device
updater backup and real isolated Keychain acceptance remain unconfirmed.

## Current observations (before owner follow-up)

| Gate | Status | Evidence and remaining boundary |
| --- | --- | --- |
| Preserve merged implementation | pass, source | GitHub records #73 merged at `19c9e9e`, #74 at `68c0219`, #77 at `d664978`; inspected main includes them. No replacement packaging architecture is needed. |
| Synchronized current version | pass | `python3 scripts/release_metadata.py --check` returned beta.0/build 3.0.1. |
| Actual Release Please configuration/event path | pass, preparation only | Protected `release-automation` has App ID and private-key secret metadata. [Run 34068092284](https://github.com/oliverdougherC/Lyra/actions/runs/34068092284) at this SHA has prepare success; stage/promote skipped. It does not prove distribution staging. |
| Generated next version | pass, review pending | [PR #79](https://github.com/oliverdougherC/Lyra/pull/79), head `40db6427da845656126d09a781d1794b29fcb401`, remains open; beta.1/build 3.0.2 changes cover all ten version/changelog files. [CI 34068113166](https://github.com/oliverdougherC/Lyra/actions/runs/34068113166) has all eleven checks green, including CI Gate. Merging it is a release action requiring owner review. |
| Current-base CI requirement | pass, configuration | Ruleset 20537110 is active, no bypass actors, required CI Gate with strict freshness true. Advance-main behavioral demonstration belongs to PLA-475; this read does not prove it. |
| Persistent updater custody | pass, presence only | Local private file exists, mode 0600, parent 0700; `release-signing` secret metadata exists, last updated 2026-09-06T01:15:32Z. Private contents were not read. Tracked public key equals compiled Tauri key; public-text SHA-256 `03c5a05fcb85245c07209823ccfbf5eeed4e4295edd14e9c3dfed68d1131e925`. Secret presence does not establish its cryptographic match; final archive verification must do that. |
| Off-device updater recovery | blocked | No owner-confirmed encrypted backup/recovery receipt in inspected issue/comments or maintained docs. Do not create a replacement key or count fixture keys as production evidence. |
| Hardened ad-hoc policy | pass, source | `sign_and_package.sh` signs real nested code with runtime flags; helper exception is limited to Python sidecar/llama-server; signing receipt and mounted DMG checks are mandatory; archive authentication uses the separate persistent updater key. No Apple Developer ID/notarization prerequisite under #77. Final candidate execution still required. |
| Promotion review | pass, configuration | Protected release-promotion environment requires oliverdougherC review. `prevent_self_review` is false; agent must still never self-approve under the execution contract. |
| Distribution clearance | blocked, evidence inconsistent | Live `DISTRIBUTION_LICENSE_REVIEWED=true` differs from the ledger's old absent-variable record. It records a configuration assertion, but no dependency-specific decision/receipt was found. PyMuPDF licensing basis and final inventory obligations remain unresolved in maintained notices and latest execution plan. Do not infer clearance from this Boolean or Apache-2.0 source licensing. |
| Anonymous download/feed | fail, expected before promotion | Fresh unauthenticated curl requests returned HTTP 404 for both beta page and beta/latest.json. Only release listed is draft `review-0.2.0-beta.0-532845c`; no approved public candidate/feed exists. Matching checksums/feed remain blocked until approved publication. |
| First-launch instructions | pass, documentation only | Added the existing per-app Privacy & Security → Open Anyway guidance and renewed Keychain prompt explanation to tester-facing `docs/beta-testing.md`, checked against [Apple's instructions](https://support.apple.com/en-us/102445). Global security settings remain enabled. Actual quarantined first launch remains not run. |
| Native update/rollback and work/credential preservation | not run here | Existing authenticated archive/schema/corruption protections were preserved and isolated regressions rerun; this is not installed N→N+1/rollback or genuine OS Keychain acceptance. PLA-159 remains open. |
| Complete settled process tree, sustained use, sleep/wake, 8 GB | blocked for target acceptance | This 24 GiB host is not the supported 8 GB target. PLA-337/329/147/160 require actual target runs, separately from source harness tests. |

## Distribution question requiring an owner decision

The locked installed PyMuPDF distribution is 1.28.0; `importlib.metadata` reports
`Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License`. The authoritative
[PyMuPDF License and Copyright documentation](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)
confirms AGPL and commercial alternatives for PyMuPDF/MuPDF. This audit does not determine
which obligations the owner has met or change that licensing choice. The final bundle inventory,
native transitive libraries and separately downloaded model terms require a documented basis;
notice collection is attribution, not legal clearance. A commercial-rights receipt or a reviewed
applicable open-source compliance decision must identify the shipped dependency version and
required accompanying material without exposing private contract data.

## Executed checks

At the source SHA above:

```sh
python3 scripts/release_metadata.py --check
uv run --locked --extra dev pytest backend/tests/test_release_workflow.py backend/tests/test_release_artifacts.py backend/tests/test_release_dmg.py backend/tests/test_release_signing_evidence.py backend/tests/test_update_schema_guard.py -q
```

Result: metadata consistent; **79 tests passed in 2.07 seconds**, five SWIG deprecation warnings.
These tests cover existing workflow/artifact/signing/DMG and future-schema boundaries using
isolated fixtures. They are not real production-key signing, a mounted final release candidate,
native user journeys, or hardware certification. GitHub API reads verified environment/secret
names and dates only; anonymous curl had no authentication header. No secret values retained.

## Smallest owner actions

1. Confirm the existing updater key's encrypted off-device backup and recovery ownership.
2. Record the actual dependency/model distribution basis, especially PyMuPDF, reconciling the
   already-true promotion variable with an explicit clearance receipt. Do not change source or
   support policy merely to clear a gate.
3. Supply a supported 8 GB Apple Silicon target and disposable OS account/approved isolated
   Keychain setup for physical acceptance. Keep one coordinator for installation/profile use.
4. Review integration and release PRs; after approved integration, authorize the immutable final
   candidate's acceptance and eventual promotion only when its complete evidence is satisfactory.

No new GitHub App setup or Apple Developer account purchase is presently necessary. Final
SHA/version/build, app/DMG/archive hashes and every integration-sensitive UX/model/runtime
result must be collected by the release owner after approved integration; this audit supplies
no substitute candidate identity.
