#!/usr/bin/env bash
# =============================================================
# AI-Trading-System — DigitalOcean Ubuntu 24 (Noble) setup
# Run once as root on a fresh droplet:
#   bash setup_server.sh
#
# What it does:
#   1. System updates + Python 3.12 + git + build tools
#   2. Creates 'trading' user with restricted shell
#   3. Clones repo to /opt/ai-trading-system
#   4. Creates venv, installs requirements
#   5. Installs systemd service
#   6. Configures log rotation
#   7. Adds fail2ban + ufw firewall rules
# =============================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────
REPO_URL="${REPO_URL:-}"          # set via env: REPO_URL=git@github.com:you/ai-trading-system.git
INSTALL_DIR="/opt/ai-trading-system"
SERVICE_USER="trading"
SERVICE_FILE="/etc/systemd/system/trading-system.service"
PYTHON_VERSION="3.12"
LOG_DIR="/var/log/ai-trading-system"

if [[ -z "$REPO_URL" ]]; then
  echo "ERROR: set REPO_URL before running."
  echo "  export REPO_URL=https://github.com/yourname/ai-trading-system.git"
  exit 1
fi

echo "=== [1/8] System update ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
  software-properties-common curl wget git build-essential \
  libssl-dev libffi-dev zlib1g-dev libbz2-dev \
  sqlite3 ufw fail2ban logrotate

echo "=== [2/8] Python ${PYTHON_VERSION} ==="
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -qq
apt-get install -y -qq "python${PYTHON_VERSION}" "python${PYTHON_VERSION}-venv" "python${PYTHON_VERSION}-dev"
# Verify
python${PYTHON_VERSION} --version

echo "=== [3/8] Create service user '${SERVICE_USER}' ==="
id -u "${SERVICE_USER}" &>/dev/null || useradd --system --shell /usr/sbin/nologin --home "${INSTALL_DIR}" "${SERVICE_USER}"

echo "=== [4/8] Clone repo to ${INSTALL_DIR} ==="
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  echo "Repo already exists — pulling latest."
  git -C "${INSTALL_DIR}" pull --ff-only
else
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

echo "=== [5/8] Virtual environment + dependencies ==="
sudo -u "${SERVICE_USER}" python${PYTHON_VERSION} -m venv "${INSTALL_DIR}/.venv"
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --upgrade --quiet pip wheel
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

# Scaffold .env if not already present
if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
  chown "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/.env"
  chmod 600 "${INSTALL_DIR}/.env"
  echo "⚠️  ${INSTALL_DIR}/.env created from example — FILL IN REAL SECRETS BEFORE STARTING SERVICE."
fi

echo "=== [6/8] Log directory ==="
mkdir -p "${LOG_DIR}"
chown "${SERVICE_USER}:${SERVICE_USER}" "${LOG_DIR}"

# Log rotation
cat > /etc/logrotate.d/ai-trading-system << EOF
${LOG_DIR}/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    su ${SERVICE_USER} ${SERVICE_USER}
}
EOF

echo "=== [7/8] systemd service ==="
cp "${INSTALL_DIR}/trading-system.service" "${SERVICE_FILE}"
# Patch install dir into service file
sed -i "s|/opt/ai-trading-system|${INSTALL_DIR}|g" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable trading-system.service
echo "Service installed. Start with: systemctl start trading-system"

echo "=== [8/8] Firewall (ufw) ==="
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
# Streamlit dashboard (only if you have a trusted IP; uncomment + replace X.X.X.X)
# ufw allow from X.X.X.X to any port 8501
ufw --force enable

echo ""
echo "========================================================="
echo " Setup complete."
echo " 1. Edit ${INSTALL_DIR}/.env with real secrets."
echo " 2. systemctl start trading-system"
echo " 3. journalctl -fu trading-system   # follow logs"
echo "========================================================="
