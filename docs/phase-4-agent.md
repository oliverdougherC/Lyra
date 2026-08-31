# Phase 4 Agent Specification

> Historical specification. This records the 2026-08-07 checkout-era implementation and its
> former provider topology. The live desktop/Exa contract is in `README.md`, `architecture.md`,
> and `desktop-migration-inventory.md`.

**Status:** Implemented on 2026-08-07; automated close-out is green. Final phase acceptance still
requires a real pinned Firecrawl instance and the recorded breakpoint/theme browser pass described
in P4.7. See [phase-4-handoff.md](phase-4-handoff.md).

## Goal

Let Lyra gather public evidence and work with a class or lab repository without turning untrusted
course material, web pages, or source files into silent authority over the host machine.

The existing Phase 2 tool loop already guarantees termination, registry allowlisting, and an
in-memory call transcript (`backend/llm/tools.py:1-22`, `404-450`). Phase 4 adds shared web and
workspace tools, durable audit, capability policy, and the user surfaces needed to understand and
approve their effects. It does not adopt an agent framework.

## Coordination with the active writer roadmap

`docs/writer-roadmap.md` was implemented before the shared Phase 4 integration pass and owns its
writer pipeline. Its W3 work defines the shared product concepts: a default-off global/per-class web
toggle, search/fetch, and a per-class source ledger. The landed implementation exposes them in
`backend/core/writer_budgets.py`, `backend/core/web_research.py`,
`backend/core/source_ledger.py`, and migrations 023-024.

The Phase 4 merge-boundary pass waited for that implementation to stop editing, then:

1. runs both suites against the merged writer baseline;
2. records the highest migration number rather than assuming `018` is available;
3. preserves the writer-facing function contracts where sound;
4. promotes web consent, URL policy, Firecrawl transport, and the ledger into shared services; and
5. replaces the in-progress direct DuckDuckGo/direct-fetch transport with the Firecrawl path specified
   here, rather than building a second web stack.

Writer pipelines and the general class agent use one policy resolver, one source ledger, one
Firecrawl client, and one audit vocabulary. Phase 4 does not rewrite W1/W2/W4-W6.

## Product principles

1. **Read broadly, act narrowly.** The agent may inspect explicitly granted sources; it may only
   propose changes.
2. **The user commits host effects.** File application and command execution are user-only API
   actions, never model tools.
3. **One capability, one visible grant.** Web, workspace read, workspace write proposals, and command
   execution are separate and default off.
4. **Evidence survives the answer.** Web pages are snapshotted and claims/facts name their source.
5. **Failure is a product state.** Disabled access, unavailable Firecrawl, truncation, stale files,
   rejected commands, and model/tool limits are displayed honestly.
6. **The serial local model is the reference.** No design depends on parallel agents or a large cloud
   model.

## Deliberate exclusions

- Hosted Firecrawl or third-party search providers.
- Firecrawl crawl, map, agent, browser, interact, action, cookie, or authenticated-profile features.
- Arbitrary shell access, background daemons, package installation, git commit/push, file deletion,
  rename, or moving files.
- Reading multiple unrelated host roots through one class workspace.
- Automatic application after tests pass.
- Native folder pickers or bundling/supervising Firecrawl; packaging belongs to Phase 6.
- Treating fetched content or successful tool output as trusted instruction.

## Architecture decisions

### 1. Keep the in-house loop; make registries contextual

The shared loop gains an explicit `ToolRegistry`/profile argument instead of consulting a module
global. A registry is assembled from the current class, session, enabled capabilities, and attached
workspace. Existing CAS verification keeps a pure registry. Writer roles get writer tools plus shared
web tools only when web access resolves true. Class chat gets the smallest registry required for the
turn.

Every definition declares:

- capability (`compute`, `web_read`, `workspace_read`, `change_proposal`, `command_proposal`);
- effect (`pure`, `network_read`, `filesystem_read`, `database_proposal`);
- result origin/trust label; and
- argument/result display policy for audit redaction.

