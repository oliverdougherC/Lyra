#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Run the real-stack acceptance tests from a clean checkout.
#
# This script reproduces the real-backend acceptance lane from CI:
#   1. Installs backend and frontend dependencies.
#   2. Builds the production frontend.
#   3. Installs Playwright browsers.
#   4. Runs the acceptance test suite (which starts the full stack internally).
#
# Usage:
#   ./scripts/run-acceptance.sh            # full run
#   ./scripts/run-acceptance.sh --headed   # run with visible browser
#   ./scripts/run-acceptance.sh --debug    # run with Playwright inspector
#
# Optional env overrides:
#   ACCEPTANCE_FRONTEND_PORT  default 3000
#   ACCEPTANCE_BACKEND_PORT   default 8000
#   ACCEPTANCE_TUTOR_PORT     default 18900
#   ACCEPTANCE_HELPER_PORT    default: an available ephemeral port for this run
#
# The script checks for port conflicts and refuses to start if its selected
# frontend, backend, or tutor-fixture ports are already in use.
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_PORT="${ACCEPTANCE_FRONTEND_PORT:-3000}"
BACKEND_PORT="${ACCEPTANCE_BACKEND_PORT:-8000}"
TUTOR_PORT="${ACCEPTANCE_TUTOR_PORT:-18900}"

# ── Port guard ────────────────────────────────────────────────────────────
check_port() {
  local port=$1 label=$2
  if curl -sf --connect-timeout 1 "http://127.0.0.1:$port" >/dev/null 2>&1; then
    echo "ERROR: Port $port ($label) is already in use."
    echo "       Stop any running Lyra servers before running acceptance tests."
    exit 1
  fi
}

check_port "$FRONTEND_PORT" "frontend"
check_port "$BACKEND_PORT" "backend"
check_port "$TUTOR_PORT" "tutor fixture"

# ── Dependencies ──────────────────────────────────────────────────────────
echo "Installing backend dependencies..."
(cd "$ROOT" && uv sync --extra dev --quiet)

echo "Installing frontend dependencies..."
(cd "$ROOT/frontend" && pnpm install --frozen-lockfile --silent)

# ── Build ─────────────────────────────────────────────────────────────────
echo "Building production frontend..."
(cd "$ROOT/frontend" && pnpm build)

# ── Playwright browsers ──────────────────────────────────────────────────
echo "Installing Playwright browsers..."
(cd "$ROOT/frontend" && pnpm exec playwright install chromium)

# ── Run ───────────────────────────────────────────────────────────────────
echo ""
echo "Running acceptance tests..."
echo ""
cd "$ROOT/frontend"
ACCEPTANCE_FRONTEND_PORT="$FRONTEND_PORT" \
ACCEPTANCE_BACKEND_PORT="$BACKEND_PORT" \
ACCEPTANCE_TUTOR_PORT="$TUTOR_PORT" \
pnpm exec playwright test --config playwright.acceptance.config.ts "$@"
