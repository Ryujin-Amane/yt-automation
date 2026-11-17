#!/bin/bash
set -e

echo "============================================================"
echo "🚀 Starting AutoTube AI (Render 24/7 Lightweight Edition)"
echo "============================================================"

# ── Layer: Cloudflare WARP Userspace VPN Proxy (wireproxy) ────
if [ "${ENABLE_WARP:-true}" = "true" ] && command -v wireproxy >/dev/null 2>&1; then
    echo "🛡️ Initializing Cloudflare WARP userspace proxy (wireproxy)..."
    mkdir -p /tmp/warp
    cd /tmp/warp

    if [ ! -f "/tmp/warp/wireproxy.conf" ]; then
        if [ -n "${WARP_PRIVATE_KEY:-}" ]; then
            echo "🔑 Using pre-configured WARP_PRIVATE_KEY from environment..."
            cat > /tmp/warp/wireproxy.conf <<EOF
[Interface]
Address = ${WARP_ADDRESS_IPV4:-172.16.0.2/32}
PrivateKey = ${WARP_PRIVATE_KEY}
DNS = 1.1.1.1
MTU = 1280

[Peer]
PublicKey = bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=
Endpoint = engage.cloudflareclient.com:2408
KeepAlive = 25

[Socks5]
BindAddress = 127.0.0.1:1080

[http]
BindAddress = 127.0.0.1:1081
EOF
        elif command -v wgcf >/dev/null 2>&1; then
            echo "🌐 Registering free Cloudflare WARP profile via wgcf..."
            if wgcf register --accept-tos >/dev/null 2>&1 && wgcf generate >/dev/null 2>&1; then
                cp wgcf-profile.conf /tmp/warp/wireproxy.conf
                cat >> /tmp/warp/wireproxy.conf <<EOF

[Socks5]
BindAddress = 127.0.0.1:1080

[http]
BindAddress = 127.0.0.1:1081
EOF
                echo "✅ Generated WARP WireGuard profile successfully."
            else
                echo "⚠️ wgcf registration rate-limited or unavailable; continuing with fallback proxy/TikWM layers."
            fi
        fi
    fi

    if [ -f "/tmp/warp/wireproxy.conf" ]; then
        wireproxy -c /tmp/warp/wireproxy.conf >/tmp/warp/wireproxy.log 2>&1 &
        WP_PID=$!
        sleep 2
        if curl -fsSL --proxy http://127.0.0.1:1081 --max-time 6 https://www.cloudflare.com/cdn-cgi/trace >/tmp/warp/trace.txt 2>/dev/null; then
            echo "✅ wireproxy active and verified (SOCKS5: 127.0.0.1:1080, HTTP: 127.0.0.1:1081)"
            grep -E "^(ip|warp|loc)=" /tmp/warp/trace.txt || true
        else
            echo "⚠️ wireproxy tunnel handshake failed — stopping wireproxy and using fallback layers."
            kill "$WP_PID" 2>/dev/null || true
        fi
    fi

    cd /app
else
    echo "ℹ️ Built-in WARP skipped (ENABLE_WARP=${ENABLE_WARP:-true}). Using Custom Proxy / TLS Impersonation / TikWM layers."
fi

# ── Start Gunicorn Web Server + APScheduler + Self-Pinger ─────
exec gunicorn bot:app -c gunicorn_conf.py
