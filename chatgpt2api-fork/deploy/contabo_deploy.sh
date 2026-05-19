#!/usr/bin/env bash
# ==============================================================================
# ChatGPT2API — Contabo VPS One-Command Deploy Script
# Usage:
#   export REPO_URL=https://github.com/YOUR_USER/chatgpt2api.git
#   export CHATGPT2API_AUTH_KEY=your_secret_key
#   bash contabo_deploy.sh
#
# Optional:
#   export DOMAIN=api.yourdomain.com   # for Nginx + Let's Encrypt SSL
#   export EMAIL=you@mail.com          # for Let's Encrypt
#
# Tested: Ubuntu 22.04 / 24.04 (Contabo x86_64)
# ==============================================================================
set -euo pipefail

APP_DIR="/opt/chatgpt2api"
SERVICE_NAME="chatgpt2api"
APP_PORT="${APP_PORT:-8000}"
CHATGPT2API_AUTH_KEY="${CHATGPT2API_AUTH_KEY:-}"
REPO_URL="${REPO_URL:-}"

# ── Validate required inputs ─────────────────────────────────────────────────
if [ -z "$CHATGPT2API_AUTH_KEY" ]; then
    echo "ERROR: Set CHATGPT2API_AUTH_KEY before running, e.g.:"
    echo "  export CHATGPT2API_AUTH_KEY=your_secret_key"
    exit 1
fi

if [ -z "$REPO_URL" ] && [ ! -d "$APP_DIR/.git" ]; then
    echo "ERROR: Set REPO_URL to your GitHub fork, e.g.:"
    echo "  export REPO_URL=https://github.com/YOUR_USER/chatgpt2api.git"
    exit 1
fi

echo "====================================================="
echo " ChatGPT2API — Contabo Deploy"
echo "====================================================="

# ── 1. System packages ──────────────────────────────────
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    curl git nginx certbot python3-certbot-nginx \
    build-essential libssl-dev \
    2>/dev/null

# ── 2. Install uv (Python package manager) ─────────────
echo "[2/7] Installing uv..."
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
fi
UV=$(which uv || echo "$HOME/.local/bin/uv")
echo "      uv: $($UV --version)"

# ── 3. Clone / pull repo ────────────────────────────────
echo "[3/7] Setting up application code..."
if [ -d "$APP_DIR/.git" ]; then
    echo "      Pulling latest code..."
    git -C "$APP_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$APP_DIR"
fi

# ── 4. Install Python dependencies ──────────────────────
echo "[4/7] Installing Python dependencies (uv sync)..."
cd "$APP_DIR"
# Override Aliyun mirror with PyPI for international servers
UV_INDEX_URL="https://pypi.org/simple" \
UV_DEFAULT_INDEX="https://pypi.org/simple" \
$UV sync --no-dev --override-values "tool.uv.index=[{url='https://pypi.org/simple',default=true}]" 2>/dev/null || \
$UV sync --no-dev 2>/dev/null || \
UV_INDEX_URL=https://pypi.org/simple $UV sync --no-dev

PYTHON_BIN="$APP_DIR/.venv/bin/python"
echo "      Python: $($PYTHON_BIN --version)"

# ── 5. Config / data dirs ───────────────────────────────
echo "[5/7] Creating data directories and config..."
mkdir -p "$APP_DIR/data"

# Write .env file (read by systemd EnvironmentFile)
ENV_FILE="$APP_DIR/.env"
cat > "$ENV_FILE" <<EOF
# ChatGPT2API environment — edit as needed
CHATGPT2API_AUTH_KEY=${CHATGPT2API_AUTH_KEY}
PORT=${APP_PORT}
EOF
chmod 600 "$ENV_FILE"
echo "      Config written to $ENV_FILE"

# Write minimal config.json if missing (app also reads from env vars)
CONFIG_FILE="$APP_DIR/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
cat > "$CONFIG_FILE" <<EOF
{
  "auth-key": "${CHATGPT2API_AUTH_KEY}"
}
EOF
fi

# Fix permissions
chown -R root:root "$APP_DIR"
chmod -R 755 "$APP_DIR"
chmod 600 "$ENV_FILE" "$CONFIG_FILE"
chmod -R 777 "$APP_DIR/data"

