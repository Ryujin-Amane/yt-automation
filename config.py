"""
Configuration module — loads secrets from environment variables and
behavioural parameters from settings_manager (settings.json) so they
can be changed at runtime through the web dashboard.
"""

import os
import json
import re
from dotenv import load_dotenv

load_dotenv()

# ── Supabase ──────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# ── Groq API Key ───────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

# ── YouTube API Key ────────────────────────────────────────
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

# ── Gemini API Key ─────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()


# ── Flask Keep-Alive ──────────────────────────────────────
PORT = int(os.getenv("PORT", "7860"))

# ── Temp directory for video downloads ────────────────────
TEMP_DIR = os.getenv("TEMP_DIR", "/tmp/yt-automation")
os.makedirs(TEMP_DIR, exist_ok=True)

# ── Optional: Path to an outro video ─────────────────────
OUTRO_VIDEO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outro.mp4")


# ═══════════════════════════════════════════════════════════
# Dynamic settings — read from settings_manager at call time
# so changes via the dashboard take effect immediately.
# ═══════════════════════════════════════════════════════════

def _sm():
    """Lazy import to avoid circular imports at module load time."""
    import settings_manager
    return settings_manager


# ── TikTok Accounts to Monitor ───────────────────────────
@property
def _tiktok_usernames():
    pass  # Replaced by the function below

def _get_tiktok_usernames() -> list[str]:
    """Return TikTok usernames from settings, falling back to env var."""
    sm_names = _sm().get("tiktok_usernames", [])
    if sm_names:
        return sm_names
    return [u.strip() for u in os.getenv("TIKTOK_USERNAMES", "").split(",") if u.strip()]

# Module-level properties are not possible, so we use a wrapper.
# Other modules should use config.TIKTOK_USERNAMES which we keep as
# a dynamic descriptor via __getattr__.
_STATIC_TIKTOK_USERNAMES = [
    u.strip() for u in os.getenv("TIKTOK_USERNAMES", "").split(",") if u.strip()
]

class _ConfigModule:
    """Descriptor wrapper that makes dynamic settings look like module attributes."""
    pass

# We expose dynamic getters so other modules that do `config.CHECK_INTERVAL_MINUTES`
# keep working seamlessly.

def get_check_interval_minutes() -> int:
    return int(_sm().get("check_interval_minutes", os.getenv("CHECK_INTERVAL_MINUTES", 60)))

def get_max_uploads_per_channel() -> int:
    return int(_sm().get("max_uploads_per_channel", os.getenv("MAX_UPLOADS_PER_CHANNEL", 10)))

def get_unlimited_channel() -> int:
    return int(_sm().get("unlimited_channel", os.getenv("UNLIMITED_CHANNEL", 1)))

def get_uploads_per_channel_per_run() -> int:
    return int(_sm().get("uploads_per_channel_per_run", os.getenv("UPLOADS_PER_CHANNEL_PER_RUN", 1)))

def get_video_max_age_hours() -> int:
    return int(_sm().get("video_max_age_hours", os.getenv("VIDEO_MAX_AGE_HOURS", 12)))

def get_speed_factor() -> float:
    return float(_sm().get("speed_factor", os.getenv("SPEED_FACTOR", 1.05)))

def get_zoom_percent() -> float:
    return float(_sm().get("zoom_percent", os.getenv("ZOOM_PERCENT", 1.02)))

def get_saturation_boost() -> float:
    return float(_sm().get("saturation_boost", os.getenv("SATURATION_BOOST", 1.1)))

def get_contrast_boost() -> float:
    return float(_sm().get("contrast_boost", os.getenv("CONTRAST_BOOST", 1.05)))

def get_custom_hashtags() -> str:
    return str(_sm().get("custom_hashtags", os.getenv("CUSTOM_HASHTAGS", "#shorts #viral #trending #fyp"))).strip()

def get_ai_primary_provider() -> str:
    return str(_sm().get("ai_primary_provider", os.getenv("AI_PRIMARY_PROVIDER", "gemini"))).strip().lower()

