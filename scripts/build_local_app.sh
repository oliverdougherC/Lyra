#!/bin/bash
# Build, sign, verify, and consume the local bundle into one canonical installation.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ "$(uname -s)" != Darwin ]]; then
  echo 'Local app installation requires macOS.' >&2
  exit 1
fi

# Refuse before PyInstaller/Tauri can replace a running build-output bundle.
python3 -c 'import sys; sys.path.insert(0, "scripts"); from install_local_app import assert_not_running; assert_not_running()'
LICENSE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/lyra-licenses.XXXXXX")"
trap 'rm -rf "$LICENSE_DIR"' EXIT

python3 scripts/release_metadata.py --check
uv python install 3.12
uv sync --locked --python 3.12 --extra packaging --extra dev
# Nested frontend scripts use uv too; keep the resolved packaging environment intact.
export UV_NO_SYNC=1
uv run --no-sync pyinstaller --clean --noconfirm packaging/lyra_backend.spec
uv run --no-sync python scripts/frozen_backend_smoke.py dist/lyra-backend/lyra-backend
uv run --no-sync python packaging/stage_sidecar.py
(
  cd frontend
  pnpm install --frozen-lockfile
  pnpm licenses list --prod --json > "$LICENSE_DIR/frontend-licenses.json"
)
uv run --no-sync python scripts/collect_distribution_notices.py \
  --frontend-inventory "$LICENSE_DIR/frontend-licenses.json"
(
  cd frontend
  pnpm build
  pnpm tauri:build --bundles app --no-sign --ci
)
APP="$REPO_ROOT/src-tauri/target/release/bundle/macos/Lyra.app"
uv run --no-sync python scripts/release_metadata.py --bundle "$APP" \
  --source "$(git rev-parse HEAD)"
uv run --no-sync python scripts/sign_local_app.py "$APP"
uv run --no-sync python scripts/install_local_app.py "$APP" --open "$@"
