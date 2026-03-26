#!/usr/bin/env bash
# ------------------------------------------------------------------
# auto-ig — Oracle Cloud VM Setup Script
#
# Installs Python, creates a virtualenv, installs dependencies,
# opens the temp HTTP port, and enables the systemd service.
#
# Usage:  bash deploy/setup.sh
# Run from the auto-ig project root directory.
# ------------------------------------------------------------------

set -euo pipefail

# ---- Configuration ------------------------------------------------
VENV_DIR=".venv"
SERVICE_NAME="auto-ig"
SERVICE_FILE="deploy/auto-ig.service"
TEMP_HTTP_PORT=8765
# -------------------------------------------------------------------

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ---- Pre-flight checks -------------------------------------------
if [[ ! -f "main.py" ]]; then
    error "main.py not found. Run this script from the auto-ig project root."
    exit 1
fi

if [[ ! -f "$SERVICE_FILE" ]]; then
    error "Service file not found at $SERVICE_FILE."
    exit 1
fi

if [[ "$(id -u)" -eq 0 ]]; then
    warn "Running as root. The service should run as a non-root user."
    warn "Consider running this script as the user who will own the service."
fi

# ---- Step 1: Install system dependencies -------------------------
info "Updating package lists..."
sudo apt-get update -qq

# Try python3.11 first, fall back to python3
PYTHON_CMD=""
if command -v python3.11 &>/dev/null; then
    PYTHON_CMD="python3.11"
    info "Found python3.11 on PATH."
elif sudo apt-get install -y -qq python3.11 python3.11-venv 2>/dev/null; then
    PYTHON_CMD="python3.11"
    info "Installed python3.11 from package manager."
elif command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
    if [[ "$PY_MAJOR" -ge 3 && "$PY_MINOR" -ge 11 ]]; then
        PYTHON_CMD="python3"
        info "Using python3 (version $PY_VERSION)."
    else
        error "Python 3.11+ is required but found $PY_VERSION."
        error "Install Python 3.11+ manually, then re-run this script."
        exit 1
    fi
else
    error "No suitable Python found. Install Python 3.11+ and re-run."
    exit 1
fi

# Ensure venv module is available
sudo apt-get install -y -qq python3-venv python3-pip 2>/dev/null || true

# ---- Step 2: Create virtualenv and install dependencies ----------
if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating Python virtualenv at $VENV_DIR..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
else
    info "Virtualenv already exists at $VENV_DIR."
fi

info "Installing Python dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r requirements.txt -q
info "Dependencies installed."

# ---- Step 3: Create .env if it does not exist --------------------
if [[ ! -f ".env" ]]; then
    if [[ -f ".env.example" ]]; then
        cp .env.example .env
        warn ".env created from .env.example — edit it now to add your API keys:"
        warn "  nano .env"
    else
        warn "No .env or .env.example found. Create .env manually with your API keys."
    fi
else
    info ".env already exists (not overwritten)."
fi

# ---- Step 4: Create required directories -------------------------
mkdir -p storage/media
info "Created storage/media/ directory."

# ---- Step 5: Open temp HTTP port in iptables ---------------------
info "Configuring iptables to allow TCP on port $TEMP_HTTP_PORT..."
if sudo iptables -C INPUT -p tcp --dport "$TEMP_HTTP_PORT" -j ACCEPT 2>/dev/null; then
    info "iptables rule for port $TEMP_HTTP_PORT already exists."
else
    sudo iptables -I INPUT -p tcp --dport "$TEMP_HTTP_PORT" -j ACCEPT
    info "iptables rule added for port $TEMP_HTTP_PORT."
fi

# Persist iptables rules if iptables-persistent is available
if command -v netfilter-persistent &>/dev/null; then
    sudo netfilter-persistent save 2>/dev/null || true
    info "iptables rules persisted."
else
    warn "iptables-persistent not installed. Rule will not survive reboot."
    warn "Install with: sudo apt-get install -y iptables-persistent"
fi

# ---- Step 6: Install systemd service ----------------------------
PROJECT_DIR="$(pwd)"
SERVICE_USER="$(whoami)"

info "Installing systemd service..."

# Create a temporary service file with paths substituted
TEMP_SERVICE=$(mktemp)
sed \
    -e "s|User=ubuntu|User=$SERVICE_USER|" \
    -e "s|Group=ubuntu|Group=$SERVICE_USER|" \
    -e "s|WorkingDirectory=/home/ubuntu/auto-ig|WorkingDirectory=$PROJECT_DIR|" \
    -e "s|EnvironmentFile=/home/ubuntu/auto-ig/.env|EnvironmentFile=$PROJECT_DIR/.env|" \
    -e "s|ExecStart=/home/ubuntu/auto-ig/.venv/bin/python|ExecStart=$PROJECT_DIR/.venv/bin/python|" \
    -e "s|ReadWritePaths=/home/ubuntu/auto-ig/accounts /home/ubuntu/auto-ig/storage|ReadWritePaths=$PROJECT_DIR/accounts $PROJECT_DIR/storage|" \
    -e "s|ProtectHome=read-only|ProtectHome=read-only|" \
    "$SERVICE_FILE" > "$TEMP_SERVICE"

sudo cp "$TEMP_SERVICE" "/etc/systemd/system/${SERVICE_NAME}.service"
rm -f "$TEMP_SERVICE"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
info "Service installed and enabled (will start on boot)."

# ---- Summary -----------------------------------------------------
echo ""
echo "============================================================"
echo "  auto-ig setup complete"
echo "============================================================"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Edit .env with your API keys:"
echo "       nano .env"
echo ""
echo "  2. IMPORTANT: Open port $TEMP_HTTP_PORT in Oracle Cloud Console:"
echo "       VCN > Subnet > Security List > Add Ingress Rule"
echo "       Source CIDR: 0.0.0.0/0"
echo "       Destination Port: $TEMP_HTTP_PORT"
echo "       Protocol: TCP"
echo ""
echo "  3. Start the service:"
echo "       sudo systemctl start $SERVICE_NAME"
echo ""
echo "  4. Check logs:"
echo "       journalctl -u $SERVICE_NAME -f"
echo ""
echo "  5. Smoke test (dry run):"
echo "       $VENV_DIR/bin/python main.py --account veggie_alternatives --dry-run"
echo ""
echo "============================================================"