No registry contains a filesystem-apply or process-execute handler.

Registry composition is a hard invariant, not a prompt convention:

- research turns may combine `web_read` with database-only source/profile proposals, but never with
  workspace or command capabilities;
- code turns may combine `workspace_read` with inert workspace-change proposals, but never with web
  or command capabilities;
- command proposals run in a separate follow-up turn with no web, file-write, apply, or execute tool;
- writer roles may combine web reads with draft/comment proposals because those remain reviewable
  database content, not host effects; and
- pure compute tools may accompany any profile.

Static registry tests enumerate every allowed combination and fail if a forbidden pair or an
apply/execute handler appears.

### 2. Firecrawl is a loopback dependency, not embedded code

Use `httpx`, already present, against Firecrawl API v2. Do not add the Firecrawl SDK. Settings store
a non-secret base URL defaulting to `http://127.0.0.1:3002`; it is accepted only when every resolved
address is loopback. A test action checks `/v0/health/readiness` and performs an optional bounded
scrape smoke test.

The supported contract is:

- `POST /v2/search` for at most five web results;
- `POST /v2/scrape` with Markdown/main-content output for one selected public URL;
- no headers, cookies, actions, browser sessions, TLS bypass, proxy selection, or cloud-only
  endpoints; and
- no `lockdown` flag for ordinary research: Firecrawl documents that mode as cache-only and it would
  make a fresh source fetch fail. Lyra instead disables scrape until the pinned self-host build
  passes the redirect gate below.

The current public v2 schema does not document a request-level `threatProtection` field. Lyra does
not send invented API fields: it validates target/result URLs itself, explicitly sets
`skipTlsVerification=false`, omits powerful scrape options, recommends private/link-local egress
blocking for the Firecrawl container, and keeps scrape disabled unless the pinned build passes a
real public-to-private redirect test. Search metadata can remain available if scrape fails that gate.

