# UI/UX remediation for tester onboarding

The 50 findings in [PLA-404](https://linear.app/platinum-labs/issue/PLA-404/resolve-uiux-audit-findings-before-broad-tester-onboarding) are implemented in this checkout. The changes preserve the existing design system and add no dependencies. This record describes the repairs and their verification; it does not certify packaged distribution or real-model answer quality.

## Changes

- Writing preserves unsaved plans and live paragraphs across tool navigation. Finalization waits for all paragraph saves; concurrent typing and model continuations survive delayed or lost responses. Plan responses are applied monotonically so older replies cannot erase a newer version. Review-comment actions use the same save barrier as other writer actions.
- Conflict review shows both versions and offers copying or downloading them before choosing a replacement. Writer tools use direct navigation, and the message input has its own footer. Short windows switch between the full-height document and tools while keeping their state mounted.
- Flashcards expose their content separately from the flip button, support editing and confirmed removal, and report honest session/deck counts. Rating and deletion retries reconcile lost responses. Quiz attempts guard asynchronous completion, announce feedback, and move focus predictably. Numeric options validate what users actually typed.
- Files distinguish no matches, true emptiness and read failures. Single-file deletion requires the same intentional confirmation as bulk deletion. Cross-filter selections have explicit scope, and outline/import/history/source failures have contextual recovery.
- Settings expose persistent save state, serialize updates, retain newer key input during delayed saves and invalidate stale diagnostics. Backend tool/image probes reject results for changed endpoint/model/credentials; credential replacement clears stored capability results.
- Solutions support child statement/label editing and final-removal Undo. Only exact duplicate child blocks are normalized from parent statements; ordinary and trailing instructions remain intact. Verdict disclosures are keyboard/tap operable and sit outside accordion buttons. PDF sources support independent zoom and extracted text.
- Navigation drawers close after selecting a destination. Material lists scroll inside their popover. Mobile navigation reserves usable space, and flashcard ratings remain visible.
- Agent proposal recovery uses live file review data, preserves it across stored-list polling, and rechecks the actual file. Stale proposals cannot be confirmed. Retries and automatic continuation require healthy prerequisite reads.
- Presentation removes repeated privacy explanations and prominent runtime counters, uses readable statuses and answer-style labels, collapses optional reference selection, and exposes secondary actions without requiring hover.

## Verification

Final verification:

- Frontend unit and component tests: 857 passed across 86 files (baseline: 778).
- Full backend tests: 2,760 passed, 1 skipped.
- Real-stack browser acceptance: 113 passed, including the new short-window writer test and clean process teardown.
- Mocked browser smoke and viewport suite: 21 passed.
- Frontend lint, TypeScript, formatting and production build passed.
- Backend Ruff lint and formatting passed; contrast, active-reference and diff-whitespace checks passed.

The build retains the existing large-chunk advisory for the editor bundle. Backend tests retain dependency deprecation warnings; no test failure remains in the completed suites.

Completed manual checks used isolated synthetic data and the repository's deterministic tutor fixture:

- Material picker at 1280 by 720: all rows remain inside the panel; scrolling and selecting the last file work.
- Writer at 1280 by 720: composer bottom is 693 pixels, inside the 720-pixel viewport.
- Plan: unsaved thesis survives Sources and back; typing First, Enter, Second retains two evidence lines.
- Live draft: unsaved paragraph reaches the proposal after Review and merge through real API calls.
- Flashcard at 375 by 667: all ratings clear the bottom navigation; corrected answer persists after reload.
- PDF at 1024 by 600: independent zoom to 175 percent and extracted-text reading both work.
- Settings at 375 by 667: key controls fit; changing an endpoint removes prior Connected feedback and disables stale model choices.
- File search: no-match state is truthful; individual Delete opens a named confirmation and Cancel preserves the file.
- Verdict disclosure opens without collapsing its problem.
- Writer at 640 by 360: final compact mode places composer bottom at 333 pixels; Document/Assistant switching preserves an unsent question. This is an effective CSS viewport check, not a claim of native browser zoom testing.

Independent cross-reviews revisited the high-risk save, retry, settings, study, and agent flows. Every concrete follow-up was fixed with regression coverage, including uncertain write responses and version races.

## Issue register

Each issue contains the original evidence and acceptance criteria. The implementation notes and full test output are attached to the parent remediation record in Linear.

| Issue                                                                                                                            | Repair                                                                                       |
| -------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| [PLA-405](https://linear.app/platinum-labs/issue/PLA-405/show-recoverable-errors-when-saved-conversations-fail-to-load)          | Show recoverable errors when saved conversations fail to load                                |
| [PLA-406](https://linear.app/platinum-labs/issue/PLA-406/protect-individual-document-deletion-with-confirmation-or-reversible)   | Protect individual document deletion with confirmation or reversible undo                    |
| [PLA-407](https://linear.app/platinum-labs/issue/PLA-407/prevent-ime-candidate-confirmation-from-submitting-a-chat)              | Prevent IME candidate confirmation from submitting a chat                                    |
| [PLA-408](https://linear.app/platinum-labs/issue/PLA-408/distinguish-file-search-no-results-from-an-empty-document-library)      | Distinguish file-search no-results from an empty document library                            |
| [PLA-409](https://linear.app/platinum-labs/issue/PLA-409/invalidate-endpoint-diagnostics-and-models-after-configuration-changes) | Invalidate endpoint diagnostics and models after configuration changes                       |
| [PLA-410](https://linear.app/platinum-labs/issue/PLA-410/dismiss-the-temporary-navigation-drawer-when-opening-a-destination)     | Dismiss the temporary navigation drawer when opening a destination                           |
| [PLA-411](https://linear.app/platinum-labs/issue/PLA-411/show-outline-loading-failures-without-inventing-document-structure)     | Show outline loading failures without inventing document structure facts                     |
| [PLA-412](https://linear.app/platinum-labs/issue/PLA-412/distinguish-import-status-failure-from-unsupported-platform)            | Distinguish import status failure from unsupported platform                                  |
| [PLA-413](https://linear.app/platinum-labs/issue/PLA-413/make-file-selection-scope-stable-and-explicit-across-search-changes)    | Make file selection scope stable and explicit across search changes                          |
| [PLA-414](https://linear.app/platinum-labs/issue/PLA-414/expose-persistent-save-state-for-automatically-saved-settings)          | Expose persistent save state for automatically saved settings                                |
| [PLA-415](https://linear.app/platinum-labs/issue/PLA-415/consolidate-repeated-technical-privacy-and-import-explanations)         | Consolidate repeated technical privacy and import explanations                               |
| [PLA-416](https://linear.app/platinum-labs/issue/PLA-416/offer-a-direct-route-to-archived-classes-from-the-class-index)          | Offer a direct route to archived classes from the class index                                |
| [PLA-417](https://linear.app/platinum-labs/issue/PLA-417/guard-quiz-restart-against-stale-answer-and-finish-responses)           | Guard quiz restart against stale answer and finish responses                                 |
| [PLA-418](https://linear.app/platinum-labs/issue/PLA-418/expose-flashcard-question-and-answer-as-accessible-content)             | Expose flashcard question and answer as accessible content                                   |
| [PLA-419](https://linear.app/platinum-labs/issue/PLA-419/allow-extracted-subproblem-statements-to-be-corrected-before-solving)   | Allow extracted subproblem statements to be corrected before solving                         |
| [PLA-420](https://linear.app/platinum-labs/issue/PLA-420/keep-visible-undo-after-the-final-extracted-problem-is-removed)         | Keep visible Undo after the final extracted problem is removed                               |
| [PLA-421](https://linear.app/platinum-labs/issue/PLA-421/do-not-label-session-only-flashcard-counts-as-deck-wide-progress)       | Do not label session-only flashcard counts as deck-wide progress                             |
| [PLA-422](https://linear.app/platinum-labs/issue/PLA-422/provide-a-correction-and-removal-path-for-generated-flashcards)         | Provide a correction and removal path for generated flashcards                               |
| [PLA-423](https://linear.app/platinum-labs/issue/PLA-423/make-solution-verdict-explanations-accessible-without-hover)            | Make solution-verdict explanations accessible without hover                                  |
| [PLA-424](https://linear.app/platinum-labs/issue/PLA-424/separate-solution-source-fetch-errors-from-empty-class-guidance)        | Separate solution-source fetch errors from empty-class guidance                              |
| [PLA-425](https://linear.app/platinum-labs/issue/PLA-425/announce-quiz-grading-and-manage-focus-between-questions-and-results)   | Announce quiz grading and manage focus between questions and results                         |
| [PLA-426](https://linear.app/platinum-labs/issue/PLA-426/validate-study-counts-explicitly-instead-of-silently-changing-inputs)   | Validate study counts explicitly instead of silently changing inputs                         |
| [PLA-427](https://linear.app/platinum-labs/issue/PLA-427/expose-study-and-solution-secondary-actions-on-touch-devices)           | Expose study and solution secondary actions on touch devices                                 |
| [PLA-428](https://linear.app/platinum-labs/issue/PLA-428/add-independent-source-page-zoom-and-a-readable-text-alternative)       | Add independent source-page zoom and a readable text alternative                             |
| [PLA-429](https://linear.app/platinum-labs/issue/PLA-429/offer-contextual-retry-when-revision-history-cannot-load)               | Offer contextual retry when revision history cannot load                                     |
| [PLA-430](https://linear.app/platinum-labs/issue/PLA-430/offer-document-fetch-retry-inside-the-study-creation-dialog)            | Offer document-fetch retry inside the study creation dialog                                  |
| [PLA-431](https://linear.app/platinum-labs/issue/PLA-431/provide-recovery-actions-for-a-failed-source-page-render)               | Provide recovery actions for a failed source-page render                                     |
| [PLA-432](https://linear.app/platinum-labs/issue/PLA-432/reduce-duplicate-source-list-scanning-in-solution-setup)                | Reduce duplicate source-list scanning in solution setup                                      |
| [PLA-433](https://linear.app/platinum-labs/issue/PLA-433/replace-ornamental-study-copy-with-direct-guidance)                     | Replace ornamental study copy with direct guidance                                           |
| [PLA-434](https://linear.app/platinum-labs/issue/PLA-434/save-live-draft-edits-before-review-and-merge)                          | Save live-draft edits before Review and merge                                                |
| [PLA-435](https://linear.app/platinum-labs/issue/PLA-435/preserve-typing-made-while-a-live-paragraph-save-is-in-flight)          | Preserve typing made while a live paragraph save is in flight                                |
| [PLA-436](https://linear.app/platinum-labs/issue/PLA-436/preserve-unsaved-plan-edits-across-section-switches-and-refreshed)      | Preserve unsaved plan edits across section switches and refreshed versions                   |
| [PLA-437](https://linear.app/platinum-labs/issue/PLA-437/allow-normal-multiline-typing-in-plan-evidence-fields)                  | Allow normal multiline typing in plan evidence fields                                        |
| [PLA-438](https://linear.app/platinum-labs/issue/PLA-438/save-current-draft-before-starting-address-comment-work)                | Save current draft before starting Address comment work                                      |
| [PLA-439](https://linear.app/platinum-labs/issue/PLA-439/distinguish-writer-and-work-loading-failures-from-empty-content)        | Distinguish writer and work loading failures from empty content                              |
| [PLA-440](https://linear.app/platinum-labs/issue/PLA-440/make-per-change-reject-controls-actionable-or-remove-unsupported)       | Make per-change Reject controls actionable or remove unsupported controls                    |
| [PLA-441](https://linear.app/platinum-labs/issue/PLA-441/expose-recovery-actions-for-stale-workspace-proposals)                  | Expose recovery actions for stale workspace proposals                                        |
| [PLA-442](https://linear.app/platinum-labs/issue/PLA-442/give-diff-additions-and-removals-accessible-meaning)                    | Give diff additions and removals accessible meaning                                          |
| [PLA-443](https://linear.app/platinum-labs/issue/PLA-443/provide-a-usable-comparison-and-preservation-path-for-draft-conflicts)  | Provide a usable comparison and preservation path for draft conflicts                        |
| [PLA-444](https://linear.app/platinum-labs/issue/PLA-444/retain-workspace-path-when-attachment-fails)                            | Retain workspace path when attachment fails                                                  |
| [PLA-445](https://linear.app/platinum-labs/issue/PLA-445/match-work-filter-keyboard-behavior-to-its-accessibility-semantics)     | Match Work filter keyboard behavior to its accessibility semantics                           |
| [PLA-446](https://linear.app/platinum-labs/issue/PLA-446/replace-prominent-implementation-details-with-decision-relevant)        | Replace prominent implementation details with decision-relevant writing and command guidance |
| [PLA-447](https://linear.app/platinum-labs/issue/PLA-447/make-draft-row-actions-discoverable-on-touch-devices)                   | Make draft row actions discoverable on touch devices                                         |
| [PLA-448](https://linear.app/platinum-labs/issue/PLA-448/reduce-nested-navigation-for-draft-plan-sources-and-history)            | Reduce nested navigation for draft plan, sources, and history                                |
| [PLA-449](https://linear.app/platinum-labs/issue/PLA-449/keep-material-picker-rows-inside-a-bounded-scrollable-popover)          | Keep material picker rows inside a bounded scrollable popover                                |
| [PLA-450](https://linear.app/platinum-labs/issue/PLA-450/keep-flashcard-ratings-clear-of-the-fixed-bottom-navigation)            | Keep flashcard ratings clear of the fixed bottom navigation                                  |
| [PLA-451](https://linear.app/platinum-labs/issue/PLA-451/keep-the-writer-assistant-composer-visible-while-reading-a-long)        | Keep the writer assistant composer visible while reading a long conversation                 |
| [PLA-452](https://linear.app/platinum-labs/issue/PLA-452/make-first-class-onboarding-lead-to-usable-material-and-tutor-setup)    | Make first-class onboarding lead to usable material and tutor setup                          |
| [PLA-453](https://linear.app/platinum-labs/issue/PLA-453/translate-raw-work-status-codes-into-actionable-student-language)       | Translate raw Work status codes into actionable student language                             |
| [PLA-454](https://linear.app/platinum-labs/issue/PLA-454/explain-answer-style-in-terms-of-the-help-the-student-will-receive)     | Explain answer style in terms of the help the student will receive                           |

## Remaining release boundaries

The automated acceptance stack exercises production backend routes, storage and workers with deterministic model fixtures. It does not establish real-model quality, a full VoiceOver/IME device matrix, OS-native file picker/import behavior, signing/notarization, or the physical low-memory Mac soak. Those release checks remain separate from closing these 50 implementation findings.

Original audit screenshots and repaired screenshots are retained with the Linear audit. No production data, credentials, deployment or dependency graph was changed.
