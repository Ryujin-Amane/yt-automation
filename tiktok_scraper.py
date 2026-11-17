"""
TikTok Scraper — Multi-Layered Anti-Block Fetcher & Downloader.

Combines 4 independent extraction/proxy layers so datacenter IPs (like Render)
never get blocked by TikTok:
  1. Custom Rotating Proxies (HTTP / SOCKS5 via proxy_manager)
  2. Cloudflare WARP Userspace VPN (wireproxy socks5://127.0.0.1:1080)
  3. yt-dlp with Chrome TLS Impersonation (curl_cffi) + Patched WAF JS Challenge Solver
  4. TikWM API Fallback (fetches user feeds & HD watermark-free MP4s via TikWM infrastructure)

Also optimized for Render Free Tier (0% FFmpeg usage):
  - Downloads single pre-muxed MP4 streams (video+audio combined) so FFmpeg
    is never required to merge or re-encode streams.
"""

import os
import re
import time
import logging
import requests
import yt_dlp
import config
import proxy_manager

log = logging.getLogger(__name__)

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
    IMPERSONATE_TARGET = ImpersonateTarget("chrome")
except Exception:
    IMPERSONATE_TARGET = None


# ══════════════════════════════════════════════════════════════
#  Monkey-patch: Fix yt-dlp TikTokUserIE to solve WAF challenge
#  on user profile pages (yt-dlp only does this for video pages)
# ══════════════════════════════════════════════════════════════
try:
    from yt_dlp.extractor.tiktok import TikTokUserIE, traverse_obj, ExtractorError

    _orig_tiktok_user_real_extract = TikTokUserIE._real_extract

    def _patched_tiktok_user_real_extract(self, url):
        """Patched _real_extract that solves WAF JS challenges on user pages."""
        user_name = self._match_id(url)

        # If it's a raw sec_uid, skip patch and use original
        if re.fullmatch(r'MS4wLjABAAAA[\w-]{64}', user_name):
            return _orig_tiktok_user_real_extract(self, url)

        # Step 1: Download user webpage
        webpage = self._download_webpage(
            self._UPLOADER_URL_FORMAT % user_name, user_name,
            'Downloading user webpage', 'Unable to download user webpage',
            impersonate=True, fatal=False,
            headers=self._generate_blockbuster_headers()) or ''

        # Step 2: Try to extract universal data
        universal_data = self._get_universal_data(webpage, user_name)

        # Step 3: If no data, solve the WAF JS challenge (the key fix!)
        if not universal_data and webpage:
            try:
                cookie_names = self._solve_challenge_and_set_cookies(webpage)
                webpage = self._download_webpage(
                    self._UPLOADER_URL_FORMAT % user_name, user_name,
                    'Re-downloading user webpage after challenge',
                    fatal=False, impersonate=True,
                    headers=self._generate_blockbuster_headers()) or ''
                for cookie_name in filter(None, cookie_names or []):
                    try:
                        self.cookiejar.clear(
                            domain='.tiktok.com', path='/', name=cookie_name)
                    except KeyError:
                        pass
                universal_data = self._get_universal_data(webpage, user_name)
            except Exception as e:
                log.debug(f"WAF challenge solve failed for @{user_name}: {e}")

        # Step 4: Extract sec_uid from the data
        detail = traverse_obj(
            universal_data, ('webapp.user-detail', {dict})) or {}

        video_count = traverse_obj(
            detail, ('userInfo', ('stats', 'statsV2'),
                     'videoCount', {int}, any))
        if not video_count and detail.get('statusCode') == 10222:
            self.raise_login_required(
                "This user's account is private. "
                "Log into an account that has access")
        elif video_count == 0:
            raise ExtractorError(
                'This account does not have any videos posted', expected=True)

        sec_uid = traverse_obj(
            detail, ('userInfo', 'user', 'secUid', {str}))

        if sec_uid:
            fail_early = not traverse_obj(
                detail, ('userInfo', 'itemList', ...))
        else:
            sec_uid = self._extract_sec_uid_from_embed(user_name)
            fail_early = False

        if not sec_uid:
            return _orig_tiktok_user_real_extract(self, url)

        return self.playlist_result(
            self._entries(sec_uid, user_name, fail_early),
            sec_uid, user_name)

    TikTokUserIE._real_extract = _patched_tiktok_user_real_extract
    log.info("🔧 Patched TikTokUserIE with WAF challenge solver")

