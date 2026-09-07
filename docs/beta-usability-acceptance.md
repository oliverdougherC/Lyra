# Beta usability candidate acceptance — September 6, 2026

This is the PLA-404 implementation and acceptance register for the PR based on
`e96bf4886977c648f9e7905c7807c806b1ae7a80`. It preserves merged PRs #73/#74/#75/#77/#78.
The PR and Linear handoff record the exact tested source SHA and local bundle identity.
An unmerged development candidate is not public release approval.

## Product behavior

The application retains Ask / Practice / Work / Files, one composer, and the writer's single tool
picker. Navigation labels name their destination. Back/Forward and refresh restore the main
reading pane and Files list; the class breadcrumb returns to the originating tab/filter. Search
state is local to the class and panel. A new unrelated route starts at the top. Anchor navigation
keeps its existing focus behavior; ordinary query refresh does not move keyboard focus.

Primary titles and file names wrap. Solver questions and equations are the visual priority; Edit
reveals subpart corrections and removal, while grouping, merging, source pages, undo, save and
reload remain available. Routine tab inventory counts and repeated instructional captions no
longer compete with the task. Long class names in short windows leave Files a usable reading area.

Work and Practice use title search and bounded lists. Conversation history has search and bounded
pages, with the active conversation/work retained. New quiz creates work; Continue quiz requires
backend active-attempt metadata; older servers truthfully show Open quiz. Review due uses the real
due count. Generated-question counts are never described as questions answered. Deck completion
keeps detailed rating counts in a disclosure and offers a return when there are no due cards.

Completed agent history is subordinate. Pending proposals, unresolved failures, exact commands,
working folders, consequences, and reviewable diffs remain accessible. Optional Settings tasks use
disclosures that open for active operations, unresolved errors and targeted deep links. Acknowledged
remote use and a populated installation are ordinary states; missing consent remains actionable.
Cached content survives refresh errors, and recovery buttons expose pending and failed outcomes.

## Automated and rendered evidence

The test receipt in the PR/Linear differentiates full production-frontend browser tests with
intercepted synthetic APIs from real-backend acceptance with a deterministic tutor fixture. Neither
is a real-provider quality result or physical-device acceptance. Before/after captures use only
synthetic classes, filenames, questions, commands and writing. The macOS WebKit keyboard matrix
uses Option-Tab to include buttons when full keyboard access is off; this is browser automation,
not physical keyboard or spoken screen-reader acceptance.

Required reproducible commands from this worktree:

```sh
(cd frontend && pnpm test && pnpm typecheck && pnpm lint && pnpm format:check)
(cd frontend && pnpm test:e2e)
ACCEPTANCE_BACKEND_PORT=18141 ACCEPTANCE_FRONTEND_PORT=18142 ACCEPTANCE_TUTOR_PORT=18143 \
  PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring \
  ./scripts/run-acceptance.sh
uv run python scripts/check_docs.py
uv run python scripts/check_active_references.py
```

Do not run two builds against the same frontend output directory or two Playwright runs against
the same result directory. Independent visual fixtures use separate build trees/results. The
first combined acceptance attempt was interrupted after seven passes when another lane replaced
its frontend build and its result directory; it is retained as interrupted, not a product pass.

## Physical and human protocol

These existing issues remain **In Review** until the exact check below actually runs. Preparation,
DOM assertions, touch emulation and CSS viewport scaling do not close them. Record pass/fail/not
run with source SHA, app version/build/hash, macOS and hardware, input device/reader version,
operator, timestamp, fixture identifier, and screenshot or spoken-output notes. Preserve failures.

Before any launch, the release owner must provide the candidate and prove distinct disposable
paths for data/database/cache/logs/models plus fail-closed test credential storage through every
child/relaunch. Do not mutate the operator's normal student profile or shared Keychain. Only the
release owner coordinates replacing or launching the shared installed app.

