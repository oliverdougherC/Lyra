# Desktop beta releases

The release pipeline is implemented in `.github/workflows/release.yml`. It is not a claim
that a distributable beta exists. The initial `0.2.0-beta.0` is an **unpublished development
baseline**, above the internal `0.1.0` builds. Until the external gates below are satisfied,
a locally Apple Development signed candidate is review evidence only.

## Routine and review boundary

Release Please prepares a release PR from Conventional Commit subjects (`fix:`, `feat:`,
`feat!:`, with the repository's Lore decision trailers in the body). Its GitHub App token
also synchronizes all derived version files on that PR, so normal PR CI tests the actual
metadata to ship. **Review and merge the release PR** is the routine maintainer action.
Do not manually tag, rebuild, rename, or upload a DMG.

The same push workflow creates a draft release/tag, waits for successful `ci.yml` on the
exact tagged main SHA, builds the complete Apple Silicon runtime, signs and notarizes it,
and stages assets. It directly continues into promotion; it does not depend on a
`GITHUB_TOKEN` tag triggering a second workflow. The App token is necessary for release PR
pushes to trigger required CI. Existing `CI Gate` and current-main branch requirements remain.

For the **first public beta**, configure `release-promotion` with an owner required reviewer
and require explicit owner approval. The owner reviews the actual retained candidate, installed-app
acceptance, notarization records and release ledger, then approves that environment. No
agent may self-approve. Subsequent releases can retain this additional promotion review or,
by an explicit owner policy change, rely on the release PR review as the sole routine action.
Do not remove the initial review protection merely to make the pipeline green.

## One-time external configuration

These are configuration prerequisites, not requests to send secrets through chat:

1. Apple Developer Account Holder issues a **Developer ID Application** certificate with its
   private key. Export the matching identity as password-protected `.p12` and store its base64
   contents in `release-signing` environment secret `APPLE_CERTIFICATE`; store the export
   password in `APPLE_CERTIFICATE_PASSWORD` and exact identity in `APPLE_SIGNING_IDENTITY`.
   Apple Development and ad-hoc identities are explicitly refused by the publisher.
2. Configure `APPLE_ID`, `APPLE_TEAM_ID`, and an **app-specific** `APPLE_PASSWORD` in that
   environment. These authorize `notarytool`, not an ordinary Apple login password.
3. Retain the single generated Tauri updater key outside the repository and back it up encrypted
   off-device. Its public half is `src-tauri/updater-public-key.txt`; put the protected private
   file contents in `TAURI_SIGNING_PRIVATE_KEY`. Set `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` if
   encrypted (an empty password is supported). Never regenerate the key per build; installed
   clients trust its public half. Loss/rotation requires a separately reviewed transition.
4. Create a repository-scoped GitHub App with Contents, Issues and Pull requests write
   permissions and no administration or bypass capability. Install it only on Lyra. Configure
   `RELEASE_APP_ID` as a variable and `RELEASE_APP_PRIVATE_KEY` as a secret in the
   `release-automation` environment. Permit Actions to create PRs. The automation never
   approves a PR. Test that its actual generated release PR receives `CI Gate`.
5. Restrict `release-automation`, `release-signing` and `release-promotion` to protected main;
   add initial owner review to `release-promotion`. Enable GitHub Pages with **GitHub Actions**
   as its source. Review the Pages environment/deployment branch policy as well.
6. Resolve the project/distribution license and dependency obligations, including PyMuPDF's
   AGPL/commercial terms. Only after owner clearance set `DISTRIBUTION_LICENSE_REVIEWED=true`
   in `release-promotion`. The pipeline refuses public promotion without this explicit record.
   The generated third-party inventory does not choose a project license or establish compliance.

The September 2026 preflight found no Developer ID/notarization credentials. The parent release
owner generated the persistent updater key once and provisioned the signing environment's
private-key secret. GitHub App, Apple account and first public acceptance remain external gates.

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

The protected runner imports the certificate into an ephemeral Keychain, checks the usable
private key and exact Developer ID identity, and removes that Keychain on completion. Every
real Mach-O and nested code bundle is signed inside-out with hardened runtime and a secure
timestamp, then individually verified. The backend retains `com.lyra.desktop.backend`; the
app retains `com.lyra.desktop`. Stable designated requirements are checked. No broad runtime
entitlements are added without evidence that the signed runtime needs them.

Before signing, `release_metadata.py --bundle <Lyra.app> --source <SHA>` embeds
`Contents/Resources/lyra-release.json` with actual version/build/source/schema/architecture.
The updater binds unsigned feed metadata to this contract inside the signed archive.
The app is notarized, stapled, checked with Gatekeeper and smoke-tested through its frozen
backend. Only then is the DMG created, signed, separately notarized and stapled. The updater
archive is created from the final app bytes and signed with the persistent Tauri key. The
maintained verifier checks it against the public key compiled into installed clients before
any asset is uploaded; an incorrect private-key secret fails staging. Public fixture tests
cover a matching signature, corruption and an unrelated key without CI secrets. Evidence
includes Apple submission records, Gatekeeper/signing results, complete native deployment
floors, frozen smoke, SHA-256 values, source SHA, schema support and workflow ID.

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
notarization, anonymous URLs, Gatekeeper install, physical 8 GB acceptance or an installed
N→N+1 update. Record those results against the actual candidate in the release ledger before
first approval. The client remains user-initiated; publishing does not enable automatic checks.

Official references: [Release Please](https://github.com/googleapis/release-please-action),
[GitHub token event behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow),
[Tauri macOS signing](https://v2.tauri.app/distribute/sign/macos/) and
[Tauri updater](https://v2.tauri.app/plugin/updater/).
