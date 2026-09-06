# Contributing to Lyra

Lyra is preparing for its first public beta. Contributions that protect student work, make failures
recoverable, improve accessibility, or make setup and documentation clearer are especially useful.
Read the [architecture](docs/architecture.md) and [code conventions](docs/conventions.md) first.

## Run from source

For the supported desktop development path, use an Apple Silicon Mac with:

- Python 3.12, plus `uv` for locked development and packaging environments.
- Node.js 22.13 or newer in the Node 22 line used by CI.
- pnpm at the version in [`frontend/package.json`](frontend/package.json)'s `packageManager` field.
- Git. Desktop builds also need the Rust stable toolchain and Xcode Command Line Tools.
- Disk space for dependencies, build output, course data, and downloaded model weights.

From your checkout:

```bash
uv python install 3.12
uv sync --python 3.12 --extra dev
(cd frontend && pnpm install --frozen-lockfile)
./run --dev
```

Open the frontend address printed by the launcher (normally `http://localhost:3000`). The backend
uses `127.0.0.1:8000`. `./run --dev` starts hot reload; `./run` builds and serves the frontend for
production-like browser testing. The launcher manages its own `.myenv` Python environment. The
`uv` environment above supplies the test and packaging tools.

```bash
./run doctor
./run status
./run logs
./run stop
```

Configure an OpenAI-compatible tutor in Settings to exercise generation. Deterministic tests use
fixtures and do not require a paid provider. Source runs store data in the checkout; do not point
experiments or tests at an installed student profile. See [data locations](docs/privacy-and-data-location.md)
for overrides and [troubleshooting](docs/troubleshooting.md) for common failures.

## Make a change

1. Start a focused branch from up-to-date `main`. For substantial changes, describe the problem in
   a GitHub issue first so maintainers can coordinate scope. GitHub is public intake; maintainers
   reconcile engineering work with Linear. A contributor does not need Linear access.
2. Follow existing patterns and keep the diff small. Add a regression test for a behavioral fix.
   Use synthetic course material; never commit documents, databases, model weights, or credentials.
3. Run the [relevant checks](docs/contributing-testing-migrations.md). UI or backend delivery also
   requires rebuilding and launching the signed desktop app as described in
   [local deployment](docs/local-deployment.md). Note any unavailable device verification in the PR.
4. Review **documentation impact**. Changes to behavior, setup, configuration, architecture, public
   interfaces, or contributor processes must update the relevant maintained docs in the same PR.
   If none apply, explain briefly in the PR template; no token documentation edit is required.
5. Open a PR against `main` with the problem, resulting behavior, test evidence, and remaining
   limitations. `CI Gate` must pass. Maintainers own release approval and publication.

Commit subjects use a Conventional Commit prefix with a reason for the change, such as
`fix: prevent stale saves from replacing newer writing`. Include useful decision context and
[repository Lore trailers](AGENTS.md) in the body. The prefix supports release notes; the trailers
preserve constraints, rejected approaches, and verification evidence.

## Documentation and releases

[The documentation index](docs/README.md) distinguishes maintained guidance, historical design
records, and candidate-specific evidence. Update maintained guidance; preserve historical measurements
as dated records rather than rewriting them to imply a newer run.

```bash
uv run python scripts/check_docs.py
uv run python scripts/check_active_references.py
```

[Releasing](docs/releasing.md) covers automated DMG creation, signing, notarization, GitHub Releases,
and updates. A successful build is not public-beta approval. The first release still requires the
explicit acceptance and distribution gates in the [release ledger](docs/release-evidence.md).

## Reporting problems

Use the [bug-report template](https://github.com/oliverdougherC/Lyra/issues/new/choose) for ordinary
bugs and [SECURITY.md](SECURITY.md) for private vulnerability reporting. Include only redacted logs,
screenshots, and synthetic examples. Contributions to source are under [Apache-2.0](LICENSE);
third-party redistribution obligations are recorded separately in
[distribution notices](docs/distribution-notices.md).
