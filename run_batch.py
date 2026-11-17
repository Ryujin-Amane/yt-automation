"""
Weekly Batch Orchestrator — schedules 1 video per channel per day for 7 days.

Fetches fresh videos from monitored TikTok accounts, processes them through
the anti-reused-content filter and Live RAG AI SEO agent, and schedules them
on your YouTube channels at peak viewer times using YouTube's native scheduler.

Usage:
    python run_batch.py                         # 7 days × 4 channels = 28 videos
    python run_batch.py --max-per-channel 3     # 3 days only
    python run_batch.py --channels 1,2,3,4
    python run_batch.py --dry-run
"""

import os
import sys
import time
import math
import argparse
import logging
from datetime import datetime

import config
import database
import tiktok_scraper
import video_processor
import ai_agent
import youtube_uploader
import scheduler_planner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("batch_runner")


def run_batch(
    channels: list[int] | None = None,
    max_per_channel: int = 7,
    per_day: int | None = None,
    dry_run: bool = False,
):
    print("\n" + "=" * 65)
    print("🚀 AutoTube Batch Scheduler")
    print("=" * 65)

    # 1. Verify available channels
    available = config.get_available_channels()
    if not available:
        log.error("❌ No configured YouTube channels found!")
        log.error("👉 Run 'python auth_setup.py --channel 1' to connect a channel.")
        return

    target_channels = channels if channels else available
    valid_targets = [ch for ch in target_channels if ch in available]

    if not valid_targets:
        log.error(f"❌ None of the requested channels {target_channels} are authenticated.")
        log.info(f"Available channels: {available}")
        return

    num_channels = len(valid_targets)
    log.info(f"🎯 Target channels: {valid_targets} ({num_channels} active)")

    # 2. Check TikTok handles
    tiktok_users = config._get_tiktok_usernames()
    if not tiktok_users:
        log.error("❌ No TikTok usernames configured in settings or .env!")
        return
    log.info(f"👀 Monitoring {len(tiktok_users)} TikTok accounts")

    # 3. Fetch fresh TikTok videos FIRST so we know the pool size
    log.info("🔍 Scraping TikTok accounts for fresh unposted videos...")
    all_videos = tiktok_scraper.fetch_all_new_videos(max_per_account=5)
    if not all_videos:
        log.info("😴 No fresh videos found across monitored TikTok accounts.")
        return

    log.info(f"📥 Discovered {len(all_videos)} candidates across TikTok accounts")

    # Filter out already-uploaded videos in database
    unprocessed = []
    for v in all_videos:
        vid_id = v["id"]
        if database.is_video_processed(vid_id):
            log.debug(f"Video {vid_id} already in database, skipping.")
            continue
        unprocessed.append(v)

    log.info(f"✨ Found {len(unprocessed)} fresh, unposted videos ready for processing")
    if not unprocessed:
        log.info("✅ All fetched videos have already been processed.")
        return

    # 4. Smart distribution — maximize use of all available videos
    pool_size = len(unprocessed)
    videos_per_ch = pool_size // num_channels  # e.g., 75 // 4 = 18
    if videos_per_ch < 1:
        videos_per_ch = 1

    # Calculate per_day: how many uploads per channel per day
    days = max_per_channel if max_per_channel > 0 else 7
    if per_day is None:
        per_day = math.ceil(videos_per_ch / days)  # e.g., ceil(18/7) = 3
        per_day = min(per_day, 3)  # Cap at 3 per day max
        per_day = max(per_day, 1)  # At least 1 per day
    else:
        per_day = max(1, min(int(per_day), 3))

    # Recalculate total slots: days × per_day might exceed videos_per_ch
    total_slots_per_ch = min(videos_per_ch, days * per_day)
    actual_total = total_slots_per_ch * num_channels

    print(f"\n📊 DISTRIBUTION MATH:")
    print(f"   Pool: {pool_size} videos")
    print(f"   Per channel: {videos_per_ch} videos")
    print(f"   Per day: {per_day} upload(s)/channel/day")
    print(f"   Total scheduled: {actual_total} videos across {num_channels} channels")
    print()

    # 5. Plan schedule slots for each channel based on actual allocation
    channel_plans = {}
    print(f"📅 Publication Schedule ({per_day} video(s)/day, {total_slots_per_ch} total per channel):")
    print("-" * 75)
    for ch in valid_targets:
        ch_meta = scheduler_planner.get_channel_schedule(ch)
        slots = scheduler_planner.get_upcoming_publish_slots(
            ch, count=total_slots_per_ch, per_day=per_day
        )
        channel_plans[ch] = {
            "meta": ch_meta,
            "slots": slots,
            "scheduled_count": 0,
        }
        ch_name = ch_meta.get("name", f"Channel {ch}")
        print(f"\n📺 CH{ch} [{ch_name}] — {len(slots)} uploads:")
        current_day = None
        for idx, slot in enumerate(slots, 1):
            day_label = slot['day']
            slot_label = slot.get('slot_label', 'peak')
            if day_label != current_day:
                current_day = day_label
                print(f"   ── {day_label} ──")
            print(f"      #{idx:2d} [{slot_label:7s}] {slot['publish_at_local']} [{slot['activity_level']}]")
    print("-" * 75 + "\n")

    if dry_run:
        print("🔍 DRY RUN enabled: No videos will be downloaded or uploaded.")
        print(f"   Would schedule {actual_total} videos: {total_slots_per_ch}/channel × {num_channels} channels, {per_day}/day for {days} days.")
        return

    # 6. Process & upload for each channel
    video_idx = 0
    results_summary = []

    for ch in valid_targets:
        plan = channel_plans[ch]
        slots = plan["slots"]
        ch_name = plan["meta"].get("name", f"Channel {ch}")

        log.info(f"\n{'='*50}")
        log.info(f"🎬 Processing for CH{ch} [{ch_name}] — {len(slots)} videos")
        log.info(f"{'='*50}")

        uploaded_for_ch = 0
        for slot in slots:
            if video_idx >= len(unprocessed):
                log.warning("⚠️ Ran out of fresh TikTok videos to schedule!")
                break

            video = unprocessed[video_idx]
            video_idx += 1

            vid_id = video["id"]
            author = video["author"]
            caption = video["title"]
            play_url = video["play"]
            publish_utc = slot["publish_at_utc_iso"]
            publish_local = slot["publish_at_local"]

            log.info(f"\n▶️ [{uploaded_for_ch + 1}/{len(slots)}] Video {vid_id} by @{author}")
            log.info(f"   Target Publish Time: {publish_local} ({slot['day']})")

            # Download
            downloaded = tiktok_scraper.download_video(play_url, vid_id, author=author)
            if not downloaded:
                log.error(f"Failed to download video {vid_id}, skipping")
                continue

            # Process with FFmpeg
            processed = video_processor.process_video(downloaded, vid_id)
            if not processed:
                log.error(f"Failed to process video {vid_id}, skipping")
                video_processor.cleanup_files(downloaded)
                continue

            # AI SEO Optimization
            seo_title = caption
            seo_description = ""
            seo_hashtags = ""
            try:
                seo_data = ai_agent.optimize_video_seo(
                    caption,
                    author,
                    video_path=processed,
                    cover_url=video.get("cover", ""),
                    channel=ch,
                    candidate_slots=[slot],
                )
                if seo_data:
                    seo_title = seo_data.get("seo_title", caption)
                    seo_description = seo_data.get("seo_description", "")
                    seo_hashtags = seo_data.get("seo_hashtags", "")
                    log.info(f"🧠 AI Title: {seo_title}")
            except Exception as e:
                log.warning(f"AI SEO failed ({e}), using default caption")

            # Upload with native publishAt schedule
            success, used_ch = youtube_uploader.upload_video(
                video_path=processed,
                tiktok_caption=seo_title,
                tiktok_author=author,
                tiktok_id=vid_id,
                target_channel=ch,
                seo_description=seo_description,
                seo_hashtags=seo_hashtags,
                publish_at=publish_utc,
            )

            # Cleanup local temp mp4 files
            video_processor.cleanup_files(downloaded, processed)

            if success:
                uploaded_for_ch += 1
                results_summary.append({
                    "channel": ch,
                    "title": seo_title[:45],
                    "publish_local": publish_local,
                    "day": slot["day"],
                    "status": "Scheduled ✅",
                })
                # Small pause between uploads to respect rate limits
                time.sleep(3)
            else:
                log.error(f"❌ Upload failed for {vid_id} on CH{ch}")
                results_summary.append({
                    "channel": ch,
                    "title": seo_title[:45],
                    "publish_local": publish_local,
                    "day": slot["day"],
                    "status": "Failed ❌",
                })

        plan["scheduled_count"] = uploaded_for_ch

    # 7. Final Summary Report
    print("\n" + "=" * 65)
    print("📊 BATCH RUN SUMMARY")
    print("=" * 65)

    successful = sum(1 for r in results_summary if "✅" in r["status"])
    failed = sum(1 for r in results_summary if "❌" in r["status"])

    for r in results_summary:
        print(f"CH{r['channel']} | {r['day']:9s} {r['publish_local']} | {r['status']} | {r['title']}")

    print("=" * 65)
    print(f"📈 Total: {successful} scheduled, {failed} failed out of {len(results_summary)} attempted")
    print(f"📺 Across {num_channels} channels, covering {videos_per_ch} days each")
    print("🎉 YouTube will automatically publish each video at its scheduled time.\n")


def main():
    parser = argparse.ArgumentParser(description="Weekly Batch Scheduler — 1 video/channel/day for 7 days")
    parser.add_argument(
        "--max-per-channel",
        type=int,
        default=7,
        help="Number of days to schedule per channel (default: 7 = one full week)",
    )
    parser.add_argument(
        "--per-day",
        type=int,
        default=None,
        help="Number of uploads per channel per day (1, 2, or 3. Default: auto-calculated)",
    )
    parser.add_argument(
        "--channels",
        type=str,
        default="",
        help="Comma-separated list of channel numbers to process (e.g. 1,2,3,4)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate run without downloading or uploading",
    )

    args = parser.parse_args()
    channels = [int(c.strip()) for c in args.channels.split(",") if c.strip()] if args.channels else None

    run_batch(
        channels=channels,
        max_per_channel=args.max_per_channel,
        per_day=args.per_day,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()



