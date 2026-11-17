"""
Bot Entry Point — Premium Dashboard + APScheduler automation.

This is the main file that runs on Hugging Face Spaces. It:
  1. Serves a premium web dashboard
  2. Exposes REST API endpoints for queue, settings, and upload control
  3. Runs APScheduler jobs that:
     - Fetch new TikToks into a queue
     - Auto-upload from the queue (when enabled) respecting per-channel configs
  4. Self-ping keep-alive to prevent HF Spaces from sleeping
"""

import os
import random
import logging
import threading
import time
from datetime import datetime, timezone

from flask import (
    Flask, jsonify, request, render_template,
    send_from_directory,
)
from apscheduler.schedulers.background import BackgroundScheduler

import config
import settings_manager
import queue_manager
import tiktok_scraper
import video_processor
import youtube_uploader
import database
import ai_agent
import proxy_manager

# ── Logging Setup ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")

# ── Flask App ──────────────────────────────────────────────
app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
)

# ── Global scheduler & keep-alive references ───────────────
_scheduler = None
_scheduler_lock = threading.Lock()
_self_ping_thread = None
_self_ping_lock = threading.Lock()
_BOOT_TIME = time.time()

_keep_alive_stats = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "self_ping_count": 0,
    "external_ping_count": 0,
    "last_self_ping_at": None,
    "last_self_ping_ok": None,
    "last_external_ping_at": None,
    "last_external_ping_ua": "",
}


def _record_incoming_ping():
    """Track whether an incoming health/keep-alive request is internal or external."""
    now_iso = datetime.now(timezone.utc).isoformat()
    if request.headers.get("X-Self-Ping") == "1":
        _keep_alive_stats["self_ping_count"] += 1
        _keep_alive_stats["last_self_ping_at"] = now_iso
        _keep_alive_stats["last_self_ping_ok"] = True
    else:
        _keep_alive_stats["external_ping_count"] += 1
        _keep_alive_stats["last_external_ping_at"] = now_iso
        _keep_alive_stats["last_external_ping_ua"] = request.headers.get("User-Agent", "")[:120]


# ══════════════════════════════════════════════════════════
#  Dashboard Routes
# ══════════════════════════════════════════════════════════

@app.route("/")
def index():
    """Serve the premium dashboard."""
    return render_template("index.html")


