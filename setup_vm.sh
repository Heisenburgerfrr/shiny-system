#!/usr/bin/env bash
# ==============================================================================
# Automated 1-Click Setup Script for YouTube to Instagram Reels Bot on Azure
# ==============================================================================
set -e

echo "🚀 [1/6] Updating system packages & installing dependencies..."
sudo apt update -y
sudo apt install -y python3 python3-pip python3-venv ffmpeg git curl unzip

# Install Deno JS runtime if not already installed (required by yt-dlp for YouTube JS challenge solving)
if ! command -v deno &> /dev/null; then
    echo "⚡ Installing Deno JS runtime for yt-dlp challenge solving..."
    curl -fsSL https://deno.land/install.sh | sh
    sudo ln -sf ~/.deno/bin/deno /usr/local/bin/deno
fi

echo "💾 [2/6] Configuring 4 GB Swap memory for FFmpeg stability..."
if [ ! -f /swapfile ]; then
    sudo fallocate -l 4G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
    echo "✅ 4GB swap memory created successfully."
else
    echo "ℹ️ Swap file already exists, skipping."
fi

echo "📦 [3/6] Setting up Python virtual environment..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "📁 [4/6] Ensuring storage directories exist..."
mkdir -p storage/downloads storage/processed storage/temp logs

echo "⚙️ [5/6] Configuring systemd background service..."
SERVICE_FILE="/etc/systemd/system/memoxz.service"
CURRENT_USER="$(whoami)"

sudo bash -c "cat <<EOF > $SERVICE_FILE
[Unit]
Description=Memoxz YouTube to Instagram Reels Telegram Bot
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/venv/bin/python -m bot.main
Restart=always
RestartSec=5
EnvironmentFile=$SCRIPT_DIR/.env
Environment="PATH=/home/$CURRENT_USER/.deno/bin:/usr/local/bin:/usr/bin:/bin"
StandardOutput=append:$SCRIPT_DIR/logs/systemd_stdout.log
StandardError=append:$SCRIPT_DIR/logs/systemd_stderr.log

[Install]
WantedBy=multi-user.target
EOF"

echo "🔄 [6/6] Reloading systemd & starting bot..."
sudo systemctl daemon-reload
sudo systemctl enable memoxz
sudo systemctl restart memoxz

echo ""
echo "=============================================================================="
echo "🎉 DEPLOYMENT COMPLETE!"
echo "• Bot Status: run 'sudo systemctl status memoxz'"
echo "• Live Logs:  run 'tail -f logs/systemd_stdout.log' or 'tail -f logs/bot.log'"
echo "=============================================================================="