# ── 6. Systemd service ──────────────────────────────────
echo "[6/7] Installing systemd service..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=ChatGPT2API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py
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
for i in $(seq 1 15); do
    sleep 1
    if curl -sf "http://127.0.0.1:${APP_PORT}/v1/models" -H "Authorization: Bearer ${CHATGPT2API_AUTH_KEY}" >/dev/null 2>&1; then
        echo "      Service is responding ✓"
        break
    fi
    if [ "$i" -eq 15 ]; then
        echo ""
        echo "  WARNING: Service not responding after 15s. Check logs:"
        echo "  journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
        systemctl is-active --quiet "$SERVICE_NAME" || echo "  Service status: FAILED"
    fi
done

# ── 7. Nginx reverse proxy ──────────────────────────────
echo "[7/7] Configuring Nginx..."
cat > "/etc/nginx/sites-available/${SERVICE_NAME}" <<EOF
server {
    listen 80;
    server_name ${DOMAIN:-_};

    # Large bodies for image uploads
    client_max_body_size 50M;

    location / {
        proxy_pass         http://127.0.0.1:${APP_PORT};
        proxy_http_version 1.1;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        # Required for streaming / SSE responses
        proxy_buffering           off;
        proxy_read_timeout        300s;
        proxy_send_timeout        300s;
        chunked_transfer_encoding on;
    }
}
EOF

ln -sf "/etc/nginx/sites-available/${SERVICE_NAME}" "/etc/nginx/sites-enabled/${SERVICE_NAME}"
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# Optional SSL with Let's Encrypt
DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
if [ -n "$DOMAIN" ] && [ -n "$EMAIL" ] && [ "$DOMAIN" != "_" ]; then
    echo "      Obtaining SSL certificate for ${DOMAIN}..."
    certbot --nginx -d "$DOMAIN" --email "$EMAIL" --agree-tos --non-interactive --redirect
    echo "      HTTPS enabled ✓"
else
    echo "      SSL skipped (set DOMAIN=yourdomain.com EMAIL=you@mail.com to enable)"
fi

echo ""
echo "====================================================="
echo " Deploy complete!"
echo ""
echo "  Direct URL : http://<server-ip>:${APP_PORT}"
echo "  Nginx URL  : http://<server-ip>:80"
echo "  Auth key   : ${CHATGPT2API_AUTH_KEY}"
echo "  Logs       : journalctl -u ${SERVICE_NAME} -f"
echo "  Restart    : systemctl restart ${SERVICE_NAME}"
echo "====================================================="
echo ""
echo "Next — add your ChatGPT account token:"
echo ""
echo "  curl -X POST http://localhost:${APP_PORT}/api/accounts \\"
echo "    -H 'Authorization: Bearer ${CHATGPT2API_AUTH_KEY}' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"tokens\":[\"YOUR_CHATGPT_ACCESS_TOKEN\"]}'"
echo ""
echo "Then test text chat with chat_id:"
echo ""
cat <<'EXAMPLE'
  python3 - <<'EOF'
import requests, json

BASE = "http://localhost:8000"
KEY  = "YOUR_AUTH_KEY"   # same as CHATGPT2API_AUTH_KEY

# ── Turn 1: new session ──────────────────────────────────
r1 = requests.post(f"{BASE}/v1/chat/completions",
    headers={"Authorization": f"Bearer {KEY}"},
    json={"model": "auto",
          "messages": [{"role": "user", "content": "My name is Rana. Remember it."}]},
    timeout=120)
d1 = r1.json()
chat_id = d1["chat_id"]           # save this UUID
print("GPT:", d1["choices"][0]["message"]["content"])
print("chat_id:", chat_id)

# ── Turn 2: continue same session ───────────────────────
r2 = requests.post(f"{BASE}/v1/chat/completions",
    headers={"Authorization": f"Bearer {KEY}"},
    json={"model": "auto",
          "messages": [{"role": "user", "content": "What is my name?"}],
          "chat_id": chat_id},
    timeout=120)
d2 = r2.json()
print("GPT:", d2["choices"][0]["message"]["content"])   # → "Tumhara naam Rana hai."
EOF
EXAMPLE
