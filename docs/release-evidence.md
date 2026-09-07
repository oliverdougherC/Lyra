# Beta release evidence ledger

## Implementation integration — September 7, 2026

The reviewed implementation stack **#81 → #83 → #80 → #82 (including quiz repair) → #85 → #86**
and canonical installer **#76** are integrated by this pass. **Public beta promotion remains
unapproved.** Release PR #79 (or its successor), tags, public downloads and update feeds remain
outside this implementation authorization.

| Scope | Landed implementation / retained evidence |
| --- | --- |
| Runtime | [#81](https://github.com/oliverdougherC/Lyra/pull/81), `6fb0570`: helper leases, stop/adoption and quit admission, cache root and import staging. |
| Writer/storage | [#83](https://github.com/oliverdougherC/Lyra/pull/83), `dc3be3a`: real-process SQLite locks, run ownership/cancellation, source revisions and bounded recovery. |
| Learning | [#80](https://github.com/oliverdougherC/Lyra/pull/80), `ac75e0d`: task-grounded feedback and actual active-quiz metadata. |
| UX and quiz integration | [#82](https://github.com/oliverdougherC/Lyra/pull/82), `b1908fa`: quota-safe navigation plus study-query invalidation on quiz start/answer/finish and real API/UI continuation/counts/reload/completion/restart tests. |
| Learning follow-up | [#85](https://github.com/oliverdougherC/Lyra/pull/85), `8aff363`: truthful CAS/verification and corrected flashcard schema. |
| Writing follow-up | [#86](https://github.com/oliverdougherC/Lyra/pull/86), `5df914a`: current/historical evidence delivery and exact scoped corrections preserving student wording. |
| Local delivery | [#76](https://github.com/oliverdougherC/Lyra/pull/76): verified canonical installation; opening requires explicit `--open`; private review keeps its original artifact. |

Strict current-base **CI Gate** and the no-bypass ruleset were retained. The first real merge made
#83 `BEHIND` despite its previous green gate; a normal branch refresh received new green checks.
The stacked follow-ups were retargeted to main and only their own deltas replayed, with preserved
originals and explicit force-with-lease. Both range comparisons were patch-identical. PLA-475's
live advance/refresh evidence is distinct from the earlier rehearsal.

Rehearsal [#84](https://github.com/oliverdougherC/Lyra/pull/84) was **not merged wholesale**.
Its two unique code/test changes, `a9b80ed` and `0d6478f`, are retained in #82's main commit.
Its four assembly checkpoints duplicate the separately integrated implementations; no additional
unique application fix was found. Its documentation-only `587564a`/`094b7f5` evidence and native
safety correction are preserved in the [dated preparation record](integration-preparation-2026-09-07.md).
The preparation ledger additions are superseded by this section, not new current-run evidence.

For final-main CI, exact source/version/build, full-suite results, bounded combined-model results,
and signed app/DMG/archive hashes, use the [integration delivery receipt on #76](https://github.com/oliverdougherC/Lyra/pull/76)
and the Linear Lyra project overview. Binary receipts stay beside the retained artifacts so
recording their hashes does not change their source. Final verification must cover #85 and #86
together; the historical #84 totals cannot substitute. A successful gate job whose check-run
stayed pending was retried as infrastructure recovery; its original receipt was retained.

### Remaining acceptance boundaries

- PLA-152's delivered evaluator criteria are separate from PLA-153's remaining semantic and
  long-session recovery quality. Guide explanation failures, conservative solver confidence,
  ambiguous source attribution and full-draft quality concerns remain visible; completed model
  runs are not semantic passes. Retain each internal retry and independent-versus-human distinction.
- Backend selectors and null/fail Keyring do not isolate default WebKit storage or compiled-ID IPC.
  Production-native acceptance requires an isolated account/device without an incumbent endpoint.
  This pass does not launch the normal-account native app or replace its installation. Unique-ID/store
  variants are limited evidence; earlier normal-profile startup impact remains uncertain.
- Real Keychain, installed update/rollback, Print, physical input/accessibility, unfamiliar-user
  testing and sustained/sleep-wake checks remain with their existing acceptance owners.
- The owner 8 GB waiver and distribution-clearance attestation remain decisions, not measured
  performance or independent licensing certification. Updater off-device backup confirmation and
  anonymous public delivery remain outstanding. No signing key, support policy or gate was changed.
- Local review uses the persistent development identity. Distribution review uses separate hardened
  ad-hoc bytes and the existing authenticated updater; neither requires a new Apple account or
  permits public publication. See [local deployment](local-deployment.md) and [releasing](releasing.md).

All earlier sections below are dated historical records, including their old unmerged and
external-configuration statements.

## Historical reconciliation — September 6, 2026

**Decision: NO-GO for public promotion.** Current fetched main is
`e96bf4886977c648f9e7905c7807c806b1ae7a80`. PRs #73, #74, #75, #77 and #78 are merged.
The records below describe their earlier preparation and are retained as historical receipts;
their “in review” labels and old external preflight are not current release configuration.

Runtime follow-up is on `fix/pla328-runtime-release-20260906`. The existing canonical installer
PR #76 is being rebased and verified, not recreated. Release-version PR #79 remains a separate
review decision. No merge, publication, production-key rotation or public promotion is authorized
by this ledger.

PR #77 selects **hardened ad-hoc beta distribution**, with minimal helper entitlements, final
mounted-DMG/hash verification and separately signed updater archives. Apple Developer ID and
notarization are not prerequisites for that policy. Local certificate-signed review builds remain
useful for stable Keychain identity; they do not establish the selected distribution gate.

The available test host is Apple Silicon `Mac16,8`, 24 GiB RAM, macOS 27.0 build `26A5416b`.
It cannot supply PLA-329's actual 8 GB measurements. The owner subsequently waived that
unavailable hardware check for this handoff; record it as **blocked / owner-waived**, never as a
pass or an 8 GB performance claim. No isolated OS account has been established for real Keychain
mutation. Final integration-sensitive acceptance must follow approved integration
and use one immutable candidate; independent worktree checks do not substitute for it.

The owner subsequently confirmed distribution clearance in this task. This is an owner
attestation, not independent verification of a PyMuPDF commercial license or of open-source
compliance; retain the dependency inventory and obligations. The statement did not confirm
that an encrypted off-device updater backup exists.

Current gate receipts, exact review-candidate hashes and the issue matrix are in the
[runtime/release decision packet](runtime-release-decision-2026-09-06.md). Keep production updater-key backup confirmation, dependency distribution clearance,
anonymous matching download/feed, installed update/rollback, physical input/accessibility and
8 GB sustained/sleep-wake acceptance open until direct evidence exists.

## Historical preparation receipt — September 5, 2026

Everything in this section records the earlier preparation state. In particular, its request for
Developer ID credentials was superseded by #77; it is preserved here only as history.

Status: implementation and private review candidate preparation; **not approved or published**.
Fetched main: `3e109a7ef1cdce7362d9f0e8a286881ebf6fa5a5` (2026-09-05).
Working branch: `release/beta-readiness-20260905`, isolated from the existing dirty checkout.
PR #70/#71 and their reviewed study/UX/save protections are retained.

Review: [reliability PR #73](https://github.com/oliverdougherC/Lyra/pull/73), followed by
[desktop/release PR #74](https://github.com/oliverdougherC/Lyra/pull/74). Neither is merged or
self-approved. Candidate version is `0.2.0-beta.0`, macOS build `3.0.1`. The immutable source SHA,
final bundle/DMG hashes and installed observations are in the delivered `candidate-evidence.json`
next to the local DMG and in the PR handoff. Those generated receipts are intentionally outside
source control so recording a binary hash does not change that binary's source revision.

| Blocker | Owner | PR / regression | Status | Candidate evidence |
| --- | --- | --- | --- | --- |
| PLA-464–468 writer/source/stream/deadlines | release owner / writer lane | #73; release-writer-evidence.md | source reviewed internally; external review open | historical quotation and cancellation-owner barriers, budgets/terminal/deadline tests |
| PLA-456–460,462/463 credentials/helpers/cache/logging | release owner / security lane | #73/#74; release-security-evidence.md, release-model-evidence.md | source repairs in review | credential outage/forget/snapshot, real descendants, redaction; real 146 MB model download and offline cache verification |
| PLA-471/473 workspace/native | release owner / native lane | #73/#74; release-native-evidence.md | source repairs in review | descriptor-swap tests; bounded native helpers; final native receipt |
| PLA-478 malformed study topics | release owner | #73; test_study.py | 3 failed before; 4 focused / 131 study tests passed after | mixed/invalid worker outputs; real-provider study corpus still open |
| PLA-159 updater/schema recovery | release owner / updater lane | #74; release-updater-evidence.md | source and frozen repairs in review; installed N→N+1 blocked | signed/corrupt/wrong-key fixtures; replay/architecture/schema checks; retained app; migration copies |
| PLA-327/480 backup/soak isolation | release owner / native lane | #74; release-native-evidence.md | actual frozen cross-profile roundtrip passed | real original-document download/hash; committed WAL retained; forgotten credentials stay forgotten; no source runtime required |
| PLA-479 publisher | release owner / Apple account holder | #74; releasing.md | implementation in review; credentials/license/real workflow gates open | complete immutable payload/retry checks; no notarization/publication claimed |
| PLA-475 stale-base checks | release owner | existing ruleset 20537110 | strict CI Gate enabled and verified, no bypass actors | advancing-main demonstration awaits reviewed merges |
| PLA-150–153/461 quality | provider reviewer / release owner | #73/#74; release-provider-evidence/summary.json | **blocked: latest configured-model critical run 4/6** | Qwen3.8-27B failures retained; real Exa search/content passed; no human certification |
| PLA-147/160/329 installed soak/8GB | physical tester / release owner | release-acceptance-evidence.md | open | current host macOS 27 / 24 GiB is not a clean 8 GiB reference machine |
| PLA-404 human/device checks | human tester | 407/418/425/428/442/445/446/447/448/450/452 | open; prior 39 software fixes preserved | actual CJK, screen reader, touch/comprehension and 200% zoom criteria not blanket-closed |

The complete live project backlog was read. PLA-278 remains a previously reclassified, nonblocking
future recency contract; cosmetic PLA-320/455 and optional inference PLA-154 were not used to
expand this release. Existing Done study fixes 469/470/472/474/476/477 were preserved.

## External preflight

- Repository is public; authenticated actor has admin/maintain/push/release access.
- Existing active `Default` ruleset 20537110 requires `CI Gate`, has no bypass actors.
  Enabled only `strict_required_status_checks_policy`; other parameters retained.
- No repository secrets or environments existed at preflight. Workflow default token read-only;
  Actions self-approval disabled. Private vulnerability reporting was disabled; now enabled and verified.
- macOS Keychain lists one valid signing identity: Apple Development. **No usable Developer ID
  Application identity/private-key pair is installed.** Development signing is local-review evidence only.
- No Apple/notary credential environment names were set. No configured release environment credentials.
  Apple team/account notarization authority cannot be proven without account credentials.
- No updater keys were found in standard Tauri locations. Created one persistent key using the existing
  locked Tauri CLI; retained `~/.config/lyra/release-signing/updater.key` mode 0600, parent mode 0700.
  Stored private half as `TAURI_SIGNING_PRIVATE_KEY` in GitHub `release-signing` environment,
  restricted to protected branches. Public half may be committed. No rotation on CI/retry.
- Owner: copy the private updater file to an encrypted off-device backup/password vault. Its local
  filesystem permissions and GitHub secret protect access, but are not an off-device recovery backup.
- Owner: provision Developer ID Application certificate + private key and notarization credentials
  via Keychain/GitHub environment settings. Never paste secrets into chat. See releasing.md for names.

## Bundle requirements evidence

Existing complete local app is 202 MiB. All Mach-O load commands inspected: 17 objects minimum
11.0, 60 minimum 13.3, one minimum 14.0 (`sqlite_vec/vec0.dylib`). macOS 14 is the current complete
bundle floor, subject to final candidate revalidation. No 8 GB or clean-machine pass is inferred.

## Publication boundary

The eventual beta entrypoint is https://oliverdougherc.github.io/Lyra/beta/ . Before initial approved
publication this is a planned public URL, not a verified working download. Draft releases and local
review DMGs do not establish anonymous tester access. No unreviewed beta will be published.
