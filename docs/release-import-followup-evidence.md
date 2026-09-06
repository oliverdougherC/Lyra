# PLA-481 desktop import credential preservation

Reviewed baseline: PR #73 c0d991091d6bf9ea305e21ddff3ed67a8089690a,
inherited by PR #74 06f9a874b06e3ec9203a4644b9eca1530e89a2d9.

Normal desktop import retained the destination settings row's tutor UUID while omitting
its credential records, generation and tutor/Exa authority markers from the published
profile. The distinct backup/restore path did not repair this path.

Six new regressions failed before the repair: preservation space accounting reported
zero credential bytes, and publication lost credential/generation state. The repair
extends the existing current-profile preservation list and late refresh. It uses bounded
private no-follow reads and private writes, accounts for those bytes, and preserves an
acknowledged absence or Forget over any historical imported state.

The complete desktop import module passes **34 tests**, including ten new cases covering
UUID Keychain/file fallback storage, older/current source schemas, 0600 permissions,
Forget before/after initial staging, changed endpoint plus distinct old/new keys,
interrupted publication, rollback, symlink refusal and an unrelated credential sentinel.
The tests enter through the real import manager HTTP path and desktop entry publication
CLI, then verify the selected endpoint and authentication via an injected HTTP transport.
All credentials are synthetic; the host Keychain is isolated. Ruff and diff checks pass.

This is import publication evidence, not the separate backup restore test. Final remote
head/CI and actual frozen publication/relaunch evidence are linked from the PR handoff
and review-candidate receipts. No actual OS Keychain preservation or distribution
signature/notarization pass is inferred from injected-store tests.

An additional actual frozen before-repair run reproduced the failure through manager
staging, successful `--publish-desktop-import`, and HTTP relaunch: the complete credential
authority bundle was missing. Baseline frozen executable SHA-256:
`a7fdf6d352e169c6c1b5a2818fc4dc651fcac51051c193edcfaaabe6f2568199`.
Every process used FailKeyring, fresh synthetic credentials and a private loopback provider.
The sanitized before/after JSON and reproduction driver accompany the remote candidate;
private fixture locations are excluded. This is frozen publication, not native picker UI.
