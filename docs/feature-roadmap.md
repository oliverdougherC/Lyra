# Feature Roadmap

This is Lyra's live product roadmap. It replaces the old phase-by-phase build plan, which no longer
matched the application after study tools, the draft workspace, the writer overhaul, and the
general agent landed in a different order than originally planned.

The historical phase documents are still valuable implementation records. They are evidence of
what shipped and why, not the queue for what should be built next.

## Direction

Lyra is a local-first study workspace for one student and their own course material. It should make
the common work of studying, solving, and writing faster without hiding uncertainty or taking
ownership of the student's files.

The current product rule is simple: reliability before breadth. A feature is not finished merely
because its happy path exists. It must survive restarts, dependency outages, malformed model output,
and ordinary user mistakes without corrupting work or presenting a generic failure as success.

New work is evaluated in this order:

1. Protect student data and privacy.
2. Prevent or recover from user-facing failure.
3. Make degraded behavior explicit and useful.
4. Add regression coverage and operational evidence.
5. Only then expand the product surface.

## Completed

These are current product surfaces, not promises about every edge case. Open hardening work is
tracked under **Now**.

### Class workspace and course context

- Create, rename, archive, restore, and delete class workspaces.
- Upload and manage course documents, including moving a document between classes.
- Extract, chunk, embed, and retrieve text with page and section provenance.
- Combine vector and lexical retrieval, with optional reranking and citation-aware context.
- Maintain a student-visible class profile whose inferred facts can be corrected.

### Tutor and solutions

- Hold class-scoped conversations grounded in uploaded material.
- Use Guide, Show, and Solve assistance modes.
- Parse assignments into problems, correct the parse before solving, and generate solutions in the
  background.
- Record which solution steps were checked by deterministic tools and which remain model-derived.
- Ask follow-up questions about a solution and export the result.

### Study tools

- Generate flashcard decks and quizzes from class material.
- Review cards with a persisted scheduler.
- Track quiz attempts and expose topic-level weaknesses from recorded answers.

### Writing workspace

- Create and edit drafts with revision history.
- Capture an assignment brief, maintain a document plan, and work section by section.
- Chat with a writing assistant that can read the draft, course sources, comments, and plan.
- Run longer drafting and review passes, produce review comments, and propose changes for approval.
- Track sources and excerpts, distinguish course evidence from web evidence, and export drafts.
- Use optional web research through a loopback-only Firecrawl service.

The archived writer plan is in [writer-roadmap.md](writer-roadmap.md). The decision record and
implementation shape are in [writer-overhaul.md](writer-overhaul.md) and
[phase-4-writer-integration.md](phase-4-writer-integration.md).

### Agent and local operation

- Use a general class agent with bounded tool profiles, visible activity, confirmation gates, and
  auditable workspace changes.
- Configure an OpenAI-compatible tutor endpoint, with an explicit warning before private material
  is sent to a non-local endpoint.
- Launch the frontend, backend, and optional Firecrawl dependency from one repository command.
- Inspect liveness, readiness, logs, ownership, and degraded service state from the launcher.

## Now: stabilization release

No new major feature should enter this section until these gates are closed. This work is about
making the product that already exists dependable during real study sessions.

### 1. Data integrity and privacy

- [x] Make each SQLite migration atomic and advance its version only after a successful commit.
- [x] Add a bounded SQLite busy timeout so brief write contention does not become an immediate
  `database is locked` failure.
- [x] Prevent private draft, brief, plan, comment, and conversation text from being copied into web
  search queries.
- [x] Keep raw writer search queries out of persisted activity labels.
- [x] Complete restart, interruption, and rollback tests for every long-running artifact type.
- [x] Document and test backup and restore of the local data directory before a public release.

### 2. Long-running workflow durability

- [x] Persist writer and reviewer job inputs instead of keeping the only executable copy in process
  memory.
- [x] Resume writer and reviewer work from the last durable section or review boundary after a
  restart. Mid-model-call resumption is not a goal; the interrupted call may run again.
- [x] Add explicit cancellation with truthful partial-progress reporting.
- [x] Preserve structured warning state, including course-only fallback when web research fails.
- [x] Audit ingestion, solutions, study generation, and recognition against the same lifecycle
  contract: queued, running, recoverable interruption, cancelled, completed, or failed.

### 3. Recoverable failures and degraded dependencies

- [x] Keep incomplete agent replies out of conversation history so a retry starts from a valid
  transcript.
- [x] Budget long agent conversations against the configured model context window.
- [x] Distinguish retryable agent failures from invalid configuration or permanent upstream errors.
- [x] Retry only transient Firecrawl failures, with a small bounded delay.
- [x] Treat Firecrawl as optional for core readiness while reporting unavailable and misconfigured
  states separately.
- [x] Make degraded launch without web research explicit in `status` and `doctor`.
- [x] Exercise tutor disconnects, malformed model output, Firecrawl outages, cancellation, and
  process restarts through end-to-end fault tests.

