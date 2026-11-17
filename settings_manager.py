"""
Settings Manager — dynamic runtime settings persisted in Supabase
(`app_state['settings']`) with a local settings.json fallback.

Secrets (API keys, tokens) remain in environment variables.
Behavioral parameters (intervals, limits, proxy mode, ...) live here so the
dashboard can change them without a redeploy — and they now survive Render
restarts when Supabase is configured.
"""

import logging
import threading

import state_store

log = logging.getLogger(__name__)

STATE_KEY = "settings"
_lock = threading.RLock()

DEFAULT_SETTINGS = {
    "check_interval_minutes": 60,
    "max_uploads_per_channel": 10,
    "uploads_per_channel_per_run": 1,
    "video_max_age_hours": 12,
    "max_short_duration_seconds": 60,
    "custom_hashtags": "#shorts #viral #trending #fyp",
    "ai_primary_provider": "gemini",
    "unlimited_channel": 1,
    "auto_upload_enabled": True,
    "tiktok_usernames": [],
    "proxy_mode": "auto",
    "proxy_urls": [],
    "channel_settings": {},
    # Legacy FFmpeg fields kept so old dashboards/settings files don't break.
    # They are ignored in Zero-FFmpeg mode.
    "speed_factor": 1.05,
    "zoom_percent": 1.02,
    "saturation_boost": 1.1,
    "contrast_boost": 1.05,
    "color_profile": "none",
    "video_preset": "none",
}


def _load_raw() -> dict:
    data = state_store.get(STATE_KEY, {})
    return data if isinstance(data, dict) else {}


def get_all() -> dict:
    """Return merged settings (defaults + stored overrides)."""
    merged = dict(DEFAULT_SETTINGS)
    merged.update(_load_raw())
    return merged


def get(key: str, fallback=None):
    return get_all().get(key, fallback)


def update(updates: dict) -> bool:
    with _lock:
        current = _load_raw()
        current.update(updates)
        success = state_store.set(STATE_KEY, current)
    if success:
        log.info(f"⚙️ Settings updated: {list(updates.keys())}")
    return success


def reset() -> bool:
    with _lock:
        success = state_store.set(STATE_KEY, dict(DEFAULT_SETTINGS))
    if success:
        log.info("⚙️ Settings reset to defaults")
    return success