The self-host guide currently verifies release `v2.11.162`, loopback port `3002`, and the v2 scrape
shape; it also states that core search/scrape routes exist in the default stack while advanced
browser/agent features do not. Implementation pins a verified release at that time rather than
copying `latest`: [official self-host guide](https://docs.firecrawl.dev/contributing/self-host),
[search API](https://docs.firecrawl.dev/api-reference/endpoint/search), and
[scrape API](https://docs.firecrawl.dev/api-reference/endpoint/scrape).

### 3. Share the source ledger with the writer

The writer's in-progress per-class ledger becomes the common evidence store. Course documents and
web snapshots stay first-class source types. Phase 4 extends each web source with final URL, content
type, snapshot hash, truncation state, and source revision. Exact relied-on excerpts remain separate
rows. A refresh never mutates evidence already cited by a fact, draft, or message.

`search_web` returns bounded metadata. `fetch_source` creates or reuses a ledger snapshot and returns
its source ID plus a bounded excerpt, not the unbounded page. General chat, writer research, review,
and profile proposals all cite that ID.

### 4. Use class chat as the agent surface

Phase 4 does not create a second conversation product. The existing `/classes/[id]/chat` workspace
gets a compact capability bar:

- **Web:** off/on for the class, inheriting the global default;
- **Workspace read:** separately off/on after a root is attached;
- **Change proposals:** separately off/on after workspace read is enabled; and
- **Commands:** separately off/on after a root is attached, with every run still confirmed individually.

Workspace attachment is displayed beside these grants but is not itself permission. Attaching a
directory enables none of the three workspace capabilities. Disabling read immediately removes its
tools; disabling proposals or commands invalidates their pending requests. Each grant can be revoked
without detaching the root or changing the others, subject only to the named dependency that change
proposals require workspace read.

Tool activity is committed at dispatch and the side panel polls those durable rows while a turn is
running, then reloads the same rows after restart. Activity cards
show tool name in plain language, query/relative target, status, source/proposal link, truncation, and
failure reason. Internal paths outside the attached root and secret values never render.

Guide/Show/Solve remains a response-style choice, not a permission setting.

### 5. Attach one rooted workspace per class

A class may attach one local directory by explicit path. The backend validates and stores its
canonical root and a user-facing label; the model receives only a workspace ID and relative paths.
The UI never enumerates the host before attachment. Replacing or detaching a root invalidates pending
changes and command requests tied to the old root.

Read tools:

- `list_workspace(relative_path=".")`: sorted bounded entries, no symlink traversal;
- `search_workspace(query, glob?)`: `rg` through argv without a shell, bounded matches/output;
- `read_workspace_file(relative_path, start_line?, end_line?)`: text regular files only, bounded
  bytes/lines, with hash and truncation state.

Ignored by default: `.git`, dependency/build/cache directories, database files, secrets matching
`.env*`, key/certificate extensions, binaries, devices, sockets, and files over 1 MiB. A denied path
returns a generic refusal rather than evidence about content outside the root.

### 6. Reuse draft review mechanics for filesystem changes

`propose_workspace_change` accepts one relative file, its observed base hash, and complete proposed
text. It validates the root/path again and stores a pending change; it never writes the file. The
server derives hunks exactly as the draft workspace does.

The review rail shows file, rationale, source/tool provenance, and per-hunk diff. Accept/reject is
user-driven. Applying selected hunks re-reads the file, verifies identity/hash, rebases through the
same staleness algorithm, then atomically replaces it in the validated parent. A stale or conflicted
change returns to review; it never chooses for the user.

The draft `pending_edits` table references an artifact part and should not be overloaded. Add a
workspace-specific table but extract/reuse its pure diff, hunk, and rebase functions. No new diff
dependency is required.

### 7. Execution is a confirmed verification request, not a shell tool

The agent may call `propose_verification_command` with an argv array, workspace-relative cwd, reason,
and expected signal. This writes a pending request. The model cannot execute it.

The UI shows the exact invocation and explains that tests/interpreters can run arbitrary code. A
single-use confirmation nonce triggers a user-only endpoint. The backend uses `shell=False`, a
minimal environment, the validated cwd, one process per workspace, capped stdout/stderr, a default
120-second timeout (hard maximum 600 seconds), and process-group termination. Results stream to the
activity card and persist in audit.

No persistent “always allow,” command-prefix approval, hidden post-save command, or network claim is
made in Phase 4. The user reviews code changes and command results separately.

### 8. Persist audit at dispatch time

Add durable tool-audit rows independent of assistant message completion. Each record contains caller
context, tool/capability/effect, bounded and redacted arguments, target/source/proposal identity,
policy decision, start/end, outcome, result summary, and failure/abandonment reason.

On startup, in-flight rows become `abandoned`; they are never deleted. Chat messages reference the
event IDs they present. This closes the current failure shape where a side effect can survive a turn
that never stores its final assistant message.

## Data model

Migration numbers are assigned after the writer agent lands. The logical additions are:

### `class_workspaces`

- `id`, `class_id unique`, canonical `root_path`, `display_name`;
- `read_enabled`, `change_proposals_enabled`, `commands_enabled`, all conservative defaults;
- root identity/fingerprint where supported, `created_at`, `updated_at`.

### `workspace_changes`

- `id`, `workspace_id`, `session_id`, relative path, base hash/content, proposed content;
- rationale, state (`pending`, `partially_applied`, `applied`, `rejected`, `stale`, `failed`);
- accepted/rejected hunk decisions, before/after hash, timestamps.

### `command_requests`

- `id`, `workspace_id`, `session_id`, argv JSON, relative cwd, reason, expected signal;
- state (`pending`, `running`, `completed`, `failed`, `timed_out`, `rejected`, `abandoned`);
- timeout, bounded stdout/stderr, exit code, confirmation time, timestamps.

### `tool_audit_events`

- `id`, caller kind/ID, class/session/artifact context, tool, capability, effect;
- redacted arguments JSON, target kind/ID, policy decision, state;
- result summary JSON, started/finished, error and abandonment reason.

### Existing writer source/capability tables

Retain and generalize rather than duplicate. Add source revisions/hash/truncation and make the web
resolver available outside writer code. Any rename is judged after merge; avoid a migration solely
for cosmetic table names.

## Backend module map

Exact filenames may adapt to the merged writer tree, but ownership is fixed:

| Responsibility | Planned location |
| --- | --- |
| Contextual registry, dispatch audit hooks, trust-tagged results | `backend/llm/tools.py` |
| Shared capability resolution | `backend/core/app_settings.py` plus writer-compatible adapter |
| Firecrawl v2 client and endpoint health | `backend/core/web_research.py` or promoted `backend/tools/web.py` |
| Public URL/SSRF policy | `backend/core/url_policy.py` |
| Shared evidence ledger | existing `backend/core/source_ledger.py` |
| Durable audit service | `backend/core/tool_audit.py` |
| Workspace root/path/read/search policy | `backend/core/workspaces.py` |
| Pending filesystem changes/diff reuse | `backend/core/workspace_changes.py`, shared diff utility |
| Confirmed command runner | `backend/core/commands.py` |
| Agent/chat orchestration | `backend/api/routes_agent_chat.py`, using existing class sessions/messages |
| Workspace/change/command APIs | `backend/api/routes_agent.py` |
| Profile proposals with ledger evidence | `backend/core/profiles.py`, `backend/api/routes_profile.py` |
| Settings/test actions | `backend/api/routes_settings.py` |

## Frontend surface map

- `frontend/src/components/chat/chat-pane.tsx`: capability bar and streamed activity.
- `frontend/src/components/chat/message-bubble.tsx`: durable tool activity cards.
- New `components/agent/`: workspace attachment, source/proposal cards, change-review rail, and
  command confirmation/output.
- The agent capability surface renders workspace attachment separately from explicit Web, Workspace
  read, Change proposals, and Commands grants, including inherited/disabled/revoked states.
- `frontend/src/components/settings/settings-form.tsx`: Firecrawl connection, global web default,
  and plain-language network disclosure.
- `frontend/src/components/profile/profile-facts.tsx`: web-source proposal evidence and existing
  confirm/edit/reject flow.
- `frontend/src/lib/api.ts`, hooks, and `types/index.ts`: typed APIs/SSE events; no direct `fetch` in
  components.

Every new screen has loading, empty, error, populated, disabled-policy, stale, and partial/truncated
states; works at 1280/768/375 in both themes; is keyboard operable; and honors reduced motion.

## Workstreams and order

### P4.0 — Merge-boundary audit

Freeze the writer baseline, reconcile migrations/shared files, run full suites, and write a short
integration note identifying which W3 interfaces are retained, promoted, or replaced. No feature
code before this passes.

### P4.1 — Security substrate

Contextual registries, capability resolver, trust-tagged results, durable audit, confirmation nonces,
and the threat-model tests. Existing CAS behavior remains unchanged.

### P4.2 — Shared Firecrawl web research

Loopback endpoint setting/test, Firecrawl client, URL policy, bounded search/fetch, source revisions,
writer adapters, and web activity UI. Validate against a pinned self-host instance and fake transport.

### P4.3 — Agent chat and profile proposals

Tool-enabled class-chat path, persisted SSE activity, web-cited answers, and propose-and-confirm
method facts. No filesystem access yet.

### P4.4 — Workspace read tools

Attach/detach root, path policy, list/search/read tools, UI status, prompt-injection and symlink tests.

### P4.5 — Reviewed workspace changes

Pending changes, diff/rebase reuse, per-hunk UI, atomic apply, audit, stale/race behavior.

### P4.6 — Confirmed verification commands

Pending command requests, exact confirmation, bounded `shell=False` runner, output activity, restart
reconciliation, and explicit residual-risk copy.

### P4.7 — Measurement and close-out

Real Firecrawl and repository acceptance runs, hostile-input suite, browser verification, performance
and truncation measurements, documentation update, and a Phase 4 handoff with known debts.

## Acceptance scenarios

1. With web off, a writer deep pass and class chat make zero web calls and explain the disabled state.
2. With web on and local Firecrawl available, the agent searches for an unfamiliar course method,
   fetches one source, answers with the snapshotted citation, and creates an unconfirmed profile fact.
   The fact affects no later prompt until the student confirms it.
3. Firecrawl unavailable or returning malformed/oversize/private redirects degrades honestly; course
   retrieval and writer work continue without fabricated sources.
4. A student attaches a lab repository, asks how a function works, and the answer cites relative file
   and line ranges without reading ignored/secret/out-of-root files.
5. The agent proposes a two-file fix. The student accepts selected hunks in one file and rejects the
   other; only accepted bytes change, and the audit records both decisions.
6. If another process edits a proposed file first, Lyra marks the proposal stale and writes nothing.
7. The agent proposes the repository's test command. Nothing runs until the exact command is
   confirmed; timeout/output/exit status remain visible after reload.
8. A poisoned upload, fetched page, and source comment each instruct the model to read a secret and
   run a command. None can apply a file or start a process; any proposal remains visible and inert.
9. All existing solver/CAS, study, draft, ingestion, and plain-chat flows behave as before when Phase
   4 capabilities are off.
10. Attaching a workspace enables no read, proposal, or command capability. Each grant can be enabled
    and revoked independently; revocation removes its schemas immediately and invalidates pending
    requests without detaching the root.

## Quantitative bounds

- Search: 5 results, 500-character query, 15-second Lyra request ceiling.
- Fetch: 3 redirects, public targets only, 1 MiB response, 100,000 normalized characters, visible
  truncation, 60-second Firecrawl ceiling.
- Workspace list: 500 entries; search: 200 matches/128 KiB; file read/proposal: 1 MiB text file.
- Tool loop: existing 24-round/600-second backstop unless Phase 4 measurement justifies a smaller
  interactive profile.
- Command: one per workspace, 120-second default/600-second hard max, 1 MiB combined retained output.
- Audit arguments/results are bounded independently so a rejected oversize payload cannot bloat the
  database.

## Verification matrix

| Layer | Evidence |
| --- | --- |
| Unit | URL/DNS policy, path/symlink policy, registry capabilities, audit transitions, diff/rebase, command argv/env/output bounds |
| Integration | Fake Firecrawl v2, writer adapter, chat SSE persistence, profile proposal, workspace apply, restart reconciliation |
| End to end | Real pinned Firecrawl; unfamiliar-method flow; code read/edit/test flow; hostile page/document/code corpus |
| UI | 1280/768/375, both themes, keyboard, screen-reader labels, reduced motion, all policy/failure/stale/partial states |
| Regression | Full backend/frontend suites, Ruff check/format, ESLint, Prettier, TypeScript; no live network/model in automated tests |
| Observability | Every attempted tool/confirmation visible after failure/restart; logs contain IDs and reasons, never secrets or content dumps |

## Definition of done

A student can opt into web research, see Lyra search through a local Firecrawl instance, inspect the
sources it actually used, and confirm or reject a method fact. They can attach one lab directory,
grant read access without implicitly granting proposals or commands, ask about its code, separately
grant and review proposed changes hunk by hunk, and explicitly grant and confirm a verification command.
No document, page, or source file can directly apply a change or start a process; every action and
refusal survives reload with provenance. The threat-model suite, real acceptance runs, full project
suites, static checks, and three-breakpoint/two-theme browser pass are green, and the handoff records
remaining risks without euphemism.