except Exception as patch_err:
    log.warning(f"⚠️ Could not patch TikTokUserIE: {patch_err}")


def _parse_entries(entries: list, username: str) -> list[dict]:
    """Parse yt-dlp entries into our standardized video dict format."""
    results = []
    for entry in entries:
        if entry is None:
            continue

        vid_id = str(entry.get("id", ""))
        if not vid_id:
            continue

        caption = entry.get("description", entry.get("title", ""))
        duration = entry.get("duration", 0)
        width = entry.get("width", 0) or 0
        height = entry.get("height", 0) or 0
        cover = entry.get("thumbnail", "")
        video_timestamp = entry.get("timestamp", 0)
        play_url = entry.get("url", "") or f"https://www.tiktok.com/@{username}/video/{vid_id}"

        results.append({
            "id": vid_id,
            "title": caption or "",
            "play": play_url,
            "duration": duration or 0,
            "width": width,
            "height": height,
            "cover": cover or "",
            "author": username,
            "timestamp": video_timestamp or 0,
        })

    return results


def _base_ydl_opts(proxy_url: str | None = None) -> dict:
    """Return base yt-dlp options shared across extraction modes."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "no_color": True,
        "socket_timeout": 25,
        "retries": 2,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.tiktok.com/",
        },
    }
    if IMPERSONATE_TARGET:
        opts["impersonate"] = IMPERSONATE_TARGET
    if proxy_url:
        opts["proxy"] = proxy_url
    return opts


# ══════════════════════════════════════════════════════════════
#  Layer A: TikWM API Client (Zero-Block Datacenter Fallback)
# ══════════════════════════════════════════════════════════════

TIKWM_USER_API = "https://www.tikwm.com/api/user/posts"
TIKWM_VIDEO_API = "https://www.tikwm.com/api/"


def fetch_user_videos_tikwm(username: str, count: int = 5) -> list[dict]:
    """
    Fetch latest videos for a TikTok user via TikWM public API.
    Works reliably even when datacenter IPs are blocked by TikTok's WAF.
    """
    clean_user = username.lstrip("@").strip()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://www.tikwm.com/",
    }

    # Try routes (usually direct works great with TikWM, or via proxy if needed)
    routes = proxy_manager.get_ordered_proxy_routes()
    # Put direct first for TikWM since TikWM itself is a proxy gateway and doesn't block Render IPs
    direct_first_routes = sorted(routes, key=lambda r: 0 if r["type"] == "direct" else 1)

    for route in direct_first_routes[:2]:
        proxy_url = route.get("proxy")
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        try:
            resp = requests.post(
                TIKWM_USER_API,
                data={"unique_id": f"@{clean_user}", "count": max(count, 10), "cursor": 0},
                headers=headers,
                proxies=proxies,
                timeout=20,
            )
            if resp.status_code != 200:
                continue

            payload = resp.json()
            if payload.get("code") != 0:
                log.debug(f"  TikWM API returned code {payload.get('code')}: {payload.get('msg')}")
                continue

            videos_raw = (payload.get("data") or {}).get("videos") or []
            if not videos_raw:
                continue

            results = []
            for v in videos_raw[:count]:
                vid_id = str(v.get("video_id", "")).strip()
                if not vid_id:
                    continue

                play_url = v.get("hdplay") or v.get("play") or v.get("wmplay") or ""
                if play_url and play_url.startswith("/"):
                    play_url = f"https://www.tikwm.com{play_url}"

                cover_url = v.get("cover") or v.get("origin_cover") or ""
                if cover_url and cover_url.startswith("/"):
                    cover_url = f"https://www.tikwm.com{cover_url}"

                results.append({
                    "id": vid_id,
                    "title": v.get("title", "") or "",
                    "play": play_url or f"https://www.tiktok.com/@{clean_user}/video/{vid_id}",
                    "duration": int(v.get("duration", 0) or 0),
                    "width": int(v.get("width", 1080) or 1080),
                    "height": int(v.get("height", 1920) or 1920),
                    "cover": cover_url,
                    "author": clean_user,
                    "timestamp": int(v.get("create_time", 0) or 0),
                })

            if results:
                proxy_manager.record_route_result("tikwm_api", True)
                log.info(f"📥 [TikWM API] Fetched {len(results)} videos from @{clean_user}")
                return results

        except Exception as e:
            log.debug(f"  TikWM fetch error via {route['name']} for @{clean_user}: {e}")
            proxy_manager.record_route_result("tikwm_api", False, str(e))

    return []


def download_video_tikwm(tiktok_id: str, author: str, output_path: str, direct_stream_url: str = "") -> str | None:
    """
    Download HD no-watermark MP4 using TikWM API or a pre-fetched TikWM stream URL.
    Requires zero FFmpeg (downloads single pre-muxed MP4 directly).
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.tikwm.com/",
    }

    stream_url = direct_stream_url if (direct_stream_url and "tikwm.com" in direct_stream_url) else ""

    if not stream_url:
        tiktok_page_url = (
            f"https://www.tiktok.com/@{author}/video/{tiktok_id}"
            if author else f"https://www.tiktok.com/video/{tiktok_id}"
        )
        try:
            log.info(f"  🌐 [TikWM] Resolving HD stream for {tiktok_id}...")
            resp = requests.post(
                TIKWM_VIDEO_API,
                data={"url": tiktok_page_url, "hd": 1},
                headers=headers,
                timeout=20,
            )
            if resp.status_code == 200:
                payload = resp.json()
                data = payload.get("data") or {}
                stream_url = data.get("hdplay") or data.get("play") or data.get("wmplay") or ""
                if stream_url and stream_url.startswith("/"):
                    stream_url = f"https://www.tikwm.com{stream_url}"
        except Exception as e:
            log.debug(f"  [TikWM] Stream resolution failed for {tiktok_id}: {e}")

    if not stream_url:
        return None

    try:
        log.info(f"  ⬇️ [TikWM] Downloading MP4 stream for {tiktok_id}...")
        with requests.get(stream_url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        f.write(chunk)

        if os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            log.info(f"  ✅ [TikWM] Downloaded {tiktok_id} ({size_mb:.1f} MB)")
            proxy_manager.record_route_result("tikwm_download", True)
            return output_path
        else:
            if os.path.exists(output_path):
                os.remove(output_path)
    except Exception as e:
        log.debug(f"  [TikWM] MP4 download error for {tiktok_id}: {e}")
        proxy_manager.record_route_result("tikwm_download", False, str(e))
        if os.path.exists(output_path):
            os.remove(output_path)

    return None


# ══════════════════════════════════════════════════════════════
#  Layer B: yt-dlp with Proxy Chain + TLS Impersonation + WAF Solver
# ══════════════════════════════════════════════════════════════

def fetch_user_videos_ytdlp(username: str, count: int = 5) -> list[dict]:
    """
    Fetch latest videos for a TikTok user using yt-dlp across configured proxy routes.
    Tries fast flat extraction first, then full WAF-solver extraction if needed.
    """
    url = f"https://www.tiktok.com/@{username}"
    routes = proxy_manager.get_ordered_proxy_routes()

    for route in routes:
        route_name = route["name"]
        proxy_url = route["proxy"]

        # Pass 1: Fast flat extraction
        flat_opts = _base_ydl_opts(proxy_url)
        flat_opts["extract_flat"] = True
        flat_opts["playlistend"] = count

        try:
            with yt_dlp.YoutubeDL(flat_opts) as ydl:
                log.info(f"  🔄 yt-dlp extracting @{username} (flat, route={route_name})...")
                info = ydl.extract_info(url, download=False)
                if info:
                    entries = info.get("entries", [])
                    if not entries and info.get("id"):
                        entries = [info]
                    if entries:
                        results = _parse_entries(entries, username)
                        if results:
                            proxy_manager.record_route_result(route_name, True)
                            log.info(f"📥 yt-dlp fetched {len(results)} videos from @{username} (flat, route={route_name})")
                            return results
        except Exception as e:
            log.debug(f"  Flat extraction failed on {route_name} for @{username}: {e}")

        # Pass 2: Full extraction (triggers patched WAF challenge solver)
        log.info(f"  🔁 Retrying @{username} with full WAF solver (route={route_name})...")
        full_opts = _base_ydl_opts(proxy_url)
        full_opts["extract_flat"] = False
        full_opts["playlistend"] = count
        full_opts["socket_timeout"] = 45
        full_opts["retries"] = 2

        try:
            with yt_dlp.YoutubeDL(full_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    entries = info.get("entries", [])
                    if not entries and info.get("id"):
                        entries = [info]
                    if entries:
                        results = _parse_entries(entries, username)
                        if results:
                            proxy_manager.record_route_result(route_name, True)
                            log.info(f"📥 yt-dlp fetched {len(results)} videos from @{username} (full, route={route_name})")
                            return results
        except Exception as e:
            log.debug(f"  Full extraction failed on {route_name} for @{username}: {e}")
            proxy_manager.record_route_result(route_name, False, str(e))

    return []


def download_video_ytdlp(tiktok_page_url: str, tiktok_id: str, output_path: str) -> str | None:
    """
    Download a TikTok video using yt-dlp across configured proxy routes.
    Uses single pre-muxed MP4 format ('best[ext=mp4]/b[ext=mp4]/best') so FFmpeg
    is NEVER needed to merge separate video+audio streams on Render Free Tier.
    """
    routes = proxy_manager.get_ordered_proxy_routes()

    for route in routes:
        route_name = route["name"]
        proxy_url = route["proxy"]

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "no_color": True,
            "outtmpl": output_path,
            # Pre-muxed single MP4 stream (contains both video & audio — zero FFmpeg required!)
            "format": "best[ext=mp4]/b[ext=mp4]/best",
            "format_sort": ["res:1080", "fps:60", "vcodec:h264", "br"],
            "socket_timeout": 90,
            "retries": 3,
            "overwrites": True,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.tiktok.com/",
            },
        }

        if IMPERSONATE_TARGET:
            ydl_opts["impersonate"] = IMPERSONATE_TARGET
        if proxy_url:
            ydl_opts["proxy"] = proxy_url

        try:
            log.info(f"  ⬇️ yt-dlp downloading {tiktok_id} (route={route_name})...")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([tiktok_page_url])

            if os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
                file_size = os.path.getsize(output_path)
                proxy_manager.record_route_result(route_name, True)
                log.info(f"  ✅ Downloaded {tiktok_id} via yt-dlp/{route_name} ({file_size / 1024 / 1024:.1f} MB)")
                return output_path
            else:
                if os.path.exists(output_path):
                    os.remove(output_path)
        except Exception as e:
            log.warning(f"  ⚠️ yt-dlp download failed on {route_name}: {e}")
            proxy_manager.record_route_result(route_name, False, str(e))
            if os.path.exists(output_path):
                os.remove(output_path)

    return None


