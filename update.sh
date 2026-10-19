#!/usr/bin/env bash
# ==============================================================================
# Automated Update Script for Unshockabl Telegram Bot on Azure VM
# ==============================================================================
# Usage:
#   ./update.sh
#   ./update.sh -b uns
#   ./update.sh /b uns
# ==============================================================================
set -e

# Default branch for this instance
BRANCH="uns"
SERVICE_NAME="unshockabl.service"

# Parse arguments (-b <branch>, /b <branch>, or positional)
while [[ $# -gt 0 ]]; do
    case "$1" in
        -b|/b|--branch)
            BRANCH="$2"
            shift 2
            ;;
        -b=*|/b=*)
            BRANCH="${1#*=}"
            shift
            ;;
        *)
            if [[ -n "$1" && "$1" != -* && "$1" != /* ]]; then
                BRANCH="$1"
            fi
            shift
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🔄 [1/4] Pulling latest updates from branch '$BRANCH'..."
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull origin "$BRANCH"

echo "📦 [2/4] Updating Python packages in virtual environment..."
if [ -d "venv" ]; then
    ./venv/bin/pip install --upgrade pip --quiet
    ./venv/bin/pip install -r requirements.txt --quiet
elif [ -d "../venv" ]; then
    ../venv/bin/pip install --upgrade pip --quiet
    ../venv/bin/pip install -r requirements.txt --quiet
else
    echo "⚠️ Virtualenv directory (venv) not found, skipping pip install."
fi

echo "📁 [3/4] Ensuring storage directories exist..."
mkdir -p storage/downloads storage/processed storage/temp logs

echo "🚀 [4/4] Restarting systemd service '$SERVICE_NAME'..."
sudo systemctl daemon-reload
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "✅ Update complete! Current service status:"
echo "=================================================="
sudo systemctl status "$SERVICE_NAME" --no-pager -n 15
