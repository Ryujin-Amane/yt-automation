"""
TikTok Scraper — multi-provider fetcher & downloader (Zero-FFmpeg).

Why multiple providers?  TikTok aggressively blocks datacenter IPs (Render,
AWS, ...). No single free method is reliable from a cloud server, so we try,
in order (configurable with FETCH_PROVIDERS):

  1. scrapecreators — ScrapeCreators API   (needs SCRAPECREATORS_API_KEY)  ← most reliable
  2. apify          — Apify TikTok Scraper (needs APIFY_TOKEN)
  3. ytdlp          — yt-dlp through the proxy chain (custom proxies → WARP → direct)
  4. tikwm          — TikWM public API (skipped automatically if it serves a
                      Cloudflare challenge; we never try to get around that)

Each provider has health tracking + cooldown so a broken one doesn't slow
every run. Downloads always fetch a single pre-muxed MP4, so FFmpeg is never needed.
"""

import os
import time
import logging
import threading

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

# ── Monkey-patch yt-dlp TikTokUserIE to solve WAF JS challenges on profile pages ──
try:
    import re as _re
    from yt_dlp.extractor.tiktok import TikTokUserIE, traverse_obj, ExtractorError

    _orig_tiktok_user_real_extract = TikTokUserIE._real_extract

    def _patched_tiktok_user_real_extract(self, url):
        user_name = self._match_id(url)
        if _re.fullmatch(r'MS4wLjABAAAA[\w-]{64}', user_name):
            return _orig_tiktok_user_real_extract(self, url)

        webpage = self._download_webpage(
            self._UPLOADER_URL_FORMAT % user_name, user_name,
            'Downloading user webpage', 'Unable to download user webpage',
            impersonate=True, fatal=False,
            headers=self._generate_blockbuster_headers()) or ''

        universal_data = self._get_universal_data(webpage, user_name)

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
                        self.cookiejar.clear(domain='.tiktok.com', path='/', name=cookie_name)
                    except KeyError:
                        pass
                universal_data = self._get_universal_data(webpage, user_name)
            except Exception as e:
                log.debug(f"WAF challenge solve failed for @{user_name}: {e}")

        detail = traverse_obj(universal_data, ('webapp.user-detail', {dict})) or {}
        video_count = traverse_obj(detail, ('userInfo', ('stats', 'statsV2'), 'videoCount', {int}, any))
        if not video_count and detail.get('statusCode') == 10222:
            self.raise_login_required("This user's account is private.")
        elif video_count == 0:
            raise ExtractorError('This account does not have any videos posted', expected=True)

        sec_uid = traverse_obj(detail, ('userInfo', 'user', 'secUid', {str}))
        if sec_uid:
            fail_early = not traverse_obj(detail, ('userInfo', 'itemList', ...))
        else:
            sec_uid = self._extract_sec_uid_from_embed(user_name)
            fail_early = False

        if not sec_uid:
            return _orig_tiktok_user_real_extract(self, url)

        return self.playlist_result(self._entries(sec_uid, user_name, fail_early), sec_uid, user_name)

    TikTokUserIE._real_extract = _patched_tiktok_user_real_extract
    log.info("🔧 Patched TikTokUserIE with WAF challenge solver")
except Exception as _patch_err:
    log.warning(f"⚠️ Could not patch TikTokUserIE: {_patch_err}")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SCRAPECREATORS_BASE = "https://api.scrapecreators.com"
APIFY_ACTOR = os.getenv("APIFY_TIKTOK_ACTOR", "clockworks~tiktok-scraper")
TIKWM_USER_API = "https://www.tikwm.com/api/user/posts"
TIKWM_VIDEO_API = "https://www.tikwm.com/api/"

DEFAULT_PROVIDER_ORDER = ["scrapecreators", "apify", "ytdlp", "tikwm"]

# ── Provider health / cooldown ────────────────────────────────
_PROVIDER_FAIL_THRESHOLD = 3
_PROVIDER_COOLDOWN = 20 * 60
_health_lock = threading.Lock()
_provider_health: dict[str, dict] = {}


