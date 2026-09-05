# PLA-404 recovery and review evidence

## Recovery provenance

Latest main at extraction: `ae5957720d6ba5b4be387f7f9cfaa23d9546eea2` (fetched September 5, 2026).
The original checkout was on `a6c741d051720f4af1b5364be28ade9416ffa954` with 99 changed/untracked files.
Its complete source snapshot is retained locally at `backup/pla404-local-snapshot`, commit
`82b7571c7d6d1ee09f8a6acb491cc890ec3281a5`; the original checkout remains untouched.
Tracked/untracked archives and retained issue/review/test evidence are also under
`/tmp/lyra-pla404-recovery` on the recovery host. The original audit/report archive remains
in the original checkout's `.omx/audits/ui-ux-2026-09-04` and attached to Linear.

The current tree, untracked files, branches, all worktree registrations, recent commits,
all-ref reflog, retained patches and agent reports were inspected. The 50 fixes were recovered
from the current dirty tree, not recreated. Recovery commit `3dbb0e7` contains the existing
82-file batch and tests. No dependencies were added or changed.

Later, separately requested streaming/SSE, Markdown reveal timing, Keychain presence caching,
and signing-helper changes remain preserved outside this PR. Three conflicts with main's
PLA-403 compact composer were resolved semantically: preserve current-main markup and controls,
add recovered IME/history guards, retain folder-path errors and bounded material scrolling.
A later icon-only-control experiment and its test were also excluded; main's existing compact
composer tests remain intact. The original lane report rotated PLA-413/414/415 numbering;
this register uses the actual Linear acceptance criteria.

## New integration adjustments

- A failed background draft refresh previously unmounted cached plan/live editors. Keep cached
  content mounted, show an inline retry warning, and preserve exact dirty input instances through
  failure/recovery. Two regression cases failed before this correction and passed afterward.
- Align the review action label across Work, overview, sidebar and solution rows: Review problems.
- Extend material containment to 9/30/100 files at 1280x720, 1024x600 and 375x667 with keyboard
  and mouse selection. Add touch/no-hover flashcard long-answer, math and rating-clearance checks.
- Exercise the real-stack writer with a long transcript, expanded brief and visible composer/send
  control. Preserve existing saved-body, delayed response, conflict and live-paragraph regressions.
- Update the recovered folder test to main's accessible label and format the integrated chat guard.
- Acceptance exposed a response/accounting race and machine-wide fixture sweeps. Wait for the
  exact expected failure-ledger entry; bind state lookup and process reclamation to this run,
  retain captured detached descendants and birth tokens, and preserve the zero-orphan gate.
  No product backend behavior changes were needed for these verification failures.

## Acceptance register

Every issue below has its implementation represented in the PR diff against main and retained
regression evidence. All remain **In Review** while unmerged. Links resolve within this commit.
Code/DOM coverage does not imply native input or screen-reader certification; see limitations.

