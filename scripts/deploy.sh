#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Pull latest code from Git and restart simulator services on EC2
#
# Usage (run on EC2):
#   chmod +x scripts/deploy.sh
#   ./scripts/deploy.sh
#
# First-time setup only:
#   git clone https://github.com/YOUR_USER/kite-simulator.git ~/kite-simulator
#   cd ~/kite-simulator && ./scripts/deploy.sh
# =============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_DIR/venv"

echo ""
echo "═══════════════════════════════════════════════"
echo "  Kite Simulator — Deploy"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════"

# ── 1. Pull latest code ──────────────────────────────────────────────────────
echo ""
echo "▶  git pull"
cd "$PROJECT_DIR"
git pull origin main

# ── 2. Create venv if it doesn't exist ──────────────────────────────────────
if [ ! -d "$VENV" ]; then
    echo ""
    echo "▶  Creating virtual environment…"
    python3 -m venv "$VENV"
fi

# ── 3. Install / upgrade dependencies ───────────────────────────────────────
echo ""
echo "▶  Installing dependencies…"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

# ── 4. Ensure .env exists (don't overwrite existing) ────────────────────────
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo ""
    echo "▶  Creating .env from .env.example — edit it before proceeding!"
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
fi

# ── 5. Ensure config/ directory exists (api_keys.json lives here) ───────────
mkdir -p "$PROJECT_DIR/config"

# ── 6. Install systemd services (first time only) ───────────────────────────
SERVICES=("kite-simulator" "kite-mgmt-api")
for svc in "${SERVICES[@]}"; do
    TARGET="/etc/systemd/system/${svc}.service"
    SOURCE="$PROJECT_DIR/scripts/${svc}.service"
    if [ ! -f "$TARGET" ]; then
        echo ""
        echo "▶  Installing systemd service: $svc"
        sudo cp "$SOURCE" "$TARGET"
        sudo systemctl daemon-reload
        sudo systemctl enable "$svc"
    fi
done

# ── 7. Restart services ──────────────────────────────────────────────────────
echo ""
echo "▶  Restarting services…"
sudo systemctl restart kite-simulator
sudo systemctl restart kite-mgmt-api

# ── 8. Health check ──────────────────────────────────────────────────────────
sleep 3
echo ""
echo "▶  Service status:"
sudo systemctl is-active --quiet kite-simulator \
    && echo "  ✓ kite-simulator   RUNNING" \
    || echo "  ✗ kite-simulator   FAILED"

sudo systemctl is-active --quiet kite-mgmt-api \
    && echo "  ✓ kite-mgmt-api    RUNNING" \
    || echo "  ✗ kite-mgmt-api    FAILED"

echo ""
echo "═══════════════════════════════════════════════"
echo "  Deploy complete!"
echo ""
echo "  WebSocket  →  ws://$(curl -s ifconfig.me 2>/dev/null || echo YOUR_EC2_IP):8765"
echo "  Mgmt API   →  http://$(curl -s ifconfig.me 2>/dev/null || echo YOUR_EC2_IP):8766/docs"
echo "═══════════════════════════════════════════════"
echo ""