@app.route("/health")
def health():
    """Public health check endpoint for Render, UptimeRobot, GitHub Actions, & cron-job.org."""
    _record_incoming_ping()
    return jsonify({
        "status": "alive",
        "service": "TikTok → YouTube Automation",
        "uptime_seconds": int(time.time() - _BOOT_TIME),
        "time": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/healthz")
def healthz():
    """Secondary lightweight health probe for Render & container internal checks."""
    _record_incoming_ping()
    return "OK", 200


# ══════════════════════════════════════════════════════════
#  API: Statistics & System Health
# ══════════════════════════════════════════════════════════

@app.route("/api/stats")
def api_stats():
    """Return upload statistics, proxy status, and keep-alive telemetry."""
    channels = config.get_available_channels()
    channel_stats = []

    for ch in channels:
        count = database.get_today_upload_count(ch)
        c_settings = config.get_channel_settings(ch)
        max_per = c_settings["max_uploads_per_day"]
        channel_stats.append({
            "channel": ch,
            "uploads_today": count,
            "daily_limit": max_per,
            "remaining": max(0, max_per - count),
        })

    tiktok_names = config._get_tiktok_usernames()

    return jsonify({
        "channels": channel_stats,
        "total_channels": len(channels),
        "monitored_accounts": tiktok_names,
        "queue_size": len(queue_manager.get_all()),
        "pending_count": len(queue_manager.get_pending()),
        "auto_upload": config.get_auto_upload_enabled(),
        "check_interval": config.get_check_interval_minutes(),
        "proxy_status": proxy_manager.get_status_summary(),
        "keep_alive": {
            **_keep_alive_stats,
            "uptime_seconds": int(time.time() - _BOOT_TIME),
        },
        "server_time": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/keep-alive/status")
def api_keep_alive_status():
    """Return detailed internal + external keep-alive telemetry."""
    public_url, localhost_url = _get_ping_urls()
    return jsonify({
        "public_url": public_url,
        "localhost_url": localhost_url,
        "uptime_seconds": int(time.time() - _BOOT_TIME),
        "self_ping_thread_alive": bool(_self_ping_thread and _self_ping_thread.is_alive()),
        **_keep_alive_stats,
    })


@app.route("/api/proxy/status")
def api_proxy_status():
    """Return current status of all proxy/VPN/anti-block layers."""
    return jsonify(proxy_manager.get_status_summary())


@app.route("/api/proxy/test", methods=["POST", "GET"])
def api_proxy_test():
    """Run one-by-one diagnostic tests on all proxy, VPN, and scraper layers."""
    data = request.get_json(silent=True) or {}
    test_username = data.get("username") or request.args.get("username")
    if not test_username:
        tiktok_names = config._get_tiktok_usernames()
        test_username = tiktok_names[0] if tiktok_names else "tiktok"

    report = proxy_manager.test_all_systems(test_username=test_username)
    return jsonify(report)


# ══════════════════════════════════════════════════════════
#  API: Queue Management
# ══════════════════════════════════════════════════════════

@app.route("/api/queue")
def api_queue_list():
    """Return the full video queue."""
    return jsonify({"queue": queue_manager.get_all()})


@app.route("/api/queue/add", methods=["POST"])
def api_queue_add():
    """Manually add a video to the queue."""
    data = request.get_json(silent=True) or {}
    required = ["tiktok_id", "author", "caption", "play_url"]
    for key in required:
        if not data.get(key):
            return jsonify({"error": f"Missing required field: {key}"}), 400

    # Ensure target_channels is a list
    if "target_channels" in data and not isinstance(data["target_channels"], list):
        data["target_channels"] = []

    success = queue_manager.add_video(data)
    return jsonify({"success": success})


@app.route("/api/queue/<queue_id>/update", methods=["POST"])
def api_queue_update(queue_id):
    """Update a queued video."""
    data = request.get_json(silent=True) or {}
    allowed_keys = {"target_channels", "caption"}
    updates = {k: v for k, v in data.items() if k in allowed_keys}
    if "target_channels" in updates and not isinstance(updates["target_channels"], list):
        return jsonify({"error": "target_channels must be a list"}), 400
        
    success = queue_manager.update_video(queue_id, updates)
    return jsonify({"success": success})


@app.route("/api/queue/<queue_id>/remove", methods=["POST"])
def api_queue_remove(queue_id):
    """Remove a video from the queue."""
    success = queue_manager.remove_video(queue_id)
    return jsonify({"success": success})


@app.route("/api/queue/clear", methods=["POST"])
def api_queue_clear():
    """Clear completed/failed items from the queue."""
    success = queue_manager.clear_completed()
    return jsonify({"success": success})


@app.route("/api/queue/clear-all", methods=["POST"])
def api_queue_clear_all():
    """Clear the entire queue."""
    success = queue_manager.clear_all()
    return jsonify({"success": success})


# ══════════════════════════════════════════════════════════
#  API: Force Upload
# ══════════════════════════════════════════════════════════

@app.route("/api/upload/<queue_id>", methods=["POST"])
def api_force_upload(queue_id):
    """Force-upload a specific queued video."""
    data = request.get_json(silent=True) or {}
    target_channels = data.get("target_channels", [])
    if not isinstance(target_channels, list):
        return jsonify({"error": "target_channels must be a list"}), 400

    queue_items = queue_manager.get_all()
    video = next((item for item in queue_items if item.get("queue_id") == queue_id), None)

    if not video:
        return jsonify({"error": "Video not found in queue"}), 404

    if target_channels:
        queue_manager.update_video(queue_id, {"target_channels": target_channels})
        video["target_channels"] = target_channels

    thread = threading.Thread(
        target=_upload_single_video, args=(video,), daemon=True
    )
    thread.start()

    return jsonify({"success": True, "message": "Upload started in background"})


@app.route("/api/upload-all", methods=["POST"])
def api_upload_all_pending():
    """Force-upload all pending videos."""
    thread = threading.Thread(target=_upload_pending_queue, daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "Batch upload started"})


# ══════════════════════════════════════════════════════════
#  API: Settings
# ══════════════════════════════════════════════════════════

@app.route("/api/settings")
def api_settings_get():
    """Return all current settings."""
    return jsonify({"settings": settings_manager.get_all()})


@app.route("/api/settings", methods=["POST"])
def api_settings_update():
    """Update settings and optionally reschedule the background job."""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "No data provided"}), 400

    # Handle standard settings
    type_map = {
        "check_interval_minutes": int,
        "max_uploads_per_channel": int,
        "uploads_per_channel_per_run": int,
        "video_max_age_hours": int,
        "unlimited_channel": int,
        "speed_factor": float,
        "zoom_percent": float,
        "saturation_boost": float,
        "contrast_boost": float,
        "auto_upload_enabled": bool,
        "video_preset": str,
        "proxy_mode": str,
    }
    for key, cast_fn in type_map.items():
        if key in data:
            try:
                data[key] = cast_fn(data[key])
            except (ValueError, TypeError):
                return jsonify({"error": f"Invalid type for {key}"}), 400

    # Ensure channel_settings remains a dict
    if "channel_settings" in data and not isinstance(data["channel_settings"], dict):
        return jsonify({"error": "channel_settings must be a dictionary"}), 400

    success = settings_manager.update(data)

    if "check_interval_minutes" in data:
        _reschedule_jobs()

    return jsonify({"success": success, "settings": settings_manager.get_all()})


@app.route("/api/settings/reset", methods=["POST"])
def api_settings_reset():
    """Reset all settings to defaults."""
    success = settings_manager.reset()
    _reschedule_jobs()
    return jsonify({"success": success, "settings": settings_manager.get_all()})


# ══════════════════════════════════════════════════════════
#  API: Schedule Management
# ══════════════════════════════════════════════════════════

@app.route("/api/schedule")
def api_schedule_get():
    """Return the current peak_schedules.json config."""
    import scheduler_planner
    data = scheduler_planner.load_schedules()
    channels = config.get_available_channels()
    return jsonify({"schedules": data, "available_channels": channels})


@app.route("/api/schedule", methods=["POST"])
def api_schedule_save():
    """Save updated peak_schedules.json config."""
    import scheduler_planner
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "No data provided"}), 400

    try:
        import json as _json
        with open(scheduler_planner.SCHEDULE_FILE, "w", encoding="utf-8") as f:
            _json.dump(data, f, indent=2)
        log.info("📅 Peak schedules updated via dashboard")
        return jsonify({"success": True})
    except Exception as e:
        log.error(f"Failed to save schedules: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/schedule/preview")