| Issue | Acceptance focus | Implementation | Regression evidence |
| --- | --- | --- | --- |
| PLA-405 | Show recoverable errors when saved conversations fail to load | [chat-pane.tsx](../frontend/src/components/chat/chat-pane.tsx), [class-chats-panel.tsx](../frontend/src/components/classes/class-chats-panel.tsx) | [chat-pane.test.tsx](../frontend/tests/chat-pane.test.tsx), [class-chats-recovery.test.tsx](../frontend/tests/class-chats-recovery.test.tsx) |
| PLA-406 | Protect individual document deletion with confirmation or reversible undo | [documents-pane.tsx](../frontend/src/components/documents/documents-pane.tsx) | [documents-pane-manage.test.tsx](../frontend/tests/documents-pane-manage.test.tsx) |
| PLA-407 | Prevent IME candidate confirmation from submitting a chat | [composer.tsx](../frontend/src/components/chat/composer.tsx) | [composer-focus.test.tsx](../frontend/tests/composer-focus.test.tsx) |
| PLA-408 | Distinguish file-search no-results from an empty document library | [documents-pane.tsx](../frontend/src/components/documents/documents-pane.tsx) | [documents-pane-manage.test.tsx](../frontend/tests/documents-pane-manage.test.tsx) |
| PLA-409 | Invalidate endpoint diagnostics and models after configuration changes | [settings-form.tsx](../frontend/src/components/settings/settings-form.tsx), [routes_settings.py](../backend/api/routes_settings.py) | [writer-settings.test.tsx](../frontend/tests/writer-settings.test.tsx), [test_api_settings.py](../backend/tests/test_api_settings.py) |
| PLA-410 | Dismiss the temporary navigation drawer when opening a destination | [sidebar.tsx](../frontend/src/components/ui/sidebar.tsx) | [viewport-matrix.spec.ts](../frontend/e2e/viewport-matrix.spec.ts) |
| PLA-411 | Show outline loading failures without inventing document structure facts | [document-row.tsx](../frontend/src/components/documents/document-row.tsx) | [document-row.test.tsx](../frontend/tests/document-row.test.tsx) |
| PLA-412 | Distinguish import status failure from unsupported platform | [desktop-import-section.tsx](../frontend/src/components/settings/desktop-import-section.tsx) | [desktop-import-status.test.tsx](../frontend/tests/desktop-import-status.test.tsx) |
| PLA-413 | Make file selection scope stable and explicit across search changes | [documents-pane.tsx](../frontend/src/components/documents/documents-pane.tsx) | [documents-pane-manage.test.tsx](../frontend/tests/documents-pane-manage.test.tsx) |
| PLA-414 | Expose persistent save state for automatically saved settings | [settings-form.tsx](../frontend/src/components/settings/settings-form.tsx) | [writer-settings.test.tsx](../frontend/tests/writer-settings.test.tsx) |
| PLA-415 | Consolidate repeated technical privacy and import explanations | [settings-form.tsx](../frontend/src/components/settings/settings-form.tsx), [desktop-import-section.tsx](../frontend/src/components/settings/desktop-import-section.tsx) | [writer-settings.test.tsx](../frontend/tests/writer-settings.test.tsx), [desktop-import-section.test.tsx](../frontend/tests/desktop-import-section.test.tsx) |
| PLA-416 | Offer a direct route to archived classes from the class index | [class-list.tsx](../frontend/src/components/classes/class-list.tsx) | [class-list-archived.test.tsx](../frontend/tests/class-list-archived.test.tsx) |
| PLA-417 | Guard quiz restart against stale answer and finish responses | [quiz-runner.tsx](../frontend/src/components/study/quiz-runner.tsx) | [quiz-runner.test.tsx](../frontend/tests/quiz-runner.test.tsx) |
| PLA-418 | Expose flashcard question and answer as accessible content | [deck-session.tsx](../frontend/src/components/study/deck-session.tsx) | [deck-session.test.tsx](../frontend/tests/deck-session.test.tsx), [study-remediation.spec.ts](../frontend/e2e/study-remediation.spec.ts) |
| PLA-419 | Allow extracted subproblem statements to be corrected before solving | [problem-card.tsx](../frontend/src/components/solutions/problem-card.tsx), [segmentation-review.tsx](../frontend/src/components/solutions/segmentation-review.tsx) | [segmentation-review.test.tsx](../frontend/tests/segmentation-review.test.tsx) |
| PLA-420 | Keep visible Undo after the final extracted problem is removed | [segmentation-review.tsx](../frontend/src/components/solutions/segmentation-review.tsx) | [segmentation-review.test.tsx](../frontend/tests/segmentation-review.test.tsx) |
| PLA-421 | Do not label session-only flashcard counts as deck-wide progress | [deck-session.tsx](../frontend/src/components/study/deck-session.tsx) | [deck-session.test.tsx](../frontend/tests/deck-session.test.tsx) |
| PLA-422 | Provide a correction and removal path for generated flashcards | [card-actions.tsx](../frontend/src/components/study/card-actions.tsx) | [deck-session.test.tsx](../frontend/tests/deck-session.test.tsx) |
| PLA-423 | Make solution-verdict explanations accessible without hover | [verdict-badge.tsx](../frontend/src/components/solutions/verdict-badge.tsx), [problem-panel.tsx](../frontend/src/components/solutions/problem-panel.tsx) | [verdict-badge.test.tsx](../frontend/tests/verdict-badge.test.tsx), [problem-panel.test.tsx](../frontend/tests/problem-panel.test.tsx) |
| PLA-424 | Separate solution-source fetch errors from empty-class guidance | [source-picker.tsx](../frontend/src/components/solutions/source-picker.tsx) | [source-picker.test.tsx](../frontend/tests/source-picker.test.tsx) |
| PLA-425 | Announce quiz grading and manage focus between questions and results | [quiz-runner.tsx](../frontend/src/components/study/quiz-runner.tsx) | [quiz-runner.test.tsx](../frontend/tests/quiz-runner.test.tsx) |
| PLA-426 | Validate study counts explicitly instead of silently changing inputs | [class-study-panel.tsx](../frontend/src/components/classes/class-study-panel.tsx) | [class-study-panel.test.tsx](../frontend/tests/class-study-panel.test.tsx) |
| PLA-427 | Expose study and solution secondary actions on touch devices | [class-study-panel.tsx](../frontend/src/components/classes/class-study-panel.tsx), [solution-row.tsx](../frontend/src/components/solutions/solution-row.tsx) | [class-study-panel.test.tsx](../frontend/tests/class-study-panel.test.tsx) |
| PLA-428 | Add independent source-page zoom and a readable text alternative | [source-pane.tsx](../frontend/src/components/solutions/source-pane.tsx) | [source-pane.test.tsx](../frontend/tests/source-pane.test.tsx) |
| PLA-429 | Offer contextual retry when revision history cannot load | [revision-history.tsx](../frontend/src/components/solutions/revision-history.tsx) | [revision-history.test.tsx](../frontend/tests/revision-history.test.tsx) |
| PLA-430 | Offer document-fetch retry inside the study creation dialog | [class-study-panel.tsx](../frontend/src/components/classes/class-study-panel.tsx) | [class-study-panel.test.tsx](../frontend/tests/class-study-panel.test.tsx) |
| PLA-431 | Provide recovery actions for a failed source-page render | [source-pane.tsx](../frontend/src/components/solutions/source-pane.tsx) | [source-pane.test.tsx](../frontend/tests/source-pane.test.tsx) |
| PLA-432 | Reduce duplicate source-list scanning in solution setup | [page.tsx](../frontend/src/app/classes/%5Bid%5D/solutions/new/page.tsx), [source-picker.tsx](../frontend/src/components/solutions/source-picker.tsx) | [new-solution-page.test.tsx](../frontend/tests/new-solution-page.test.tsx), [source-picker.test.tsx](../frontend/tests/source-picker.test.tsx) |
| PLA-433 | Replace ornamental study copy with direct guidance | [quiz-runner.tsx](../frontend/src/components/study/quiz-runner.tsx), [class-study-panel.tsx](../frontend/src/components/classes/class-study-panel.tsx) | [quiz-runner.test.tsx](../frontend/tests/quiz-runner.test.tsx), [class-study-panel.test.tsx](../frontend/tests/class-study-panel.test.tsx) |
| PLA-434 | Save live-draft edits before Review and merge | [live-draft-suggestion.tsx](../frontend/src/components/drafts/live-draft-suggestion.tsx) | [live-draft-suggestion.test.tsx](../frontend/tests/live-draft-suggestion.test.tsx) |
| PLA-435 | Preserve typing made while a live paragraph save is in flight | [live-draft-suggestion.tsx](../frontend/src/components/drafts/live-draft-suggestion.tsx), [use-drafts.ts](../frontend/src/lib/hooks/use-drafts.ts) | [live-draft-suggestion.test.tsx](../frontend/tests/live-draft-suggestion.test.tsx), [use-drafts-live-suggestion.test.tsx](../frontend/tests/use-drafts-live-suggestion.test.tsx) |
| PLA-436 | Preserve unsaved plan edits across section switches and refreshed versions | [plan-panel.tsx](../frontend/src/components/drafts/plan-panel.tsx), [page.tsx](../frontend/src/app/classes/%5Bid%5D/drafts/%5BartifactId%5D/page.tsx) | [draft-planning.test.tsx](../frontend/tests/draft-planning.test.tsx), [draft-workspace-polish.test.tsx](../frontend/tests/draft-workspace-polish.test.tsx) |
| PLA-437 | Allow normal multiline typing in plan evidence fields | [plan-panel.tsx](../frontend/src/components/drafts/plan-panel.tsx) | [draft-planning.test.tsx](../frontend/tests/draft-planning.test.tsx) |
| PLA-438 | Save current draft before starting Address comment work | [page.tsx](../frontend/src/app/classes/%5Bid%5D/drafts/%5BartifactId%5D/page.tsx), [comment-list.tsx](../frontend/src/components/drafts/comment-list.tsx) | [draft-workspace-polish.test.tsx](../frontend/tests/draft-workspace-polish.test.tsx), [comment-list.test.tsx](../frontend/tests/comment-list.test.tsx) |
| PLA-439 | Distinguish writer and work loading failures from empty content | [brief-card.tsx](../frontend/src/components/drafts/brief-card.tsx), [comment-list.tsx](../frontend/src/components/drafts/comment-list.tsx), [class-work-panel.tsx](../frontend/src/components/classes/class-work-panel.tsx), [work-surface.tsx](../frontend/src/components/agent/work-surface.tsx) | [brief-card.test.tsx](../frontend/tests/brief-card.test.tsx), [comment-list.test.tsx](../frontend/tests/comment-list.test.tsx), [class-work-panel.test.tsx](../frontend/tests/class-work-panel.test.tsx), [work-surface.test.tsx](../frontend/tests/work-surface.test.tsx) |
| PLA-440 | Make per-change Reject controls actionable or remove unsupported controls | [workspace-change-review.tsx](../frontend/src/components/agent/workspace-change-review.tsx) | [agent-workspace-change-review.test.tsx](../frontend/tests/agent-workspace-change-review.test.tsx) |
| PLA-441 | Expose recovery actions for stale workspace proposals | [work-surface.tsx](../frontend/src/components/agent/work-surface.tsx), [workspace-change-review.tsx](../frontend/src/components/agent/workspace-change-review.tsx) | [work-surface.test.tsx](../frontend/tests/work-surface.test.tsx), [agent-workspace-change-review.test.tsx](../frontend/tests/agent-workspace-change-review.test.tsx) |
| PLA-442 | Give diff additions and removals accessible meaning | [suggestion-panel.tsx](../frontend/src/components/drafts/suggestion-panel.tsx), [workspace-change-review.tsx](../frontend/src/components/agent/workspace-change-review.tsx) | [suggestion-panel.test.tsx](../frontend/tests/suggestion-panel.test.tsx), [agent-workspace-change-review.test.tsx](../frontend/tests/agent-workspace-change-review.test.tsx) |
| PLA-443 | Provide a usable comparison and preservation path for draft conflicts | [page.tsx](../frontend/src/app/classes/%5Bid%5D/drafts/%5BartifactId%5D/page.tsx) | [draft-workspace-polish.test.tsx](../frontend/tests/draft-workspace-polish.test.tsx), [draft-save-conflict.spec.ts](../frontend/e2e/draft-save-conflict.spec.ts) |
| PLA-444 | Retain workspace path when attachment fails | [workspace-attach.tsx](../frontend/src/components/agent/workspace-attach.tsx) | [work-surface.test.tsx](../frontend/tests/work-surface.test.tsx) |
| PLA-445 | Match Work filter keyboard behavior to its accessibility semantics | [class-work-panel.tsx](../frontend/src/components/classes/class-work-panel.tsx) | [class-work-panel.test.tsx](../frontend/tests/class-work-panel.test.tsx) |
| PLA-446 | Replace prominent implementation details with decision-relevant writing and command guidance | [command-confirmation.tsx](../frontend/src/components/agent/command-confirmation.tsx), [live-draft-suggestion.tsx](../frontend/src/components/drafts/live-draft-suggestion.tsx) | [agent-command-confirmation.test.tsx](../frontend/tests/agent-command-confirmation.test.tsx), [live-draft-suggestion.test.tsx](../frontend/tests/live-draft-suggestion.test.tsx) |
| PLA-447 | Make draft row actions discoverable on touch devices | [class-drafts-panel.tsx](../frontend/src/components/classes/class-drafts-panel.tsx) | [class-drafts-panel.test.tsx](../frontend/tests/class-drafts-panel.test.tsx) |
| PLA-448 | Reduce nested navigation for draft plan, sources, and history | [page.tsx](../frontend/src/app/classes/%5Bid%5D/drafts/%5BartifactId%5D/page.tsx) | [draft-workspace-polish.test.tsx](../frontend/tests/draft-workspace-polish.test.tsx) |
| PLA-449 | Keep material picker rows inside a bounded scrollable popover | [source-context.tsx](../frontend/src/components/chat/source-context.tsx) | [viewport-matrix.spec.ts](../frontend/e2e/viewport-matrix.spec.ts) |
| PLA-450 | Keep flashcard ratings clear of the fixed bottom navigation | [deck-session.tsx](../frontend/src/components/study/deck-session.tsx), [app-shell.tsx](../frontend/src/components/layout/app-shell.tsx) | [study-remediation.spec.ts](../frontend/e2e/study-remediation.spec.ts) |
| PLA-451 | Keep the writer assistant composer visible while reading a long conversation | [page.tsx](../frontend/src/app/classes/%5Bid%5D/drafts/%5BartifactId%5D/page.tsx), [chat-pane.tsx](../frontend/src/components/chat/chat-pane.tsx) | [writing.spec.ts](../frontend/e2e/acceptance/writing.spec.ts) |
| PLA-452 | Make first-class onboarding lead to usable material and tutor setup | [class-overview.tsx](../frontend/src/components/classes/class-overview.tsx) | [class-hub.test.tsx](../frontend/tests/class-hub.test.tsx) |
| PLA-453 | Translate raw Work status codes into actionable student language | [class-work-panel.tsx](../frontend/src/components/classes/class-work-panel.tsx), [class-overview.tsx](../frontend/src/components/classes/class-overview.tsx), [app-sidebar.tsx](../frontend/src/components/layout/app-sidebar.tsx), [solution-row.tsx](../frontend/src/components/solutions/solution-row.tsx) | [class-work-panel.test.tsx](../frontend/tests/class-work-panel.test.tsx), [class-hub.test.tsx](../frontend/tests/class-hub.test.tsx) |
| PLA-454 | Explain answer style in terms of the help the student will receive | [chat-pane.tsx](../frontend/src/components/chat/chat-pane.tsx) | [chat-pane.test.tsx](../frontend/tests/chat-pane.test.tsx) |

