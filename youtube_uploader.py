"""
YouTube Uploader — handles OAuth2 authentication and video uploads
for two YouTube channels with automatic quota management.

Setup:
  1. Create a Google Cloud project at https://console.cloud.google.com
  2. Enable YouTube Data API v3
  3. Create OAuth 2.0 credentials (Desktop App type)
  4. Run `python auth_setup.py` locally to generate token JSON for each channel
  5. Paste the token JSON strings into YOUTUBE_TOKEN_1 and YOUTUBE_TOKEN_2 env vars
"""

import os
import json
import logging
import tempfile

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from datetime import date

import config
import database

log = logging.getLogger(__name__)

# YouTube API constants
YOUTUBE_API_SERVICE = "youtube"
YOUTUBE_API_VERSION = "v3"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _build_credentials(channel: int) -> Credentials | None:
    """Build Google OAuth2 credentials from env var token JSON."""
    token_data = config.get_youtube_token(channel)
    client_secret = config.get_youtube_client_secret(channel)

    if not token_data:
        log.error(f"No token found for YouTube Channel {channel}")
        return None

    try:
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)

        # Refresh if expired
        if creds and creds.expired and creds.refresh_token:
            log.info(f"🔄 Refreshing token for Channel {channel}...")
            creds.refresh(Request())

            # Save the refreshed token back to file if it exists locally
            token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"token_channel_{channel}.json")
            new_token = {
                "token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_uri": creds.token_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scopes": list(creds.scopes) if creds.scopes else SCOPES,
            }
            if os.path.exists(token_file):
                try:
                    with open(token_file, "w", encoding="utf-8") as f:
                        json.dump(new_token, f, indent=2)
                    log.info(f"💾 Updated refreshed token saved to {token_file}")
                except Exception as ex:
                    log.warning(f"Could not save refreshed token to file: {ex}")

            log.info(f"✅ Token refreshed for Channel {channel}")
            log.debug(f"   New token preview: {json.dumps(new_token)[:80]}...")

        return creds

    except Exception as e:
        log.error(f"Failed to build credentials for Channel {channel}: {e}")
        return None


def _get_youtube_service(channel: int):
    """Create an authenticated YouTube API service object."""
    creds = _build_credentials(channel)
    if not creds:
        return None

    try:
        service = build(
            YOUTUBE_API_SERVICE,
            YOUTUBE_API_VERSION,
            credentials=creds,
        )
        return service
    except Exception as e:
        log.error(f"Failed to build YouTube service for Channel {channel}: {e}")
        return None


def _format_description(tiktok_caption: str, tiktok_author: str) -> str:
    """Format the YouTube video description."""
    desc = tiktok_caption.strip() if tiktok_caption else ""

    # The YouTube API forbids angle brackets (< >) in descriptions
    desc = desc.replace("<", "").replace(">", "").strip()

    # Truncate to ensure we don't exceed YouTube's 5000 char limit
    if len(desc) > 4500:
        desc = desc[:4500] + "..."

    # Ensure hashtags always have the '#' prefix and are space-separated
    raw_tags = config.get_custom_hashtags().replace(',', ' ').split()
    proper_hashtags = " ".join([f"#{t.replace('#', '').strip()}" for t in raw_tags if t.strip()])

    hashtags = f"\n\n{proper_hashtags}" if proper_hashtags else ""

    return f"{desc}{hashtags}"


def _format_title(tiktok_caption: str) -> str:
    """
    Create a YouTube title from TikTok caption.
    YouTube titles max 100 chars and cannot contain < or >.
    """
    title = tiktok_caption.strip() if tiktok_caption else ""

    # Remove hashtags from title (keep them in description only)
    parts = title.split("#")
    clean_title = parts[0].strip() if parts else title

    # The YouTube API STRICTLY forbids angle brackets (< >) in titles
    clean_title = clean_title.replace("<", "").replace(">", "").strip()

    if not clean_title:
        clean_title = "Check this out!"

    # Truncate to 100 chars
    if len(clean_title) > 95:
        clean_title = clean_title[:92] + "..."

    return clean_title


