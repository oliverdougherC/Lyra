# Branch housekeeping — September 6, 2026

Compared every local and remote branch with `main` at `68c02194aec2`, merged pull requests, and active worktrees. There were no open pull requests at the audit snapshot.

Deleted 44 remote and 51 local branches after verifying ancestry or aggregate squash-patch equivalence. Remote deletions used an atomic push with each expected tip as a lease; local deletions compared the expected SHA. GitHub now automatically deletes merged PR branches.

A full local Git bundle and machine-readable inventory are retained under `.omx/housekeeping/` on the maintainer checkout. Commit SHAs below also make the decisions reviewable. Active worktrees, recovery branches, and unique or unproven histories were retained. No worktrees or application data were removed.

## Deleted branches

| Branch | Local | Remote | Preserved tip | Proof |
| --- | --- | --- | --- | --- |
| `agent/pla145-agent3` | — | yes | `ff5c0c9f57ca7f2776e7ec80e0f14e34af9663ed` | merged PR squash patch equivalent |
| `agent/pla149-a1-0829` | — | yes | `66c3d2ad828db6fbee4e6fe0b6ff060ee60d0e6e` | merged PR squash patch equivalent |
| `agent/pla314-agent2` | — | yes | `62cf18ae4995b1ea42801b6795d7443d923d13f2` | merged PR squash patch equivalent |
| `chore/disable-dependabot-version-prs` | — | yes | `ac65268505fd2157befd2106db35de808f50a2dc` | merged PR squash patch equivalent |
| `codex/pla-158-tauri-desktop-migration` | yes | yes | `576de18263bbc79d03debb2407a8e8709120907e` | merged PR squash patch equivalent |
| `codex/pla-401-agent-c-delivery` | yes | yes | `5731d3eb7737d7489bf6050147f1235d9daeb8a5` | ancestor |
| `codex/pla-401-guide-prompting` | yes | — | `cdcd5041af1974eb5c7e90b29db64855ca9766df` | ancestor |
| `codex/pla-401-workstream-c` | yes | yes | `c5a5721c9ea2ab77bbebe599dc3aa16280c2e08c` | merged PR squash patch equivalent |
| `codex/pla-402-pin-verification` | yes | yes | `a6c741d051720f4af1b5364be28ade9416ffa954` | merged PR squash patch equivalent |
| `codex/pla-403-composer-cleanup` | yes | yes | `505f978fbbca863df8b9280bae005d4524ab5c92` | merged PR squash patch equivalent |
| `feature/agentic-staged-drafting` | yes | yes | `cb0d2222400d8731eb992474de1e3ab4c11b7ddc` | ancestor |
| `feature/artifact-lifecycle-tests` | yes | yes | `bb9927d805a693b6cfab286c40d14be8d0cf13fa` | ancestor |
| `feature/draft-workspace` | yes | — | `0899a719ed9d6e88f081b11716f35038d15fa115` | ancestor |
| `feature/exlibris-migration` | yes | yes | `557eda79d03ab124fba6afb37ed2acf04b8ac17c` | ancestor |
| `feature/hybrid-retrieval` | yes | — | `a43f18e159c96fd97811decf0e2aebd1bdf91fef` | ancestor |
| `feature/intent-first-ux` | yes | yes | `3109517f8180c0e6eff61fef7a9f6e634ce42afb` | merged PR squash patch equivalent |
| `feature/migration-upgrade-tests` | yes | yes | `4df3bc269e984bff91e848caec06a6c69230a8a9` | merged PR squash patch equivalent |
| `feature/solver-eval-stage-scoring` | yes | yes | `38bb6cfd3a49f68df2ef1fcdc4d72dda3170230f` | merged PR squash patch equivalent |
| `feature/solver-study-quality` | yes | yes | `d249895e1b415044adbfc4bee0a5659ee0967ade` | ancestor |
| `feature/structured-diagnostics` | yes | yes | `ac757a86925b67790158f4753033242ef587d54c` | merged PR squash patch equivalent |
| `feature/study-tools` | yes | — | `98c1f06a642e90a8ffc3023c153c74b29823988c` | ancestor |
| `main-remote` | yes | — | `dbcc3a0afeab709e88979155e9ecadacc9e6f2e6` | ancestor |
| `oliverfdougherty/fall-readiness-agent-turns-retries-inline-writing` | yes | yes | `907ceb9d25a3d0358d0e6faac18e507813dffe5b` | merged PR squash patch equivalent |
| `oliverfdougherty/fall-readiness-document-upload-readiness` | yes | yes | `8351b8c6af0338cc4aeae7fc601ce0208a60b6ba` | merged PR squash patch equivalent |
| `oliverfdougherty/fall-readiness-study-durability` | yes | yes | `f46a2a3c96752250466d5a9da40c5ce66a16a39e` | ancestor |
| `oliverfdougherty/pla-146-test-launcher-recovery-across-runtime-state-version-skew` | — | yes | `9ea04575e49cf2384fff59adaa44fb07f23d6b7e` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-149-build-class-scale-retrieval-evaluation-with-document-aware` | yes | yes | `0910cbee3ef735ce789c439ebab2032acb40ac18` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-162-block-remote-tutor-chat-until-document-context-consent-is` | yes | yes | `0554cb391ecdf214e2e9fc6caa51beec3b06b089` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-163-reject-untrusted-host-headers-to-close-dns-rebinding-access` | yes | yes | `57a4f0e31cb35a6aa35129bd9690fcc44a2a13bf` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-164-make-document-filesystem-and-sqlite-mutations-crash` | yes | yes | `3f2f38a1460bf5911f2f072ffb42f5b35bd6a503` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-166-serialize-chat-turns-per-session-and-make-regeneration` | yes | yes | `4b95af099ee30b7358283bc5e14187ce7991284f` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-167-charge-the-current-chat-question-against-the-context-budget` | yes | yes | `55753adb914ae8de69c9d0f1560332576906215a` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-168-create-lyra-data-and-backup-files-with-private-permissions` | yes | yes | `fd1b5550939591afa11499295e15dd1bc67e27f1` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-169-291-305-312-study-durability` | yes | yes | `ec10dab6ef24bb518ef1fea98c06d08261a376c5` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-169-291-305-312-study-durability-tranche` | yes | — | `88bcb5a156a191cbcd806eb5c04317cb35c289f0` | ancestor |
| `oliverfdougherty/pla-170-bound-page-rasterization-before-pymupdf-can-allocate-extreme` | yes | yes | `da91f7d0ff534f83c08b96789c9f8a7199e7ce30` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-171-do-not-write-arbitrary-tutor-upstream-error-bodies-into` | yes | yes | `146bfee94f50b1e94efd81a7a243e016f8f87630` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-207-gate-the-phase-4-agent-chat-behind-document-context-consent` | yes | yes | `ae898094c3399745f8e05e75a4ebe47fe50de38d` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-276-roadmap-reconciliation` | yes | yes | `fc06578095a6688396aad0feb609f7771588e7c3` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-289-prevent-stale-draft-autosaves-from-overwriting-newer-writing` | yes | yes | `1ae7fcbb516445905bbb9d571724593b6914682b` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-290-keep-agent-chat-turns-inside-the-configured-context-window` | yes | yes | `21a593196a9e896f739044067906857bb30649e6` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-292-add-real-backend-acceptance-coverage-for-school-critical` | — | yes | `078802332995fcbade44b73c671211f893b20355` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-294-297-release-ci-and-python-security` | yes | yes | `0ad1f9ff6a6e47b2bfe45520a5c0fde17bf875bf` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-301-health-aware-server-supervision` | yes | yes | `5fdb5db80239040f0494df341013cbe92fdc11e0` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-302-307-school-readiness-credentials-backup` | yes | yes | `d81979843762ba498e03ede1dbbc2cac281d9159` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-303-304-311-fall-readiness` | yes | — | `88bcb5a156a191cbcd806eb5c04317cb35c289f0` | ancestor |
| `oliverfdougherty/pla-303-304-311-school-readiness` | yes | yes | `17aae91fc0808ac49b25fa96cbdf845fcaeaf49b` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-306-retry-failed-tutor-turns` | yes | yes | `36bc8debc2b36e93d945ef7d65ee34b97bbfcf3e` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-308-309-310-writer-chat-tranche` | yes | — | `88bcb5a156a191cbcd806eb5c04317cb35c289f0` | ancestor |
| `oliverfdougherty/pla-308-310-writer-chat-safety-clean` | yes | yes | `7f5f2070e24d7bf1d14993f9b2ffe126f3e13005` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-316-313-315-fall-correctness` | yes | — | `7931ef25e12477517c12b9841bbd6a5d4d19edfb` | ancestor |
| `oliverfdougherty/pla-317-make-sqlite-wal-sidecar-hardening-race-safe-under-concurrent` | yes | yes | `6858359bb104a1f904eb96254569a04c14e9e04a` | merged PR squash patch equivalent |
| `oliverfdougherty/pla-401-student-ux-ws-b` | yes | — | `cdcd5041af1974eb5c7e90b29db64855ca9766df` | ancestor |
| `pr70-head` | yes | — | `8e462cbd098a7baa91cf56eaf61759425de0aba0` | ancestor |
| `pr71-head` | yes | — | `a7565fc2a57bea42e2aca03005f1a179dec27fa2` | ancestor |
| `solver/phase-2-substrate` | yes | — | `a906bbe28f798231c2597c43ae0a2d290163a2f2` | ancestor |
| `test/pr71-retarget-ci-verification-DELETEME` | — | yes | `a7565fc2a57bea42e2aca03005f1a179dec27fa2` | ancestor |
| `ui/phase-1-interface-pass` | yes | — | `8c63e7eb177f2e8a87f532c93d6213d2068a844a` | ancestor |

