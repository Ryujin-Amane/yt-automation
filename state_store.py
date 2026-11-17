"""
Persistent key/value state store (queue, settings, peak schedules).

Storage layers:
  1. In-memory cache  — authoritative inside the single gunicorn worker (fast).
  2. Supabase `app_state` table (jsonb) — survives Render restarts/redeploys.
  3. Local JSON file  — offline fallback + seed source for first Supabase sync.

Reads:  memory → Supabase → local file → default
Writes: memory + local file (always) + Supabase (best-effort; re-synced later if it failed)
"""

import os
import json
import copy
import logging
import threading
from datetime import datetime, timezone

import supabase_client

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_lock = threading.RLock()
_cache: dict[str, object] = {}
_dirty_remote: set[str] = set()   # keys whose latest value hasn't reached Supabase yet


def _local_path(key: str) -> str:
    return os.path.join(BASE_DIR, f"{key}.json")


def _read_local(key: str):
    path = _local_path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.error(f"Failed to read {path}: {exc}")
        return None


def _write_local(key: str, value) -> bool:
    path = _local_path(key)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2)
        os.replace(tmp, path)  # atomic
        return True
    except OSError as exc:
        log.error(f"Failed to write {path}: {exc}")
        return False


def _read_remote(key: str):
    """Return (found: bool, value)."""
    client = supabase_client.get_client()
    if client is None:
        return False, None
    try:
        res = client.table("app_state").select("value").eq("key", key).limit(1).execute()
        if res.data:
            return True, res.data[0]["value"]
        return False, None
    except Exception as exc:
        log.warning(f"Supabase read failed for '{key}': {exc}")
        supabase_client.mark_failed(exc)
        return False, None


def _write_remote(key: str, value) -> bool:
    client = supabase_client.get_client()
    if client is None:
        return False
    try:
        client.table("app_state").upsert({
            "key": key,
            "value": value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="key").execute()
        return True
    except Exception as exc:
        log.warning(f"Supabase write failed for '{key}': {exc}")
        supabase_client.mark_failed(exc)
        return False


def get(key: str, default=None):
    """Return a deep copy of the stored value (safe for callers to mutate)."""
    with _lock:
        if key in _cache:
            return copy.deepcopy(_cache[key])

        found, value = _read_remote(key)
        if found:
            _cache[key] = value
            _write_local(key, value)
            return copy.deepcopy(value)

        local = _read_local(key)
        if local is not None:
            _cache[key] = local
            # First boot with Supabase: seed it from the local file
            if supabase_client.get_client() is not None:
                if _write_remote(key, local):
                    log.info(f"☁️ Seeded Supabase app_state['{key}'] from local {key}.json")
                else:
                    _dirty_remote.add(key)
            return copy.deepcopy(local)

        return copy.deepcopy(default)


def set(key: str, value) -> bool:  # noqa: A001 - intentional module API
    with _lock:
        _cache[key] = copy.deepcopy(value)
        local_ok = _write_local(key, value)
        remote_ok = _write_remote(key, value)
        if remote_ok:
            _dirty_remote.discard(key)
        elif supabase_client.is_configured():
            _dirty_remote.add(key)
        return local_ok or remote_ok


def sync_pending() -> int:
    """Push any values that failed to reach Supabase earlier. Returns number synced."""
    with _lock:
        if not _dirty_remote or supabase_client.get_client() is None:
            return 0
        synced = 0
        for key in list(_dirty_remote):
            if key in _cache and _write_remote(key, _cache[key]):
                _dirty_remote.discard(key)
                synced += 1
        if synced:
            log.info(f"☁️ Re-synced {synced} state key(s) to Supabase")
        return synced


def invalidate(key: str | None = None):
    with _lock:
        if key is None:
            _cache.clear()
        else:
            _cache.pop(key, None)


def status() -> dict:
    return {
        **supabase_client.status(),
        "pending_sync_keys": sorted(_dirty_remote),
    }
