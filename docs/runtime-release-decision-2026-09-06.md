# Desktop/runtime release decision — September 6, 2026

**NO-GO for public promotion.** The review build is not the assembled final candidate. No PR
has been merged or release published by this workstream. The writing owner discovered an
actual SQLite lock-loss/corruption defect during HTTP recovery and has repaired it in
[PR #83](https://github.com/oliverdougherC/Lyra/pull/83). The learning lane retains critical
configured-model semantic failures in [PR #80](https://github.com/oliverdougherC/Lyra/pull/80). Approved
integration and rerunning affected UX/model/runtime checks remain necessary.

## Reviewed changes

[PR #81](https://github.com/oliverdougherC/Lyra/pull/81) starts from
`e96bf4886977c648f9e7905c7807c806b1ae7a80` and preserves merged #73/#74/#75/#77/#78.
It fixes helper eviction/quit/re-adoption, makes the actual packaged cache boundary work for
render/retry, permits narrow native windows, and makes existing frozen acceptance reproducible.
It introduces no dependencies or replacement packaging architecture.

[PR #76](https://github.com/oliverdougherC/Lyra/pull/76) was rebased in place, original patch
unchanged, to head `144dfeed7c4b765dcbb79694e5afb43b1072d246`. Its refreshed
[CI Gate passed](https://github.com/oliverdougherC/Lyra/actions/runs/34070152189).
Strict current-base ruleset 20537110 remains enabled, with no bypass actors. #76 supplies the
real green-before-advance checkpoint; no merge was attempted to manufacture the missing
advance-main demonstration.

## Identity and evidence boundary

Latest rebuilt review source: `d597bd8d2766b6689f10dec72e8df4cdcc7ce8c4`.
The earlier installed model/native/resource receipt is explicitly pinned to `66deb27`.
Version `0.2.0-beta.0`, macOS build `3.0.1`, database schema maximum `45`.
Host: Apple Silicon Mac16,8; 24 GiB RAM; macOS 27.0 build26A5416b.
Helper runtime: llama.cpp `b10287 (b06aa774c)`.

The owner confirmed distribution clearance and explicitly waived unavailable 8 GB hardware
for this handoff. That is owner attestation/waiver, not independently verified licensing or
measured 8 GB performance. The per-gate status remains blocked where physical evidence is
absent, annotated with the waiver. Neither public support claims nor platform security were
weakened. Real Keychain mutation still requires a disposable OS account or genuinely isolated
approved setup; null-Keyring acceptance cannot establish macOS Keychain behavior.

The exact app tree, DMG, updater archive and signed sidecar hashes are recorded in the generated
`candidate-evidence.json` beside the local review artifacts. Those binary receipts remain
outside source control so evidence updates do not change the binary's source revision.
Earlier `4822272` binary receipts are retained separately: eleven frozen backup scenarios passed,
while actual cold-download recovery reproduced HTTP500 from the wrong page-cache root.
Three regression cases failed before that root was repaired; 178 render/document/storage tests
passed after repair. The `66deb27` installed binary passed the failed retry path and all eleven backup scenarios.
Its SHA-256 is `9be2b17aac95a10664131c5ced625c5ed5cfbc49627a8030cac156527bbaa0d3`.
DMG SHA-256: `e17db3fd9624912c10718cbdd6f894b93781aa464c3f76396c04cdd154c3c095`.
Updater archive SHA-256: `b33308ee1c0dcdb3c839685acbffc3dfe3cd4173da34f3f22b2af8c278f67da6`.
App-tree SHA-256: `64806ccbcebc7df3645d8d67cd531ad3d77d6c7ce5f0f9dbed69c841bebeb89d`.
The later successful result does not erase the earlier failure.

A subsequent actual frozen import failed when only preserved profile files existed and models
were stored externally: no directory had created the stage before copying the permissions
marker. The one-line private-stage creation repair has a failing-before regression and54
import/migration/backup checks. Latest signed binary
`01d42959848aa5293738d056e21649ffbcc034f8745a01ff073898941f36eb3e` passed real
preview → stage → frozen publication → authenticated relaunch, with the source DB hash unchanged,
and reran all eleven backup scenarios. Native picker selection was a synthetic fixture, not a
claimed dialog interaction. Latest DMG SHA-256:
`b21442bd879b55f8355b035c3c7931344d571d4bba5770f835b13d1c8e50ee7d`; updater archive:
`15b4acf684592e27bf04195b6ce2e2cb125a06ec511f1c677ca71f50a27f9bfa`; app-tree:
`1512a77cffed110b02d63cb69f2c1278346cf5b6c4ef51bb7a3b2b7ce3c51fe3`.
Its mounted-DMG equality, hardened signatures, architecture/floor and persistent archive
signature were reverified. Earlier model/native measurements are not relabeled as new-byte runs.


## Gate matrix

| Gate / issue | Status | Evidence / remaining work |
| --- | --- | --- |
| Helper atomic lifecycle — PLA-328 | pass, source | Barrier reproduction killed a helper with one active lease before repair; 143 focused checks and independent cross-backend/admission review passed. |
| Package/native signing — PLA-327/479 | pass, review build | 78 native objects verified arm64/macOS14 floor; hardened runtime, minimal helper entitlements, mounted DMG equality and persistent updater signature verified. |
| Frozen backup/restore/export — PLA-327 | pass, installed review binary | Eleven real frozen scenarios preserve WAL writing, source/deck data, current settings/authority, prior profile and cross-profile original download; corrupt/future archives refused. |
| First-use failure/retry/offline/model eviction — PLA-328/160 | pass, installed review binary | Cold proxy-blocked download fails truthfully; real retry downloads/ingests in14.68s; proxy-blocked relaunch reuses verified weights; actual helper exits after default300s idle. |
| Native install/startup/retry/quit/Print — PLA-324/160 | pass installation/startup/save/relaunch/quit; blocked Print certification | Existing #76 installer verified private destination; native synthetic essay rendered, edit reached Saved and persisted after quit; app/backend reclaimed. Print menu activation was inconclusive with automation; no preview/PDF pass. |
| Native narrow size and work-area clamp — PLA-324 | pass, source; native pending | 52 native tests; browser operated 540×720/768×700/1024×768; new native minimum540×600 awaits candidate observation. |
| Original-data import — PLA-327 | pass, latest signed frozen publication; native picker pending | Actual preview/stage/publication/relaunch passed with source DB unchanged;54 focused regressions passed. Synthetic selection record does not certify native picker. |
| Real Keychain outage/denial/Forget | blocked | No disposable OS account or approved genuinely isolated Keychain setup; no real credential mutation performed. |
| Settled full process tree — PLA-337 | pass collection; resource certification not claimed | After more than60s without UI activity,5 owned processes total336.8MiB RSS,2.6% sampledCPU;200.8MiB app. Earlier post-navigation sample213.2MiB/0%. No forbidden owned helper at ordinary idle. Not sustained-use evidence. |
| 8 GB resource targets — PLA-329 | blocked / owner-waived | No target hardware; available 24 GiB host cannot provide this evidence. |
| Sustained mixed study/writing and sleep/wake — PLA-147 | not run | Requires assembled immutable candidate plus other owners' semantic scenarios; no short smoke represented as soak. |
| Installed authenticated N→N+1/rollback — PLA-159 | blocked | No approved integrated pair/feed; no publication authorized. Unit signature/schema tests and local archive authentication do not establish installed replacement/recovery. |
| Version automation / release App — PLA-479 | pass, preparation | Live setup works; #79 is open and generated beta.1/build3.0.2; owner review still required. |
| Persistent updater custody | pass, local cryptographic match | Existing key signed local archive; compiled public key verified it and rejected altered bytes. No key rotation or fixture substitution. |
| Encrypted off-device updater backup | blocked | Owner confirmation remains absent; key presence and signing are not backup evidence. |
| Distribution clearance | pass, owner-attested | Owner confirmed; inventories/notices retained, PyMuPDF terms remain applicable; no independent legal conclusion. |
| Anonymous matching download/feed | fail, expected pre-publication | Fresh unauthenticated page/feed return404; no approved public release. Repeat checksum/feed match after authorized promotion. |
| Current-base CI and advance-main — PLA-475 | pass configuration / blocked demonstration | Existing #76 fresh CI green; advance-main run awaits approved merge, refresh and revalidation. |
| Final integrated candidate — PLA-160 | blocked | Requires approved integration, including writer storage repair, then one immutable build and all affected reruns. |

## Smallest owner actions

1. Confirm an encrypted off-device backup of the existing updater key and its recovery owner.
2. Review the workstream PRs and integrate approved changes one at a time under the strict gate;
   the storage lock-loss fix is required. #79 remains a separate release-version decision.
3. Provide a disposable OS account/approved isolated Keychain setup and complete required human
   input/accessibility and Print checks. The unavailable 8 GB gate is already owner-waived for this handoff.
4. Decide public promotion only after the assembled candidate passes its remaining gates. This
   task grants neither merge nor publication authority.

## Reproduction

Use the maintained [local deployment](local-deployment.md) and [release procedure](releasing.md).
Frozen backup acceptance is now tracked:

```sh
uv run python scripts/verify_packaged_backup.py /absolute/Lyra.app/Contents/Resources/resources/lyra-backend/lyra-backend --output /private/evidence/frozen-backup.json
```

The driver runs in new disposable storage, proves selectors before each child, uses a restricted
PATH and null Keyring, verifies a real database after startup, and preserves current credential
reference authority through restore. The native test fixture is available with `--prepare-only`;
its emitted environment must be propagated through every app/recovery child. Do not open it
through an unrelated existing app instance or use the normal student profile.

## Native automation boundary

The second native launch proved every emitted filesystem and null-Keyring selector in both
the actual app and backend process environments before editing the synthetic essay. Saved
content and SQLite integrity were checked in that isolated database. No real credentials were
created or changed. CUA can automatically relaunch an exited application during a UI state
read without preserving launch overrides; this occurred after the first quit and was stopped
before any destructive or credential test. Do not request UI state after quit: verify the
expected app/backend PIDs have exited externally. Re-prove isolation after every relaunch.
This tool limitation is why no actual Keychain/restore-destructive native test is inferred.

## Local verification

On source `66deb27`: full backend **3,261 passed /1 skipped**;143 focused lifecycle/ownership
checks;178 render/document/storage checks; frontend **1,014 tests** and **32 browser E2E**;
**52 Rust release tests**, clippy, lint, typecheck, production build, Python security gate and
documentation scans passed. Existing SWIG/Starlette deprecation warnings remain.
Hosted CI correctly failed a real-stack test whose send helper silently did nothing while the
button was still disabled; the trace contained no continuation POST. The repaired helper now
requires actual enabled admission and a sent continuation, without weakening cancellation
assertions. Controlled latency reproduced the old failure;24 full-spec repetitions pass.
The original failed run is34070984857; current-head CI must be green before merge.