def get_auto_upload_enabled() -> bool:
    return bool(_sm().get("auto_upload_enabled", True))

def get_color_profile() -> str:
    return str(_sm().get("color_profile", "none")).strip().lower()

def get_video_preset() -> str:
    return str(_sm().get("video_preset", "none")).strip().lower()

def get_channel_settings(channel: int) -> dict:
    """Return specific settings for a channel, falling back to global defaults."""
    ch_str = str(channel)
    settings = _sm().get("channel_settings", {})
    ch_config = settings.get(ch_str, {})
    
    return {
        "max_uploads_per_day": int(ch_config.get("max_uploads_per_day", get_max_uploads_per_channel())),
        "uploads_per_run": int(ch_config.get("uploads_per_run", get_uploads_per_channel_per_run())),
        "upload_interval_minutes": int(ch_config.get("upload_interval_minutes", get_check_interval_minutes())),
    }


# ── Backwards-compatible module-level attributes ─────────
# These are read once at import but the dynamic getters above
# are what bot.py / other modules should migrate to.
TIKTOK_USERNAMES = _STATIC_TIKTOK_USERNAMES

CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "60"))
MAX_UPLOADS_PER_CHANNEL = int(os.getenv("MAX_UPLOADS_PER_CHANNEL", "10"))
UNLIMITED_CHANNEL = int(os.getenv("UNLIMITED_CHANNEL", "1"))
UPLOADS_PER_CHANNEL_PER_RUN = int(os.getenv("UPLOADS_PER_CHANNEL_PER_RUN", "1"))
VIDEO_MAX_AGE_HOURS = int(os.getenv("VIDEO_MAX_AGE_HOURS", "12"))
SPEED_FACTOR = float(os.getenv("SPEED_FACTOR", "1.05"))
ZOOM_PERCENT = float(os.getenv("ZOOM_PERCENT", "1.02"))
SATURATION_BOOST = float(os.getenv("SATURATION_BOOST", "1.1"))
CONTRAST_BOOST = float(os.getenv("CONTRAST_BOOST", "1.05"))
CUSTOM_HASHTAGS = os.getenv("CUSTOM_HASHTAGS", "#shorts #viral #trending #fyp").strip()
AI_PRIMARY_PROVIDER = os.getenv("AI_PRIMARY_PROVIDER", "gemini").strip().lower()


def get_available_channels() -> list[int]:
    """Find all configured YouTube channels from local token files or env vars."""
    import logging
    _log = logging.getLogger(__name__)

    channels = set()
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # 1. Scan for local token_channel_*.json files
    for ch_num in range(1, 11):
        token_file = os.path.join(base_dir, f"token_channel_{ch_num}.json")
        if os.path.exists(token_file):
            if get_youtube_client_secret(ch_num) and get_youtube_token(ch_num):
                channels.add(ch_num)

    # 2. Scan env vars for YOUTUBE_TOKEN_*
    for key in sorted(os.environ):
        match = re.match(r"^YOUTUBE_TOKEN_(\d+)$", key)
        if match:
            channel_num = int(match.group(1))
            if get_youtube_client_secret(channel_num) and get_youtube_token(channel_num):
                channels.add(channel_num)

    found = sorted(list(channels))
    _log.info(f"📺 Available YouTube channels: {found}")
    return found


def get_youtube_token(channel_number: int) -> dict | None:
    """Load YouTube token from local token_channel_X.json or env var."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    token_file = os.path.join(base_dir, f"token_channel_{channel_number}.json")
    if os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    raw = os.getenv(f"YOUTUBE_TOKEN_{channel_number}", "")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None


def get_youtube_client_secret(channel_number: int) -> dict | None:
    """Load YouTube client secret from local client_secret.json or env var."""
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # Check channel-specific client secret file or common client_secret.json
    for filename in [f"client_secret_{channel_number}.json", "client_secret.json"]:
        path = os.path.join(base_dir, filename)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

    raw = os.getenv(f"YOUTUBE_CLIENT_SECRET_{channel_number}", "")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

    return None

