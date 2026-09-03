# PLA-401 Final Report — Review 510714238 School-Readiness Correctness Pass

PR #65, branch `codex/pla-401-agent-c-delivery` (see §5 for the exact heads).
GitHub main at push time: `b6c75027966de2d20137ac3c4ebc3a84c0ae1cac`; the branch is 12
commits ahead and **0 behind**. Review `510714238` ("FINAL SCHOOL-READINESS CORRECTNESS
PASS") is fully addressed. This was a surgical correctness / compatibility / performance /
evaluation pass: no architecture, UX, or approved surface was redesigned; the one
conversation, one composer, contextual AgentWorkSurface, Workspace chip, JIT access
cards, bounded "Not now", native folder picker, explicit hunk/command confirmation,
collapsed Details/audit, Guide/Show, private-context web guard, operation-ID architecture,
"How Lyra checked this", 0-of-N grounding, Generated-vs-Your-edit, settings hierarchy,
EndpointLocalityBadge, and Tauri least privilege are all untouched and verified intact.
The branch is pushed to the existing PR, not merged.

## 1. The nine review blockers

### 1. Basic chat never required tool-call support
`_plan_agent_turn` now reads the endpoint's measured `tools_supported` verdict from the
turn's own settings snapshot (`TutorConfig.tools_supported`, threaded through
`_tutor_config_from_row` so the verdict rides the SAME single settings read as the
endpoint — a settings flip between two reads cannot pair one read's endpoint with another
read's verdict):

- **known `False`** → the turn plans tool-less at once: full tutor contract, no schemas,
  the one-sentence tool-less note instead of the capability layer, no snapshot/registry
  cost. Basic tutoring is exactly what such an endpoint carries.
- **unknown (`None`)** → the tool surface is planned and sent; when the loop's first
  request is refused (the client's non-context 400 → `NO_TOOL_SUPPORT` with no calls), the
  route settles attempt 1 as `stopped` (`no_tool_support`), **remembers the verdict** in
  the settings row (only while the stored verdict is still unknown — a measured True is
  never overridden by one turn), and re-plans the SAME turn tool-less with the retrieval
  it already computed, then runs it as attempt 2. The order is the safe one: settle, then
  re-plan (read-only), then create attempt 2 — a cancel mid-re-plan orphans nothing.
  The student's turn completes with one logical reply and the op-ID lineage intact
  (attempt 1 stopped / attempt 2 completed under the same operation).
- The window budget gate (`_TurnTooLargeError`) falls back to the tool-less surface for
  ALL support states — an oversized-tool-surface window answers tool-less instead of
  refusing, and refuses only when even the tool-less turn cannot fit.
- Agent-specific asks on a tool-less endpoint are explained, not attempted (the tool-less
  note tells the model to say so plainly).

Tests: `test_a_known_tool_incompatible_endpoint_answers_basic_chat_tool_less`,
`test_an_unknown_endpoints_first_tools_refusal_falls_back_and_remembers_the_verdict`
(two attempts, verdict written, second turn takes the tool-less path at once),
`test_a_toolless_turn_carries_retrieval_and_facts_like_the_tool_turn` (chunks + facts +
mode + note in the system prompt; retrieval ran), 512-window tool-less refusal cases.

### 2. Regeneration preserves an explicit `document_id: null`
Two-sided fix:

- **Server:** `_resolve_retry_scope` (regenerate) reads property **presence** from the
  request body (`model_fields_set`): an explicit null wins (it names "All material"), an
  absent property means "continue the persisted scope". Retry has the matching
  `_resolve_retry_scope` retry branch: a modern flagged attempt row's persisted scope
  owns the retry outright (stored null = All material, body ignored); legacy rows
  backstop from the request with stored-None-means-absent.
- **Client:** `api.ts` `regenerateAgentChat`/`retryAgentChat` serialize by presence —
  `'documentId' in scope ? { document_id: scope.documentId } : null` — so the All-material
  selection reaches the wire as an explicit `document_id: null` instead of silently
  dropping the property.

Tests: `test_a_regenerate_with_an_explicit_null_document_retrieves_class_wide`,
`test_a_retry_of_an_all_material_turn_stays_class_wide_even_when_a_document_is_named`,
`test_a_legacy_attempt_scope_backstops_from_the_request`, plus the frontend unit pin
(`frontend/tests/api.test.ts`) and the browser pin below.

### 3. Three-state lost-response reconciliation
The agent catch in `chat-pane.tsx` now runs the reconciliation for **any** non-409 error
(including non-`ApiError` transport drops — the common case) and decides from the
durable state, mirroring what the transcript already fell back to:

- **A — durable question + stored reply:** retire BOTH refs (`submittedTextRef` and
  `operationIdRef`, plus `responseAcceptedRef = true`): the composer goes empty and the
  next Send mints a fresh operation ID. No re-send, no stale key.
- **B — durable question, failed/stopped attempt, no reply:** the transcript shows the
  honest turn with its causal Retry; the composer goes **empty, no prefill**, while the
  operation ID is KEPT so an identical re-send re-runs the stored turn instead of
  appending a duplicate.
- **C — nothing durable:** the draft goes back in the box and the operation ID is kept
  for the idempotent re-send (the server reconciles by op-ID if the turn actually did
  commit).

409 handling is unchanged (`operation_id_mismatch` clears both refs; busy 409 keeps both).
Tests: `chat-pane.test.tsx` "lost-response reconciliation" (all three states, including
the fresh-vs-kept operation ID on the next send) and `ambiguity-recovery.spec.ts`
"dropped acceptance self-heals the UI" (drop the accepted response at the browser
boundary → transcript shows one Q + one A, composer empty → a different question sends
with a fresh operation ID → 2Q+2A durable, exactly two logical model calls, no duplicate
question).

### 4. Retry/regenerate attempt lifecycle
The plan now runs **before** anything is persisted, on every path:

- **Fresh send:** plan off-loop → then one transaction (mode via the non-committing
  `update_session_mode` + user message + `create_attempt(commit=False)` + uncommitted
  title + commit) → bind/touch. A preflight failure (retrieval, registry, fit, mode
  shown) persists nothing and moves no mode; the claim frees in `finally`.
- **Retry/regenerate:** resolve the target → replay-if-completed → resolve the scope →
  plan FIRST (before any mode mutation or attempt creation) → then persist.
- **Settlement on every post-attempt setup failure:** `_commit_reply`'s transaction
  settles conditionally — `stopped` when the turn is cancelled or the generator exits,
  `failed`/`persistence_failed` otherwise — so no attempt row is ever left `RUNNING`.
  The NO_TOOL_SUPPORT fallback (item 1) settles attempt 1 before re-planning, so a cancel
  during the fallback orphans nothing either.
- **Session mode mutates only after preflight succeeds** (the non-committing twin
  `set_session_title_if_unset_uncommitted` and `update_session_mode` are committed only
  inside the post-plan transaction).

Tests (item-4 suite in `test_api_agent_chat.py`): injected retrieval/registry/fit
failures on fresh send, retry, and regenerate — no attempt row, no host effect, no mode
move, existing reply intact, claim released.

### 5. "All material" NULL is an authoritative persisted scope
`agent_turn_attempts.scope_persisted` (migration 043, `check (in (0,1))`): modern
`create_attempt` always writes 1, so a stored `document_id IS NULL` on a flagged row
means "All material, deliberately" — not "scope unknown". Retry reads the flagged row's
stored scope as authoritative (item 2); only rows with the flag 0 (pre-043) treat stored
NULL as absence and backstop from the request. Regenerate keeps the body-present-wins
contract.

### 6. Blocking RAG planning moved off the event loop
Planning (`retrieve` = embedding + search + rerank) now runs in a worker thread via
`asyncio.to_thread` (`_plan_turn_offloop`), with the turn's `ToolStopGate` registered
**worker-side** (`begin_work`/`finish_work`) so a cancellation cannot clear the
registration before the thread leaves. Serialization is unchanged — the per-session claim
still serializes turns, so the planning connection is never used by two threads at once
— and cancellation of a planning turn is truthful: the loop's shutdown path shields the
quiescence wait.

Test: `test_planning_off_the_event_loop_does_not_freeze_the_app` — a real barrier inside
the planner (`entered`/`release` events, bounded wait, no sleeps for correctness): while
one turn sits in planning, an unrelated request on another session completes, a second
turn on the held session gets the ordinary 409 fast, and the held turn completes
normally when the barrier opens.

### 7. Eval harness `class_chat` surface
`scripts/eval_tutor.py` gained the `class_chat` surface as its **default**
(`--surface class_chat|tutor`):

- It assembles what the class chat ACTUALLY sends through the production planner: a new
  public seam `routes_agent_chat.assemble_class_chat_turn` returns `ClassChatAssembly`
  (the exact first-request `messages` + `tool_schemas(registry)` + tool-less flag) and
  takes an in-memory `history`/`cached_retrieval` so a corpus case plans through the same
  planner the route uses. The harness then makes the round-zero call —
  `complete_with_tools(messages, tools)` with `DETERMINISTIC_TEMPERATURE` — and records
  the reply, or truthfully records `tool_call` when the model spent the turn calling
  tools (graded as a fail that says so), or `error`/empty.
- The corpus (13 cases including the convolution ground truth), the seven-dimension
  grading, redaction (locality class only, never the URL), and the judge are unchanged;
  `grade` consumes the new `tool_call` status; `report` reads it.
- **Deterministic parity tests** (CI, no model): the harness surface and the route's
  `_plan_agent_turn` agree message-for-message and tool-for-tool on the same case; the
  assembly carries the mode contract, agent layer, context block, history, question, and
  the class's real tool schemas (ungranted capabilities absent); a known tool-incompatible
  endpoint plans tool-less with the note and no schemas.
- **Run live** (a model was available): `uv run --extra dev python
  scripts/eval_tutor.py run --surface class_chat --workspace data/eval-class-chat`
  against the class's configured endpoint (`Qwen3.8-27B`, locality `remote`), then
  `grade`/`report`: **pass rate 3/12** — 4 cases answered directly (2 passed), 7 cases
  spent the turn on CAS tools (`cas_evaluate`/`cas_integrate`/`cas_differentiate`), 1
  empty reply. The tool surface measurably shifts a small model toward acting; the
  harness now records that truthfully instead of grading it as an empty reply. Exact
  commands above; the graded workspace is `data/eval-class-chat`.

### 8. Truthful Stop during tool dispatch
The dispatch-side half of the PLA-401 stop contract, completed:

- Workers run in `asyncio.to_thread` and cannot be cancelled, so the guarantee is
  contractual: **after the Stop endpoint completes, no in-flight tool can create a new
  durable consequence.** Every durable tool re-checks the turn's `ToolStopGate` before
  its write (search before AND after `web_research.search_web`, fetch, propose_snapshot
  before upsert, propose_excerpt, propose_fact, change before create, command before
  create); a latched stop turns the in-flight read into a discarded result (audit row
  finished `refused`, no durable target); the cancelled loop makes no further model
  call; the attempt settles `stopped`; the session claim frees only after quiescence.
- Test: `test_stop_during_real_tool_dispatch_leaves_no_later_effect` — a REAL dispatch
  blocked inside a real `search_web` (barrier, no sleep for correctness: the release
  follows the LATCHED gate observed on the turn's own gate object), a real Stop in its
  own thread, then release. Asserted: Stop `{"stopped": true}`; the turn settles with
  the bounded stopped body; exactly one network search (none after the Stop completed);
  zero durable targets (`writer_sources`, `workspace_changes`, `command_requests` all 0);
  attempt `stopped`; claim released.

### 9. Non-streaming chat: documented, not expanded
Verified and recorded, no code change. The turn shows the local working indicator
("Reading your question" → "Thinking") minted client-side at send, and the reply lands
with its activity trail in one commit; Stop is the explicit endpoint. The trade (given:
first-token latency on slow/remote endpoints; bought: one durable reply per question,
tools before text, one bounded request per round) and the measured evidence (24.95 s
wall for a representative turn, 1,939-char reply, 4,796-token system prompt, remote
endpoint; 13-case corpus run fully settling) are recorded in
`docs/phase-4-agent.md` §4a. A Linear follow-up on streaming the agent turn is warranted
only if real students report the whole reply waiting on slow endpoints.

## 2. Invariants audit (item 10) — verified intact
Grep-verified across `frontend/src` and `backend/api`:

- No Agent button, Agent tab, or Agent panel: no `AgentPanel`/agent-tab identifiers; the
  class page's routes are `chat`, `drafts`, `solutions`, `study` only.
- One conversation, one composer: the only chat surface is the class conversation
  (`agent` prop on the ordinary `ChatPane`); the class-landing "Ask Lyra" box is the
  approved handoff into that conversation.
- No profile chooser on the class chat (the agent turn omits the profile; legacy
  profiles exist only for API clients), no grant dashboard (the capability bar is the
  approved section-4 surface), no second Ask Lyra surface.
- The approved UX list from the review (Workspace chip, JIT access cards, bounded
  "Not now", native folder picker, explicit hunk/command confirmation, collapsed
  Details/audit, "How Lyra checked this", 0-of-N grounding, Generated-vs-Your-edit,
  settings hierarchy, EndpointLocalityBadge, Tauri least privilege) is untouched by this
  pass: the diff is confined to the route's planning/dispatch/reconciliation, the tool
  gate, the attempt lifecycle, scope persistence, the composer error handling, and the
  eval harness.

## 3. Validation (item 11) — full local matrix, all green

**Backend** (`uv run --extra dev`): `ruff format --check` + `ruff check` on
`backend` + `scripts` — clean. `pytest backend/tests` — **2694 passed, 1 skipped**
(including the 9 new/updated agent-chat tests, the 2 new concurrency tests, and the 3
new eval-surface tests).

**Frontend** (`pnpm` in `frontend/`): `format:check` clean; `lint` (ESLint) clean;
`typecheck` (`tsc --noEmit`) clean; `test` (Vitest) — **772 passed (81 files)**,
including the 3 new reconciliation tests and the 2 new scope-serialization pins.

**Acceptance** (`./scripts/run-acceptance.sh`, real stack): **111 passed** — the full
suite including the extended `ambiguity-recovery.spec.ts` (new self-heal test) and
`regeneration.spec.ts` (new All-material regeneration wire pin: doc N → All material →
Regenerate → captured body `{"mode": "guide", "document_id": null}`).

**Desktop** (`src-tauri`): `cargo fmt --check` clean; `cargo clippy --release
--all-targets` clean; `cargo test --release` — **26 passed**; `cargo audit` — no
vulnerabilities (17 allowed unmaintained-GTK warnings, identical to CI's invocation);
`tauri build` — **Lyra.app + Lyra_0.1.0_aarch64.dmg** built, and
`./scripts/build_dmg.sh src-tauri/target/release/bundle/macos/Lyra.app dist/Lyra.dmg`
produced the 80 MB DMG.

**Dependency gates:** `pnpm audit --prod` — no known vulnerabilities;
`uv run --extra security python scripts/python_security_gate.py` — **PASS** (no known
advisories affect the locked production graph).

**Live evaluation:** `Qwen3.8-27B` (remote) available, run live — see item 7.

## 4. Tradeoffs and honest notes

- **First-token latency** on the non-streaming agent turn is a deliberate trade
  (documented in `docs/phase-4-agent.md` §4a with measured evidence); the working
  indicator + durable-reply contract are what make it tolerable.
- **The class_chat eval result (3/12) is a finding, not a failure of the pass**: with
  the full tool surface offered, the small remote model spent 7 of 12 turns calling CAS
  tools instead of answering. The harness now measures and records that behavior
  truthfully; whether the tool surface should be offered by default on small models is a
  product decision for a follow-up (the tutor `tutor` surface remains available for
  before/after comparison).
- **`cargo audit` warnings**: the 17 allowed advisories are unmaintained GTK-rs
  transitive dependencies through Tauri's own runtime (no security advisories); the
  allowlist behavior matches CI exactly.
- **Tauri DMG packaging** required cleaning a stale `/Volumes` mount left by an earlier
  failed attempt in this environment; the build and both DMG artifacts are produced as
  above.

## 5. State

- Branch: `codex/pla-401-agent-c-delivery` on PR #65, pushed, **not merged**.
- Code head (this pass's commit, parent `9a8b9630b3acfe3aad06f6e31898685fdd79b3dd`):
  `50961c9`.
- This report is committed on top of the code head; the pushed branch head is that
  report commit.
- main: `b6c75027966de2d20137ac3c4ebc3a84c0ae1cac` — 0 behind at push.
- CI: see the push run for this head (reported on push).

The previous pass's report (review 5105553464 parity pass) is superseded by this one.
