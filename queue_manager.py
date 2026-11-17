"""
Queue Manager — stores fetched TikTok videos in a local JSON queue so the
web dashboard can inspect, assign multiple target channels, and track
per-channel upload status.
"""

import os
import json
import logging
import threading
import uuid
from datetime import datetime, timezone

log = logging.getLogger(__name__)

QUEUE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue.json")
_lock = threading.Lock()


def _load() -> list[dict]:
    with _lock:
        if not os.path.exists(QUEUE_FILE):
            return []
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError) as exc:
            log.error(f"Failed to read queue: {exc}")
            return []


def _save(queue: list[dict]) -> bool:
    with _lock:
        try:
            with open(QUEUE_FILE, "w", encoding="utf-8") as fh:
                json.dump(queue, fh, indent=2)
            return True
        except OSError as exc:
            log.error(f"Failed to write queue: {exc}")
            return False


def get_all() -> list[dict]:
    return _load()


def add_video(video: dict) -> bool:
    queue = _load()
    tiktok_id = video.get("tiktok_id", "")

    if any(item.get("tiktok_id") == tiktok_id for item in queue):
        log.info(f"Video {tiktok_id} already in queue, skipping")
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
        "target_channels": video.get("target_channels", []), # Empty list = Auto
        "channel_status": {}, # e.g. {"1": "pending", "2": "uploaded"}
        "status": "pending",  # overall status
        "added_at": datetime.now(timezone.utc).isoformat(),
        "error": "",
    }

    queue.append(entry)
    success = _save(queue)
    if success:
        log.info(f"📥 Queued video {tiktok_id} by @{entry['author']}")
    return success


def update_video(queue_id: str, updates: dict) -> bool:
    queue = _load()
    for item in queue:
        if item.get("queue_id") == queue_id:
            item.update(updates)
            
            # Recalculate overall status if channel_status changed
            if "channel_status" in updates:
                c_status = item.get("channel_status", {})
                if not c_status:
                    item["status"] = "pending"
                else:
                    all_vals = list(c_status.values())
                    if all(s == "uploaded" for s in all_vals):
                        item["status"] = "uploaded"
                    elif any(s == "failed" for s in all_vals):
                        item["status"] = "failed"
                    elif any(s == "uploading" for s in all_vals):
                        item["status"] = "uploading"
                    else:
                        item["status"] = "pending"
            
            return _save(queue)
    return False


def mark_channel_status(queue_id: str, channel: int, status: str, error: str = "") -> bool:
    """Update the status for a specific channel inside a queued video."""
    queue = _load()
    ch_str = str(channel)
    for item in queue:
        if item.get("queue_id") == queue_id:
            c_status = item.get("channel_status", {})
            c_status[ch_str] = status
            
            # Recalculate overall status
            all_vals = list(c_status.values())
            if all(s == "uploaded" for s in all_vals):
                item["status"] = "uploaded"
            elif any(s == "failed" for s in all_vals):
                item["status"] = "failed"
            elif any(s == "uploading" for s in all_vals):
                item["status"] = "uploading"
            else:
                item["status"] = "pending"
                
            if error:
                item["error"] = error
            return _save(queue)
    return False


def remove_video(queue_id: str) -> bool:
    queue = _load()
    new_queue = [item for item in queue if item.get("queue_id") != queue_id]
    if len(new_queue) == len(queue):
        return False
    return _save(new_queue)


def get_pending(target_channel: int = 0) -> list[dict]:
    """Return all videos that are pending overall, or pending for a specific channel."""
    queue = _load()
    if target_channel == 0:
        return [item for item in queue if item.get("status") == "pending"]
    
    ch_str = str(target_channel)
    pending = []
    for item in queue:
        # A video is pending for a specific channel if:
        # 1. It explicitly targets this channel and its status isn't uploaded/failed
        # 2. Or it targets 'auto' (empty list) and its overall status is pending
        targets = item.get("target_channels", [])
        c_status = item.get("channel_status", {})
        
        if targets:
            if target_channel in targets and c_status.get(ch_str, "pending") == "pending":
                pending.append(item)
        else:
            if item.get("status") == "pending" and c_status.get(ch_str, "pending") == "pending":
                pending.append(item)
                
    return pending


def clear_completed() -> bool:
    queue = _load()
    new_queue = [item for item in queue if item.get("status") == "pending"]
    return _save(new_queue)


def clear_all() -> bool:
    return _save([])
