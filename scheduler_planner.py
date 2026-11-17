"""
Scheduler Planner — calculates optimal YouTube publication times
based on YouTube Studio peak viewer reports.

Converts local peak window times (e.g. Asia/Karachi PKT) into ISO-8601 UTC
timestamps formatted for YouTube Data API v3 (`publishAt`).
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

SCHEDULE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "peak_schedules.json")

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DEFAULT_WINDOWS = [
    {"day": "Saturday", "activity_level": "VERY_HIGH", "recommended_publish_pkt": "17:30"},
    {"day": "Sunday", "activity_level": "VERY_HIGH", "recommended_publish_pkt": "17:30"},
    {"day": "Friday", "activity_level": "HIGH", "recommended_publish_pkt": "16:30"},
    {"day": "Monday", "activity_level": "HIGH", "recommended_publish_pkt": "16:30"},
    {"day": "Tuesday", "activity_level": "HIGH", "recommended_publish_pkt": "17:30"},
    {"day": "Wednesday", "activity_level": "HIGH", "recommended_publish_pkt": "17:30"},
    {"day": "Thursday", "activity_level": "HIGH", "recommended_publish_pkt": "18:30"},
]


def load_schedules() -> dict:
    """Load peak schedules config file."""
    if os.path.exists(SCHEDULE_FILE):
        try:
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Could not load {SCHEDULE_FILE}: {e}")
    return {"channels": {}}


def get_channel_schedule(channel: int) -> dict:
    """Get schedule settings for a specific channel, resolving inheritance."""
    data = load_schedules().get("channels", {})
    ch_key = str(channel)
    ch_data = data.get(ch_key, {})

    if "inherit_from" in ch_data:
        parent_key = str(ch_data["inherit_from"])
        parent_data = data.get(parent_key, {})
        merged = dict(parent_data)
        merged.update(ch_data)
        return merged

    if not ch_data:
        return {
            "name": f"Channel {channel}",
            "timezone": "Asia/Karachi",
            "utc_offset": "+05:00",
            "channel_stagger_minutes": (channel - 1) * 15,
            "max_videos_per_batch": 3,
            "peak_viewer_windows": DEFAULT_WINDOWS,
        }

    return ch_data


def get_upcoming_publish_slots(
    channel: int,
    count: int = 7,
    per_day: int = 1,
    start_after: datetime | None = None,
    strategy: str = "peak_priority",
) -> list[dict]:
    """
    Calculate the next `count` optimal publishing slots for the given channel.

    Supports multiple uploads per day by generating `per_day` time slots
    spread ~3 hours apart around the peak window.

    Args:
        channel: YouTube channel number (1, 2, 3, etc.)
        count: Total number of video publication slots to generate
        per_day: Number of uploads per day (1, 2, or 3). Slots are spread
                 3 hours apart: e.g. peak=17:30 → [14:30, 17:30, 20:30]
        start_after: Earliest allowable publish time (defaults to now + 30 minutes)
        strategy: 'peak_priority' (prioritizes VERY_HIGH activity days)
                  or 'chronological' (takes next chronological windows)

    Returns:
        List of dicts with publish_at_utc_iso, publish_at_local, day, etc.
    """
    ch_config = get_channel_schedule(channel)
    tz_str = ch_config.get("timezone", "Asia/Karachi")
    tz = ZoneInfo(tz_str)
    stagger_mins = ch_config.get("channel_stagger_minutes", 0)
    windows = ch_config.get("peak_viewer_windows", DEFAULT_WINDOWS)

    now_local = datetime.now(tz)
    min_allowable = (start_after.astimezone(tz) if start_after else now_local) + timedelta(minutes=30)

    # Activity score map for sorting
    activity_scores = {
        "VERY_HIGH": 100,
        "HIGH": 80,
        "MEDIUM": 50,
        "LOW": 20,
    }

    # Time offsets for multi-slot days (hours relative to peak):
    # 1 slot:  [0]           → just the peak time
    # 2 slots: [-3, 0]       → 3h before peak + peak
    # 3 slots: [-3, 0, +3]   → morning + peak + evening
    slot_offsets = {
        1: [0],
        2: [-3, 0],
        3: [-3, 0, 3],
    }
    offsets = slot_offsets.get(min(per_day, 3), [0])

    # Map windows by day name
    window_by_day = {w["day"].lower(): w for w in windows}

    # Fetch already-booked slots so we never double-book two videos into the same slot
    try:
        import database
        booked_slots = database.get_booked_schedule_slots(channel)
    except Exception:
        booked_slots = set()

    # Build potential slots for the upcoming 14 days
    potential_slots = []

    for day_offset in range(14):
        candidate_date = (now_local + timedelta(days=day_offset)).date()
        day_name = WEEKDAY_NAMES[candidate_date.weekday()].lower()

        if day_name not in window_by_day:
            continue

        w = window_by_day[day_name]
        time_parts = w.get("recommended_publish_pkt", "17:30").split(":")
        peak_hour = int(time_parts[0])
        peak_minute = int(time_parts[1]) if len(time_parts) > 1 else 0
        level = w.get("activity_level", "HIGH")

        for slot_num, hour_offset in enumerate(offsets):
            slot_hour = peak_hour + hour_offset
            slot_minute = peak_minute

            # Clamp to reasonable hours (10:00 - 23:00)
            if slot_hour < 10:
                slot_hour = 10
            if slot_hour > 23:
                slot_hour = 23

            slot_dt = datetime(
                year=candidate_date.year,
                month=candidate_date.month,
                day=candidate_date.day,
                hour=slot_hour,
                minute=slot_minute,
                tzinfo=tz,
            )

            # Apply channel-specific stagger (e.g. +15m, +30m)
            if stagger_mins:
                slot_dt += timedelta(minutes=stagger_mins)

            if slot_dt > min_allowable:
                dt_utc_check = slot_dt.astimezone(timezone.utc)
                iso_utc_check = dt_utc_check.strftime("%Y-%m-%dT%H:%M:%SZ")
                if iso_utc_check in booked_slots:
                    continue

                # Slot label for display (e.g., "morning", "peak", "evening")
                if hour_offset < 0:
                    slot_label = "early"
                elif hour_offset > 0:
                    slot_label = "evening"
                else:
                    slot_label = "peak"

                potential_slots.append({
                    "datetime": slot_dt,
                    "day": w["day"],
                    "activity_level": level,
                    "score": activity_scores.get(level, 50) - (day_offset * 2),
                    "stagger_minutes": stagger_mins,
                    "slot_label": slot_label,
                })

    if not potential_slots:
        # Fallback: tomorrow at 17:30 + stagger
        fallback_dt = (now_local + timedelta(days=1)).replace(
            hour=17, minute=30, second=0, microsecond=0
        ) + timedelta(minutes=stagger_mins)
        potential_slots = [{
            "datetime": fallback_dt,
            "day": WEEKDAY_NAMES[fallback_dt.weekday()],
            "activity_level": "DEFAULT",
            "score": 50,
            "stagger_minutes": stagger_mins,
            "slot_label": "peak",
        }]

    if strategy == "peak_priority":
        # Group by score/priority within the next 7 days first
        within_week = [s for s in potential_slots if (s["datetime"] - now_local).days <= 7]
        beyond_week = [s for s in potential_slots if (s["datetime"] - now_local).days > 7]

        sorted_within = sorted(within_week, key=lambda s: s["score"], reverse=True)
        chosen = sorted_within[:count]

        if len(chosen) < count:
            needed = count - len(chosen)
            sorted_beyond = sorted(beyond_week, key=lambda s: s["score"], reverse=True)
            chosen.extend(sorted_beyond[:needed])

        # Always return in chronological order
        chosen = sorted(chosen, key=lambda s: s["datetime"])
    else:
        chosen = sorted(potential_slots, key=lambda s: s["datetime"])[:count]

    results = []
    for s in chosen:
        dt_utc = s["datetime"].astimezone(timezone.utc)
        iso_utc = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        local_str = s["datetime"].strftime("%Y-%m-%d %H:%M") + " PKT"
        results.append({
            "publish_at_utc_iso": iso_utc,
            "publish_at_local": local_str,
            "day": s["day"],
            "activity_level": s["activity_level"],
            "stagger_minutes": s["stagger_minutes"],
            "slot_label": s.get("slot_label", "peak"),
        })

    return results