## Retained branches

| Branch | Location | Reason |
| --- | --- | --- |
| `backup/pla-168-pre-rebase` | local | Recovery checkpoint or release history |
| `backup/pla-401-c-pre-final-rebase` | local | Recovery checkpoint or release history |
| `backup/pla-401-c-pre-rebase-onto-b` | local | Recovery checkpoint or release history |
| `backup/pla404-local-snapshot` | local | Recovery checkpoint or release history |
| `codex/pla-402-self-provisioning-embedding-model` | local | Unique or unproven commits; requires focused review |
| `codex/pla-404-recovered` | local | Checked out in an existing worktree |
| `fix/frontend-fast-uri-advisory` | local | Checked out in an existing worktree |
| `fix/study-recovery-followup` | local | Checked out in an existing worktree |
| `fix/study-reliability` | local | Checked out in an existing worktree |
| `housekeeping/contributor-beta-readiness` | local | Checked out in an existing worktree |
| `housekeeping/local-preservation-20260906` | local | Recovery checkpoint or release history |
| `main` | local | Default branch |
| `oliverfdougherty/pla-146-test-launcher-recovery-across-runtime-state-version-skew` | local | Unique or unproven commits; requires focused review |
| `oliverfdougherty/pla-292-add-real-backend-acceptance-coverage-for-school-critical` | local | Unique or unproven commits; requires focused review |
| `oliverfdougherty/pla-294-require-the-repository-ci-checks-before-main-can-advance` | local | Unique or unproven commits; requires focused review |
| `oliverfdougherty/pla-303-304-311-school-readiness-boundaries` | local | Unique or unproven commits; requires focused review |
| `oliverfdougherty/pla-308-310-writer-chat-safety` | local | Unique or unproven commits; requires focused review |
| `oliverfdougherty/pla-308-writer-chat-serialization-budget-recovery` | local | Unique or unproven commits; requires focused review |
| `pla-401-workstream-b` | local | Checked out in an existing worktree |
| `pr-55` | local | Unique or unproven commits; requires focused review |
| `release/beta-readiness-20260905` | local | Checked out in an existing worktree |
| `release/reliability-20260905` | local | Checked out in an existing worktree |
| `test/ci-gate-verification-DELETEME` | local | Unique or unproven commits; requires focused review |
| `worktree-pla-316-313-315` | local | Checked out in an existing worktree |
| `codex/pla-402-self-provisioning-embedding-model` | origin | Unique or unproven commits; requires focused review |
| `codex/pla-404-recovered` | origin | Checked out in an existing worktree |
| `fix/frontend-fast-uri-advisory` | origin | Checked out in an existing worktree |
| `fix/study-reliability` | origin | Checked out in an existing worktree |
| `main` | origin | Default branch |
| `pla-401-workstream-b` | origin | Checked out in an existing worktree |
| `release/beta-readiness-20260905` | origin | Checked out in an existing worktree |
| `release/reliability-20260905` | origin | Checked out in an existing worktree |
| `worktree-pla-316-313-315` | origin | Checked out in an existing worktree |

## Ongoing policy

Start task branches from current `main`, reconcile before merge, and let GitHub remove merged PR heads. Delete local task branches once they have no unique work and no active worktree. Keep unique histories until their intent is reviewed; do not infer abandonment from age alone. Release/recovery branches need an explicit retention reason. See [Contributing](../CONTRIBUTING.md).
