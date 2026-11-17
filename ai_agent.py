"""
AI Agent Module — Cloud-Native Lightweight Viral SEO & Smart Upload Scheduler.

Optimized for Render Free Tier (0% local CPU video/audio decoding, <80MB RAM):
  1. Uses lightweight thumbnail vision (in-memory cover image) + TikTok metadata
     instead of heavy local OpenCV / faster-whisper CPU extraction.
  2. Live RAG Trend Scouting via Official YouTube Data API v3.
  3. AI-Driven Smart Scheduling: analyzes candidate peak slots for the target
     YouTube channel and selects the exact best publication time for the video's
     niche and audience vibe.
"""

import os
import json
import logging
import base64
import time
import requests
from groq import Groq
from google import genai
from google.genai import types
from pydantic import BaseModel

import config
import scheduler_planner

log = logging.getLogger(__name__)


def _get_groq_client():
    """Initialize the Groq client."""
    if not config.GROQ_API_KEY:
        log.error("❌ No GROQ_API_KEY configured! Set it in your .env file.")
        return None
    return Groq(api_key=config.GROQ_API_KEY)


def _get_gemini_client():
    """Initialize the Gemini client."""
    if not config.GEMINI_API_KEY:
        log.error("❌ No GEMINI_API_KEY configured! Set it in your .env file.")
        return None
    return genai.Client(api_key=config.GEMINI_API_KEY)


# Stage 1 Schema
class VideoContext(BaseModel):
    niche: str
    subjects: list[str]
    vibe: str


# Final Stage Schema (SEO + AI Smart Slot Selection)
class VideoSEOAndSchedule(BaseModel):
    seo_title: str
    seo_description: str
    seo_hashtags: str
    selected_slot_index: int
    schedule_reason: str


def _fetch_cover_bytes(cover_url: str) -> bytes | None:
    """Download the lightweight video cover thumbnail in memory (~30KB, zero disk/CPU overhead)."""
    if not cover_url or not cover_url.startswith("http"):
        return None
    try:
        resp = requests.get(
            cover_url,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"},
        )
        if resp.status_code == 200 and len(resp.content) > 500:
            return resp.content
    except Exception as e:
        log.debug(f"  Could not fetch cover thumbnail: {e}")
    return None


def _scrape_youtube_trends(search_query: str, max_results: int = 5) -> str:
    """Scrape live YouTube search for trending videos in this niche via Official YouTube API."""
    log.info(f"  🔍 Scouting live YouTube trends for: '{search_query}'")
    try:
        if not config.YOUTUBE_API_KEY:
            log.warning("  ⚠️ YOUTUBE_API_KEY not set — skipping live trend lookup.")
            return "No trend data found."

        from googleapiclient.discovery import build
        import html

        youtube = build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY, cache_discovery=False)
        request = youtube.search().list(
            part="snippet",
            q=search_query + " shorts",
            type="video",
            maxResults=max_results,
            order="viewCount",
        )
        response = request.execute()

        trend_data = []
        for item in response.get("items", []):
            title = item.get("snippet", {}).get("title", "")
            clean_title = html.unescape(title)
            trend_data.append(f"- Title: {clean_title} | Views: High (Trend)")

        if trend_data:
            result_str = "\n".join(trend_data)
            log.info(
                "  🔥 Top Trending Competitors Found (via Official API Key):\n"
                + "\n".join([f"     {line}" for line in trend_data])
            )
            return result_str
        return "No trend data found."
    except Exception as e:
        log.warning(f"  ⚠️ YouTube trend lookup failed: {e}")
        return "Failed to fetch trend data."


def _format_slots_for_prompt(candidate_slots: list[dict]) -> str:
    """Format candidate publish slots for the AI prompt."""
    if not candidate_slots:
        return "0: Default next available peak slot"
    lines = []
    for idx, s in enumerate(candidate_slots):
        lines.append(
            f"Slot {idx}: {s.get('day', '')} at {s.get('publish_at_local', '')} "
            f"(UTC: {s.get('publish_at_utc_iso', '')}) — "
            f"Window: {s.get('slot_label', 'peak').upper()}, "
            f"Viewer Activity: {s.get('activity_level', 'HIGH')}"
        )
    return "\n".join(lines)


