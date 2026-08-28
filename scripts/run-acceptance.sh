#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Run the real-stack acceptance tests from a clean checkout.
#
# This script reproduces exactly what CI does:
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
# The script checks for port conflicts and refuses to start if ports 3000,
# 8000, or 18900 are already in use.
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Port guard ────────────────────────────────────────────────────────────
check_port() {
  local port=$1 label=$2
  if curl -sf --connect-timeout 1 "http://127.0.0.1:$port" >/dev/null 2>&1; then
    echo "ERROR: Port $port ($label) is already in use."
    echo "       Stop any running Lyra servers before running acceptance tests."
    exit 1
  fi
}

check_port 3000 "frontend"
check_port 8000 "backend"
check_port 18900 "tutor fixture"

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
pnpm exec playwright test --config playwright.acceptance.config.ts "$@"
