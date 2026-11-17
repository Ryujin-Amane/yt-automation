"""
Proxy & Anti-Block Manager — Multi-layer system to prevent TikTok datacenter IP blocks on Render.

Provides 4 independent layers that can be tested individually or used in 'auto' fallback mode:
  1. Custom Proxy Pool (HTTP / HTTPS / SOCKS5 rotating proxies via PROXY_URLS or dashboard)
  2. Cloudflare WARP Userspace VPN (wireproxy running on socks5://127.0.0.1:1080 inside Docker)
  3. Direct with Chrome TLS Fingerprint Impersonation (curl_cffi / yt-dlp ImpersonateTarget)
  4. TikWM Public API Fallback (fetches TikTok feeds & HD no-watermark MP4s via TikWM infrastructure)
"""

import os
import time
import socket
import platform
import logging
import threading
import subprocess
from urllib.parse import urlparse
import requests

import config
import settings_manager

log = logging.getLogger(__name__)

_lock = threading.Lock()
_warp_init_lock = threading.Lock()
_warp_init_attempted = False
_warp_proc = None
_proxy_index = 0
_proxy_health: dict[str, dict] = {}
_last_working_route: str = "none"

# Default local wireproxy SOCKS5 address started by entrypoint.sh or ensure_warp_running()
DEFAULT_WARP_SOCKS5 = "socks5h://127.0.0.1:1080"
DEFAULT_WARP_HTTP = "http://127.0.0.1:1081"
WARP_DIR = "/tmp/warp"


def get_proxy_mode() -> str:
    """
    Return the active proxy mode:
      - 'auto'          : Try Custom Proxy -> WARP -> Direct (with TLS impersonation) -> TikWM API
      - 'warp'          : Force Cloudflare WARP proxy first
      - 'custom_proxy'  : Force Custom Proxy pool first
      - 'tikwm_first'   : Use TikWM API first (ultra-lightweight, bypasses direct TikTok scraping)
      - 'direct'        : Direct connection with Chrome TLS impersonation only
    """
    mode = settings_manager.get("proxy_mode", os.getenv("PROXY_MODE", "auto"))
    return str(mode).strip().lower() or "auto"


def get_custom_proxies() -> list[str]:
    """Return list of configured proxy URLs from settings.json or PROXY_URLS env var."""
    proxies = []

    # 1. From runtime settings.json
    sm_proxies = settings_manager.get("proxy_urls", [])
    if isinstance(sm_proxies, str):
        sm_proxies = [p.strip() for p in sm_proxies.replace("\n", ",").split(",") if p.strip()]
    elif isinstance(sm_proxies, list):
        sm_proxies = [str(p).strip() for p in sm_proxies if str(p).strip()]
    else:
        sm_proxies = []
    proxies.extend(sm_proxies)

    # 2. From environment variables (PROXY_URLS or TIKTOK_PROXY)
    env_raw = os.getenv("PROXY_URLS", "") or os.getenv("TIKTOK_PROXY", "")
    if env_raw:
        for p in env_raw.replace("\n", ",").split(","):
            p = p.strip()
            if p and p not in proxies:
                proxies.append(p)

    # Normalize scheme if missing (default to http://)
    normalized = []
    for p in proxies:
        if "://" not in p:
            p = f"http://{p}"
        normalized.append(p)

    return normalized


def get_warp_proxy_url() -> str:
    """Return the Cloudflare WARP proxy URL if configured or running locally."""
    custom_warp = settings_manager.get("warp_proxy_url", os.getenv("WARP_PROXY_URL", "")).strip()
    if custom_warp:
        return custom_warp
    return DEFAULT_WARP_SOCKS5