def upload_video(
    video_path: str,
    tiktok_caption: str,
    tiktok_author: str,
    tiktok_id: str,
    target_channel: int = 0,
    seo_description: str = "",
    seo_hashtags: str = "",
    publish_at: str | None = None,
    allow_fallback: bool = False,
) -> tuple[bool, int]:
    """
    Upload a video to YouTube, automatically selecting the channel
    with remaining quota. If a channel hits a quota error, it retries
    on the next available channel.

    Args:
        target_channel: If > 0, attempt this channel first before falling
                        back to auto-selection.
        publish_at: Optional ISO 8601 UTC timestamp string (e.g. '2026-10-31T12:30:00Z').
                    If provided, privacyStatus is set to 'private' and YouTube
                    automatically publishes the video at that time.

    Returns:
        (success: bool, channel_used: int)
    """
    if not os.path.exists(video_path):
        log.error(f"Video file not found: {video_path}")
        return False, 0

    # ── Prepare metadata (only once) ────────────────────
    title = _format_title(tiktok_caption)

    if seo_description or seo_hashtags:
        description = seo_description.strip()
        if seo_hashtags:
            description += f"\n\n{seo_hashtags.strip()}"
            
        raw_tags = seo_hashtags.replace(',', ' ').split()
        yt_tags = [t.replace("#", "").strip() for t in raw_tags if t.strip()]
    else:
        description = _format_description(tiktok_caption, tiktok_author)
        raw_tags = config.get_custom_hashtags().replace(',', ' ').split()
        yt_tags = [t.replace("#", "").strip() for t in raw_tags if t.strip()]

    if not yt_tags:
        yt_tags = ["shorts", "viral", "trending", "fyp", "tiktok"]

    # YouTube rejects uploads whose tags exceed ~500 chars total ("invalidTags")
    safe_tags, total = [], 0
    for t in dict.fromkeys(t[:30] for t in yt_tags if t):
        cost = len(t) + (2 if " " in t else 0) + 1
        if total + cost > 450:
            break
        safe_tags.append(t)
        total += cost
    yt_tags = safe_tags

    # Description limit is 5000 bytes
    description = description.replace("<", "").replace(">", "")
    while len(description.encode("utf-8")) > 4900:
        description = description[:-100]

    status_dict = {
        "selfDeclaredMadeForKids": False,
    }

    if publish_at:
        # YouTube requires privacyStatus to be "private" when publishAt is set
        status_dict["privacyStatus"] = "private"
        status_dict["publishAt"] = publish_at
        log.info(f"📅 Setting video schedule: private until {publish_at} (UTC)")
    else:
        status_dict["privacyStatus"] = "public"

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": yt_tags,
            "categoryId": "22",  # People & Blogs
        },
        "status": status_dict,
    }

    # ── Retry loop ──
    # When a specific target channel is requested (queue/batch flows), we do NOT
    # silently fall over to another channel — that would desync the queue status
    # and the per-channel publish schedule. Pass allow_fallback=True to opt in.
    tried_channels = set()
    first_try = True
    while True:
        if first_try and target_channel > 0:
            channel = target_channel
            first_try = False
        else:
            if target_channel > 0 and not allow_fallback:
                return False, 0
            first_try = False
            channel = _pick_channel(tried_channels)

        if channel == 0:
            log.warning("⚠️  All configured channels have hit their daily upload limit!")
            return False, 0

        tried_channels.add(channel)
        log.info(f"📤 Uploading to Channel {channel}...")

        service = _get_youtube_service(channel)
        if not service:
            log.error(f"Could not authenticate Channel {channel}, skipping to next...")
            continue

        try:
            media = MediaFileUpload(
                video_path,
                mimetype="video/mp4",
                resumable=True,
                chunksize=1024 * 1024,
            )

            request = service.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            )

            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    progress = int(status.progress() * 100)
                    log.info(f"   ⏫ Upload progress: {progress}%")

            youtube_id = response.get("id", "unknown")
            log.info(f"🎉 Upload successful! YouTube ID: {youtube_id}")
            log.info(f"   🔗 https://youtube.com/shorts/{youtube_id}")

            database.mark_video_processed(
                tiktok_id=tiktok_id,
                tiktok_user=tiktok_author,
                youtube_id=youtube_id,
                channel_used=channel,
                caption=tiktok_caption[:500],
                scheduled_at=publish_at,
            )

            return True, channel

        except Exception as e:
            error_msg = str(e).lower()
            log.error(f"Upload failed on Channel {channel}: {e}")

            if "quota" in error_msg or "limit" in error_msg or "403" in error_msg:
                log.warning(f"⚠️ Channel {channel} hit YouTube API quota! Trying next channel...")
                continue  # retry the same video on next channel
            else:
                return False, 0


def _pick_channel(tried_channels: set = None) -> int:
    """
    Pick which YouTube channel to upload to.

    Strategy:
      Iterate through all configured channels. Return the first channel
      that hasn't hit the `MAX_UPLOADS_PER_CHANNEL` limit today and
      hasn't been tried yet in the current upload loop.

    Returns the channel number (e.g. 1, 2, 3) or 0 if all are exhausted.
    """
    if tried_channels is None:
        tried_channels = set()

    max_per = config.get_max_uploads_per_channel()
    channels = config.get_available_channels()

    if not channels:
        return 0

    for channel in channels:
        if channel in tried_channels:
            continue

        count = database.get_today_upload_count(channel)
        log.info(f"📊 Today's uploads — CH{channel}: {count}/{max_per}")
        if count < max_per:
            return channel

    return 0