def api_schedule_preview():
    """Preview upcoming publish slots for all channels."""
    import scheduler_planner
    channels = config.get_available_channels()
    per_day = int(request.args.get("per_day", 1))
    count = int(request.args.get("count", 7))

    result = {}
    for ch in channels:
        ch_meta = scheduler_planner.get_channel_schedule(ch)
        slots = scheduler_planner.get_upcoming_publish_slots(
            ch, count=count, per_day=per_day
        )
        result[str(ch)] = {
            "name": ch_meta.get("name", f"Channel {ch}"),
            "timezone": ch_meta.get("timezone", "Asia/Karachi"),
            "stagger": ch_meta.get("channel_stagger_minutes", 0),
            "slots": slots,
        }
    return jsonify({"channels": result})


@app.route("/api/schedule/batch", methods=["POST"])
def api_schedule_batch():
    """Trigger a full batch: fetch → schedule across all channels."""
    data = request.get_json(silent=True) or {}
    channels = data.get("channels", None)  # list of ints or None for all
    max_per_channel = int(data.get("max_per_channel", 7))
    per_day = int(data.get("per_day", 1))
    dry_run = bool(data.get("dry_run", False))

    def _run():
        import run_batch
        run_batch.run_batch(
            channels=channels,
            max_per_channel=max_per_channel,
            per_day=per_day,
            dry_run=dry_run,
        )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({
        "success": True,
        "message": f"Batch scheduler started (channels={channels or 'all'}, max_per_channel={max_per_channel}, per_day={per_day}, dry_run={dry_run})",
    })


