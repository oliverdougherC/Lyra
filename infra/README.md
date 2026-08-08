# Lyra local infrastructure

Lyra provisions Firecrawl from the official `v2.11.162` source tag. The
provisioner verifies both the signed tag object identity and its peeled source
commit before Docker builds anything. Runtime state lives under `.lyra/`; the
generated environment file is mode `0600` and is never printed.

```bash
python -m infra.firecrawl start
python -m infra.firecrawl status
python -m infra.firecrawl doctor
python -m infra.firecrawl logs
python -m infra.firecrawl stop
```

`start` is idempotent. It checks Git, Docker Compose 2.24.4 or newer, the Docker
daemon, allocated CPU and memory, free disk, and port 3002. Allocations below
8 GiB receive a reliability warning, while only clearly unworkable allocations
below 4 GiB fail. On macOS it makes one
attempt to open Docker Desktop and waits for the daemon. It then builds the exact
pinned source (or reuses image IDs recorded for that exact source and override),
starts the stack, waits for readiness, performs a real scrape of
`https://example.com`, and verifies that a public-to-loopback redirect is clearly
refused. A readiness response alone is not considered success.

Only the Firecrawl API is published, strictly on `127.0.0.1:3002`. Postgres,
Redis, RabbitMQ, and Playwright remain on the private Compose network. Named
volumes preserve database and queue state. The Compose project and volumes are
derived from the checkout path, so another Lyra checkout cannot stop or reuse
this one's state. No command in this module deletes volumes; recovery that
would discard data is always a manual decision.
The pinned release's required `postgres` database/user names are retained and
`ALLOW_LOCAL_WEBHOOKS=false` is explicit, preserving its initial-URL and redirect
private-address checks.

The redirect drill defaults to an HTTPS httpbin endpoint and can be replaced
with `LYRA_FIRECRAWL_REDIRECT_TEST_URL`. A replacement must still be a public
HTTPS URL that redirects to loopback. The default path uses a unique probe token
and requires Firecrawl's own `Connection violated security rules` diagnostic;
generic upstream errors do not pass. If the public service is unavailable or
the response is ambiguous, the security gate fails closed and reports manual intervention.

## Future inference services

Add llama.cpp, vLLM, or another inference engine as a separate Compose file and
profile rather than editing the pinned Firecrawl checkout. Keep model storage in
a dedicated named volume, publish inference ports on loopback only, and extend
the Python provisioner with explicit resource and functional health gates. This
keeps the app launcher stable while each infrastructure component remains
independently diagnosable and replaceable.
