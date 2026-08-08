# Phase 4 Agent Handoff

**Status:** complete on 2026-08-07. The implementation, local deployment drill, live Firecrawl
checks, evidence flow, and responsive visual acceptance all passed.

## What landed

- Explicit, mutually exclusive research, code, and command registries. Pure compute may accompany
  each profile; no model registry contains file-apply or process-execute handlers.
- One default-off web grant shared with the writer, a loopback-only Firecrawl v2 client, guarded
  public queries and URLs, bounded search/scrape results, and immutable source revisions.
- Web-evidenced profile proposals linked to an exact stored excerpt. They are low-confidence and
  absent from prompts until the existing profile screen confirms them.
- One canonical workspace per class with independent read, change-proposal, and command grants.
  Reads reject ignored, secret, binary, oversized, symlinked, and out-of-root targets.
- Full-text file proposals with server-derived hunks, identity/hash checks, per-hunk decisions,
  atomic replacement, and durable partial-review state.
- Exact argv verification proposals, single-use origin-bound confirmation, `shell=False`, minimal
  environment, validated cwd, one running process per workspace, timeout/process-group termination,
  and bounded retained output.
- Dispatch-time audit with redacted arguments, terminal outcomes, targets, and startup reconciliation.
  The class side panel polls the same durable rows while a turn runs and after reload.
- Agent controls inside the existing class-chat workspace: profile composer, capability grants,
  source ledger, activity, hunk review, command confirmation, and persisted results.
- An app-like `./run` lifecycle that provisions the pinned Firecrawl `v2.11.162` source stack,
  prepares and starts Lyra, checks each readiness layer, reports recovery actions, and opens the
  browser only after the application is usable. Firecrawl remains explicitly skippable for a
  degraded local-document launch.
- `/api/health/live` for process liveness and `/api/health/ready` for required database readiness.
  The latter reports Firecrawl separately and does not fail core Lyra readiness when optional web
  research is unavailable; the normal launcher enforces the stricter full-stack policy.

## Writer integration

Writer W1-W6 finished before the shared integration. Phase 4 retained its budget/profile/job APIs,
replaced direct web transport behind the existing `search_web` and `fetch_source` contracts, and
extended the ledger rather than creating another research store. An idempotent course-source upsert
was also corrected so serial and parallel review prompts remain byte-stable across clock seconds.

## Verification evidence

The automated suites keep their model and Firecrawl transports deterministic. They are supplemented
by the dated live deployment and browser evidence in the final acceptance record below.

- Backend: `1298 passed` in 55.90 seconds on the final run.
- Backend static checks: all 171 Python files pass `ruff format --check` and `ruff check`.
- Frontend: 52 files and 487 tests pass.
- Frontend static/build checks: ESLint, Prettier, `tsc --noEmit`, and the optimized Next.js
  production build pass.

Security coverage includes query privacy, DNS/URL policy, redirects, path/symlink races, stale file
identity, confirmation replay/origin/scope, argv/cwd validation, output truncation, registry
separation, grant revocation, and restart reconciliation.

## Final acceptance record

Do not convert this section to a prose claim. Check each item against the real launcher and preserve
the command output or screenshot location beside it.

- [x] On a host with Docker running but no Lyra services, `./run doctor` names every prerequisite
      accurately and `./run` builds the pinned `v2.11.162` source, migrates Lyra, starts the complete
      stack, reaches all health gates, and opens `http://localhost:3000`.
      Evidence, 2026-08-07: exact tag object `ebfe595c3e218266c627f8651ccbcbe38f4ecfdd`
      and peeled source commit `7666c1f9ae8720a6bba271e0f60b6a217f8a5210` were verified before
      the cold source build; `./run --no-browser` then reached the full-stack ready state.
- [x] A second `./run` is idempotent: it adopts/reuses healthy owned services, performs no needless
      Firecrawl rebuild, preserves user data, and still reaches readiness.
      Evidence, 2026-08-07: the second launch used the image-ID build receipt, reported current
      backend/frontend dependencies, reused the healthy owned app processes, and reran the live
      scrape and redirect-safety gates without rebuilding Firecrawl.
- [x] `./run status` identifies every required service; `./run logs` exposes a failed dependency;
      `./run stop` stops only services owned by this checkout; and a subsequent start recovers.
      Evidence, 2026-08-07: status listed API, Playwright, Redis, RabbitMQ, and NuQ Postgres;
      stop released ports `3000`, `8000`, and `3002`, preserved volumes, and the following start
      recovered without a rebuild. The launcher refuses healthy listeners it cannot prove it owns.
- [x] `GET /api/health/live` answers independently of dependencies. `GET /api/health/ready` returns
      ready with current SQLite migrations, reports Firecrawl independently, and returns `503` for
      a deliberately stale or inaccessible database without exposing its filesystem path.
      Evidence, 2026-08-07: the running stack returned database, Firecrawl, and web-scrape `ready`;
      the stale/inaccessible database cases pass in `backend/tests/test_api_health.py`, including
      response redaction.
- [x] Firecrawl readiness and a bounded public search pass through `127.0.0.1:3002`.
      Evidence, 2026-08-07: the launch gate passed and a direct `/v2/search` smoke returned one
      bounded public result.
- [x] A public `https://example.com` scrape succeeds with TLS verification on and cache storage off;
      its exact source snapshot is retained in Lyra's immutable source ledger.
      Evidence, 2026-08-07: the live scrape gate passed; the production research registry stored
      the resulting `https://example.com/` snapshot as source `36` with its fetch/proposal audit.
- [x] A public URL that redirects to a loopback/private target is refused. No private response body
      reaches the model, transcript, source ledger, audit arguments, or logs. Only after this passes
      is `firecrawl_scrape_enabled` set true for the acceptance class.
      Evidence, 2026-08-07: the unique-token httpbin-to-loopback drill was accepted only when the
      matching Firecrawl request log contained `Connection violated security rules.`; a generic
      upstream error was deliberately insufficient. Scraping was enabled only after that pass.
- [x] An unfamiliar-method research flow proposes a profile fact from the exact stored excerpt; the
      fact is absent from prompts before confirmation and present after confirmation, with the tool
      activity record intact.
      Evidence, 2026-08-07: the live tutor searched, fetched, and stored Nyquist source `35`; its
      paraphrased excerpt was refused and the turn ended explicitly at its timeout. The exact stored
      excerpt then passed the production registry as excerpt `1`, proposed low-confidence fact
      `464`, remained outside `select_active_facts` before confirmation, and entered it only after
      the ordinary profile confirmation route. Every allowed and refused call remains in activity.
- [x] The class workspace is recorded at 1280, 768, and 375 pixels in light and dark themes,
      including keyboard traversal, reduced motion, stale/partial changes, command output, and web
      failure states.
      Evidence, 2026-08-07: screenshots are in `output/playwright/phase4-agent-{1280,768,375}-`
      `{light,dark}.png`; all six were visually inspected and the browser console stayed at zero
      errors and warnings. Keyboard focus reached the profile control and reduced-motion media was
      exercised. The persisted stale/partial, command-output, and failed-activity variants pass in
      `agent-workspace-change-review.test.tsx`, `agent-command-confirmation.test.tsx`, and
      `activity-trail.test.tsx`.

These are release evidence, not hidden implementation work. Every box was checked and dated on
2026-08-07, so Phase 4 is complete. See [local-deployment.md](local-deployment.md) for topology and
recovery.