# ══════════════════════════════════════════════════════════════
#  Public Unified API (Used by bot.py & run_batch.py)
# ══════════════════════════════════════════════════════════════

def get_user_videos(username: str, count: int = 5) -> list[dict]:
    """
    Fetch the latest videos from a TikTok user using our multi-layered fallback system:
      - If mode == 'tikwm_first': tries TikWM API -> yt-dlp (with proxy chain)
      - Otherwise: tries yt-dlp (with proxy chain: Custom Proxy -> WARP -> Direct) -> TikWM API
    """
    mode = proxy_manager.get_proxy_mode()

    if mode == "tikwm_first":
        videos = fetch_user_videos_tikwm(username, count=count)
        if videos:
            return videos
        log.info(f"  🔁 TikWM empty for @{username}, falling back to yt-dlp proxy chain...")
        return fetch_user_videos_ytdlp(username, count=count)

    # Default ('auto', 'warp', 'custom_proxy', 'direct'):
    videos = fetch_user_videos_ytdlp(username, count=count)
    if videos:
        return videos

    log.info(f"  🔁 yt-dlp blocked/empty for @{username}, falling back to TikWM API...")
    videos = fetch_user_videos_tikwm(username, count=count)
    if videos:
        return videos

    log.error(f"  ❌ All scraper layers failed to fetch videos for @{username}")
    return []


