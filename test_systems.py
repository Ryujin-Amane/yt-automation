"""
Diagnostic Test Suite — Tests every anti-block proxy/VPN layer, TikTok scraper
method, Zero-FFmpeg lightweight pass-through, Cloud AI Smart Scheduler, and
Render Keep-Alive endpoints one by one.

Usage:
    python test_systems.py
    python test_systems.py --username tiktok
    python test_systems.py --test-download
"""

import os
import sys
import json
import time
import argparse
import logging

import config
import proxy_manager
import tiktok_scraper
import video_processor
import scheduler_planner
import ai_agent
import bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("test_systems")


def print_header(title: str):
    print("\n" + "=" * 70)
    print(f"🧪 {title}")
    print("=" * 70)


def run_all_tests(username: str, test_download: bool = False):
    results = {}

    # ── 1. Proxy & Anti-Block Layers ────────────────────────────
    print_header(f"STAGE 1: Testing All Proxy / VPN / Scraper Layers (@{username})")
    proxy_report = proxy_manager.test_all_systems(test_username=username)
    for layer_key, layer_data in proxy_report["layers"].items():
        ok = layer_data.get("ok", False)
        badge = "✅ PASS" if ok else "ℹ️ SKIP/OFF" if layer_data.get("status") in ("not_running", "not_configured") else "⚠️ WARN"
        print(f"  {badge} | {layer_key}: {json.dumps(layer_data)}")
    results["proxy_layers"] = proxy_report

    # ── 2. Unified TikTok Fetch (Multi-Layer Auto Fallback) ─────
    print_header(f"STAGE 2: Unified TikTok Fetcher (@{username})")
    t0 = time.time()
    videos = tiktok_scraper.get_user_videos(username, count=3)
    elapsed = time.time() - t0
    if videos:
         sample = videos[0]
         print(f"  ✅ PASS | Fetched {len(videos)} videos in {elapsed:.2f}s")
         print(f"         Sample ID: {sample['id']} | Duration: {sample['duration']}s | Res: {sample['width']}x{sample['height']}")
         print(f"         Caption:   {sample['title'][:75]}")
         results["unified_fetch"] = {"ok": True, "count": len(videos), "sample": sample}
    else:
        print(f"  ❌ FAIL | Could not fetch videos for @{username}")
        results["unified_fetch"] = {"ok": False}

    # ── 3. Zero-FFmpeg Download & Pass-Through Test (Optional) ──
    sample_video = videos[0] if videos else None
    if test_download and sample_video:
        print_header(f"STAGE 3: Zero-FFmpeg Download & Pass-Through ({sample_video['id']})")
        dl_path = tiktok_scraper.download_video(
            sample_video["play"], sample_video["id"], author=username
        )
        if dl_path and os.path.exists(dl_path):
            proc_path = video_processor.process_video(dl_path, sample_video["id"])
            size_mb = os.path.getsize(proc_path) / (1024 * 1024) if proc_path else 0
            print(f"  ✅ PASS | Downloaded & validated without FFmpeg: {proc_path} ({size_mb:.2f} MB)")
            video_processor.cleanup_files(dl_path, proc_path)
            results["download_and_passthrough"] = {"ok": True, "size_mb": round(size_mb, 2)}
        else:
            print("  ❌ FAIL | Video download failed")
            results["download_and_passthrough"] = {"ok": False}

    # ── 4. Smart Scheduler & AI SEO Optimization ────────────────
    print_header("STAGE 4: Smart Peak Scheduler & Cloud-Light AI Agent")
    slots = scheduler_planner.get_upcoming_publish_slots(1, count=4, per_day=2)
    print(f"  📅 Generated {len(slots)} unbooked candidate slots for CH1:")
    for idx, s in enumerate(slots):
        print(f"     Slot {idx}: {s['day']} {s['publish_at_local']} (UTC: {s['publish_at_utc_iso']}) [{s['activity_level']}]")

    if sample_video:
        seo = ai_agent.optimize_video_seo(
            tiktok_caption=sample_video["title"] or "Insane viral moment #shorts",
            tiktok_author=username,
            cover_url=sample_video.get("cover", ""),
            channel=1,
            candidate_slots=slots,
        )
        if seo:
            print(f"  ✅ PASS | AI SEO & Smart Slot Selected:")
            print(f"         Title:     {seo.get('seo_title')}")
            print(f"         Tags:      {seo.get('seo_hashtags')}")
            print(f"         Scheduled: {seo.get('publish_at_local')} ({seo.get('publish_at_utc_iso')})")
            print(f"         Reason:    {seo.get('schedule_reason')}")
            results["ai_and_scheduler"] = {"ok": True, "seo": seo}
        else:
            print("  ❌ FAIL | AI SEO returned None")
            results["ai_and_scheduler"] = {"ok": False}

    # ── 5. Keep-Alive & Flask API Endpoints Test ────────────────
    print_header("STAGE 5: Render Keep-Alive & Diagnostic API Endpoints")
    with bot.app.test_client() as client:
        # Test internal self-ping
        r_self = client.get("/keep-alive", headers={"X-Self-Ping": "1"})
        # Test external ping (simulating GitHub Actions / UptimeRobot)
        r_ext = client.get("/health", headers={"User-Agent": "GitHubActions-KeepAlive/1.0"})
        # Test status endpoint
        r_status = client.get("/api/keep-alive/status")
        ka_data = r_status.get_json()

        ok = (
            r_self.status_code == 200
            and r_ext.status_code == 200
            and ka_data.get("self_ping_count", 0) >= 1
            and ka_data.get("external_ping_count", 0) >= 1
        )
        print(f"  {'✅ PASS' if ok else '❌ FAIL'} | /keep-alive & /health & /api/keep-alive/status")
        print(f"         Telemetry: {json.dumps(ka_data)}")
        results["keep_alive_endpoints"] = {"ok": ok, "telemetry": ka_data}

    print("\n" + "=" * 70)
    print("🎉 DIAGNOSTIC TEST SUMMARY COMPLETE")
    print("=" * 70)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test all anti-block, AI scheduling, and keep-alive systems")
    parser.add_argument("--username", type=str, default="", help="TikTok username to test with")
    parser.add_argument("--test-download", action="store_true", help="Also test full MP4 download")
    args = parser.parse_args()

    configured_users = config._get_tiktok_usernames()
    target_user = args.username or (configured_users[0] if configured_users else "tiktok")
    run_all_tests(username=target_user, test_download=args.test_download)