class ProviderBlocked(Exception):
    """Provider refused us in a way retrying won't fix soon (challenge page, bad key, no credits)."""

    def __init__(self, msg: str, cooldown: int = 6 * 3600):
        super().__init__(msg)
        self.cooldown = cooldown


def _record(provider: str, ok: bool, error: str = "", cooldown: int | None = None):
    with _health_lock:
        h = _provider_health.setdefault(provider, {
            "successes": 0, "failures": 0, "consecutive_failures": 0,
            "skip_until": 0, "last_error": "", "last_success": None,
        })
        if ok:
            h["successes"] += 1
            h["consecutive_failures"] = 0
            h["skip_until"] = 0
            h["last_error"] = ""
            h["last_success"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return
        h["failures"] += 1
        h["consecutive_failures"] += 1
        h["last_error"] = str(error)[:240]
        if cooldown:
            h["skip_until"] = time.time() + cooldown
            log.warning(f"⏸️ Provider '{provider}' blocked/unavailable — skipping for {cooldown // 60} min: {error}")
        elif h["consecutive_failures"] >= _PROVIDER_FAIL_THRESHOLD:
            h["skip_until"] = time.time() + _PROVIDER_COOLDOWN
            h["consecutive_failures"] = 0
            log.warning(f"⏸️ Provider '{provider}' failing repeatedly — skipping for {_PROVIDER_COOLDOWN // 60} min")


def _provider_ready(provider: str) -> bool:
    h = _provider_health.get(provider)
    return not h or time.time() >= h.get("skip_until", 0)


def _provider_configured(provider: str) -> bool:
    if provider == "scrapecreators":
        return bool(os.getenv("SCRAPECREATORS_API_KEY", "").strip())
    if provider == "apify":
        return bool(os.getenv("APIFY_TOKEN", "").strip())
    if provider == "tikwm":
        return os.getenv("ENABLE_TIKWM", "true").strip().lower() != "false"
    return provider == "ytdlp"


def get_provider_order() -> list[str]:
    raw = os.getenv("FETCH_PROVIDERS", "").strip()
    order = [p.strip().lower() for p in raw.split(",") if p.strip()] if raw else DEFAULT_PROVIDER_ORDER
    return [p for p in order if p in DEFAULT_PROVIDER_ORDER]


def get_provider_status() -> dict:
    now = time.time()
    return {
        p: {
            "configured": _provider_configured(p),
            "cooling_down": now < _provider_health.get(p, {}).get("skip_until", 0),
            **{k: v for k, v in _provider_health.get(p, {}).items() if k != "skip_until"},
        }
        for p in get_provider_order()
    }


# ══════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════

def _clean_user(username: str) -> str:
    return username.strip().lstrip("@").strip()


def _page_url(author: str, vid_id: str) -> str:
    return f"https://www.tiktok.com/@{author}/video/{vid_id}" if author else f"https://www.tiktok.com/video/{vid_id}"


def _first_url(obj) -> str:
    if isinstance(obj, dict):
        lst = obj.get("url_list") or []
        return lst[0] if lst else (obj.get("url") or "")
    if isinstance(obj, list) and obj:
        return obj[0] if isinstance(obj[0], str) else ""
    return obj if isinstance(obj, str) else ""


def _to_seconds(value) -> int:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return int(round(v / 1000)) if v > 1000 else int(round(v))  # TikTok mobile API uses ms


def _is_cloudflare_challenge(resp: requests.Response) -> bool:
    if resp.status_code not in (403, 429, 503):
        return False
    body = resp.text[:2000].lower()
    return "just a moment" in body or "challenges.cloudflare.com" in body or "cf-chl" in body


def _video(vid_id, author, caption, play, duration, width, height, cover, ts, source) -> dict:
    return {
        "id": str(vid_id),
        "title": caption or "",
        "play": play or _page_url(author, str(vid_id)),
        "page_url": _page_url(author, str(vid_id)),
        "duration": _to_seconds(duration),
        "width": int(width or 0),
        "height": int(height or 0),
        "cover": cover or "",
        "author": author,
        "timestamp": int(ts or 0),
        "source": source,
    }


# ══════════════════════════════════════════════════════════════
#  Provider 1: ScrapeCreators
# ══════════════════════════════════════════════════════════════

def _sc_headers() -> dict:
    return {"x-api-key": os.getenv("SCRAPECREATORS_API_KEY", "").strip()}


def _sc_check(resp: requests.Response) -> dict:
    if resp.status_code in (401, 403):
        raise ProviderBlocked(f"ScrapeCreators auth error HTTP {resp.status_code} (check SCRAPECREATORS_API_KEY)")
    if resp.status_code == 402:
        raise ProviderBlocked("ScrapeCreators out of credits (HTTP 402)", cooldown=3600)
    resp.raise_for_status()
    return resp.json()


def _parse_aweme(aw: dict, author: str, source: str) -> dict | None:
    if not isinstance(aw, dict) or aw.get("image_post_info"):
        return None  # skip photo slideshows
    vid_id = aw.get("aweme_id") or aw.get("id")
    if not vid_id:
        return None
    v = aw.get("video") or {}
    play = _first_url(v.get("play_addr")) or _first_url(v.get("download_addr"))
    cover = _first_url(v.get("cover")) or _first_url(v.get("origin_cover"))
    author = ((aw.get("author") or {}).get("unique_id")) or author
    return _video(
        vid_id, author, aw.get("desc", ""), play,
        v.get("duration") or aw.get("duration"),
        v.get("width") or (v.get("play_addr") or {}).get("width"),
        v.get("height") or (v.get("play_addr") or {}).get("height"),
        cover, aw.get("create_time"), source,
    )


def fetch_scrapecreators(username: str, count: int = 5) -> list[dict]:
    user = _clean_user(username)
    resp = requests.get(
        f"{SCRAPECREATORS_BASE}/v3/tiktok/profile/videos",
        params={"handle": user, "sort_by": "latest"},
        headers=_sc_headers(),
        timeout=45,
    )
    data = _sc_check(resp)
    items = data.get("aweme_list") or data.get("videos") or []
    out = [v for v in (_parse_aweme(a, user, "scrapecreators") for a in items) if v]
    return out[:count]


def resolve_scrapecreators_video(page_url: str) -> str:
    """Get a fresh no-watermark MP4 URL for one video (CDN URLs expire after a few hours)."""
    resp = requests.get(
        f"{SCRAPECREATORS_BASE}/v2/tiktok/video",
        params={"url": page_url},
        headers=_sc_headers(),
        timeout=45,
    )
    data = _sc_check(resp)
    aw = data.get("aweme_detail") or data.get("aweme") or data
    parsed = _parse_aweme(aw, "", "scrapecreators")
    return parsed["play"] if parsed and parsed["play"].startswith("http") and "tiktok.com/@" not in parsed["play"] else ""


# ══════════════════════════════════════════════════════════════
#  Provider 2: Apify (clockworks/tiktok-scraper)
# ══════════════════════════════════════════════════════════════

def fetch_apify(username: str, count: int = 5) -> list[dict]:
    user = _clean_user(username)
    download = os.getenv("APIFY_DOWNLOAD_VIDEOS", "true").strip().lower() != "false"
    resp = requests.post(
        f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
        params={"token": os.getenv("APIFY_TOKEN", "").strip()},
        json={
            "profiles": [user],
            "resultsPerPage": count,
            "profileScrapeSections": ["videos"],
            "profileSorting": "latest",
            "excludePinnedPosts": True,
            "shouldDownloadVideos": download,
            "shouldDownloadCovers": False,
            "shouldDownloadSubtitles": False,
            "shouldDownloadSlideshowImages": False,
        },
        timeout=300,
    )
    if resp.status_code in (401, 403):
        raise ProviderBlocked(f"Apify auth error HTTP {resp.status_code} (check APIFY_TOKEN)")
    if resp.status_code == 402:
        raise ProviderBlocked("Apify account out of credits (HTTP 402)", cooldown=3600)
    resp.raise_for_status()
    items = resp.json() or []

    out = []
    for it in items:
        if not isinstance(it, dict) or it.get("isSlideshow") or not it.get("id"):
            continue
        meta = it.get("videoMeta") or {}
        media = it.get("mediaUrls") or []
        play = media[0] if media else (it.get("webVideoUrl") or "")
        author = (it.get("authorMeta") or {}).get("name") or user
        out.append(_video(
            it["id"], author, it.get("text", ""), play,
            meta.get("duration"), meta.get("width"), meta.get("height"),
            meta.get("coverUrl") or meta.get("originalCoverUrl"),
            it.get("createTime"), "apify",
        ))
    return out[:count]


# ══════════════════════════════════════════════════════════════
#  Provider 3: yt-dlp through the proxy chain
# ══════════════════════════════════════════════════════════════

def _ydl_opts(proxy_url: str | None, **extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
        "http_headers": {"User-Agent": BROWSER_UA, "Referer": "https://www.tiktok.com/"},
    }
    if IMPERSONATE_TARGET:
        opts["impersonate"] = IMPERSONATE_TARGET
    if proxy_url:
        opts["proxy"] = proxy_url
    opts.update(extra)
    return opts


def fetch_ytdlp(username: str, count: int = 5) -> list[dict]:
    user = _clean_user(username)
    url = f"https://www.tiktok.com/@{user}"
    last_err = "no routes"
    for route in proxy_manager.get_ordered_proxy_routes():
        for flat in (True, False):
            mode_label = "flat" if flat else "full-waf"
            try:
                log.info(f"  🔄 yt-dlp listing @{user} via {route['name']} ({mode_label})...")
                with yt_dlp.YoutubeDL(_ydl_opts(route["proxy"], extract_flat=flat, playlistend=count)) as ydl:
                    info = ydl.extract_info(url, download=False) or {}
                entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
                if entries:
                    proxy_manager.record_route_result(route["name"], True)
                    return [
                        _video(e["id"], user, e.get("description") or e.get("title", ""),
                               _page_url(user, str(e["id"])), e.get("duration"),
                               e.get("width"), e.get("height"), e.get("thumbnail"),
                               e.get("timestamp"), f"ytdlp:{route['name']}")
                        for e in entries[:count]
                    ]
                last_err = "empty result (ISP/IP blocked)"
            except Exception as e:
                last_err = str(e)[:200]
        proxy_manager.record_route_result(route["name"], False, last_err)
    raise RuntimeError(f"yt-dlp failed on all routes: {last_err}")


# ══════════════════════════════════════════════════════════════
#  Provider 4: TikWM (no challenge circumvention)
# ══════════════════════════════════════════════════════════════

def _tikwm_post(url: str, data: dict) -> dict:
    resp = requests.post(
        url, data=data, timeout=20,
        headers={"User-Agent": BROWSER_UA, "Accept": "application/json", "Referer": "https://www.tikwm.com/"},
    )
    if _is_cloudflare_challenge(resp):
        raise ProviderBlocked("TikWM is serving a Cloudflare 'verify you are human' page — skipping (not bypassing)")
    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError("TikWM returned non-JSON response")
    if payload.get("code") != 0:
        raise RuntimeError(f"TikWM error: {payload.get('msg')}")
    return payload.get("data") or {}


def _tikwm_abs(u: str) -> str:
    return f"https://www.tikwm.com{u}" if u and u.startswith("/") else (u or "")


def fetch_tikwm(username: str, count: int = 5) -> list[dict]:
    user = _clean_user(username)
    data = _tikwm_post(TIKWM_USER_API, {"unique_id": f"@{user}", "count": max(count, 10), "cursor": 0})
    out = []
    for v in (data.get("videos") or [])[:count]:
        if not v.get("video_id") or v.get("images"):
            continue
        out.append(_video(
            v["video_id"], user, v.get("title", ""),
            _tikwm_abs(v.get("hdplay") or v.get("play")),
            v.get("duration"), v.get("width") or 1080, v.get("height") or 1920,
            _tikwm_abs(v.get("cover") or v.get("origin_cover")), v.get("create_time"), "tikwm",
        ))
    return out


def resolve_tikwm_video(page_url: str) -> str:
    data = _tikwm_post(TIKWM_VIDEO_API, {"url": page_url, "hd": 1})
    return _tikwm_abs(data.get("hdplay") or data.get("play") or "")


_FETCHERS = {
    "scrapecreators": fetch_scrapecreators,
    "apify": fetch_apify,
    "ytdlp": fetch_ytdlp,
    "tikwm": fetch_tikwm,
}


# ══════════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════════

def get_user_videos(username: str, count: int = 5) -> list[dict]:
    """Try each configured, healthy provider in order until one returns videos."""
    user = _clean_user(username)
    errors = []
    for provider in get_provider_order():
        if not _provider_configured(provider):
            continue
        if not _provider_ready(provider):
            errors.append(f"{provider}: cooling down")
            continue
        try:
            videos = _FETCHERS[provider](user, count)
            if videos:
                _record(provider, True)
                log.info(f"📥 [{provider}] fetched {len(videos)} videos from @{user}")
                return videos
            _record(provider, False, "empty result")
            errors.append(f"{provider}: empty")
        except ProviderBlocked as e:
            _record(provider, False, str(e), cooldown=e.cooldown)
            errors.append(f"{provider}: {e}")
        except Exception as e:
            _record(provider, False, str(e))
            errors.append(f"{provider}: {str(e)[:150]}")
            log.warning(f"  ⚠️ [{provider}] failed for @{user}: {str(e)[:200]}")

    log.error(f"  ❌ All providers failed for @{user}: {' | '.join(errors) or 'none configured'}")
    return []


def _looks_like_mp4(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
        return len(head) >= 8 and head[4:8] == b"ftyp"
    except OSError:
        return False


def _download_direct(url: str, output_path: str) -> bool:
    """Stream a direct MP4 URL to disk, trying direct first then proxy routes."""
    if not url or not url.startswith("http") or "tiktok.com/@" in url:
        return False
    routes = [{"name": "direct", "proxy": None}] + [
        r for r in proxy_manager.get_ordered_proxy_routes() if r["proxy"]
    ]
    headers = {"User-Agent": BROWSER_UA, "Referer": "https://www.tiktok.com/"}
    for route in routes:
        proxies = {"http": route["proxy"], "https": route["proxy"]} if route["proxy"] else None
        try:
            with requests.get(url, headers=headers, proxies=proxies, stream=True, timeout=(15, 90)) as r:
                if r.status_code != 200:
                    continue
                with open(output_path, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            fh.write(chunk)
            if os.path.getsize(output_path) > 10_000 and _looks_like_mp4(output_path):
                return True
        except Exception as e:
            log.debug(f"  direct download via {route['name']} failed: {e}")
        if os.path.exists(output_path):
            os.remove(output_path)
    return False


def _download_ytdlp(page_url: str, output_path: str) -> bool:
    for route in proxy_manager.get_ordered_proxy_routes():
        try:
            opts = _ydl_opts(
                route["proxy"],
                outtmpl=output_path,
                overwrites=True,
                format="best[ext=mp4][vcodec!=none][acodec!=none]/best[ext=mp4]/best",
                socket_timeout=60,
                retries=2,
            )
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([page_url])
            if os.path.exists(output_path) and os.path.getsize(output_path) > 10_000:
                proxy_manager.record_route_result(route["name"], True)
                return True
            proxy_manager.record_route_result(route["name"], False, "no file")
        except Exception as e:
            proxy_manager.record_route_result(route["name"], False, str(e))
        if os.path.exists(output_path):
            os.remove(output_path)
    return False


def download_video(video_url: str, tiktok_id: str, author: str = "") -> str | None:
    """Download a TikTok video as a single MP4 (no FFmpeg). Returns path or None."""
    output_path = os.path.join(config.TEMP_DIR, f"tiktok_{tiktok_id}.mp4")
    if os.path.exists(output_path) and os.path.getsize(output_path) > 10_000:
        return output_path

    author = _clean_user(author)
    page_url = _page_url(author, tiktok_id)

    attempts = [("stored direct URL", lambda: _download_direct(video_url, output_path))]
    if _provider_configured("scrapecreators") and _provider_ready("scrapecreators"):
        attempts.append(("ScrapeCreators fresh URL",
                         lambda: _download_direct(resolve_scrapecreators_video(page_url), output_path)))
    attempts.append(("yt-dlp", lambda: _download_ytdlp(page_url, output_path)))
    if _provider_configured("tikwm") and _provider_ready("tikwm"):
        attempts.append(("TikWM", lambda: _download_direct(resolve_tikwm_video(page_url), output_path)))

    for label, fn in attempts:
        try:
            log.info(f"  ⬇️ Downloading {tiktok_id} via {label}...")
            if fn():
                size_mb = os.path.getsize(output_path) / (1024 * 1024)
                log.info(f"  ✅ Downloaded {tiktok_id} via {label} ({size_mb:.1f} MB)")
                return output_path
        except ProviderBlocked as e:
            _record("tikwm" if "TikWM" in label else "scrapecreators", False, str(e), cooldown=e.cooldown)
        except Exception as e:
            log.warning(f"  ⚠️ {label} download failed for {tiktok_id}: {str(e)[:200]}")

    log.error(f"  ❌ Could not download {tiktok_id} with any method")
    return None


def fetch_all_new_videos(max_per_account: int = 5) -> list[dict]:
    """
    Collect fresh, unprocessed, vertical Shorts from every monitored account.
    Sorted newest first.
    """
    from database import is_video_processed
    from datetime import datetime, timezone, timedelta
    import settings_manager

    max_age_hours = config.get_video_max_age_hours()
    max_duration = int(settings_manager.get("max_short_duration_seconds", 60) or 60)
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).timestamp()

    all_new: list[dict] = []
    seen: set[str] = set()

    for idx, raw_user in enumerate(config._get_tiktok_usernames()):
        username = _clean_user(raw_user)
        if not username:
            continue
        if idx:
            time.sleep(1.5)

        log.info(f"🔍 Checking @{username} for new Shorts...")
        added = 0
        for video in get_user_videos(username, count=max_per_account):
            vid_id = video["id"]
            if vid_id in seen:
                continue
            duration = video.get("duration", 0)
            if duration and duration > max_duration:
                log.info(f"  ⏭️ {vid_id}: {duration}s > {max_duration}s, skipping")
                continue
            w, h = video.get("width", 0), video.get("height", 0)
            if w and h and w > h:
                log.info(f"  ⏭️ {vid_id}: landscape {w}x{h}, skipping")
                continue
            ts = video.get("timestamp", 0)
            if ts and ts < cutoff_ts:
                continue
            if is_video_processed(vid_id):
                continue
            seen.add(vid_id)
            all_new.append(video)
            added += 1
        if added:
            log.info(f"  📦 {added} new Shorts from @{username}")

    all_new.sort(key=lambda v: v.get("timestamp", 0), reverse=True)
    log.info(f"🆕 Found {len(all_new)} new fresh Shorts across all accounts")
    return all_new


def test_providers(username: str) -> dict:
    """Try every provider individually (ignoring cooldowns) and report results."""
    user = _clean_user(username)
    results = {}
    for provider in get_provider_order():
        if not _provider_configured(provider):
            results[provider] = {"ok": False, "status": "not_configured"}
            continue
        t0 = time.time()
        try:
            vids = _FETCHERS[provider](user, 2)
            results[provider] = {
                "ok": bool(vids),
                "videos": len(vids),
                "sample_id": vids[0]["id"] if vids else None,
                "latency_ms": int((time.time() - t0) * 1000),
            }
            _record(provider, bool(vids), "" if vids else "empty result")
        except Exception as e:
            results[provider] = {"ok": False, "error": str(e)[:240], "latency_ms": int((time.time() - t0) * 1000)}
            _record(provider, False, str(e), cooldown=getattr(e, "cooldown", None))
    return results
