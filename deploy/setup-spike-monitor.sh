#!/usr/bin/env bash
# Kalshi Spike Monitor — Hetzner deployment script
# Run this on the server: bash deploy/setup-spike-monitor.sh
set -euo pipefail

REPO_DIR="/home/tyler/Weather"
SERVICE_NAME="spike-monitor"

echo "=== Kalshi Spike Monitor — Server Setup ==="
echo ""

# ── 1. Check prerequisites ──────────────────────────────────────────
if [ ! -d "$REPO_DIR" ]; then
    echo "ERROR: $REPO_DIR not found. Clone the repo first:"
    echo "  git clone <your-repo-url> $REPO_DIR"
    exit 1
fi

cd "$REPO_DIR"

# ── 2. Install dependencies ─────────────────────────────────────────
echo "[1/5] Installing dependencies..."
if command -v uv &> /dev/null; then
    uv sync
else
    echo "  uv not found, installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    uv sync
fi
echo "  OK"

# ── 3. Check .env.local exists ───────────────────────────────────────
echo "[2/5] Checking credentials..."
if [ ! -f "$REPO_DIR/.env.local" ]; then
    echo "ERROR: $REPO_DIR/.env.local not found."
    echo "Create it with:"
    echo "  KALSHI_API_KEY_ID=..."
    echo "  KALSHI_PRIVATE_KEY_PATH=Key/your-key.pem"
    echo "  GMAIL_ADDRESS=..."
    echo "  GMAIL_APP_PASSWORD=..."
    exit 1
fi
echo "  OK"

# ── 4. Install systemd service ──────────────────────────────────────
echo "[3/5] Installing systemd service..."
sudo cp deploy/spike-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
echo "  OK"

# ── 5. Enable and start ─────────────────────────────────────────────
echo "[4/5] Enabling service (auto-start on boot)..."
sudo systemctl enable "$SERVICE_NAME"
echo "  OK"

echo "[5/5] Starting service..."
sudo systemctl start "$SERVICE_NAME"
echo "  OK"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Commands:"
echo "  sudo systemctl status $SERVICE_NAME    # Check status"
echo "  sudo journalctl -u $SERVICE_NAME -f    # Live logs"
echo "  sudo systemctl restart $SERVICE_NAME   # Restart"
echo "  sudo systemctl stop $SERVICE_NAME      # Stop"
echo ""

# Show current status
sudo systemctl status "$SERVICE_NAME" --no-pager || true
