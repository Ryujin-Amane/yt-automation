"""
Video Processor — Ultra-Lightweight Pass-Through Mode for Render 24/7 Stability.

All heavy CPU/RAM FFmpeg re-encoding and filter chains have been removed so the
service runs within Render Free Tier CPU and 512MB RAM limits without ever getting
throttled, OOM-killed, or suspended.

Downloaded HD no-watermark MP4 files are validated and passed directly to the
AI SEO & Smart Scheduling pipeline and YouTube Uploader.
"""

import os
import logging

log = logging.getLogger(__name__)


def process_video(input_path: str, tiktok_id: str) -> str | None:
    """
    Validate the downloaded TikTok MP4 and pass it through directly without
    spawning FFmpeg CPU transcoding.
    """
    if not input_path or not os.path.exists(input_path):
        log.error(f"Input file not found: {input_path}")
        return None

    file_size = os.path.getsize(input_path)
    if file_size < 10000:  # Less than 10KB = corrupted/empty download
        log.error(f"Input file too small ({file_size} bytes), likely corrupted")
        return None

    size_mb = file_size / (1024 * 1024)
    log.info(
        f"⚡ Lightweight pass-through (Zero-FFmpeg mode) for {tiktok_id}: "
        f"{os.path.basename(input_path)} ({size_mb:.1f} MB)"
    )
    return input_path


def cleanup_files(*paths: str):
    """Remove temporary video files after upload completes (deduplicates paths safely)."""
    seen = set()
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            if os.path.exists(path):
                os.remove(path)
                log.debug(f"🗑️ Cleaned up: {os.path.basename(path)}")
        except OSError as e:
            log.warning(f"Cleanup failed for {path}: {e}")