def download_video(video_url: str, tiktok_id: str, author: str = "") -> str | None:
    """
    Download a TikTok video using the multi-layer fallback system (zero FFmpeg required).
    """
    output_path = os.path.join(config.TEMP_DIR, f"tiktok_{tiktok_id}.mp4")

    if os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
        file_size = os.path.getsize(output_path)
        log.info(f"Video {tiktok_id} already downloaded ({file_size / 1024 / 1024:.1f} MB), skipping")
        return output_path

    if author:
        tiktok_page_url = f"https://www.tiktok.com/@{author}/video/{tiktok_id}"
    else:
        tiktok_page_url = video_url

    mode = proxy_manager.get_proxy_mode()

    # If video_url is already a direct TikWM stream or mode is tikwm_first, try TikWM download first
    if mode == "tikwm_first" or (video_url and "tikwm.com" in video_url):
        res = download_video_tikwm(tiktok_id, author, output_path, direct_stream_url=video_url)
        if res:
            return res
        return download_video_ytdlp(tiktok_page_url, tiktok_id, output_path)

    # Otherwise try yt-dlp (via proxy chain) first, then fall back to TikWM API
    res = download_video_ytdlp(tiktok_page_url, tiktok_id, output_path)
    if res:
        return res

    log.info(f"  🔁 yt-dlp download failed for {tiktok_id}, falling back to TikWM HD downloader...")
    return download_video_tikwm(tiktok_id, author, output_path, direct_stream_url=video_url)


