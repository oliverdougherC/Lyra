# Desktop beta releases

The release pipeline is implemented in `.github/workflows/release.yml`. It is not a claim
that a distributable beta exists. The initial `0.2.0-beta.0` is an **unpublished development
baseline**, above the internal `0.1.0` builds. Until the external gates below are satisfied,
a locally signed candidate is review evidence only. Public CI packages use ad-hoc code
signatures without an Apple Developer account; they are not Developer ID signed or notarized.

## Routine and review boundary

Release Please prepares a release PR from Conventional Commit subjects (`fix:`, `feat:`,
`feat!:`, with the repository's Lore decision trailers in the body). Its GitHub App token
also synchronizes all derived version files on that PR, so normal PR CI tests the actual
metadata to ship. **Review and merge the release PR** is the routine maintainer action.
Do not manually tag, rebuild, rename, or upload a DMG.

The same push workflow creates a draft release/tag, waits for successful `ci.yml` on the
exact tagged main SHA, builds the complete Apple Silicon runtime, applies ad-hoc code signatures,
and stages assets. It directly continues into promotion; it does not depend on a
`GITHUB_TOKEN` tag triggering a second workflow. The App token lets generated release PRs trigger required CI automatically. Existing `CI Gate` and current-main branch requirements remain.

For the **first public beta**, configure `release-promotion` with an owner required reviewer
and require explicit owner approval. The owner reviews the actual retained candidate, installed-app
acceptance, distribution-signing evidence and release ledger, then approves that environment. No
agent may self-approve. Subsequent releases can retain this additional promotion review or,
by an explicit owner policy change, rely on the release PR review as the sole routine action.
Do not remove the initial review protection merely to make the pipeline green.

## One-time external configuration

These are configuration prerequisites, not requests to send secrets through chat:

### GitHub release bot: the missing credentials

1. Open [New GitHub App](https://github.com/settings/apps/new) (account **Settings → Developer
   settings → GitHub Apps → New GitHub App**). Choose a unique name such as
   `Lyra release automation` and use the repository URL as its homepage. Leave user authorization
   and callback/setup URLs unused. Clear **Webhook → Active**; this bot needs no hosted server.
2. Under **Repository permissions**, give **Contents**, **Issues**, and **Pull requests**
   **Read and write**. Keep other optional permissions at **No access**; GitHub includes
   mandatory Metadata read access. Select **Only on this account**, then create the App.
3. On its settings page copy the numeric **App ID** (not the Client ID). Under **Private keys**,
   choose **Generate a private key** and retain the downloaded `.pem` securely.
4. Choose **Install App**, install it on the repository owner account, and select **Only select
   repositories → Lyra**. Registration alone does not grant repository access.
5. Open [Lyra environments](https://github.com/oliverdougherC/Lyra/settings/environments) →
   **release-automation**. Add environment **variable** `RELEASE_APP_ID` with that numeric ID.
   Add environment **secret** `RELEASE_APP_PRIVATE_KEY` with the complete PEM file contents,
   including its BEGIN/END lines. Never paste the private key into an issue or chat.

The App authenticates Release Please and version-file pushes so release PR CI can run.
It is unrelated to Apple signing. GitHub suppresses ordinary `GITHUB_TOKEN` push-triggered
workflows; supported PR events from that token require manual approval before CI runs.
The App token avoids that extra approval step for generated release PRs. The bot has no administration,
branch bypass or approval permission. Confirm the generated PR receives `CI Gate`.
The repository’s **Actions → General → Allow GitHub Actions to create and approve pull requests**
checkbox controls `GITHUB_TOKEN` behavior. It was unchecked at preflight; the App token uses its
own granted permissions, so that checkbox is not a substitute for installing/configuring the App.

### Remaining environment settings

- **release-signing:** retain the existing `TAURI_SIGNING_PRIVATE_KEY` secret; it was
  provisioned during the September 2026 preflight. Its public half is
  `src-tauri/updater-public-key.txt`. Keep an encrypted backup of the private key off-device.
  Set `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` if that key is encrypted (an empty password is
  supported). Never regenerate it per build: existing clients trust the matching public key.
  This signature authenticates updates without any Apple account.
- **Apple:** no Apple certificate, certificate password, signing identity, Apple ID, team ID,
  or notarization password is required. The release workflow does not consume `APPLE_*` secrets.
- **release-promotion:** the owner required reviewer is already configured. Resolve the
  project/distribution license and dependency obligations, including PyMuPDF's AGPL/commercial
  terms. Only after owner clearance add the environment **variable**
  `DISTRIBUTION_LICENSE_REVIEWED=true`. It was absent at the September 2026 preflight; the
  pipeline refuses public promotion without it. A dependency inventory does not choose a
  project license or establish compliance.
- Restrict `release-automation`, `release-signing` and `release-promotion` to protected main.
  Keep the first-public-beta owner approval gate. GitHub Pages is already configured with
  **GitHub Actions** as its source; retain the Pages environment/deployment branch policy.

Install the bot credentials before merging these workflow changes into main; that push runs
release PR preparation. If credentials are added afterward, rerun the latest push-triggered
**Desktop release** run for the updated main revision. The manual stage/promote options do
not create release PRs. Review and merge the generated release PR after its CI is green. Staging then builds the
candidate; approve **release-promotion** only after license clearance and the first-beta
acceptance evidence is complete. Old failed runs remain in history; a successful new run
establishes the current status. Use the stage/promote recovery operations below for those phases.

## Versions and exact inputs

`version.txt` is authoritative. `scripts/release_metadata.py --sync` derives Tauri, Cargo,
Cargo.lock, frontend package, Python project, uv.lock and backend runtime versions. Python uses the equivalent
PEP 440 `0.2.0bN`. This command runs on the release PR, not as an unreviewed release-build change.
CI and publishing use `--check` to reject metadata drift. The installer uses
`Lyra_<version>_aarch64.dmg`; the tag, updater feed and native version use identical SemVer.

macOS build numbers are `(major * 100 + minor + 1).patch.(beta + 1)`, with stable releases
using final component `99`. Bounds are major/minor 0–98, patch 0–99, beta 0–97. CI fails before
exhaustion; moving to the next patch resets beta without decreasing the build number. This
preserves numeric ordering through beta.9→beta.10, beta→stable and internal `0.1.0`→beta.
Stable-channel introduction is a reviewed Release Please configuration transition, not a
rename of a published beta. Keep `versioning: prerelease` and set `prerelease` to false;
verify the generated PR version before merging. Beta and stable feeds stay separate.

The locked Python environment is frozen with PyInstaller; the pinned llama runtime is verified
and staged; frontend dependencies use the frozen lockfile; Rust builds with `--locked`.
`collect_distribution_notices.py` collects installed dependency notices and the pinned helper's
pinned upstream license files are bundled. No developer runtime is required by the installed product.
The complete bundle's native load commands must support the declared macOS 14 floor and arm64.
A dependency raising that floor fails the pipeline instead of silently widening requirements.

## Signed bytes and immutable staging

CI signs every real Mach-O and nested code bundle inside-out with an ad-hoc signature,
with hardened runtime enabled, then individually verifies them and the complete bundle. This requires no certificate or Apple
account. The backend retains `com.lyra.desktop.backend`; the app retains `com.lyra.desktop`.
The Python sidecar and `llama-server` alone receive
`com.apple.security.cs.disable-library-validation`: a real frozen smoke test with ad-hoc
hardened signatures otherwise fails loading `libpython` because ad-hoc libraries lack matching
Team IDs. The desktop shell receives no such exception. Both helpers are executed during
packaging, and all native objects still require the hardened-runtime flag.
Ad-hoc signatures provide code integrity but no Developer ID trust or stable certificate identity.
Local review rebuilds continue using the persistent development identity documented in
`docs/local-deployment.md`; CI distribution does not require that local identity.

Before signing, `release_metadata.py --bundle <Lyra.app> --source <SHA>` embeds
`Contents/Resources/lyra-release.json` with actual version/build/source/schema/architecture.
The updater binds feed metadata to this contract inside the authenticated archive.
The signed app passes native compatibility checks and its frozen-backend smoke check before
DMG creation. The app and DMG are not notarized or stapled, and no Gatekeeper acceptance is
claimed. `distribution-signing.json` records `mode: "ad-hoc"`,
`developer_id_signed: false`, `notarized: false`, and `hardened_runtime: true`. These values
are derived from inspected `codesign -dv` output retained for every native object; promotion
revalidates the observations and rejects missing runtime flags. The ad-hoc identity rules out
Developer ID notarization; the receipt does not claim an Apple service lookup.

After DMG creation, `hdiutil verify` checks the image, then a read-only mount is compared
against the source app (files, modes, and symlinks). Every mounted code signature and runtime
flag is inspected and the mounted frozen backend is smoke-tested. `dmg-verification.json`
binds this evidence to the DMG checksum. Detachment runs even when verification fails.

The updater archive is created from the final app bytes with `COPYFILE_DISABLE=1` and signed
with the persistent Tauri key. This omits macOS AppleDouble pseudo-files outside Lyra.app while
preserving ordinary file bytes and symlinks. The extracted archive's code signatures are verified
before staging. The maintained verifier checks its Tauri signature against the public key compiled
into installed clients; an incorrect private-key secret fails staging. Public fixture tests cover
a matching signature, corruption and an unrelated key without CI secrets. Evidence includes
ad-hoc signing results, distribution mode, complete native deployment floors, frozen smoke,
SHA-256 values, source SHA, schema support and workflow ID.

Downloaded builds may require an explicit first-launch exception: after attempting to open the
trusted app, open **System Settings → Privacy & Security → Open Anyway** and confirm. See
[Apple's instructions](https://support.apple.com/en-us/102445). Do not disable Gatekeeper globally.
Because ad-hoc signatures change code identity across builds, macOS Keychain can ask again for
access to stored credentials after an update. Test this on the retained candidate and document
any required user action; do not promise that existing “Always Allow” approvals survive updates.

Draft assets are uploaded without replacement, with `SHA256SUMS` last. A repeat upload accepts
only identical server SHA-256 digests and uploads missing files; conflicting or unexpected
assets fail. Actions retains the complete candidate so a failed upload can reuse **the same
signed bytes**, instead of generating different signatures in a new build.

## Retry and promotion

Run **Desktop release → Run workflow → stage** to retry without entering a version. It resolves
the version's tag, requires it to be an ancestor of main with successful exact-SHA main CI,
and restores retained same-SHA candidate bytes if present. Partial draft assets without their
original retained candidate fail closed; recover the original artifact rather than delete or
replace uploaded binaries.

Use **operation: promote** to resume a completed draft or repair a channel after publication.
This downloads and validates the staged assets; it never invokes a compiler or signer. A
published version is never replaced. A failed public download check leaves the channel
unchanged and promotion is retryable. Repository-wide release concurrency plus semantic
version comparison prevents an older run from downgrading a channel. Published provenance
with an equal version must match exactly.

## Tester URLs and remaining evidence

The intended permanent beta entrypoint is
[Download Lyra Beta](https://oliverdougherC.github.io/Lyra/beta/), with
[beta updater metadata](https://oliverdougherC.github.io/Lyra/beta/latest.json).
The stable channel uses `/Lyra/stable/` and `/Lyra/stable/latest.json`. These are **prospective
URLs until first promotion and anonymous verification**; a draft GitHub release is not a
working tester download. There is no custom server and no latest-release API dependency.

Promotion first publishes immutable GitHub versioned assets and downloads them anonymously,
checking hashes. It reconstructs both channel pages from versioned published provenance,
checks ordering, and deploys Pages last. The final job fetches the anonymous channel/feed and
compares it with the staged feed. A beta-only repository is covered by the site regression.

Source tests cover version bounds/order, synchronization, every missing required asset before
any release mutation, corrupt artifacts, metadata tampering,
channel collisions and beta-only URLs. They do not establish live GitHub App event delivery,
anonymous URLs, first-launch approval, physical 8 GB acceptance or an installed
N→N+1 update. Record those results against the actual candidate in the release ledger before
first approval. The client remains user-initiated; publishing does not enable automatic checks.

Official references: [Registering a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app),
[GitHub App private keys](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps),
[Release Please](https://github.com/googleapis/release-please-action),
[GitHub token event behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow),
[Tauri macOS signing](https://v2.tauri.app/distribute/sign/macos/) and
[Tauri updater](https://v2.tauri.app/plugin/updater/).


## Internal 0.1.0 installations

The internal 0.1.0 apps predate the updater and future-schema safeguard. Their first approved
beta installation uses the DMG; a later installer cannot retrofit safety code into old copies.
Keep the verified pre-migration backup and do not reopen an old internal binary on migrated
student data. Use a separate restored profile for an older compatible app. New beta builds refuse
future schemas before recovery writes; that guarantee must not be attributed to historical binaries.
