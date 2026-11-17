"""
Database module — tracks processed TikTok videos and booked publish slots.

  1. Supabase (PostgreSQL) — persistent across Render restarts (primary when reachable).
  2. Local SQLite (`database.sqlite3`) — always written too; offline fallback.

Reads merge both sources, so dedup and slot-booking keep working even if
Supabase is temporarily down.
"""

import os
import sqlite3
import logging
from datetime import datetime, timezone

import supabase_client

log = logging.getLogger(__name__)

SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.sqlite3")


def _init_sqlite():
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
            cols = [row[1] for row in conn.execute("PRAGMA table_info(processed_videos)").fetchall()]
            if "scheduled_at" not in cols:
                conn.execute("ALTER TABLE processed_videos ADD COLUMN scheduled_at TEXT;")
            conn.commit()
    except Exception as e:
        log.error(f"Failed to initialize SQLite database: {e}")


_init_sqlite()


def _sb():
    return supabase_client.get_client()


def _parse_ts(value: str) -> float:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # SQLite datetime('now') is UTC
        return dt.timestamp()
    except Exception:
        return 0.0


def is_video_processed(tiktok_id: str) -> bool:
    sb = _sb()
    if sb:
        try:
            result = sb.table("processed_videos").select("id").eq("tiktok_id", tiktok_id).limit(1).execute()
            if result.data:
                return True
        except Exception as e:
            supabase_client.mark_failed(e)

    try:
        with sqlite3.connect(SQLITE_PATH) as conn:
            row = conn.execute("SELECT 1 FROM processed_videos WHERE tiktok_id = ?", (tiktok_id,)).fetchone()
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
    ok = False
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
            ok = True
    except Exception as e:
        log.error(f"SQLite insert error: {e}")

    sb = _sb()
    if sb:
        try:
            sb.table("processed_videos").upsert({
                "tiktok_id": tiktok_id,
                "tiktok_user": tiktok_user,
                "youtube_id": youtube_id,
                "channel_used": channel_used,
                "caption": caption,
                "scheduled_at": scheduled_at,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="tiktok_id").execute()
            ok = True
        except Exception as e:
            log.warning(f"Could not sync video {tiktok_id} to Supabase: {e}")
            supabase_client.mark_failed(e)

    if ok:
        log.info(f"✅ Recorded {tiktok_id} → YouTube {youtube_id} (CH{channel_used}, publish={scheduled_at or 'now'})")
    return ok


def get_booked_schedule_slots(channel: int) -> set[str]:
    """ISO UTC timestamps already booked for future publication on this channel."""
    booked: set[str] = set()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    sb = _sb()
    if sb:
        try:
            res = (
                sb.table("processed_videos")
                .select("scheduled_at")
                .eq("channel_used", channel)
                .gte("scheduled_at", now_iso)
                .execute()
            )
            booked.update(str(r["scheduled_at"]).strip() for r in res.data or [] if r.get("scheduled_at"))
        except Exception as e:
            supabase_client.mark_failed(e)

    try:
        with sqlite3.connect(SQLITE_PATH) as conn:
            rows = conn.execute(
                "SELECT scheduled_at FROM processed_videos "
                "WHERE channel_used = ? AND scheduled_at IS NOT NULL AND scheduled_at >= ?",
                (channel, now_iso),
            ).fetchall()
            booked.update(str(r[0]).strip() for r in rows if r and r[0])
    except Exception as e:
        log.debug(f"SQLite booked slots lookup error: {e}")
    return booked


def get_today_upload_count(channel: int) -> int:
    """How many uploads happened today (UTC) on a channel."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    sb_count = 0
    sb = _sb()
    if sb:
        try:
            result = (
                sb.table("processed_videos")
                .select("id", count="exact")
                .eq("channel_used", channel)
                .gte("uploaded_at", today_start.isoformat())
                .execute()
            )
            sb_count = result.count or 0
        except Exception as e:
            supabase_client.mark_failed(e)

    local_count = 0
    try:
        with sqlite3.connect(SQLITE_PATH) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM processed_videos WHERE channel_used = ? AND date(uploaded_at) = date('now')",
                (channel,),
            ).fetchone()
            local_count = row[0] if row else 0
    except Exception as e:
        log.error(f"SQLite count error: {e}")

    return max(sb_count, local_count)


def get_last_upload_time(channel: int) -> float:
    """Unix timestamp of the last upload for a channel, or 0."""
    latest = 0.0
    sb = _sb()
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
            if result.data and result.data[0].get("uploaded_at"):
                latest = max(latest, _parse_ts(result.data[0]["uploaded_at"]))
        except Exception as e:
            supabase_client.mark_failed(e)

    try:
        with sqlite3.connect(SQLITE_PATH) as conn:
            row = conn.execute(
                "SELECT uploaded_at FROM processed_videos WHERE channel_used = ? ORDER BY uploaded_at DESC LIMIT 1",
                (channel,),
            ).fetchone()
            if row and row[0]:
                latest = max(latest, _parse_ts(row[0]))
    except Exception as e:
        log.error(f"SQLite last upload time error: {e}")

    return latest
