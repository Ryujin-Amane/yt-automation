"""
Database module — tracks which TikTok videos have been processed and which
future publish time slots are already booked per YouTube channel.

Supports both:
  1. Local SQLite database (`database.sqlite3`) — Zero setup, runs 100% offline, never pauses.
  2. Supabase (PostgreSQL) — Cloud database (if active and reachable, persists across Render restarts).

If Supabase is paused or unreachable, it automatically falls back to local SQLite
so your bot never crashes or halts.
"""

import os
import sqlite3
import logging
from datetime import datetime, timezone
import config

log = logging.getLogger(__name__)

SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.sqlite3")

_supabase_client = None
_supabase_tested = False
_supabase_working = False


def _init_sqlite():
    """Ensure local SQLite database, table, and scheduled_at column exist."""
    try:
        with sqlite3.connect(SQLITE_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tiktok_id TEXT UNIQUE NOT NULL,
                    tiktok_user TEXT NOT NULL,
                    youtube_id TEXT,
                    channel_used INTEGER DEFAULT 1,
                    caption TEXT,
                    scheduled_at TEXT,
                    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Auto-migrate existing database if scheduled_at column wasn't present
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(processed_videos)")
            cols = [row[1] for row in cur.fetchall()]
            if "scheduled_at" not in cols:
                conn.execute("ALTER TABLE processed_videos ADD COLUMN scheduled_at TEXT;")
            conn.commit()
    except Exception as e:
        log.error(f"Failed to initialize SQLite database: {e}")


# Initialize SQLite on import
_init_sqlite()


def _get_supabase_client():
    """Lazy-init Supabase client with reachability check."""
    global _supabase_client, _supabase_tested, _supabase_working
    if _supabase_tested:
        return _supabase_client if _supabase_working else None

    _supabase_tested = True
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        log.info("📁 Supabase not configured. Using local SQLite database (database.sqlite3).")
        _supabase_working = False
        return None

    try:
        from supabase import create_client
        client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
        client.table("processed_videos").select("id").limit(1).execute()
        _supabase_client = client
        _supabase_working = True
        log.info("☁️ Connected to Supabase cloud database.")
        return _supabase_client
    except Exception as e:
        log.warning(f"⚠️ Supabase unreachable or paused ({e}).")
        log.info("📁 Seamlessly falling back to local SQLite database (database.sqlite3).")
        _supabase_working = False
        return None


def is_video_processed(tiktok_id: str) -> bool:
    """Check if a TikTok video ID already exists in the database."""
    sb = _get_supabase_client()
    if sb:
        try:
            result = sb.table("processed_videos").select("id").eq("tiktok_id", tiktok_id).execute()
            if len(result.data) > 0:
                return True
        except Exception:
            pass

    try:
        with sqlite3.connect(SQLITE_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM processed_videos WHERE tiktok_id = ?", (tiktok_id,))
            row = cur.fetchone()
            return row is not None
    except Exception as e:
        log.error(f"SQLite check error: {e}")
        return False


def mark_video_processed(
    tiktok_id: str,
    tiktok_user: str,
    youtube_id: str,
    channel_used: int,
    caption: str = "",
    scheduled_at: str | None = None,
) -> bool:
    """Record a processed video in the database (SQLite + Supabase)."""
    sqlite_ok = False
    try:
        with sqlite3.connect(SQLITE_PATH) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO processed_videos 
                (tiktok_id, tiktok_user, youtube_id, channel_used, caption, scheduled_at, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (tiktok_id, tiktok_user, youtube_id, channel_used, caption, scheduled_at),
            )
            conn.commit()
            sqlite_ok = True
            log.info(
                f"✅ [SQLite] Recorded video {tiktok_id} → YouTube {youtube_id} "
                f"(CH{channel_used}, scheduled={scheduled_at or 'immediate'})"
            )
    except Exception as e:
        log.error(f"SQLite insert error: {e}")

    sb = _get_supabase_client()
    if sb:
        try:
            payload = {
                "tiktok_id": tiktok_id,
                "tiktok_user": tiktok_user,
                "youtube_id": youtube_id,
                "channel_used": channel_used,
                "caption": caption,
            }
            sb.table("processed_videos").insert(payload).execute()
            log.info(f"☁️ [Supabase] Synced video {tiktok_id} → YouTube {youtube_id}")
        except Exception as e:
            log.warning(f"Could not sync to Supabase: {e}")

    return sqlite_ok


def get_booked_schedule_slots(channel: int) -> set[str]:
    """Return a set of ISO UTC timestamps already booked for future publication on this channel."""
    booked = set()
    try:
        with sqlite3.connect(SQLITE_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT scheduled_at FROM processed_videos
                WHERE channel_used = ? AND scheduled_at IS NOT NULL AND scheduled_at != ''
                """,
                (channel,),
            )
            for row in cur.fetchall():
                if row and row[0]:
                    booked.add(str(row[0]).strip())
    except Exception as e:
        log.debug(f"SQLite booked slots lookup error: {e}")
    return booked


def get_today_upload_count(channel: int) -> int:
    """Return how many videos were uploaded today for a specific channel."""
    sb = _get_supabase_client()
    if sb:
        try:
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            result = (
                sb.table("processed_videos")
                .select("id", count="exact")
                .eq("channel_used", channel)
                .gte("uploaded_at", today_start)
                .execute()
            )
            return result.count or 0
        except Exception:
            pass

    try:
        with sqlite3.connect(SQLITE_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) FROM processed_videos
                WHERE channel_used = ? AND date(uploaded_at) = date('now')
                """,
                (channel,),
            )
            row = cur.fetchone()
            return row[0] if row else 0
    except Exception as e:
        log.error(f"SQLite count error: {e}")
        return 0


def get_last_upload_time(channel: int) -> float:
    """Return the timestamp of the last upload for a specific channel, or 0 if none."""
    sb = _get_supabase_client()
    if sb:
        try:
            result = (
                sb.table("processed_videos")
                .select("uploaded_at")
                .eq("channel_used", channel)
                .order("uploaded_at", desc=True)
                .limit(1)
                .execute()
            )
            if result.data and len(result.data) > 0:
                uploaded_at_str = result.data[0].get("uploaded_at")
                if uploaded_at_str:
                    dt = datetime.fromisoformat(uploaded_at_str.replace("Z", "+00:00"))
                    return dt.timestamp()
        except Exception:
            pass

    try:
        with sqlite3.connect(SQLITE_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT uploaded_at FROM processed_videos
                WHERE channel_used = ?
                ORDER BY uploaded_at DESC LIMIT 1
                """,
                (channel,),
            )
            row = cur.fetchone()
            if row and row[0]:
                dt = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                return dt.timestamp()
    except Exception as e:
        log.error(f"SQLite last upload time error: {e}")

    return 0.0
