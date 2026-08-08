# Phase 4 Writer Integration Boundary

**Status:** P4.0 complete, 2026-08-07. The writer owner finished W1-W6 before shared integration;
Phase 4 retained its public contracts, replaced its direct web transport with the shared Firecrawl
adapter, and extended its source ledger additively.

This document is the compatibility contract between the concurrently implemented
[writer roadmap](writer-roadmap.md) and the approved [Phase 4 agent specification](phase-4-agent.md).
It deliberately names interfaces rather than migration numbers or working-tree line numbers.

## Baseline snapshot

- Git base: `b3e11ae` on `main`.
- Writer migrations end at `027_pending_edit_comments.sql`; Phase 4 begins at `028` and adds the
  web-profile evidence link in `029`.
- The writer roadmap reports W1-W6 implemented, with its own landing baseline of 1,240 backend and
  475 frontend tests.
- The integrated baseline is recorded in [phase-4-handoff.md](phase-4-handoff.md). The two moving-
  tree prompt failures and two Ruff failures observed during the initial audit were repaired by the
  writer owner before integration and do not remain.

## Retain, promote, replace, or delete

| Existing writer surface | Decision | Phase 4 constraint |
| --- | --- | --- |
| `app_settings.allow_web_research` | Retain | Remains the global default; do not add a second global web switch. |
| `writer_budgets.WriterCapabilities` and class overrides | Promote | Extend the same resolver with independent workspace-read, change-proposal, and command grants; preserve current web and parallel fields. |
| `web_research.search_web` / `fetch_source` public signatures and safe exceptions | Retain adapter | Existing callers keep their gated contract while transport moves behind the shared Firecrawl client. |
| Direct DuckDuckGo HTML search and direct public-page HTTP fetch | Replace | Remove only after writer and class chat pass the same Firecrawl contract tests. No reachable fallback remains. |
| `source_ledger` source/excerpt CRUD and prompt formatting | Promote | It becomes the shared class research ledger. Add revision/hash/truncation fields compatibly; preserve existing citations and row projections. |
| `writer_tools` profiles and `RunEffects` | Retain | Writer profiles may combine web reads with draft/comment database proposals, but never filesystem apply or process execution. |
| Module-global tool registry access | Replace compatibly | The shared loop receives an explicit contextual registry; the existing CAS registry and writer profile schemas retain behavior. |
| Writer chat, pass, review, status, and SSE routes | Retain | Phase 4 does not rewrite W1/W2/W4-W6 or change writer queue/job-state semantics. |
| Writer-only audit carried solely in a completed assistant message | Replace | Every Phase 4 network/filesystem/process attempt is durably recorded at dispatch, including failed or abandoned turns. |

## Shared contract guardlist

Before merging a Phase 4 workstream, tests must prove:

1. `allow_web_research = false` produces zero writer and class-chat network calls.
2. `web_research.search_web(..., allowed=...)` and `fetch_source(..., allowed=...)` preserve their
   result and exception contract while using the shared Firecrawl transport.
3. Existing `writer_sources` rows and excerpts load unchanged after additive source-ledger migration.
4. `get_writer_capabilities`, `resolve_writer_capabilities`, and class override updates retain their
   existing web/parallel behavior.
5. Every writer tool profile contains the same non-web tools it contained at handoff; web tools are
   absent or refused when the resolved capability is false.
6. `/pass`, `/review`, `/status`, writer chat SSE, and ordinary class chat retain their existing
   state and failure behavior.
7. No model registry contains a file-apply or process-execute definition.

## Final P4.0 release gate — complete

The completed pass:

1. captured the writer handoff and highest migration;
2. ran the merged writer/agent focused suites before shared edits;
3. allocated Phase 4 migrations after `027`;
4. preserved writer profiles, budgets, job states, and SSE routes; and
5. integrated one Firecrawl policy/client and one revisioned ledger across writer and class agent.
