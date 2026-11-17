"""
Gunicorn config — ensures the APScheduler starts alongside the Flask server.
This file is auto-loaded by gunicorn when using: gunicorn bot:app -c gunicorn_conf.py
"""

import os

# ── Gunicorn Settings ───────────────────────────────────────
bind = f"0.0.0.0:{os.environ.get('PORT', '7860')}"
workers = 1           # Single worker to avoid duplicate schedulers
timeout = 600         # 10 min — video processing + uploads can be very slow
keepalive = 300       # Keep connections alive for 5 minutes (helps HF proxy)
accesslog = "-"       # Log to stdout
errorlog = "-"
graceful_timeout = 30 # Give worker 30s to finish on restart
preload_app = False   # Must be False — post_fork won't fire with preload


def on_starting(server):
    """Called just before the master process is initialized."""
    pass


def post_fork(server, worker):
    """Called after a worker has been forked — start scheduler + self-ping."""
    from bot import start_scheduler, start_self_ping
    start_scheduler()
    start_self_ping()