def _analyze_with_gemini(
    client,
    initial_prompt: str,
    cover_bytes: bytes | None,
    candidate_slots: list[dict],
) -> dict | None:
    try:
        log.info("  🧠 [Stage 1] Gemini Flash extracting video context (Cloud-Light Mode)...")
        contents = [initial_prompt]
        if cover_bytes:
            contents.append(types.Part.from_bytes(data=cover_bytes, mime_type="image/jpeg"))

        context_response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config={
                "response_mime_type": "application/json",
                "response_schema": VideoContext,
                "temperature": 0.2,
            },
        )

        context_result = context_response.parsed
        subjects_str = " ".join(context_result.subjects)
        search_query = f"{context_result.niche} {subjects_str}".strip()

        # Stage 2: Scrape Trends
        trend_data = _scrape_youtube_trends(search_query)

        time.sleep(2)

        # Stage 3: Synthesize Final SEO + Select Optimal Publish Slot
        log.info("  🧠 [Stage 3] Gemini Flash synthesizing viral SEO & selecting optimal upload time...")
        slots_text = _format_slots_for_prompt(candidate_slots)

        synthesis_prompt = f"""You are the absolute best YouTube Shorts SEO & Audience Growth Strategist in the world.
Your goal is to engineer a viral hit AND schedule it at the exact best time slot for maximum viewer velocity.

Here is the exact context of the video we are uploading:
- Niche: {context_result.niche}
- Subjects: {', '.join(context_result.subjects)}
- Vibe: {context_result.vibe}

Here is the LIVE TREND DATA of the top 5 competing shorts in this exact niche right now:
{trend_data}

Here are the AVAILABLE UPCOMING PUBLISH SLOTS for this YouTube channel (already filtered so no slot conflicts):
{slots_text}

CRITICAL INSTRUCTIONS:
1. TITLE & DESCRIPTION:
   - Make the title punchy, curiosity-inducing, and under 90 characters.
   - Make the description engaging, relatable, or ask a question that drives comments.
   - Incorporate winning hooks from the live trend data without copying verbatim.
2. SMART SCHEDULING (selected_slot_index & schedule_reason):
   - Choose the best Slot Index (0 to {max(0, len(candidate_slots) - 1)}) that matches this video's vibe and viewer behavior, prioritizing early chronological high-activity windows so the channel maintains consistent daily momentum.
   - Provide a brief 1-sentence `schedule_reason` explaining why this slot is optimal."""

        synthesis_response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[synthesis_prompt],
            config={
                "response_mime_type": "application/json",
                "response_schema": VideoSEOAndSchedule,
                "temperature": 0.85,
            },
        )

        result = synthesis_response.parsed
        title = result.seo_title.strip()
        desc = result.seo_description.strip()
        tags = result.seo_hashtags.strip()
        slot_idx = int(result.selected_slot_index or 0)
        reason = result.schedule_reason.strip()

        if not title:
            log.warning("  ⚠️ Gemini returned empty title, falling back")
            return None

        return {
            "seo_title": title,
            "seo_description": desc,
            "seo_hashtags": tags,
            "selected_slot_index": slot_idx,
            "schedule_reason": reason,
        }
    except Exception as e:
        log.error(f"  ❌ Gemini analysis failed: {e}")
        return None


def _analyze_with_groq(
    client,
    prompt_text: str,
    cover_bytes: bytes | None,
    candidate_slots: list[dict],
) -> dict | None:
    slots_text = _format_slots_for_prompt(candidate_slots)
    full_prompt = (
        f"{prompt_text}\n\n"
        f"AVAILABLE UPCOMING PUBLISH SLOTS:\n{slots_text}\n\n"
        "Respond strictly in JSON with keys: "
        '"seo_title", "seo_description", "seo_hashtags", '
        '"selected_slot_index" (integer), and "schedule_reason" (string).'
    )

    content_items = [{"type": "text", "text": full_prompt}]
    if cover_bytes:
        b64 = base64.b64encode(cover_bytes).decode("utf-8")
        content_items.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    try:
        log.info("  🧠 AI Agent sending payload to Groq...")
        response = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role": "user", "content": content_items}],
            temperature=0.7,
            response_format={"type": "json_object"},
        )

        text = response.choices[0].message.content.strip()
        result = json.loads(text)

        title = str(result.get("seo_title", "")).strip()
        desc = str(result.get("seo_description", "")).strip()
        tags = str(result.get("seo_hashtags", "")).strip()
        slot_idx = int(result.get("selected_slot_index", 0) or 0)
        reason = str(result.get("schedule_reason", "Selected optimal peak viewer window.")).strip()

        if not title:
            log.warning("  ⚠️ Groq returned empty title, falling back")
            return None

        return {
            "seo_title": title,
            "seo_description": desc,
            "seo_hashtags": tags,
            "selected_slot_index": slot_idx,
            "schedule_reason": reason,
        }
    except Exception as e:
        log.error(f"  ❌ Groq analysis failed: {e}")
        return None


