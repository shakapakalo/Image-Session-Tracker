#!/usr/bin/env bash
# ==============================================================================
# ChatGPT2API — Contabo VPS One-Command Deploy Script
# Usage:  bash contabo_deploy.sh
# Tested: Ubuntu 22.04 / 24.04
# ==============================================================================
set -euo pipefail

APP_DIR="/opt/chatgpt2api"
SERVICE_NAME="chatgpt2api"
APP_PORT="${APP_PORT:-8000}"
API_KEY="${API_KEY:-chatgpt2api}"      # change before production
REPO_URL="${REPO_URL:-}"               # set your GitHub fork URL, e.g.
                                        # https://github.com/youruser/chatgpt2api.git

echo "====================================================="
echo " ChatGPT2API — Contabo Deploy"
echo "====================================================="

# ── 1. System packages ──────────────────────────────────
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    git curl nginx certbot python3-certbot-nginx \
    2>/dev/null

# ── 2. Clone / pull repo ────────────────────────────────
echo "[2/7] Setting up application code..."
if [ -d "$APP_DIR/.git" ]; then
    echo "      Pulling latest code..."
    git -C "$APP_DIR" pull --ff-only
else
    if [ -z "$REPO_URL" ]; then
        echo "ERROR: Set REPO_URL to your GitHub fork, e.g.:"
        echo "  REPO_URL=https://github.com/youruser/chatgpt2api.git bash $0"
        exit 1
    fi
    git clone "$REPO_URL" "$APP_DIR"
fi

# ── 3. Python venv + dependencies ───────────────────────
echo "[3/7] Installing Python dependencies..."
python3 -m venv "$APP_DIR/.venv"
source "$APP_DIR/.venv/bin/activate"
pip install -q --upgrade pip
pip install -q -r "$APP_DIR/requirements.txt"
deactivate

# ── 4. Config / data dirs ───────────────────────────────
echo "[4/7] Creating data directories..."
mkdir -p "$APP_DIR/data"
chown -R www-data:www-data "$APP_DIR/data" 2>/dev/null || true

# Write minimal .env if not present
ENV_FILE="$APP_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
cat > "$ENV_FILE" <<EOF
API_KEY=${API_KEY}
PORT=${APP_PORT}
EOF
    echo "      Created $ENV_FILE — edit to set your API_KEY."
fi

# ── 5. Systemd service ──────────────────────────────────
echo "[5/7] Installing systemd service..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=ChatGPT2API
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/python main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "      Service is running ✓"
else
    echo "      Service FAILED — check: journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi

# ── 6. Nginx reverse proxy ──────────────────────────────
echo "[6/7] Configuring Nginx..."
DOMAIN="${DOMAIN:-_}"     # set DOMAIN=yourdomain.com before running for SSL
cat > "/etc/nginx/sites-available/${SERVICE_NAME}" <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    # Large bodies for image uploads
    client_max_body_size 50M;

    location / {
        proxy_pass         http://127.0.0.1:${APP_PORT};
        proxy_http_version 1.1;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        # Streaming / SSE support
        proxy_buffering    off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        chunked_transfer_encoding on;
    }
}
EOF

ln -sf "/etc/nginx/sites-available/${SERVICE_NAME}" "/etc/nginx/sites-enabled/${SERVICE_NAME}"
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# ── 7. Optional: SSL with Let's Encrypt ─────────────────
if [ "${DOMAIN}" != "_" ] && [ -n "${EMAIL:-}" ]; then
    echo "[7/7] Obtaining SSL certificate for ${DOMAIN}..."
    certbot --nginx -d "$DOMAIN" --email "$EMAIL" --agree-tos --non-interactive --redirect
    echo "      HTTPS enabled ✓"
else
    echo "[7/7] Skipping SSL (set DOMAIN=yourdomain.com EMAIL=you@mail.com to enable)."
fi

echo ""
echo "====================================================="
echo " Deploy complete!"
echo "  API URL : http://${DOMAIN}:${APP_PORT}  (or via nginx on :80)"
echo "  Auth key: ${API_KEY}"
echo "  Logs    : journalctl -u ${SERVICE_NAME} -f"
echo "  Config  : ${ENV_FILE}"
echo "====================================================="
echo ""
echo "Next step — add your ChatGPT token:"
echo "  curl -X POST http://localhost:${APP_PORT}/api/accounts \\"
echo "    -H 'Authorization: Bearer ${API_KEY}' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"tokens\":[\"YOUR_CHATGPT_ACCESS_TOKEN\"]}'"
