# Beta release evidence ledger

Status: implementation and private review candidate preparation; **not approved or published**.
Fetched main: `3e109a7ef1cdce7362d9f0e8a286881ebf6fa5a5` (2026-09-05).
Working branch: `release/beta-readiness-20260905`, isolated from the existing dirty checkout.
PR #70/#71 and their reviewed study/UX/save protections are retained.

| Blocker | Owner | PR / regression | Status | Candidate evidence |
| --- | --- | --- | --- | --- |
| PLA-464–468 writer/source/stream/deadlines | writer agent | pending; release-writer-evidence.md | implementing | pending integrated build |
| PLA-456–460,462/463 credentials/helpers/cache/logging | security agent | pending; release-security-evidence.md | implementing | pending integrated build |
| PLA-471/473 workspace/native | native agent | pending; release-native-evidence.md | implementing | pending integrated build |
| PLA-478 malformed study topics | release owner | worker fixtures in test_study.py | 3 failed before, 4 passed after | full study/candidate pending |
| PLA-159 updater/schema recovery | updater agent | pending; release-updater-evidence.md | implementing | actual N→N+1 not yet executed |
| PLA-479 release publishing | publisher agent | pending; docs/releasing.md | implementing | Developer ID/notarization blocked |
| PLA-475 stale-base checks | release owner | ruleset 20537110 | strict enabled and read back | two-PR advancement demonstration pending |
| PLA-150–153/461 quality | acceptance agent / provider reviewer | release-acceptance-evidence.md | open | no fabricated real-provider pass |
| PLA-147/160/329 installed soak/8GB | release owner / physical tester | release-acceptance-evidence.md | open | this Mac has 24 GiB |
| PLA-404 physical/human cases | human tester | 407/418/425/428/442/445/446/447/448/450/452 | open | prior 39 software fixes preserved |

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
- No updater keys found in standard Tauri locations. Created one persistent key using the existing
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