def fetch_all_new_videos(max_per_account: int = 5) -> list[dict]:
    """
    Grab ALL eligible Shorts (<= 60s) from each monitored TikTok account.

    Filters out:
      - Videos longer than 60s (not Shorts)
      - Landscape videos (not vertical)
      - Videos older than VIDEO_MAX_AGE_HOURS
      - Already-processed videos

    Returns videos sorted by timestamp (newest first).
    """
    from database import is_video_processed
    from datetime import datetime, timezone, timedelta

    max_age_hours = config.get_video_max_age_hours()
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    cutoff_ts = cutoff_time.timestamp()

    all_new = []
    seen_ids = set()

    usernames = config._get_tiktok_usernames()
    for idx, username in enumerate(usernames):
        if idx > 0:
            # Polite 1.2s delay between accounts to avoid rate-limiting
            time.sleep(1.2)

        log.info(f"🔍 Checking @{username} for new Shorts...")
        videos = get_user_videos(username, count=max_per_account)

        if not videos:
            log.info(f"  ⏭️ No videos found for @{username}, skipping")
            continue

        added_from_user = 0
        for video in videos:
            vid_id = video["id"]
            if not vid_id or vid_id in seen_ids:
                continue

            duration = video.get("duration", 0)

            # YouTube Shorts must be 60 seconds or less
            if duration > 60:
                log.info(f"  ⏭️ Video {vid_id} from @{username} is {duration}s (>60s), skipping (not a Short)")
                continue

            width = video.get("width", 0)
            height = video.get("height", 0)

            # YouTube Shorts must be vertical or square
            if width > height and width != 0 and height != 0:
                log.info(f"  ⏭️ Video {vid_id} from @{username} is landscape ({width}x{height}), skipping (not a Short)")
                continue

            # Freshness check: skip videos older than cutoff
            video_ts = video.get("timestamp", 0)
            if video_ts and video_ts < cutoff_ts:
                age_hours = (datetime.now(timezone.utc).timestamp() - video_ts) / 3600
                log.info(f"  ⏭️ Video {vid_id} from @{username} is {age_hours:.1f}h old (>{max_age_hours}h), expired — skipping")
                continue

            if is_video_processed(vid_id):
                log.info(f"  ⏭️ Video {vid_id} from @{username} already processed, skipping")
                continue

            log.info(f"  🆕 New Short from @{username}: {vid_id} ({duration}s)")
            seen_ids.add(vid_id)
            all_new.append(video)
            added_from_user += 1

        if added_from_user:
            log.info(f"  📦 Collected {added_from_user} new Shorts from @{username}")

    all_new.sort(key=lambda v: v.get("timestamp", 0), reverse=True)
    log.info(f"🆕 Found {len(all_new)} new fresh Shorts across all accounts")
    return all_new
