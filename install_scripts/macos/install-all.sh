#!/usr/bin/env bash
# install-all.sh
# Runs all four setup steps in order. If .env didn't exist yet, step 2 will
# create it from .env.example and stop here so you can fill in secrets --
# just re-run this script once that's done.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

"$SCRIPT_DIR/01-check-tools.sh"
"$SCRIPT_DIR/02-setup-python-env.sh"

if [ ! -f "$REPO_ROOT/.env" ]; then
    # 02-setup-python-env.sh already printed the "fill in .env" instructions.
    exit 0
fi

"$SCRIPT_DIR/03-setup-database.sh"
"$SCRIPT_DIR/04-build-frontend.sh"

echo ""
echo "=================================================================="
echo " Install complete. Start the bot and dashboard in separate terminals:"
echo "=================================================================="
echo "  venv/bin/python main.py"
echo "  venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 8080"
echo ""
echo "See docs/ARCHITECTURE.md sections 5 and 11 for more detail."
