#!/usr/bin/env bash
# 03-setup-database.sh
# Applies the dashboard backend's Alembic migrations (server/dashboard.db).
# The bot's own database (bot.db) does NOT need a manual step here -- it
# migrates itself automatically the first time main.py runs (see
# docs/ARCHITECTURE.md section 7).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

if [ ! -f ".env" ]; then
    echo "ERROR: .env not found. Run 02-setup-python-env.sh first and fill in .env." >&2
    exit 1
fi

if [ ! -x "venv/bin/python" ]; then
    echo "ERROR: venv not found. Run 02-setup-python-env.sh first." >&2
    exit 1
fi

echo "== Applying dashboard database migrations (Alembic) =="
echo "This must run from the repo root -- alembic.ini's script_location is relative to it."
venv/bin/python -m alembic -c server/alembic.ini upgrade head

echo ""
echo "Dashboard database ready (server/dashboard.db)."
echo "The bot's own database (bot.db) will be created and migrated automatically"
echo "the first time you run main.py -- no manual step needed for it."
