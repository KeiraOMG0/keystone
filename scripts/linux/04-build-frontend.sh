#!/usr/bin/env bash
# 04-build-frontend.sh
# Installs frontend npm dependencies and builds the production bundle
# (server/frontend/dist), which server/app.py serves statically.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if ! command -v npm >/dev/null 2>&1; then
    echo "ERROR: npm not found. Run 01-check-tools.sh first (installs Node.js, which includes npm)." >&2
    exit 1
fi

cd "$REPO_ROOT/server/frontend"

echo "== Installing frontend dependencies =="
npm install

echo ""
echo "== Building production frontend (tsc -b && vite build) =="
npm run build

cd "$REPO_ROOT"

echo ""
echo "Frontend built to server/frontend/dist/."
