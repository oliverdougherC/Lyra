# Beta release evidence ledger

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