def optimize_video_seo(
    tiktok_caption: str,
    tiktok_author: str,
    video_path: str = "",
    cover_url: str = "",
    channel: int = 1,
    candidate_slots: list[dict] | None = None,
) -> dict | None:
    """
    Lightweight Cloud AI pipeline that generates viral YouTube Shorts metadata
    AND selects the optimal upcoming publication time slot for the target channel.
    """
    # 1. Fetch candidate unbooked peak slots for this channel
    if candidate_slots is None:
        try:
            candidate_slots = scheduler_planner.get_upcoming_publish_slots(
                channel, count=4, per_day=2
            )
        except Exception as e:
            log.warning(f"  ⚠️ Could not compute candidate slots: {e}")
            candidate_slots = []

    # 2. Download tiny cover thumbnail in memory (optional, 0% CPU)
    cover_bytes = _fetch_cover_bytes(cover_url)

    base_prompt = f"""You are an expert YouTube Shorts SEO strategist and growth planner.

I am uploading a vertical video as a YouTube Short on Channel {channel}.
Analyze the provided context (and cover thumbnail if attached) to extract its core niche, subjects, and vibe.

ORIGINAL TIKTOK CAPTION:
"{tiktok_caption}"
Creator Handle: @{tiktok_author}
"""

    result = None
    primary_provider = config.get_ai_primary_provider()
    log.info(f"  🔄 Primary AI Provider: {primary_provider} (Cloud-Light Mode)")

    if primary_provider == "gemini":
        gemini_client = _get_gemini_client()
        if gemini_client:
            result = _analyze_with_gemini(gemini_client, base_prompt, cover_bytes, candidate_slots)

        if not result:
            log.warning("  ⚠️ Falling back to Groq...")
            groq_client = _get_groq_client()
            if groq_client:
                result = _analyze_with_groq(groq_client, base_prompt, cover_bytes, candidate_slots)

    elif primary_provider == "groq":
        groq_client = _get_groq_client()
        if groq_client:
            result = _analyze_with_groq(groq_client, base_prompt, cover_bytes, candidate_slots)

        if not result:
            log.warning("  ⚠️ Falling back to Gemini...")
            gemini_client = _get_gemini_client()
            if gemini_client:
                result = _analyze_with_gemini(gemini_client, base_prompt, cover_bytes, candidate_slots)

    # 3. Resolve chosen schedule slot
    chosen_slot = candidate_slots[0] if candidate_slots else None
    if result and candidate_slots:
        idx = result.get("selected_slot_index", 0)
        if isinstance(idx, int) and 0 <= idx < len(candidate_slots):
            chosen_slot = candidate_slots[idx]

    if result:
        title = result["seo_title"].replace("<", "").replace(">", "").strip()
        desc = result["seo_description"].replace("<", "").replace(">", "").strip()
        tags = result["seo_hashtags"].strip()

        if len(title) > 95:
            title = title[:92] + "..."

        result["seo_title"] = title
        result["seo_description"] = desc
        if chosen_slot:
            result["publish_at_utc_iso"] = chosen_slot["publish_at_utc_iso"]
            result["publish_at_local"] = chosen_slot["publish_at_local"]
            result["publish_day"] = chosen_slot.get("day", "")

        log.info(f"  ✨ AI Title: {title}")
        if chosen_slot:
            log.info(
                f"  📅 AI Scheduled Slot: {chosen_slot['publish_at_local']} "
                f"({result.get('schedule_reason', 'Optimal peak window')})"
            )
        return result

    # Fallback if AI keys hit quota: still return optimal schedule slot so scheduling never breaks
    if chosen_slot:
        return {
            "seo_title": (tiktok_caption or "Check this out!")[:92].replace("<", "").replace(">", ""),
            "seo_description": tiktok_caption or "",
            "seo_hashtags": config.get_custom_hashtags(),
            "publish_at_utc_iso": chosen_slot["publish_at_utc_iso"],
            "publish_at_local": chosen_slot["publish_at_local"],
            "publish_day": chosen_slot.get("day", ""),
            "schedule_reason": "Fallback to top peak viewer window",
        }

    return None
