#!/usr/bin/env bash
# 01-check-tools.sh
# Checks for Python 3, Node.js, and ffmpeg. Installs any that are missing
# via Homebrew. Installs Homebrew itself first if it isn't present.
set -euo pipefail

echo "== Checking for Homebrew =="
if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew not found. Installing..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Apple Silicon Homebrew installs to /opt/homebrew, not on PATH by default
    # in a fresh shell -- add it for the rest of this script's run.
    if [ -x /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
else
    echo "Found: $(brew --version | head -n1)"
fi

echo ""
echo "== Checking for Python 3 =="
if command -v python3 >/dev/null 2>&1; then
    echo "Found: $(python3 --version)"
else
    echo "python3 not found. Installing via Homebrew..."
    brew install python@3.12
fi

echo ""
echo "== Checking for Node.js =="
if command -v node >/dev/null 2>&1; then
    echo "Found: node $(node --version)"
else
    echo "node not found. Installing via Homebrew..."
    brew install node
fi

echo ""
echo "== Checking for ffmpeg =="
if command -v ffmpeg >/dev/null 2>&1; then
    echo "Found: ffmpeg"
else
    echo "ffmpeg not found. Installing via Homebrew..."
    brew install ffmpeg
fi

echo ""
echo "Tool check complete."