## Remaining acceptance limits

- PLA-418 and PLA-425: semantic math, inactive-face exclusion, keyboard flow, focus and live
  announcements are represented and automatically tested. Actual VoiceOver/NVDA use has not been
  run in this recovery. Do not describe their assistive-technology acceptance as fully verified.
- PLA-431: the original-file double-failure gap and PLA-409 atomic-publication race received
  targeted follow-ups in the same PR. See [follow-up evidence](pr-70-followups.md); the PR and
  Linear records identify the exact follow-up head and hosted verification. Earlier totals below
  remain historical recovery evidence.
- PLA-407: browser composition flags and WebKit keyCode 229 are tested; physical CJK input in
  the packaged app is not certified. Compact CSS viewport tests are not native browser zoom.
- Real-stack acceptance uses deterministic tutor/embedding fixtures. Real-model quality,
  notarization, clean 8 GB hardware, extended soak and physical accessibility remain release gates.
- Separate PLA-476 scheduling and full PLA-477 server retry-payload validation are not claimed
  fixed. Recovered UI retries retain the submitted rating and operation ID, preventing a changed
  rating from being presented as acknowledged. No unrelated backend architecture is introduced.

## Verification

Recovery verification (the PR description and parent Linear record hold final packaging/hosted-CI results):

- Frontend: **870 tests across 88 files passed**; formatting, ESLint and TypeScript passed.
- Backend: **2,760 passed, 1 skipped**; Ruff formatting/lint passed. Initial full run had one
  timeout in the unchanged agent concurrency test's five-second pre-loop wait. Focused 41-test,
  prefix 50-test and full-suite reruns passed. No timeout inflation or product change hid it.
