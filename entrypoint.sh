#!/bin/bash
set -e

echo "============================================================"
echo "🚀 Starting AutoTube AI (Render 24/7 Lightweight Edition)"
echo "============================================================"

# Cloudflare WARP is started (and health-checked) from Python by
# proxy_manager.ensure_warp_running() on the first TikTok request, so there is
# a single code path with retries/cooldowns. Set ENABLE_WARP=false to disable.

mkdir -p /tmp/yt-automation /tmp/warp

exec gunicorn bot:app -c gunicorn_conf.py
