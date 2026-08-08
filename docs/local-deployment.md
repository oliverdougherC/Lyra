# Local Deployment

Lyra is a local web application with an app-like lifecycle. The user-facing contract is one
command:

```bash
./run
```

The launcher owns setup, start, health, and browser launch for this checkout. It is idempotent: a
healthy service is reused, a missing dependency is prepared, a stopped owned service is restarted,
and an unknown process on a required port is reported rather than killed. Backend, frontend, and
bundled services are one supervised lifecycle: if either Lyra process exits, the remaining process
and Firecrawl are stopped promptly so the checkout cannot sit in a resource-consuming half-state.

## Host prerequisites

The launcher checks prerequisites before changing service state:

- Docker Desktop or Docker Engine is installed, the daemon answers, and Compose v2 is available.
- The host architecture and available disk can build and run the pinned Firecrawl source stack.
- Compatible Python, Node.js, and package-manager tooling is available for Lyra's host processes.
- Ports `3000`, `8000`, and `3002` are free or already belong to this checkout's healthy services.

Installing or starting a privileged container runtime is outside a repository script's safe
authority. If Docker is missing or its daemon cannot start, `./run` stops before provisioning and
prints the manual recovery step. Build, dependency, migration, port, and health failures likewise
exit nonzero and name the failing layer. Browser-opening failure is non-fatal: the launcher prints
the URL.

Run `./run doctor` for the same diagnostics without starting the application. Use
`./run --skip-firecrawl` only when local document work is more important than web research; the
degraded state is explicit and Firecrawl-backed tools remain unavailable.

## Topology and boundaries

```text
browser
  -> localhost:3000              Next.js
       -> 127.0.0.1:8000         FastAPI + SQLite
            -> 127.0.0.1:3002    Firecrawl API
                 -> Docker-only  workers, Playwright, Postgres, Redis, RabbitMQ
```

All published listeners bind to loopback. Firecrawl's internal service ports are reachable only on
its Compose network. Lyra has no authentication because loopback is the security boundary; changing
these bindings to `0.0.0.0` turns a local application into an unauthenticated network service and is
not supported.

