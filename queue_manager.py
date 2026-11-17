"""
Queue Manager — stores fetched TikTok videos in a persistent queue
(Supabase `app_state['queue']` with local queue.json fallback) so the web
dashboard can inspect, assign target channels, and track per-channel status.

All read-modify-write operations hold a single re-entrant lock so concurrent
fetch / upload / dashboard threads can never overwrite each other's changes.
"""

import uuid
import logging
import threading
from datetime import datetime, timezone, timedelta

import state_store

log = logging.getLogger(__name__)

STATE_KEY = "queue"
_lock = threading.RLock()

# Uploaded/failed items older than this are pruned automatically to keep the
# Supabase row small (dedup is guaranteed separately by processed_videos).
_PRUNE_AFTER_DAYS = 7


def _load() -> list[dict]:
    data = state_store.get(STATE_KEY, [])
    return data if isinstance(data, list) else []


def _save(queue: list[dict]) -> bool:
    return state_store.set(STATE_KEY, queue)


def _recalc_status(item: dict):
    c_status = item.get("channel_status", {}) or {}
    vals = list(c_status.values())
    if not vals:
        item["status"] = "pending"
    elif all(s == "uploaded" for s in vals):
        item["status"] = "uploaded"
    elif any(s == "uploading" for s in vals):
        item["status"] = "uploading"
    elif any(s == "failed" for s in vals):
        item["status"] = "failed"
    else:
        item["status"] = "pending"


def get_all() -> list[dict]:
    with _lock:
        return _load()


def add_video(video: dict) -> bool:
    with _lock:
        queue = _load()
        tiktok_id = str(video.get("tiktok_id", "")).strip()
        if not tiktok_id:
            return False

        if any(item.get("tiktok_id") == tiktok_id for item in queue):
            log.debug(f"Video {tiktok_id} already in queue, skipping")
            return False

        entry = {
            "queue_id": uuid.uuid4().hex[:12],
            "tiktok_id": tiktok_id,
            "author": video.get("author", ""),
            "caption": video.get("caption", ""),
            "play_url": video.get("play_url", ""),
            "duration": video.get("duration", 0),
            "width": video.get("width", 0),
            "height": video.get("height", 0),
            "cover": video.get("cover", ""),
            "timestamp": video.get("timestamp", 0),
            "source": video.get("source", ""),
            "target_channels": video.get("target_channels", []),  # Empty list = Auto
            "channel_status": {},  # e.g. {"1": "pending", "2": "uploaded"}
            "status": "pending",
            "added_at": datetime.now(timezone.utc).isoformat(),
            "error": "",
        }

        queue.append(entry)
        success = _save(queue)
        if success:
            log.info(f"📥 Queued video {tiktok_id} by @{entry['author']}")
        return success


def update_video(queue_id: str, updates: dict) -> bool:
    with _lock:
        queue = _load()
        for item in queue:
            if item.get("queue_id") == queue_id:
                item.update(updates)
                if "channel_status" in updates:
                    _recalc_status(item)
                return _save(queue)
        return False


def mark_channel_status(queue_id: str, channel: int, status: str, error: str = "") -> bool:
    """Update the status for a specific channel inside a queued video."""
    with _lock:
        queue = _load()
        for item in queue:
            if item.get("queue_id") == queue_id:
                c_status = item.get("channel_status", {}) or {}
                c_status[str(channel)] = status
                item["channel_status"] = c_status
                _recalc_status(item)
                item["error"] = error if error else ("" if status != "failed" else item.get("error", ""))
                item["updated_at"] = datetime.now(timezone.utc).isoformat()
                return _save(queue)
        return False


def claim_for_channel(queue_id: str, channel: int) -> bool:
    """
    Atomically mark a video as 'uploading' for a channel, but only if it is
    still pending there. Prevents two threads from uploading the same video.
    """
    with _lock:
        queue = _load()
        for item in queue:
            if item.get("queue_id") != queue_id:
                continue
            c_status = item.get("channel_status", {}) or {}
            if c_status.get(str(channel), "pending") != "pending":
                return False
            targets = item.get("target_channels") or []
            if targets and channel not in targets:
                return False
            if not targets:
                item["target_channels"] = [channel]
            c_status[str(channel)] = "uploading"
            item["channel_status"] = c_status
            _recalc_status(item)
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            return _save(queue)
        return False


def remove_video(queue_id: str) -> bool:
    with _lock:
        queue = _load()
        new_queue = [item for item in queue if item.get("queue_id") != queue_id]
        if len(new_queue) == len(queue):
            return False
        return _save(new_queue)


def get_pending(target_channel: int = 0) -> list[dict]:
    """Return all videos that are pending overall, or pending for a specific channel."""
    with _lock:
        queue = _load()
    if target_channel == 0:
        return [item for item in queue if item.get("status") == "pending"]

    ch_str = str(target_channel)
    pending = []
    for item in queue:
        targets = item.get("target_channels", [])
        c_status = item.get("channel_status", {})
        if targets:
            if target_channel in targets and c_status.get(ch_str, "pending") == "pending":
                pending.append(item)
        elif item.get("status") == "pending" and c_status.get(ch_str, "pending") == "pending":
            pending.append(item)
    return pending


def recover_stuck_uploads() -> int:
    """
    On startup, any item still marked 'uploading' was interrupted by a restart
    (Render redeploy/spin-down). Reset it to 'pending' so it is retried.
    """
    with _lock:
        queue = _load()
        recovered = 0
        for item in queue:
            c_status = item.get("channel_status", {}) or {}
            changed = False
            for ch, st in list(c_status.items()):
                if st == "uploading":
                    c_status[ch] = "pending"
                    changed = True
            if changed:
                item["channel_status"] = c_status
                item["error"] = "Recovered after restart"
                _recalc_status(item)
                recovered += 1
        if recovered:
            _save(queue)
            log.info(f"♻️ Recovered {recovered} interrupted upload(s) back to pending")
        return recovered


def prune_old(days: int = _PRUNE_AFTER_DAYS) -> int:
    """Remove finished (uploaded/failed) items older than `days`."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with _lock:
        queue = _load()
        kept = []
        for item in queue:
            if item.get("status") in ("uploaded", "failed"):
                ts = item.get("updated_at") or item.get("added_at")
                try:
                    if ts and datetime.fromisoformat(ts) < cutoff:
                        continue
                except ValueError:
                    pass
            kept.append(item)
        removed = len(queue) - len(kept)
        if removed:
            _save(kept)
            log.info(f"🧹 Pruned {removed} old finished queue item(s)")
        return removed


def clear_completed() -> bool:
    with _lock:
        queue = _load()
        return _save([item for item in queue if item.get("status") == "pending"])


def clear_all() -> bool:
    with _lock:
        return _save([])
