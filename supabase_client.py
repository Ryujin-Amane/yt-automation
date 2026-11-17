"""
Shared Supabase client with lazy init, thread-safety, and automatic retry.

If Supabase is unreachable (paused project, DNS failure, missing keys) every
caller gets `None` and falls back to local storage. Unlike the old version,
a failed connection is retried after a cooldown instead of being disabled
for the whole lifetime of the process.
"""

import time
import logging
import threading

import config

log = logging.getLogger(__name__)

_RETRY_COOLDOWN_SECONDS = 300

_lock = threading.RLock()
_client = None
_last_failure_ts = 0.0
_last_error = ""
_announced_local_mode = False


def is_configured() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_KEY)


def get_client():
    """Return a working Supabase client or None (never raises)."""
    global _client, _last_failure_ts, _last_error, _announced_local_mode

    if not is_configured():
        if not _announced_local_mode:
            log.info("📁 Supabase not configured — using local files/SQLite only (data is lost on Render restarts).")
            _announced_local_mode = True
        return None

    with _lock:
        if _client is not None:
            return _client

        if _last_failure_ts and (time.time() - _last_failure_ts) < _RETRY_COOLDOWN_SECONDS:
            return None

        try:
            from supabase import create_client
            client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
            # Cheap reachability probe
            client.table("app_state").select("key").limit(1).execute()
            _client = client
            _last_error = ""
            log.info("☁️ Connected to Supabase (persistent queue/settings/history enabled).")
            return _client
        except Exception as e:
            _last_failure_ts = time.time()
            _last_error = str(e)[:300]
            hint = ""
            msg = _last_error.lower()
            if "nodename" in msg or "name or service not known" in msg or "getaddrinfo" in msg:
                hint = " (DNS lookup failed — the Supabase project is probably paused/deleted. Restore it in the Supabase dashboard.)"
            elif "app_state" in msg and ("does not exist" in msg or "not find" in msg or "42p01" in msg or "pgrst205" in msg):
                hint = " (Table app_state is missing — run supabase_schema.sql in the Supabase SQL editor.)"
            elif "401" in msg or "jwt" in msg or "permission" in msg or "row-level security" in msg:
                hint = " (Auth/RLS error — use the service_role key for SUPABASE_KEY.)"
            log.warning(f"⚠️ Supabase unavailable: {_last_error}{hint} — retrying in {_RETRY_COOLDOWN_SECONDS // 60} min, using local fallback.")
            return None


def mark_failed(error: Exception | str):
    """Called by users of the client when a request fails mid-flight (forces reconnect later)."""
    global _client, _last_failure_ts, _last_error
    with _lock:
        _client = None
        _last_failure_ts = time.time()
        _last_error = str(error)[:300]


def status() -> dict:
    return {
        "configured": is_configured(),
        "connected": _client is not None,
        "last_error": _last_error,
    }