Firecrawl is not actually one container. Lyra pins the
[official upstream source at `v2.11.162`](https://github.com/firecrawl/firecrawl/tree/v2.11.162) and
builds the cooperating API, worker, browser, database, cache, and queue services described by its
[self-host guide](https://docs.firecrawl.dev/contributing/self-host). The pin is deliberate:
`latest` can change schemas or health behavior between launches. Upgrade it only as a reviewed
maintenance change, then rerun every Firecrawl item in
[the Phase 4 acceptance record](phase-4-handoff.md#final-acceptance-record).

## Startup state machine

The default production-like launch proceeds in order:

1. Acquire the checkout's launcher lock and inspect any recorded owned processes.
2. Check host tools, Docker daemon and Compose, capacity, port ownership, and writable state.
3. Prepare Lyra's locked Python and JavaScript dependencies when their inputs changed.
4. Fetch/build the pinned Firecrawl source and start its services in dependency order.
5. Wait for container health and Firecrawl's own readiness endpoint with bounded retries.
6. Apply Lyra's forward-only SQLite migrations, build the frontend when needed, and start FastAPI
   and Next.js on their fixed loopback ports.
7. Require `/api/health/live`, `/api/health/ready`, and the frontend HTTP probe. The normal path also
   requires Firecrawl; the explicit skip path records a degraded state.
8. Record the desired whole-stack state and start the detached lifecycle supervisor.
9. Open the browser only after readiness. On failure, retain diagnostics and transactionally stop
   the partial stack without touching unrelated processes or user data.

This sequencing prevents a half-started stack from looking like a blank browser page. It also leaves
the database migration authority with the backend startup path instead of teaching Compose about
Lyra's schema. Once startup completes, the supervisor checks the saved process-birth identities,
not just reusable PIDs. A normal `./run stop`, a backend/frontend crash, or graceful termination of
the supervisor converges the same owned stack to stopped. Docker volumes and built images remain
intact, while the containers release CPU and memory.

## Health semantics

`GET /api/health/live` answers `200 {"status":"ok"}` if FastAPI can dispatch a request. It does not
touch SQLite or the network and is suitable for process liveness.

`GET /api/health/ready` checks that SQLite is accessible, its `user_version` matches the newest
checked-in migration, and the singleton settings row exists. A database failure returns `503` with
a generic message; absolute paths and driver details are not returned. Firecrawl is probed through
the configured loopback URL and appears as a non-required component. Its outage therefore leaves
the response at `200` when the database is ready while clearly reporting that web research is
unavailable.

The launcher deliberately has a stronger success condition than this backend endpoint. Normal
whole-stack startup requires Firecrawl's containers and readiness probe. This distinction lets Lyra
keep serving the parts that need no web access after a later Firecrawl crash.

## Commands and recovery

```bash
./run                     # production-like start and browser launch
./run --dev               # developer servers with the same prerequisite gates
./run --no-browser        # start and print the URL
./run --skip-firecrawl    # degraded start without web research
./run status              # owned process/container and health summary
./run doctor              # prerequisite, daemon, capacity, and port diagnostics
./run logs                # aggregated recent logs
./run stop                # stop this checkout's owned services only
./run backup --archive /absolute/path/lyra-backup.tgz
./run restore --archive /absolute/path/lyra-backup.tgz --data-dir /absolute/path/restored-data
```

Recovery starts with `./run doctor`, then `./run logs`. Common manual interventions are starting
Docker Desktop, freeing disk used outside Lyra, or deciding what to do with an unrelated process on
a fixed port. Do not solve a port collision by terminating an unknown PID; Lyra reports its identity
and exits. `stop` is ownership-aware and leaves both user data and built images recoverable.

`--clean` is a compatibility/rebuild path, not a data reset. It may rebuild generated application
artifacts, but it must not delete `data/`, model weights, source evidence, or Docker volumes that hold
Firecrawl state. A destructive data reset is intentionally not part of the one-command launcher.

## Backup and restore

Lyra's supported local backup surface is the launcher itself:

```bash
./run backup --archive /absolute/path/lyra-backup.tgz
./run restore --archive /absolute/path/lyra-backup.tgz --data-dir /absolute/path/restored-data
```

The backup command stops this checkout's owned services first, checkpoints SQLite's WAL, and refuses
to proceed if another process still holds the database busy. The archive contains the configured
`LYRA_DATA_DIR` tree plus the active SQLite database, even when `LYRA_DB_PATH` points outside that
tree.

Restore is intentionally narrow and non-destructive:

- `--archive` and `--data-dir` are required.
- `--data-dir` must not already exist.
- If the backup was created with `LYRA_DB_PATH` outside `LYRA_DATA_DIR`, restore also requires an
  explicit `--db-path`, and that parent directory must already exist.
- Every archive member is validated before restore writes anything to the requested targets.
- Restore extracts into a temporary sibling, runs SQLite `pragma quick_check`, and only then renames
  the restored data into place.

Recovery limitations are explicit:

- The OS-keychain API key is not inside the backup because Lyra never stores it under `data/`.
- If Lyra fell back to `data/.api_key`, that file is inside the archive because it lives under the
  data directory.
- `.lyra/` runtime ownership metadata and `logs/` are not part of the backup.
- When the database is restored to an external `--db-path`, Lyra still stages both restores first.
  If the final external database-file rename fails, Lyra rolls back the requested `--data-dir` and
  leaves the requested targets absent.

## Future inference service

Phase 6 adds inference to the same supervised topology rather than inventing a second launcher. The
portable profile uses llama.cpp and an OpenAI-compatible loopback endpoint. A vLLM profile may be
offered for supported Linux/NVIDIA hosts, but cannot replace the cross-platform default. In both
cases weights are explicit host-owned data, health is a separate component, and the browser talks
only to FastAPI. A later signed native wrapper can replace `./run` as the entry point while retaining
these service and health boundaries.

Bundled integrations register one standard-library `BundledService` entry in the launcher and
provide a helper with the `start`, `stop`, `status`, `doctor`, and `logs --follow` contract.
Multi-service internals remain private to that helper. Startup is ordered and transactional;
shutdown runs in reverse registration order. This is the extension point for llama.cpp and vLLM,
so adding inference does not duplicate ownership, rollback, status, or crash-cleanup logic.
