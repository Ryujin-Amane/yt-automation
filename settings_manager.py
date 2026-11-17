"""
Settings Manager — provides dynamic runtime settings via a local JSON file.

Secrets (API keys, tokens) remain in environment variables for security.
Behavioral parameters (intervals, limits, processing options) are stored in
settings.json so the web UI can update them without a restart.
"""

import os
import json
import logging
import threading

log = logging.getLogger(__name__)

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
_lock = threading.Lock()

DEFAULT_SETTINGS = {
    "check_interval_minutes": 60,
    "max_uploads_per_channel": 10,
    "uploads_per_channel_per_run": 1,
    "video_max_age_hours": 12,
    "speed_factor": 1.05,
    "zoom_percent": 1.02,
    "saturation_boost": 1.1,
    "contrast_boost": 1.05,
    "custom_hashtags": "#shorts #viral #trending #fyp",
    "ai_primary_provider": "gemini",
    "unlimited_channel": 1,
    "auto_upload_enabled": True,
    "tiktok_usernames": [],
    "color_profile": "none",
    "video_preset": "none",
    "proxy_mode": "auto",
    "proxy_urls": [],
    "channel_settings": {}
}


def _load_raw() -> dict:
    """Read the settings file from disk. Returns empty dict on failure."""
    with _lock:
        if not os.path.exists(SETTINGS_FILE):
            return {}
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.error(f"Failed to read {SETTINGS_FILE}: {exc}")
            return {}


def _save_raw(data: dict) -> bool:
    """Write settings dict to disk atomically."""
    with _lock:
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            return True
        except OSError as exc:
            log.error(f"Failed to write {SETTINGS_FILE}: {exc}")
            return False


def get_all() -> dict:
    """Return merged settings (defaults + overrides from file)."""
    merged = dict(DEFAULT_SETTINGS)
    merged.update(_load_raw())
    return merged


def get(key: str, fallback=None):
    """Return a single setting value with type-safe default fallback."""
    all_settings = get_all()
    return all_settings.get(key, fallback)


def update(updates: dict) -> bool:
    """Merge partial updates into the settings file."""
    current = _load_raw()
    current.update(updates)
    success = _save_raw(current)
    if success:
        log.info(f"⚙️ Settings updated: {list(updates.keys())}")
    return success


def reset() -> bool:
    """Reset all settings to their defaults."""
    success = _save_raw(dict(DEFAULT_SETTINGS))
    if success:
        log.info("⚙️ Settings reset to defaults")
    return success
