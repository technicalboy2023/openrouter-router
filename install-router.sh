#!/bin/bash

# ─────────────────────────────────────────────────────────────
#  LLM Gateway — Install Script
#  Usage: bash install-router.sh <router-name> <port>
#  Example: bash install-router.sh openrouter-router 8080
# ─────────────────────────────────────────────────────────────

ROUTER_NAME=$1
PORT=$2

if [ -z "$ROUTER_NAME" ] || [ -z "$PORT" ]; then
  echo "Usage: bash install-router.sh <router-name> <port>"
  echo "Example: bash install-router.sh openrouter-router 8080"
  exit 1
fi

BASE_DIR="/home/aman/routers"
ROUTER_DIR="$BASE_DIR/$ROUTER_NAME"
REPO_URL="https://github.com/technicalboy2023/$ROUTER_NAME"

echo "=================================="
echo "  LLM Gateway Installer"
echo "=================================="
echo "  Router  : $ROUTER_NAME"
echo "  Port    : $PORT"
echo "  Dir     : $ROUTER_DIR"
echo "=================================="

# ── system packages ───────────────────────────────────────────
echo "[1/7] Installing system packages..."
sudo apt update -qq
sudo apt install -y python3 python3-venv python3-pip git curl

# ── create base directory ──────────────────────────────────────
echo "[2/7] Setting up directories..."
mkdir -p "$BASE_DIR"
cd "$BASE_DIR" || exit 1

# ── clone or update repo ───────────────────────────────────────
echo "[3/7] Fetching router code..."
if [ -d "$ROUTER_DIR" ]; then
  echo "  → Router already exists, pulling latest..."
  cd "$ROUTER_DIR"
  git pull
else
  echo "  → Cloning from GitHub..."
  git clone "$REPO_URL"
  cd "$ROUTER_DIR" || exit 1
fi

# ── verify required files exist ───────────────────────────────
echo "[4/7] Verifying project files..."
REQUIRED_FILES=("main.py" "router_core.py" "provider_openrouter.py" "requirements.txt")
for f in "${REQUIRED_FILES[@]}"; do
  if [ ! -f "$f" ]; then
    echo "  ✗ MISSING: $f — aborting."
    exit 1
  fi
  echo "  ✓ Found: $f"
done

# ── virtual environment ────────────────────────────────────────
echo "[5/7] Creating virtual environment..."
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "  ✓ Dependencies installed."

# ── .env file ─────────────────────────────────────────────────
echo "[6/7] Checking .env file..."
if [ ! -f ".env" ]; then
  echo "  → Creating blank .env — PLEASE ADD YOUR API KEYS!"
  cat > .env <<'ENVEOF'
# OpenRouter API Keys (add up to 20)
OPENROUTER_KEY_1=sk-or-v1-
OPENROUTER_KEY_2=sk-or-v1-
OPENROUTER_KEY_3=sk-or-v1-

# Or provide all keys as comma-separated list:
# OPENROUTER_KEYS=sk-or-v1-aaa,sk-or-v1-bbb
ENVEOF
  echo "  ⚠  Edit now: nano $ROUTER_DIR/.env"
else
  echo "  ✓ .env already exists."
fi

# ── systemd service ────────────────────────────────────────────
echo "[7/7] Creating systemd service..."

SERVICE_FILE="/etc/systemd/system/$ROUTER_NAME.service"

sudo bash -c "cat > $SERVICE_FILE" <<EOF
[Unit]
Description=LLM Gateway — $ROUTER_NAME
After=network.target

[Service]
User=aman
WorkingDirectory=$ROUTER_DIR
ExecStart=$ROUTER_DIR/venv/bin/uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$ROUTER_NAME"
sudo systemctl restart "$ROUTER_NAME"

# ── final summary ──────────────────────────────────────────────
echo ""
echo "=================================="
echo "  ✅  Installation Complete!"
echo "=================================="
echo ""
echo "  Router  : $ROUTER_NAME"
echo "  Port    : $PORT"
echo "  Dir     : $ROUTER_DIR"
echo ""
echo "  ──── Add API Keys ────"
echo "  nano $ROUTER_DIR/.env"
echo ""
echo "  ──── Restart after adding keys ────"
echo "  sudo systemctl restart $ROUTER_NAME"
echo ""
echo "  ──── Service Commands ────"
echo "  systemctl status  $ROUTER_NAME"
echo "  systemctl stop    $ROUTER_NAME"
echo "  systemctl restart $ROUTER_NAME"
echo "  journalctl -u $ROUTER_NAME -f"
echo ""
echo "  ──── Test Endpoints ────"
echo "  curl http://localhost:$PORT/health"
echo "  curl http://localhost:$PORT/router/status"
echo "  curl http://localhost:$PORT/metrics"
echo "=================================="
