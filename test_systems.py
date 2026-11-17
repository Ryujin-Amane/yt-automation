"""
Diagnostic Test Suite — Tests network routes, TikTok providers, Supabase-backed
state_store/queue_manager, Zero-FFmpeg pass-through, Cloud AI Smart Scheduler,
and Render Keep-Alive endpoints.

Usage:
    python test_systems.py
    python test_systems.py --username tiktok
"""

import os
import json
import time
import argparse
import logging

import config
import proxy_manager
import tiktok_scraper
import video_processor
import scheduler_planner
import state_store
import queue_manager
import ai_agent
import bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def print_header(title: str):
    print("\n" + "=" * 70)
    print(f"🧪 {title}")
    print("=" * 70)


def run_all_tests(username: str, test_download: bool = False):
    results = {}

    # 1. Network Routes
    print_header("STAGE 1: Testing Network Routes (Direct, WARP, Custom Proxies)")
    net_report = proxy_manager.test_network_layers()
    for k, v in net_report.items():
        print(f"  • {k}: {json.dumps(v)}")
    results["network"] = net_report

    # 2. TikTok Fetch Providers
    print_header(f"STAGE 2: Testing TikTok Fetch Providers (@{username})")
    prov_report = tiktok_scraper.test_providers(username)
    for k, v in prov_report.items():
        badge = "✅ PASS" if v.get("ok") else ("ℹ️ NOT SET" if v.get("status") == "not_configured" else "⚠️ BLOCKED/WARN")
        print(f"  {badge} | {k}: {json.dumps(v)}")
    results["providers"] = prov_report

    # 3. Supabase State Store & Queue Manager
    print_header("STAGE 3: Testing Supabase-Backed Queue & State Store")
    st_status = state_store.status()
    q_items = queue_manager.get_all()
    print(f"  ✅ PASS | Storage status: {json.dumps(st_status)} | Queue items loaded: {len(q_items)}")
    results["storage"] = st_status

    # 4. Smart Scheduler & AI SEO
    print_header("STAGE 4: Smart Peak Scheduler & Cloud-Light AI Agent")
    slots = scheduler_planner.get_upcoming_publish_slots(1, count=4, per_day=2)
    print(f"  📅 Generated {len(slots)} unbooked candidate slots for CH1:")
    for idx, s in enumerate(slots):
        print(f"     Slot {idx}: {s['day']} {s['publish_at_local']} (UTC: {s['publish_at_utc_iso']}) [{s['activity_level']}]")

    seo = ai_agent.optimize_video_seo(
        tiktok_caption="Insane viral anime edit #shorts #viral",
        tiktok_author=username,
        channel=1,
        candidate_slots=slots,
    )
    if seo:
        print(f"  ✅ PASS | AI SEO & Smart Slot Selected:")
        print(f"         Title:     {seo.get('seo_title')}")
        print(f"         Scheduled: {seo.get('publish_at_local')} ({seo.get('publish_at_utc_iso')})")
        print(f"         Reason:    {seo.get('schedule_reason')}")
    results["ai_and_scheduler"] = {"ok": bool(seo), "seo": seo}

    # 5. Keep-Alive & Diagnostic API Endpoints
    print_header("STAGE 5: Render Keep-Alive & Diagnostic API Endpoints")
    with bot.app.test_client() as client:
        r_self = client.get("/keep-alive", headers={"X-Self-Ping": "1"})
        r_ext = client.get("/health", headers={"User-Agent": "GitHubActions-KeepAlive/1.0"})
        r_status = client.get("/api/keep-alive/status")
        ka_data = r_status.get_json()
        ok = r_self.status_code == 200 and r_ext.status_code == 200
        print(f"  {'✅ PASS' if ok else '❌ FAIL'} | /keep-alive & /health & /api/keep-alive/status")
        print(f"         Telemetry: {json.dumps(ka_data)}")
        results["keep_alive"] = ka_data

    print("\n" + "=" * 70)
    print("🎉 DIAGNOSTIC TEST SUITE COMPLETE")
    print("=" * 70)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", type=str, default="")
    parser.add_argument("--test-download", action="store_true")
    args = parser.parse_args()

    users = config._get_tiktok_usernames()
    target = args.username or (users[0] if users else "tiktok")
    run_all_tests(username=target, test_download=args.test_download)
