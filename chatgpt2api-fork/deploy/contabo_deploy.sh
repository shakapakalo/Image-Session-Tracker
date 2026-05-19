#!/usr/bin/env bash
# ==============================================================================
# ChatGPT2API — Contabo VPS One-Command Deploy Script
# Usage:
#   export CHATGPT2API_AUTH_KEY="your_secret_key"
#   export REPO_URL="https://github.com/shakapakalo/Image-Session-Tracker.git"
#   bash <(curl -fsSL https://raw.githubusercontent.com/shakapakalo/Image-Session-Tracker/main/chatgpt2api-fork/deploy/contabo_deploy.sh)
#
# Optional:
#   export DOMAIN=api.yourdomain.com   # for Nginx + SSL
#   export EMAIL=you@mail.com          # for Let's Encrypt
#
# Tested: Ubuntu 22.04 / 24.04 (Contabo x86_64)
# ==============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/shakapakalo/Image-Session-Tracker.git}"
CLONE_DIR="/opt/chatgpt2api-repo"
APP_DIR="${CLONE_DIR}/chatgpt2api-fork"   # Python app lives here
SERVICE_NAME="chatgpt2api"
APP_PORT="${APP_PORT:-7637}"
CHATGPT2API_AUTH_KEY="${CHATGPT2API_AUTH_KEY:-}"

# ── Validate required inputs ─────────────────────────────────────────────────
if [ -z "$CHATGPT2API_AUTH_KEY" ]; then
    echo "ERROR: Set CHATGPT2API_AUTH_KEY before running:"
    echo "  export CHATGPT2API_AUTH_KEY=your_secret_key"
    exit 1
fi

echo "====================================================="
echo " ChatGPT2API — Contabo Deploy"
echo " Repo   : $REPO_URL"
echo " App dir: $APP_DIR"
echo "====================================================="

# ── 1. System packages ──────────────────────────────────
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    curl git nginx certbot python3-certbot-nginx \
    build-essential libssl-dev ca-certificates 2>/dev/null

# ── 2. Install uv ──────────────────────────────────────
echo "[2/7] Installing uv (Python package manager)..."
if ! command -v uv &>/dev/null && [ ! -f "$HOME/.local/bin/uv" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/root/.local/bin:$PATH"
UV=$(command -v uv || echo "$HOME/.local/bin/uv")
echo "      uv: $($UV --version)"

# ── 3. Clone / update repo ─────────────────────────────
echo "[3/7] Setting up application code..."
if [ -d "$CLONE_DIR/.git" ]; then
    echo "      Pulling latest code..."
    git -C "$CLONE_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$CLONE_DIR"
fi

if [ ! -f "$APP_DIR/main.py" ]; then
    echo "ERROR: main.py not found at $APP_DIR"
    echo "       Repo structure may have changed. Files found:"
    ls "$CLONE_DIR"
    exit 1
fi

# ── 4. Install Python 3.13 + dependencies ──────────────
echo "[4/7] Installing Python 3.13 + dependencies..."
cd "$APP_DIR"
$UV python install 3.13
$UV sync --no-dev
echo "      Python: $($UV run python --version)"

# ── 4b. Recreate web_dist symlink ──────────────────────
echo "      Setting up web panel..."
if [ -d "$APP_DIR/web/out" ]; then
    ln -sfn web/out "$APP_DIR/web_dist"
    echo "      web_dist → web/out ✓"
else
    echo "      WARNING: web/out not found, panel may not load"
fi

# ── 5. Config + data dirs ──────────────────────────────
echo "[5/7] Creating config and data directories..."
mkdir -p "$APP_DIR/data"
chmod 777 "$APP_DIR/data"

ENV_FILE="$APP_DIR/.env"
cat > "$ENV_FILE" <<EOF
CHATGPT2API_AUTH_KEY=${CHATGPT2API_AUTH_KEY}
PORT=${APP_PORT}
EOF
chmod 600 "$ENV_FILE"

CONFIG_FILE="$APP_DIR/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" <<EOF
{
  "auth-key": "${CHATGPT2API_AUTH_KEY}"
}
EOF
fi

echo "      Config: $ENV_FILE"

# ── 6. Systemd service ─────────────────────────────────
echo "[6/7] Installing systemd service..."
UV_VENV_PYTHON="$APP_DIR/.venv/bin/python"

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=ChatGPT2API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${UV_VENV_PYTHON} ${APP_DIR}/main.py
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

echo "      Waiting for service to start..."
READY=0
for i in $(seq 1 20); do
    sleep 1
    if curl -sf "http://127.0.0.1:${APP_PORT}/v1/models" \
        -H "Authorization: Bearer ${CHATGPT2API_AUTH_KEY}" >/dev/null 2>&1; then
        echo "      Service is responding ✓ (${i}s)"
        READY=1
        break
    fi
done

if [ "$READY" -eq 0 ]; then
    echo "  WARNING: Service not responding after 20s. Check logs:"
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager
fi

# ── 7. Nginx ───────────────────────────────────────────
echo "[7/7] Configuring Nginx..."
cat > "/etc/nginx/sites-available/${SERVICE_NAME}" <<EOF
server {
    listen 80;
    server_name ${DOMAIN:-_};
    client_max_body_size 50M;

    location / {
        proxy_pass         http://127.0.0.1:${APP_PORT};
        proxy_http_version 1.1;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_buffering           off;
        proxy_read_timeout        300s;
        proxy_send_timeout        300s;
        chunked_transfer_encoding on;
    }
}
EOF

ln -sf "/etc/nginx/sites-available/${SERVICE_NAME}" \
       "/etc/nginx/sites-enabled/${SERVICE_NAME}"
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
if [ -n "$DOMAIN" ] && [ -n "$EMAIL" ] && [ "$DOMAIN" != "_" ]; then
    certbot --nginx -d "$DOMAIN" --email "$EMAIL" \
            --agree-tos --non-interactive --redirect
    echo "      HTTPS enabled for $DOMAIN ✓"
fi

# ── Done ───────────────────────────────────────────────
echo ""
echo "====================================================="
echo " DEPLOY COMPLETE"
echo ""
echo "  API Port  : http://$(hostname -I | awk '{print $1}'):${APP_PORT}"
echo "  Nginx     : http://$(hostname -I | awk '{print $1}'):80"
echo "  Auth Key  : ${CHATGPT2API_AUTH_KEY}"
echo "  App Dir   : ${APP_DIR}"
echo "  Logs      : journalctl -u ${SERVICE_NAME} -f"
echo "====================================================="
echo ""
echo "NEXT STEP — Add your ChatGPT token:"
echo ""
echo "  curl -X POST http://localhost:${APP_PORT}/api/accounts \\"
echo "    -H 'Authorization: Bearer ${CHATGPT2API_AUTH_KEY}' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"tokens\":[\"YOUR_CHATGPT_ACCESS_TOKEN\"]}'"
echo ""
echo "Then test:"
echo "  curl http://localhost:${APP_PORT}/v1/models \\"
echo "    -H 'Authorization: Bearer ${CHATGPT2API_AUTH_KEY}'"
