FROM python:3.11-slim

LABEL maintainer="yt-automation"

# ── Install lightweight system utilities + Cloudflare WARP userspace tools (wgcf + wireproxy) ──
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates tar && \
    ARCH="$(dpkg --print-architecture)" && \
    if [ "$ARCH" = "amd64" ]; then \
        WGCF_ARCH="amd64"; \
        WIREPROXY_ARCH="amd64"; \
    elif [ "$ARCH" = "arm64" ]; then \
        WGCF_ARCH="arm64"; \
        WIREPROXY_ARCH="arm64"; \
    else \
        WGCF_ARCH="amd64"; \
        WIREPROXY_ARCH="amd64"; \
    fi && \
    curl -fsSL "https://github.com/ViRb3/wgcf/releases/download/v2.2.22/wgcf_2.2.22_linux_${WGCF_ARCH}" -o /usr/local/bin/wgcf && \
    chmod +x /usr/local/bin/wgcf && \
    curl -fsSL "https://github.com/pufferffish/wireproxy/releases/download/v1.0.9/wireproxy_linux_${WIREPROXY_ARCH}.tar.gz" -o /tmp/wireproxy.tar.gz && \
    tar -xzf /tmp/wireproxy.tar.gz -C /usr/local/bin wireproxy && \
    chmod +x /usr/local/bin/wireproxy && \
    rm -f /tmp/wireproxy.tar.gz && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ── Set working directory ──────────────────────────────────
WORKDIR /app

# ── Install Python dependencies ───────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy application code ─────────────────────────────────
COPY . .

# ── Prepare temp directories and permissions ──────────────
RUN chmod +x /app/entrypoint.sh && \
    mkdir -p /tmp/yt-automation /tmp/warp && \
    useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app /tmp/yt-automation /tmp/warp

USER appuser

# Render sets $PORT dynamically (default 10000 on Render, 7860 locally)
EXPOSE 10000
EXPOSE 7860

# ── Dynamic PORT Healthcheck for Render & Docker ──────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.environ.get('PORT', '7860'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=5)" || exit 1

# ── Launch WARP userspace proxy + Gunicorn + APScheduler ──
CMD ["/app/entrypoint.sh"]