| Issue | Exact remaining check and runnable procedure | Acceptance |
| --- | --- | --- |
| PLA-407 | In packaged WebKit, enable a real Chinese/Japanese/Korean IME. Type a multi-candidate phrase in Ask; confirm a candidate with Enter. Observe no message sent. Press a subsequent Enter once: one message. Shift+Enter inserts a newline. Repeat while a reply streams to preserve the next-turn draft. | Physical IME not run |
| PLA-418 | Start VoiceOver (NVDA only on a supported browser host) and open a synthetic deck containing prose and math. Read the active question, activate Show answer and read the answer. Verify hidden face is absent and equation content is meaningful. Rate using keyboard and verify next card. | Assistive technology not run |
| PLA-425 | With a screen reader, answer one MCQ incorrectly and one fill-blank correctly. Record spoken grading once, focus at the next question after Next, and results focus after completion. Use quiz help, then Back; confirm recorded reveal and attempt retained. | Assistive technology not run |
| PLA-428 | Open solver source containing math and a page image. Use independent source zoom/reset, then the extracted-text alternative. Traverse using the screen reader and verify equivalent text is reachable without interpreting the image. Include a page-render failure and text fallback. | Assistive technology not run |
| PLA-442 | Open a synthetic writer/agent diff with added, removed and unchanged lines. Traverse with the reader. Verify each line's meaning is announced without relying on color; read exact command and folder before approval. Do not execute the fixture command. | Assistive technology not run |
| PLA-445 | On Work, use Tab, Enter and Space for each filter, then search and clear. Record spoken filter label and pressed state, preserved query after task/Back, and no false tab semantics. | Spoken interaction not run |
| PLA-446 | Give an unfamiliar tester a draft proposal and a harmless command proposal. Without explaining the interface, ask what each primary action does, what changes, and what is sent/accessed. They must identify review versus apply and command consequences; exact command/diff remains available. Record their words and mistakes. | First-time tester not run |
| PLA-447 | On physical touch input without hover, open populated Work/Drafts and locate Rename/Delete for a long title. Rename a disposable draft; open Delete then cancel. Record discoverability without tapping an invisible target. | Physical touch not run |
| PLA-448 | Give an unfamiliar tester an existing synthetic draft. Ask them to find Plan, Sources and History, return to prose, edit, and exit immersive mode. Record path/time/confusion and verify edited prose survives. Do not coach picker location. | First-time tester not run |
| PLA-450 | Set actual native/browser zoom to 200% using its real zoom control and record it. At a short window, inspect initial and maximum scroll on card front/back with long answer/math. All four ratings and intervals must remain reachable and unobscured by bottom navigation. CSS zoom/device-scale emulation is not this test. | Native 200% zoom not run |
| PLA-452 | Start a proven-empty synthetic profile with no files or tutor endpoint. Ask an unfamiliar tester to make the class ready for studying. They must identify tutor setup and upload immediately; no action should falsely imply uploaded materials. Then upload synthetic material and verify Ask/Practice become usable. | First-time tester not run |

## Implementation issue matrix

All implementation is reviewable in the linked PR; final automated counts and exact SHA are in its
receipt and Linear. No issue is marked Done merely because code changed.

| Issue | Implemented scope | Remaining gate |
| --- | --- | --- |
| PLA-404 | Cohesive journey and shared behavior in this PR | Review/integration and physical/human rows above |
| PLA-482 | Readable extraction review and all correction operations | Final candidate and student comprehension |
| PLA-486 | Work destination, canonical handoffs, actual pane history, return context | Final packaged navigation |
| PLA-487 | Wrapping titles/files, readable questions/commands, panel-responsive brief | Native 200% zoom |
| PLA-488 | Stable task tabs, less duplicate chrome, subordinate settled writer detail | Unfamiliar-user traversal |
| PLA-489 | Bounded searchable histories and retained list context | Final integrated large-inventory journey |
| PLA-490 | Explicit create/open/continue/due actions and concise completion | Learning-owner continuation API integration |
| PLA-491 | Compact settled history and discoverable approvals/failures | Final packaged approval journey |
| PLA-493 | Task disclosures, neutral acknowledged states, deep-link opening | Final packaged maintenance journey |
| PLA-494 | Retained cached content and visible recovery outcomes | Native bootstrap/update recovery |
