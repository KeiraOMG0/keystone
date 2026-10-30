#!/usr/bin/env bash
# 01-check-tools.sh
# Checks for Python 3, Node.js, and ffmpeg. Installs any that are missing
# using whichever package manager is detected on this system (apt, dnf,
# pacman, or zypper). If none of those are found, prints manual-install
# instructions instead of guessing.
set -euo pipefail

PKG_MANAGER=""
if command -v apt-get >/dev/null 2>&1; then
    PKG_MANAGER="apt"
elif command -v dnf >/dev/null 2>&1; then
    PKG_MANAGER="dnf"
elif command -v pacman >/dev/null 2>&1; then
    PKG_MANAGER="pacman"
elif command -v zypper >/dev/null 2>&1; then
    PKG_MANAGER="zypper"
fi

if [ -z "$PKG_MANAGER" ]; then
    echo "Could not detect a supported package manager (apt, dnf, pacman, zypper)." >&2
    echo "Install these manually, then re-run the remaining scripts:" >&2
    echo "  - Python 3.10+ and pip" >&2
    echo "  - Node.js 20+ and npm" >&2
    echo "  - ffmpeg" >&2
    exit 1
fi

echo "Detected package manager: $PKG_MANAGER"

install_pkg() {
    # $1 = human-readable name for logging, remaining args = package name(s)
    # per manager, in the order apt dnf pacman zypper.
    local label="$1"
    case "$PKG_MANAGER" in
        apt)
            sudo apt-get update -qq
            sudo apt-get install -y "$2"
            ;;
        dnf)
            sudo dnf install -y "$3"
            ;;
        pacman)
            sudo pacman -Sy --noconfirm "$4"
            ;;
        zypper)
            sudo zypper install -y "$5"
            ;;
    esac
}

echo ""
echo "== Checking for Python 3 =="
if command -v python3 >/dev/null 2>&1; then
    echo "Found: $(python3 --version)"
else
    echo "python3 not found. Installing..."
    install_pkg "Python 3" "python3 python3-venv python3-pip" "python3 python3-pip" "python python-pip" "python3 python3-pip"
fi

echo ""
echo "== Checking for Node.js =="
if command -v node >/dev/null 2>&1; then
    echo "Found: node $(node --version)"
else
    echo "node not found. Installing..."
    install_pkg "Node.js" "nodejs npm" "nodejs npm" "nodejs npm" "nodejs npm"
    echo "NOTE: distro-packaged Node.js versions can lag behind current LTS."
    echo "If 'node --version' below is older than 20.x, install a newer Node"
    echo "via https://github.com/nvm-sh/nvm or your distro's NodeSource repo."
fi

echo ""
echo "== Checking for ffmpeg =="
if command -v ffmpeg >/dev/null 2>&1; then
    echo "Found: ffmpeg"
else
    echo "ffmpeg not found. Installing..."
    install_pkg "ffmpeg" "ffmpeg" "ffmpeg" "ffmpeg" "ffmpeg"
fi

echo ""
echo "Tool check complete."
