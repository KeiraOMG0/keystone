#!/usr/bin/env bash
# 02-setup-python-env.sh
# Creates the venv at the repo root and installs both requirement files.
# Copies .env.example to .env if .env doesn't exist yet, then stops so you
# can fill in the required secrets before continuing.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

echo "== Creating virtual environment =="
if [ -d "venv" ]; then
    echo "venv/ already exists, skipping creation."
else
    python3 -m venv venv
    echo "Created venv/"
fi

echo ""
echo "== Installing bot dependencies (requirements.txt) =="
venv/bin/pip install -r requirements.txt

echo ""
echo "== Installing dashboard backend dependencies (server/requirements.txt) =="
venv/bin/pip install -r server/requirements.txt

echo ""
echo "== Checking for .env =="
if [ -f ".env" ]; then
    echo ".env already exists, leaving it untouched."
else
    cp ".env.example" ".env"
    echo "Created .env from .env.example."
    echo ""
    echo "ACTION REQUIRED: open .env and fill in at least:"
    echo "  DISCORD_TOKEN, INTERNAL_API_SECRET, DISCORD_CLIENT_ID,"
    echo "  DISCORD_CLIENT_SECRET, SESSION_COOKIE_SECRET"
    echo "See docs/ARCHITECTURE.md section 6 for what each variable does."
    echo ""
    echo "Re-run this script (or just continue to 03-setup-database.sh) once .env is filled in."
    exit 0
fi

echo ""
echo "Python environment ready."