def is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Quickly check if a local TCP port (e.g. wireproxy 1080) is listening."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_warp_running() -> bool:
    """Check if local WARP / wireproxy SOCKS5 or HTTP port is listening."""
    warp_url = get_warp_proxy_url()
    try:
        parsed = urlparse(warp_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 1080
        if is_port_open(host, port, timeout=1.0):
            return True
    except Exception:
        pass

    # Also check standard wireproxy HTTP port (1081) or Cloudflare WARP desktop port (40000)
    if is_port_open("127.0.0.1", 1081, timeout=0.5):
        return True
    if is_port_open("127.0.0.1", 40000, timeout=0.5):
        return True
    return False


def ensure_warp_running() -> bool:
    """
    Automatically start the userspace Cloudflare WARP proxy (wireproxy on 127.0.0.1:1080)
    if it is not already running. Works in both Render Docker (Linux) and Local (macOS).
    """
    global _warp_init_attempted, _warp_proc
    if is_warp_running():
        return True

    if os.getenv("ENABLE_WARP", "true").strip().lower() == "false":
        return False

    with _warp_init_lock:
        if is_warp_running():
            return True
        if _warp_init_attempted and _warp_proc and _warp_proc.poll() is None:
            return is_warp_running()
        _warp_init_attempted = True

        try:
            os.makedirs(WARP_DIR, exist_ok=True)
            sys_name = platform.system().lower()
            mach = platform.machine().lower()
            os_tag = "darwin" if sys_name == "darwin" else "linux"
            arch_tag = "arm64" if mach in ("arm64", "aarch64") else "amd64"

            wgcf_bin = "/usr/local/bin/wgcf" if os.path.exists("/usr/local/bin/wgcf") else os.path.join(WARP_DIR, "wgcf")
            wp_bin = "/usr/local/bin/wireproxy" if os.path.exists("/usr/local/bin/wireproxy") else os.path.join(WARP_DIR, "wireproxy")
            conf_path = os.path.join(WARP_DIR, "wireproxy.conf")

            # Download wgcf if missing
            if not os.path.exists(wgcf_bin):
                wgcf_url = f"https://github.com/ViRb3/wgcf/releases/download/v2.2.22/wgcf_2.2.22_{os_tag}_{arch_tag}"
                log.info(f"⬇️ Downloading wgcf ({os_tag}/{arch_tag})...")
                subprocess.run(["curl", "-fsSL", wgcf_url, "-o", wgcf_bin], check=True, timeout=30)
                os.chmod(wgcf_bin, 0o755)

            # Download wireproxy if missing
            if not os.path.exists(wp_bin):
                wp_url = f"https://github.com/pufferffish/wireproxy/releases/download/v1.0.9/wireproxy_{os_tag}_{arch_tag}.tar.gz"
                tar_path = os.path.join(WARP_DIR, "wp.tar.gz")
                log.info(f"⬇️ Downloading wireproxy ({os_tag}/{arch_tag})...")
                subprocess.run(["curl", "-fsSL", wp_url, "-o", tar_path], check=True, timeout=30)
                subprocess.run(["tar", "-xzf", tar_path, "-C", WARP_DIR, "wireproxy"], check=True, timeout=15)
                os.chmod(wp_bin, 0o755)

            # Generate wireproxy.conf if missing
            if not os.path.exists(conf_path):
                warp_priv_key = os.getenv("WARP_PRIVATE_KEY", "").strip()
                if warp_priv_key:
                    warp_addr = os.getenv("WARP_ADDRESS_IPV4", "172.16.0.2/32").strip()
                    with open(conf_path, "w", encoding="utf-8") as f:
                        f.write(
                            f"[Interface]\nAddress = {warp_addr}\nPrivateKey = {warp_priv_key}\n"
                            "DNS = 1.1.1.1\nMTU = 1280\n\n"
                            "[Peer]\nPublicKey = bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=\n"
                            "Endpoint = engage.cloudflareclient.com:2408\nKeepAlive = 25\n\n"
                            "[Socks5]\nBindAddress = 127.0.0.1:1080\n\n"
                            "[http]\nBindAddress = 127.0.0.1:1081\n"
                        )
                else:
                    log.info("🌐 Registering Cloudflare WARP profile via wgcf...")
                    acct_path = os.path.join(WARP_DIR, "wgcf-account.toml")
                    prof_path = os.path.join(WARP_DIR, "wgcf-profile.conf")
                    if not os.path.exists(acct_path):
                        subprocess.run([wgcf_bin, "register", "--accept-tos"], cwd=WARP_DIR, check=True, timeout=25)
                    if not os.path.exists(prof_path):
                        subprocess.run([wgcf_bin, "generate"], cwd=WARP_DIR, check=True, timeout=15)
                    with open(prof_path, "r", encoding="utf-8") as src:
                        base_conf = src.read()
                    with open(conf_path, "w", encoding="utf-8") as dst:
                        dst.write(
                            base_conf
                            + "\n[Socks5]\nBindAddress = 127.0.0.1:1080\n\n"
                            "[http]\nBindAddress = 127.0.0.1:1081\n"
                        )

            if os.path.exists(conf_path) and os.path.exists(wp_bin):
                log_file = open(os.path.join(WARP_DIR, "wireproxy.log"), "a")
                _warp_proc = subprocess.Popen(
                    [wp_bin, "-c", conf_path],
                    cwd=WARP_DIR,
                    stdout=log_file,
                    stderr=log_file,
                    start_new_session=True,
                )
                for _ in range(15):
                    time.sleep(0.2)
                    if is_port_open("127.0.0.1", 1081, timeout=0.5):
                        # Verify the WireGuard tunnel actually completes handshake (not UDP-blocked by local ISP)
                        probe = probe_ip_via_proxy(DEFAULT_WARP_HTTP, timeout=4)
                        if probe.get("ok"):
                            log.info(
                                f"🛡️ Cloudflare WARP userspace proxy active on {DEFAULT_WARP_SOCKS5} "
                                f"(Exit IP: {probe.get('ip')}, WARP={probe.get('warp')})"
                            )
                            return True
                        else:
                            log.warning(
                                "⚠️ Local ISP blocked WireGuard UDP handshake (normal in PK locally; works on Render). "
                                "Stopping local wireproxy and falling back to next layer."
                            )
                            try:
                                _warp_proc.terminate()
                                _warp_proc.wait(timeout=2)
                            except Exception:
                                pass
                            _warp_proc = None
                            return False
        except Exception as e:
            log.warning(f"⚠️ Auto-start WARP failed: {e}")

    return is_warp_running()


def get_active_warp_url(auto_start: bool = True) -> str | None:
    """Return the active local WARP proxy URL if reachable (auto-starting if enabled)."""
    if auto_start and not is_warp_running():
        ensure_warp_running()

    warp_url = get_warp_proxy_url()
    try:
        parsed = urlparse(warp_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 1080
        if is_port_open(host, port, timeout=1.0):
            return warp_url
    except Exception:
        pass

    if is_port_open("127.0.0.1", 1081, timeout=0.5):
        return DEFAULT_WARP_HTTP
    if is_port_open("127.0.0.1", 40000, timeout=0.5):
        return "socks5h://127.0.0.1:40000"
    return None


def get_next_custom_proxy() -> str | None:
    """Round-robin select the next custom proxy from the pool."""
    global _proxy_index
    proxies = get_custom_proxies()
    if not proxies:
        return None
    with _lock:
        proxy = proxies[_proxy_index % len(proxies)]
        _proxy_index += 1
        return proxy


def record_route_result(route_name: str, success: bool, error: str = ""):
    """Record health metrics for a proxy/route."""
    global _last_working_route
    with _lock:
        stats = _proxy_health.setdefault(route_name, {
            "successes": 0,
            "failures": 0,
            "last_checked": None,
            "last_error": "",
        })
        if success:
            stats["successes"] += 1
            stats["last_error"] = ""
            _last_working_route = route_name
        else:
            stats["failures"] += 1
            stats["last_error"] = str(error)[:200]
        stats["last_checked"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def get_ordered_proxy_routes() -> list[dict]:
    """
    Return an ordered list of network routes to try for yt-dlp / HTTP requests
    based on the current `proxy_mode`.

    Each route is a dict:
      {"name": str, "proxy": str | None, "type": "custom" | "warp" | "direct"}
    """
    mode = get_proxy_mode()
    custom_proxies = get_custom_proxies()
    warp_url = get_active_warp_url()

    routes = []

    if mode == "warp":
        if warp_url:
            routes.append({"name": "warp", "proxy": warp_url, "type": "warp"})
        else:
            # Even if port didn't respond yet, allow trying configured warp URL if forced
            routes.append({"name": "warp_configured", "proxy": get_warp_proxy_url(), "type": "warp"})
        for idx, p in enumerate(custom_proxies):
            routes.append({"name": f"custom_proxy_{idx+1}", "proxy": p, "type": "custom"})
        routes.append({"name": "direct", "proxy": None, "type": "direct"})

    elif mode == "custom_proxy":
        for idx, p in enumerate(custom_proxies):
            routes.append({"name": f"custom_proxy_{idx+1}", "proxy": p, "type": "custom"})
        if warp_url:
            routes.append({"name": "warp", "proxy": warp_url, "type": "warp"})
        routes.append({"name": "direct", "proxy": None, "type": "direct"})

    elif mode == "direct":
        routes.append({"name": "direct", "proxy": None, "type": "direct"})

    else:
        # 'auto' or 'tikwm_first'
        # Rotate starting proxy if multiple custom proxies exist
        if custom_proxies:
            start = _proxy_index % len(custom_proxies)
            ordered_custom = custom_proxies[start:] + custom_proxies[:start]
            for idx, p in enumerate(ordered_custom):
                routes.append({"name": f"custom_proxy_{idx+1}", "proxy": p, "type": "custom"})

        if warp_url:
            routes.append({"name": "warp", "proxy": warp_url, "type": "warp"})

        routes.append({"name": "direct", "proxy": None, "type": "direct"})

    return routes


def probe_ip_via_proxy(proxy_url: str | None = None, timeout: int = 10) -> dict:
    """
    Check outbound IP, country, and Cloudflare trace info through the given proxy (or direct).
    """
    start_ts = time.time()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        resp = requests.get(
            "https://www.cloudflare.com/cdn-cgi/trace",
            proxies=proxies,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"},
        )
        latency_ms = int((time.time() - start_ts) * 1000)
        if resp.status_code == 200:
            trace = {}
            for line in resp.text.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    trace[k.strip()] = v.strip()
            return {
                "ok": True,
                "ip": trace.get("ip", "unknown"),
                "loc": trace.get("loc", "unknown"),
                "warp": trace.get("warp", "off"),
                "colo": trace.get("colo", "unknown"),
                "latency_ms": latency_ms,
            }
        return {"ok": False, "error": f"HTTP {resp.status_code}", "latency_ms": latency_ms}
    except Exception as e:
        latency_ms = int((time.time() - start_ts) * 1000)
        return {"ok": False, "error": str(e)[:180], "latency_ms": latency_ms}


def get_status_summary() -> dict:
    """Return current status of all proxy/VPN layers for the dashboard."""
    warp_active = is_warp_running()
    custom_list = get_custom_proxies()
    # Mask credentials in proxy URLs for safe UI display
    masked_proxies = []
    for p in custom_list:
        try:
            parsed = urlparse(p)
            if parsed.password:
                masked = p.replace(f":{parsed.password}@", ":****@")
            else:
                masked = p
            masked_proxies.append(masked)
        except Exception:
            masked_proxies.append(p)

    return {
        "proxy_mode": get_proxy_mode(),
        "warp_running": warp_active,
        "warp_proxy_url": get_active_warp_url() or get_warp_proxy_url(),
        "custom_proxies_count": len(custom_list),
        "custom_proxies_masked": masked_proxies,
        "last_working_route": _last_working_route,
        "route_health": _proxy_health,
    }


def test_all_systems(test_username: str = "tiktok") -> dict:
    """
    Test every anti-block / proxy / scraper layer one-by-one and return a comprehensive report.
    """
    import tiktok_scraper

    results = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "test_username": test_username,
        "proxy_mode": get_proxy_mode(),
        "layers": {},
    }

    # 1. Test Direct Outbound Connection
    log.info("🧪 [Test 1/5] Probing Direct Outbound IP...")
    direct_probe = probe_ip_via_proxy(None, timeout=8)
    results["layers"]["1_direct_network"] = direct_probe

    # 2. Test Cloudflare WARP Proxy (if running)
    log.info("🧪 [Test 2/5] Probing Cloudflare WARP Proxy...")
    warp_url = get_active_warp_url()
    if warp_url:
        warp_probe = probe_ip_via_proxy(warp_url, timeout=10)
        warp_probe["proxy_url"] = warp_url
        results["layers"]["2_cloudflare_warp"] = warp_probe
    else:
        results["layers"]["2_cloudflare_warp"] = {
            "ok": False,
            "status": "not_running",
            "message": (
                "Local wireproxy/WARP port (1080/40000) is not open. "
                "In Docker on Render, entrypoint.sh starts wireproxy automatically."
            ),
        }

    # 3. Test Custom Proxies (if configured)
    log.info("🧪 [Test 3/5] Probing Custom Proxies...")
    custom_proxies = get_custom_proxies()
    if custom_proxies:
        custom_results = []
        for idx, p in enumerate(custom_proxies[:5]):
            probe = probe_ip_via_proxy(p, timeout=10)
            probe["index"] = idx + 1
            custom_results.append(probe)
        results["layers"]["3_custom_proxies"] = {
            "ok": any(r.get("ok") for r in custom_results),
            "tested_count": len(custom_results),
            "results": custom_results,
        }
    else:
        results["layers"]["3_custom_proxies"] = {
            "ok": False,
            "status": "not_configured",
            "message": "No custom proxies configured in PROXY_URLS or settings.",
        }

    # 4. Test TikWM API Fallback (Ultra-lightweight)
    log.info(f"🧪 [Test 4/5] Testing TikWM API fetch for @{test_username}...")
    t0 = time.time()
    try:
        tikwm_videos = tiktok_scraper.fetch_user_videos_tikwm(test_username, count=2)
        elapsed_ms = int((time.time() - t0) * 1000)
        results["layers"]["4_tikwm_api"] = {
            "ok": len(tikwm_videos) > 0,
            "videos_fetched": len(tikwm_videos),
            "sample_video_id": tikwm_videos[0]["id"] if tikwm_videos else None,
            "latency_ms": elapsed_ms,
        }
    except Exception as e:
        results["layers"]["4_tikwm_api"] = {
            "ok": False,
            "error": str(e)[:200],
            "latency_ms": int((time.time() - t0) * 1000),
        }

    # 5. Test yt-dlp + curl_cffi TLS Impersonation + WAF Solver
    log.info(f"🧪 [Test 5/5] Testing yt-dlp (Chrome TLS Impersonation + WAF Solver) for @{test_username}...")
    t0 = time.time()
    try:
        ydl_videos = tiktok_scraper.fetch_user_videos_ytdlp(test_username, count=2)
        elapsed_ms = int((time.time() - t0) * 1000)
        results["layers"]["5_ytdlp_impersonate"] = {
            "ok": len(ydl_videos) > 0,
            "videos_fetched": len(ydl_videos),
            "sample_video_id": ydl_videos[0]["id"] if ydl_videos else None,
            "impersonate_enabled": tiktok_scraper.IMPERSONATE_TARGET is not None,
            "latency_ms": elapsed_ms,
        }
    except Exception as e:
        results["layers"]["5_ytdlp_impersonate"] = {
            "ok": False,
            "error": str(e)[:200],
            "latency_ms": int((time.time() - t0) * 1000),
        }

    # Overall verdict
    scraper_working = (
        results["layers"]["4_tikwm_api"].get("ok", False)
        or results["layers"]["5_ytdlp_impersonate"].get("ok", False)
    )
    results["overall_ok"] = scraper_working
    return results