- Chromium smoke/viewport: **31 passed**. WebKit external navigation: **6 passed**.
- Production build passed; existing editor chunk-size warning remains. Production dependency
  audit found no known vulnerabilities. Contrast: **27 pairs, zero failures**; active-reference
  scan and diff-whitespace checks passed.
- Real-stack acceptance: **113 passed**, clean failure ledger and teardown. The initial local
  attempt had one accounting race followed by backend SIGTERM/connection failures during
  concurrent runs; the scoped harness changes address the identified cross-run hazard.
- Rust desktop tests: **26 passed**; Rust formatting passed.
- Frozen backend build and authenticated ephemeral-loopback smoke passed. The local app was
  rebuilt, signed with the existing Apple Development identity (79 code objects verified), and
  passed the completed signed-bundle smoke. Native Classes and Settings loaded successfully in
  an isolated profile, its app/backend exited, and the previous Lyra review app was restored.
- Narrow flashcard screenshot compared against retained repair: visual verdict **95/pass**;
  the long answer stays contained and all ratings clear the fixed navigation.

The totals in [the retained original report](ui-ux-remediation.md) describe its historical local
run only. Fresh real-stack, signed-app and hosted-CI results belong to this published PR's exact
head, and must not be inferred from the old report.

## Parallel integration warning

A read-only merge-tree check against [study reliability PR #71](https://github.com/oliverdougherC/Lyra/pull/71)
finds conflicts in `frontend/src/components/study/deck-session.tsx` and
`frontend/tests/deck-session.test.tsx`. Do not choose one side wholesale: preserve this batch's
accessible card face, correction/removal controls, session/deck counts and layout, while integrating
the other PR's persistent retry recovery. Whichever PR merges second needs a fresh rebase and
combined study/acceptance verification. This PR has not merged either branch.