### 4. Release gates

- [x] Run backend formatting, lint, and tests in CI.
- [x] Run frontend formatting, lint, type checking, unit tests, production build, and production
  dependency audit in CI.
- [x] Add a nonvisual browser smoke test for route hydration and runtime errors without coupling it
  to the ongoing visual redesign.
- [x] Add end-to-end coverage for one representative class, document, chat, solution, study, and
  draft lifecycle.
- [x] Define a macOS Apple Silicon release checklist covering clean installation, first launch,
  restart recovery, offline/degraded use, and data preservation.
- [x] Keep production dependency audits free of known high and critical advisories.

### 5. Documentation and supportability

- [x] Replace the internal top-level README with a user-facing overview and quick start.
- [x] Separate live roadmap material from historical phase and handoff records.
- [x] Add a troubleshooting guide for the failures a student can resolve without reading source
  code.
- [x] Add a concise privacy and data-location reference covering the tutor endpoint, Firecrawl,
  local files, key storage, and deletion.
- [x] Add contribution guidance for bug reproduction, tests, migrations, and release verification.

### Stabilization exit criteria

The stabilization release is ready only when all of the following are true:

- a clean macOS Apple Silicon setup can launch through `./run` without manual process cleanup;
- the app remains useful when optional web research is unavailable;
- a restart does not silently discard queued work or corrupt a partially completed artifact;
- cancellation and upstream failures settle into an honest, retryable state;
- no known high-severity correctness, privacy, or production dependency issue is open;
- CI passes from a clean checkout; and
- the README, local deployment guide, privacy notes, and troubleshooting steps match the shipped
  behavior.

## Next: measured quality

After stabilization, improve the quality of existing answers and artifacts with repeatable
measurements rather than adding unrelated surfaces.

### Retrieval and document quality

- Detect text layers that are present but unusable, such as photographed pages containing only mail
  headers or scattered equation characters.
- Add a page-selective recognition quality gate so lossy pages can be re-read without retranscribing
  a good document wholesale.
- Maintain class-scale retrieval evaluations that distinguish the right passage in the wrong
  document from a true hit.
- Revisit embedding or reranking changes only when a recorded failure case justifies re-indexing or
  added latency.

### Solver and study quality

- Expand the real-course solver evaluation beyond the existing engineering-course corpus.
- Track parsing, reasoning, deterministic verification, and final-answer accuracy separately.
- Test flashcard and quiz grounding, duplicate control, scheduling, and weakness reporting over
  complete course workspaces.

### Writer quality

- Build a small, versioned evaluation set for planning, source use, citation placement, revision
  quality, comment usefulness, and instruction following.
- Test long-horizon passes across restarts and retries, including drafts with existing student prose.
- Measure whether review passes find seeded problems without duplicating comments or rewriting
  unrelated sections.
- Improve transitions, argument pressure-testing, and citation support only when the evaluation can
  show the change helped.

### Operational quality

- Add structured local diagnostics that are useful in bug reports without exposing document text,
  prompts, API keys, or private paths.
- Exercise migrations and launcher recovery against upgrades from released versions, not only fresh
  databases.
- Establish a small, repeatable release-candidate soak test for long study and writing sessions.

## Later: distribution and deliberate expansion

These are valid directions, but none outranks the stabilization and measurement work above.

- Bundle a local inference engine and supported model choices so Lyra can deliver its privacy posture
  without requiring a separately managed tutor endpoint.
- Add memory-aware model recommendations and per-feature capability checks.
- Package Lyra as a signed native application with one supervised lifecycle and a safe update path.
- Add bulk course import after the document lifecycle is fully recoverable.
- Add practice-problem and study-guide workflows by reusing the existing artifact, provenance, and
  job contracts.
- Explore deadline and calendar views after the class profile and extraction data are consistently
  accurate.
- Consider code-aware class assistance only after its filesystem boundary, prompt-injection threat
  model, preview, and confirmation requirements are specified and tested.

## Explicitly out of scope

The following are not current product goals:

- accounts, multi-user collaboration, or cloud sync;
- telemetry, behavioral analytics, or silent update checks;
- hosted third-party model accounts managed by Lyra;
- social feeds or public sharing;
- a plugin marketplace;
- voice interaction or video lecture processing; and
- filesystem writes or command execution that bypass preview and explicit confirmation.

The responsive web interface remains important, but a separate mobile application is not planned.

## Maintaining this roadmap

- Move an item to **Completed** only when the behavior exists and its release evidence is recorded.
- Keep defects and hardening work in **Now** until their verification gates pass.
- Do not use historical phase numbers as current priority.
- Record measurement details in focused handoff or evaluation documents and link them here instead
  of turning this file into an implementation log.
- When scope changes, revise the roadmap in the same change so the public plan never trails the
  product again.
