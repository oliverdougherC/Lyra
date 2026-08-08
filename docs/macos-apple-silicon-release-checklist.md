# macOS Apple Silicon Release Checklist

Use this checklist for a release candidate on a clean Apple Silicon Mac.

## 1. Clean install

- [ ] Confirm the machine is Apple Silicon and running a supported macOS version.
- [ ] Install Docker Desktop, Python 3.12+, Node.js 20.9+, and pnpm.
- [ ] Run `./run doctor` from a clean checkout and verify the prerequisites pass.
- [ ] Run `./run` once without manually cleaning up any processes.
- [ ] Confirm Lyra opens in the browser and reports a healthy backend and frontend.

## 2. First launch

- [ ] Open Settings and confirm the tutor endpoint, key storage, and Firecrawl state are shown.
- [ ] Upload a document and confirm it ingests.
- [ ] If the tutor endpoint is remote, acknowledge document-text sending in Settings only after
      that behavior is expected.

## 3. Restart recovery

- [ ] While jobs are running, stop the app and start it again with `./run`.
- [ ] Confirm interrupted work is reconciled on startup instead of being silently lost.
- [ ] Confirm `./run stop` only stops this checkout's owned services.

## 4. Offline and degraded use

- [ ] Run `./run --skip-firecrawl`.
- [ ] Confirm local documents, chat, study, and draft work still function.
- [ ] Confirm web research stays unavailable and the UI says so plainly.
- [ ] Verify `./run status` and `./run doctor` report the degraded state honestly.

## 5. Data preservation

- [ ] Confirm the default workspace data lives under `data/`, including `data/lyra.db`,
      `data/uploads/`, `data/text/`, `data/pages/`, and `data/models/`.
- [ ] Confirm `.lyra/` is launcher metadata, not user content.
- [ ] Run `./run backup --archive /absolute/path/lyra-backup.tgz` and confirm it succeeds only
      after the launcher-owned stack is stopped.
- [ ] Restore that archive with `./run restore --archive /absolute/path/lyra-backup.tgz
      --data-dir /absolute/path/restored-data` and confirm class, document, artifact, and settings
      data round-trip.
- [ ] Confirm restore refuses an already-existing target directory instead of overwriting it.
- [ ] Confirm the API key is absent from the backup when macOS Keychain is in use.
- [ ] Confirm `./run --clean` only rebuilds the frontend cache and does not delete user data.
- [ ] Confirm deleting a document removes its upload, extracted text, and rendered pages.
- [ ] Confirm deleting a class removes only that class's uploads and derived files.
- [ ] Confirm a stop-start cycle preserves the workspace without manual cleanup.
