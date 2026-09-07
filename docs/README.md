# Documentation

Start with [beta testing and first use](beta-testing.md) or [CONTRIBUTING](../CONTRIBUTING.md).
The [architecture](architecture.md) explains component boundaries; [local deployment](local-deployment.md)
covers the signed desktop build. [Releasing](releasing.md) owns publication mechanics and
[the release ledger](release-evidence.md) owns approval status.

## Maintenance policy

Update maintained guidance in the same PR as changes to behavior, setup, configuration, interfaces,
architecture, or development workflow. Explain when a PR has no documentation impact. Run
`uv run python scripts/check_docs.py` and `uv run python scripts/check_active_references.py`.
Prefer links to source configuration over copied version values and duplicated command sequences.

Historical records keep their original names so issue links and source comments continue to resolve.
Their benchmarks, screenshots, commands, and task states describe the recorded revision only.
Candidate evidence is likewise scoped to its recorded commit and device; it does not certify a newer build.

## Document inventory

Reviewed September 6, 2026 against the integrated repository. Maintained entries own current guidance;
history retains research/provenance and is excluded from current setup instructions.

| Document | Status |
| --- | --- |
| [Agentic long-form drafting](agentic-drafting.md) | Maintained |
| [System Architecture](architecture.md) | Maintained |
| [Beta testing and first use](beta-testing.md) | Maintained |
| [Branch housekeeping — September 6, 2026](branch-housekeeping-2026-09.md) | Candidate/incident evidence; recorded revision only |
| [Contributing, Testing, and Migrations](contributing-testing-migrations.md) | Maintained |
| [Code conventions](conventions.md) | Maintained |
| [Lyra Design System](design-system.md) | Maintained |
| [Desktop Migration Inventory](desktop-migration-inventory.md) | Historical record |
| [Lyra desktop resource summary](desktop-runtime-summary.md) | Historical record |
| [Distribution notices and license review](distribution-notices.md) | Maintained |
| [Draft autosave concurrency (PLA-289)](draft-autosave-concurrency.md) | Maintained |
| [Ex Libris: The Lyra Design System (Approved Brief)](exlibris-design-system.md) | Historical record |
| [Ex Libris Migration: Component Inventory and Transition Plan](exlibris-migration.md) | Historical record |
| [Project direction](feature-roadmap.md) | Maintained |
| [Integration Handoff: Study Tools, Hybrid Retrieval, and the Draft Workspace](integration-handoff.md) | Historical record |
| [Local Deployment](local-deployment.md) | Maintained |
| [Learning beta quality evidence](learning-beta-evidence/README.md) | Candidate evidence; recorded revisions and configurations only |
| [macOS Apple Silicon Release Checklist](macos-apple-silicon-release-checklist.md) | Maintained |
| [Phase 2 Handoff](phase-2-handoff.md) | Historical record |
| [Phase 3 Handoff](phase-3-handoff.md) | Historical record |
| [Phase 3 Verification Handoff](phase-3-verification-handoff.md) | Historical record |
| [Phase 4 Agent Specification](phase-4-agent.md) | Historical record |
| [Phase 4 Agent Handoff](phase-4-handoff.md) | Historical record |
| [Phase 4 Threat Model](phase-4-threat-model.md) | Maintained |
| [Phase 4 Writer Integration Boundary](phase-4-writer-integration.md) | Historical record |
| [PLA-404 recovery and review evidence](pla-404-recovery.md) | Historical record |
| [PR #70 targeted review follow-ups](pr-70-followups.md) | Historical record |
| [Privacy and Data Location](privacy-and-data-location.md) | Maintained |
| [Historical RAG research and phase specification](rag-pipeline-history.md) | Historical record |
| [Document processing and retrieval](rag-pipeline.md) | Maintained |
| [Raster envelope for page rendering](raster-envelope.md) | Maintained |
| [Release acceptance evidence — September 6, 2026](release-acceptance-evidence.md) | Candidate/incident evidence; recorded revision only |
| [Stop recovery race — CI follow-up](release-chat-stop-evidence.md) | Candidate/incident evidence; recorded revision only |
| [Beta release evidence ledger](release-evidence.md) | Current approval ledger; evidence scoped to recorded candidate |
| [PLA-461 targeted Guide prompt repair](release-guide-prompt-evidence.md) | Candidate/incident evidence; recorded revision only |
| [PLA-481 desktop import credential preservation](release-import-followup-evidence.md) | Candidate/incident evidence; recorded revision only |
| [PLA-462 delayed Keychain read follow-up](release-keychain-followup-evidence.md) | Candidate/incident evidence; recorded revision only |
| [Required model cache and download evidence](release-model-evidence.md) | Candidate/incident evidence; recorded revision only |
| [Native/workspace release repairs](release-native-evidence.md) | Candidate/incident evidence; recorded revision only |
| [PR 73 original-file recovery acceptance fix](release-original-recovery-evidence.md) | Candidate/incident evidence; recorded revision only |
| [PLA-473 ordinary Quit regression — 2026-09-06](release-quit-followup-evidence.md) | Candidate/incident evidence; recorded revision only |
| [Release credential, process, and log evidence](release-security-evidence.md) | Candidate/incident evidence; recorded revision only |
| [PLA-159 update and schema evidence](release-updater-evidence.md) | Candidate/incident evidence; recorded revision only |
| [Writer release evidence — PLA-464–468](release-writer-evidence.md) | Candidate/incident evidence; recorded revision only |
| [Desktop beta releases](releasing.md) | Maintained |
| [Security and CI Gates](security-and-ci-gates.md) | Maintained |
| [Homework Solver Specification](solver-phase-2.md) | Historical record |
| [Storage Consistency](storage-consistency.md) | Maintained |
| [Study reliability contracts](study-reliability.md) | Maintained |
| [Study beta quality evaluation](study-beta-quality.md) | Maintained evaluation workflow; candidate-specific results |
| [Unusable text-layer detection (PLA-148)](text-layer-detector-eval.md) | Historical record |
| [Troubleshooting](troubleshooting.md) | Maintained |
| [Tutor prompt contract](tutor-prompt-contract.md) | Maintained |
| [UI Overhaul: Audit and Design Brief](ui-overhaul.md) | Historical record |
| [Phase 1 Interface Specification](ui-phase-1.md) | Historical record |
| [Phase 2 Interface Specification](ui-phase-2.md) | Historical record |
| [Phase 3 Interface Specification](ui-phase-3.md) | Historical record |
| [UI/UX remediation for tester onboarding](ui-ux-remediation.md) | Historical record |
| [Writer Overhaul: One Assistant That Actually Writes](writer-overhaul.md) | Historical record |
| [Writer Roadmap Archive](writer-roadmap.md) | Historical record |

Other documentation and retained assets:

- [Root README](../README.md), [CONTRIBUTING](../CONTRIBUTING.md), [security reporting](../SECURITY.md),
  [agent instructions](../AGENTS.md), and the [PR template](../.github/pull_request_template.md)
  are maintained entry/workflow guidance. GitHub issue templates provide public intake.
- [Product screenshot provenance](images/README.md) describes the current README image.
- `desktop-migration-screenshots/` and `pla-289-conflict-ui/` are historical screenshot evidence.
- `desktop-resource-report.json` and `desktop-runtime-report.json` are historical machine samples.
- [Provider evidence](release-provider-evidence/README.md) indexes retained JSON/text evaluation
  outputs. These are data records, not setup instructions or an endorsement of a provider.

## September 2026 audit changes and limits

- Replaced the README with a concise tester/contributor entry, an actual current frontend screenshot,
  clear unpublished-beta status, and Apache-2.0 source licensing with separate dependency review.
- Added first-use and contribution paths. Corrected Node/pnpm guidance and the distinction between
  hot reload (`./run --dev`) and the production-like browser launcher (`./run`).
- Corrected privacy claims: requested page recognition uses the configured vision tutor and may
  send images remotely after consent. Local OCR helper code alone does not establish local-only ingestion.
- Updated native backup, architecture/update boundaries, configuration, branching, commit conventions,
  and documentation-impact expectations. Consolidated prerequisites and packaging commands.
- Replaced the phase-era RAG specification with a source-linked current map; preserved the original
  research in `rag-pipeline-history.md`. Added explicit historical banners to old handoff/UI/solver
  and recovery records. No benchmark data or provenance was deleted.
- Checked maintained paths/commands against launcher, package manifests, configuration, routes,
  helper dispatch, workflow, backup/update, prompt, and storage sources. Link checks validate local
  targets; external URLs and private Linear evidence are not re-certified by that check.

Remaining external verification: first public-beta approval, Developer ID/notarization prerequisites,
dependency distribution clearance, and physical clean-device/native acceptance remain in the release
ledger. Old benchmark claims are intentionally preserved as history rather than rerun as part of a
documentation audit. The README screenshot is a browser rendering with synthetic fixtures; it is not
a native packaged acceptance capture.

## Repository housekeeping

[September 2026 branch reconciliation](branch-housekeeping-2026-09.md) records deleted branches, preserved commit identities, retained work, and the branch lifecycle policy.