# ══════════════════════════════════════════════════════════
#  API: Manual Fetch
# ══════════════════════════════════════════════════════════

@app.route("/api/fetch", methods=["POST"])
def api_manual_fetch():
    """Trigger a manual fetch of new TikToks into the queue."""
    thread = threading.Thread(target=fetch_job, daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "Fetch started in background"})


# ══════════════════════════════════════════════════════════
#  API: Restart Scheduler
# ══════════════════════════════════════════════════════════

@app.route("/api/restart", methods=["POST"])
def api_restart_scheduler():
    """Kill and restart the APScheduler (fetch + upload jobs)."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
                log.info("🔄 Scheduler shut down for restart.")
            except Exception as exc:
                log.warning(f"Scheduler shutdown error (non-fatal): {exc}")
            _scheduler = None

    start_scheduler()
    return jsonify({"success": True, "message": "Scheduler restarted successfully"})


# ══════════════════════════════════════════════════════════
#  Core Jobs (decoupled & multi-channel)
# ══════════════════════════════════════════════════════════

def fetch_job():
    """Fetch new TikTok videos and add them to the queue."""
    log.info("=" * 60)
    log.info("🔍 Starting fetch job...")
    log.info("=" * 60)

    tiktok_names = config._get_tiktok_usernames()
    if not tiktok_names:
        log.warning("⚠️ No TikTok usernames configured!")
        return

    new_videos = tiktok_scraper.fetch_all_new_videos()
    if not new_videos:
        log.info("😴 No new fresh videos found.")
        return

    added_count = 0
    for video in new_videos:
        success = queue_manager.add_video({
            "tiktok_id": video["id"],
            "author": video["author"],
            "caption": video["title"],
            "play_url": video["play"],
            "duration": video.get("duration", 0),
            "width": video.get("width", 0),
            "height": video.get("height", 0),
            "cover": video.get("cover", ""),
            "timestamp": video.get("timestamp", 0),
            "target_channels": [], # Empty list = Auto
        })
        if success:
            added_count += 1

    log.info(f"📥 Fetch complete: {added_count} new videos added to queue")


def auto_upload_job():
    """Auto upload job."""
    if not config.get_auto_upload_enabled():
        log.info("⏸️ Auto-upload is disabled, skipping upload job")
        return
    _upload_pending_queue()


def _upload_pending_queue():
    """Upload all pending videos, respecting per-channel targets and limits."""
    log.info("=" * 60)
    log.info("🚀 Starting auto-upload job...")
    log.info("=" * 60)

    channels = config.get_available_channels()
    if not channels:
        log.error("⚠️ No YouTube channels configured!")
        return

    # Check eligibility for each channel
    eligible_channels = []
    current_ts = time.time()
    
    for ch in channels:
        ch_config = config.get_channel_settings(ch)
        count = database.get_today_upload_count(ch)
        max_per_day = ch_config["max_uploads_per_day"]
        
        last_upload_ts = database.get_last_upload_time(ch)
        interval_secs = ch_config["upload_interval_minutes"] * 60
        time_since_upload = current_ts - last_upload_ts
        
        if count >= max_per_day:
            log.info(f"🛑 CH{ch} reached daily limit ({count}/{max_per_day}).")
            continue
            
        if time_since_upload < interval_secs:
            mins_left = (interval_secs - time_since_upload) / 60
            log.info(f"⏳ CH{ch} cooldown active. Wait {mins_left:.1f}m.")
            continue
            
        eligible_channels.append({
            "channel": ch,
            "uploads_left_this_run": ch_config["uploads_per_run"]
        })

    if not eligible_channels:
        log.info("🛑 No channels are currently eligible for upload. Skipping.")
        return

    pending = queue_manager.get_pending()
    if not pending:
        log.info("📭 No pending videos in queue.")
        return

    # Process pending videos against eligible channels
    # Round-robin style across channels
    
    for ch_info in eligible_channels:
        channel = ch_info["channel"]
        uploads_left = ch_info["uploads_left_this_run"]
        
        for video in pending:
            if uploads_left <= 0:
                break
                
            targets = video.get("target_channels", [])
            c_status = video.get("channel_status", {})
            ch_str = str(channel)
            
            # Skip if explicitly targeting other channels
            if targets and channel not in targets:
                continue
                
            # Skip if already uploaded/uploading on this channel
            if c_status.get(ch_str, "pending") != "pending":
                continue
                
            if not targets:
                # Bind this video exclusively to this channel to prevent duplicate uploads
                targets = [channel]
                video["target_channels"] = targets
                queue_manager.update_video(video["queue_id"], {"target_channels": targets})
                
            log.info(f"⚡ Processing video {video['tiktok_id']} for CH{channel}")
            _do_upload(video, channel)
            uploads_left -= 1
            
            # Since we uploaded, the channel is now on cooldown for the rest of this run
            # Break so we don't upload more to this channel than `uploads_per_run` allows
            if uploads_left <= 0:
                break


def _upload_single_video(video: dict):
    """Force upload a single video from the queue to its target channels."""
    channels = config.get_available_channels()
    targets = video.get("target_channels", [])
    
    if not channels:
        queue_manager.update_video(video["queue_id"], {"status": "failed", "error": "No channels"})
        return
        
    if not targets:
        # Default auto behavior - pick the first available channel that hasn't processed it
        targets = [channels[0]]
        video["target_channels"] = targets
        queue_manager.update_video(video["queue_id"], {"target_channels": targets})
        
    for channel in targets:
        if channel in channels:
            ch_str = str(channel)
            c_status = video.get("channel_status", {})
            if c_status.get(ch_str, "pending") == "pending":
                _do_upload(video, channel)


def _do_upload(video: dict, channel: int):
    """Perform download → process → upload pipeline for a video on a specific channel."""
    queue_id = video["queue_id"]
    vid_id = video["tiktok_id"]
    author = video["author"]
    caption = video["caption"]
    play_url = video["play_url"]

    queue_manager.mark_channel_status(queue_id, channel, "uploading")
    log.info(f"🎬 Uploading {vid_id} by @{author} → CH{channel}")

    downloaded_path = tiktok_scraper.download_video(play_url, vid_id, author=author)
    if not downloaded_path:
        queue_manager.mark_channel_status(queue_id, channel, "failed", "Download failed")
        return

    processed_path = video_processor.process_video(downloaded_path, vid_id)
    if not processed_path:
        queue_manager.mark_channel_status(queue_id, channel, "failed", "Processing failed")
        video_processor.cleanup_files(downloaded_path)
        return

    seo_title = caption
    seo_description = ""
    seo_hashtags = ""
    publish_utc = None

    seo_data = ai_agent.optimize_video_seo(
        caption,
        author,
        video_path=processed_path,
        cover_url=video.get("cover", ""),
        channel=channel,
    )
    if seo_data:
        seo_title = seo_data.get("seo_title", caption)
        seo_description = seo_data.get("seo_description", "")
        seo_hashtags = seo_data.get("seo_hashtags", "")
        publish_utc = seo_data.get("publish_at_utc_iso")
        log.info(f"🧠 AI optimized: {seo_title}")

    # Fallback schedule slot if AI didn't already assign one
    if not publish_utc:
        import scheduler_planner
        slots = scheduler_planner.get_upcoming_publish_slots(channel, count=1)
        publish_utc = slots[0]["publish_at_utc_iso"] if slots else None
        if publish_utc:
            log.info(f"📅 Scheduling upload for CH{channel} at {slots[0]['publish_at_local']}")

    success, used_channel = youtube_uploader.upload_video(
        video_path=processed_path,
        tiktok_caption=seo_title,
        tiktok_author=author,
        tiktok_id=vid_id,
        target_channel=channel,
        seo_description=seo_description,
        seo_hashtags=seo_hashtags,
        publish_at=publish_utc,
    )

    if success:
        queue_manager.mark_channel_status(queue_id, channel, "uploaded")
    else:
        queue_manager.mark_channel_status(queue_id, channel, "failed", "Upload failed")

    video_processor.cleanup_files(downloaded_path, processed_path)


# ══════════════════════════════════════════════════════════
#  Scheduler
# ══════════════════════════════════════════════════════════

def _reschedule_jobs():
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            return

        interval = config.get_check_interval_minutes()

        try:
            _scheduler.reschedule_job(
                "fetch_job",
                trigger="interval",
                minutes=interval,
            )
            # Run the upload check every 5 minutes regardless of fetch interval
            # so that it can accurately enforce per-channel intervals
            _scheduler.reschedule_job(
                "upload_job",
                trigger="interval",
                minutes=5, 
            )
            log.info(f"⏰ Scheduler adjusted: Fetch every {interval}m, Upload poll every 5m")
        except Exception as exc:
            log.error(f"Failed to reschedule: {exc}")


def start_scheduler():
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            return

        interval = config.get_check_interval_minutes()
        _scheduler = BackgroundScheduler()

        _scheduler.add_job(
            fetch_job,
            "interval",
            minutes=interval,
            id="fetch_job",
            name="TikTok Fetcher",
            next_run_time=datetime.now(timezone.utc),
        )

        _scheduler.add_job(
            auto_upload_job,
            "interval",
            minutes=5, # Upload poll is faster to check per-channel cooldowns
            id="upload_job",
            name="Queue Auto-Uploader",
            next_run_time=datetime.now(timezone.utc),
        )

        # Layer 3: Scheduler-based heartbeat (belt-and-suspenders keep-alive)
        _scheduler.add_job(
            _scheduler_heartbeat,
            "interval",
            minutes=4,
            id="heartbeat_job",
            name="Keep-Alive Heartbeat",
            next_run_time=datetime.now(timezone.utc),
        )

        _scheduler.start()
        log.info(f"⏰ Scheduler started — Fetch every {interval}m, Upload poll every 5m, Heartbeat every 4m")


# ══════════════════════════════════════════════════════════
#  Multi-Layer Keep-Alive System (Render 24/7 + External Pingers)
#
#  Render Free Web Services spin down after 15 minutes without inbound
#  external HTTP traffic through Render's load balancer. This system uses:
#
#  Layer 1: Internal Self-Ping Thread (pings RENDER_EXTERNAL_URL / PUBLIC_URL
#           + localhost every ~4 minutes with realistic HTTP headers)
#  Layer 2: APScheduler Heartbeat (every 4 min) — pings public URL and
#           auto-respawns the self-ping thread if it ever stops
#  Layer 3: External GitHub Actions Cron (.github/workflows/keep_alive.yml)
#           + Cloudflare Worker / UptimeRobot / cron-job.org hitting /keep-alive
# ══════════════════════════════════════════════════════════

_consecutive_ping_failures = 0
_MAX_PING_FAILURES_BEFORE_RESTART = 5


@app.route("/keep-alive")
def keep_alive():
    """Dedicated lightweight keep-alive endpoint for internal & external pingers."""
    _record_incoming_ping()
    return jsonify({
        "status": "alive",
        "keepalive": True,
        "uptime_seconds": int(time.time() - _BOOT_TIME),
        "time": datetime.now(timezone.utc).isoformat(),
    })


def _get_ping_urls() -> tuple[str, str]:
    """Return (public_url, localhost_url) for self-pinging on Render / Cloud / Local."""
    localhost_url = f"http://127.0.0.1:{config.PORT}"

    # 1. Explicit PUBLIC_URL or APP_URL env var
    explicit_url = (os.getenv("PUBLIC_URL") or os.getenv("APP_URL") or "").strip().rstrip("/")
    if explicit_url:
        return explicit_url, localhost_url

    # 2. Render automatically sets RENDER_EXTERNAL_URL (e.g. https://my-app.onrender.com)
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if render_url:
        return render_url, localhost_url

    # 3. Render hostname fallback
    render_host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if render_host:
        return f"https://{render_host}", localhost_url

    # 4. Legacy Hugging Face Spaces fallback
    space_id = os.getenv("SPACE_ID", "").strip()
    if space_id:
        slug = space_id.replace("/", "-")
        return f"https://{slug}.hf.space", localhost_url

    return localhost_url, localhost_url


def _do_ping(url: str, timeout: int = 15) -> bool:
    """Ping a URL and return True on success."""
    import urllib.request
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
            "Cache-Control": "no-cache",
            "X-Self-Ping": "1",
        }
        hf_token = os.getenv("HF_TOKEN", "")
        if hf_token and "hf.space" in url:
            headers["Authorization"] = f"Bearer {hf_token}"

        req = urllib.request.Request(url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _self_ping_loop():
    """Ping our own public & local endpoints every ~4 minutes (well under Render's 15m idle limit)."""
    global _consecutive_ping_failures

    # Brief initial delay so Gunicorn finishes binding port first
    time.sleep(5)

    public_url, localhost_url = _get_ping_urls()
    base_interval = int(os.getenv("SELF_PING_INTERVAL_SECONDS", "240"))
    log.info(f"📡 Self-ping targets: public={public_url} local={localhost_url} (interval=~{base_interval}s)")

    endpoints = ["/keep-alive", "/health", "/healthz"]
    tick = 0

    while True:
        public_url, localhost_url = _get_ping_urls()
        endpoint = endpoints[tick % len(endpoints)]
        tick += 1

        # Layer 1: Localhost ping
        local_ok = _do_ping(f"{localhost_url}{endpoint}", timeout=10)

        # Layer 2: External Public URL ping (routes through Render's edge router)
        if public_url != localhost_url:
            public_ok = _do_ping(f"{public_url}{endpoint}", timeout=20)
        else:
            public_ok = local_ok

        if local_ok or public_ok:
            _consecutive_ping_failures = 0
            log.debug(f"📡 Self-ping OK (local={local_ok} public={public_ok}) [{endpoint}]")
        else:
            _consecutive_ping_failures += 1
            _keep_alive_stats["last_self_ping_ok"] = False
            log.warning(
                f"📡 Self-ping FAILED (attempt {_consecutive_ping_failures}/"
                f"{_MAX_PING_FAILURES_BEFORE_RESTART}) [{endpoint}]"
            )

        # Add slight jitter (±15s) to avoid synchronized pattern throttling
        sleep_secs = max(45, base_interval + random.randint(-15, 15))
        time.sleep(sleep_secs)


def _scheduler_heartbeat():
    """Layer 3: APScheduler-based heartbeat that pings public & local URLs and heals self-ping thread."""
    # Ensure self-ping thread is still alive
    if _self_ping_thread is None or not _self_ping_thread.is_alive():
        log.warning("💓 Self-ping thread was not running — restarting it via scheduler heartbeat!")
        start_self_ping()

    public_url, localhost_url = _get_ping_urls()
    target = public_url if public_url != localhost_url else localhost_url
    ok = _do_ping(f"{target}/keep-alive", timeout=15)
    if ok:
        log.debug(f"💓 Scheduler heartbeat OK ({target})")
    else:
        log.warning(f"💓 Scheduler heartbeat — ping failed ({target})")


def start_self_ping():
    """Launch the self-ping thread (idempotent)."""
    global _self_ping_thread
    with _self_ping_lock:
        if _self_ping_thread is not None and _self_ping_thread.is_alive():
            return
        _self_ping_thread = threading.Thread(target=_self_ping_loop, daemon=True, name="self-ping")
        _self_ping_thread.start()
        log.info("📡 Self-ping keep-alive thread started (~4m interval, Render-aware)")


if __name__ == "__main__":
    log.info("🤖 TikTok → YouTube Automation Bot starting...")
    start_scheduler()
    start_self_ping()
    app.run(host="0.0.0.0", port=config.PORT)

