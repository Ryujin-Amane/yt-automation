"""
Proxy / Network Route Manager — decides HOW outbound requests to TikTok leave the server.

Routes (tried in order according to `proxy_mode`):
  1. Custom proxy pool  — HTTP/HTTPS/SOCKS5 proxies from PROXY_URLS env or dashboard
                          (residential proxies are by far the most reliable on Render).
  2. Cloudflare WARP    — userspace WireGuard via wireproxy on 127.0.0.1:1080/1081.
                          Auto-started on Render (or when ENABLE_WARP=true).
  3. Direct             — the server's own IP.

Every route has health tracking: after repeated failures it is put on a
cooldown so slow/dead routes don't stall every fetch.
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

import settings_manager

log = logging.getLogger(__name__)

DEFAULT_WARP_SOCKS5 = "socks5h://127.0.0.1:1080"
DEFAULT_WARP_HTTP = "http://127.0.0.1:1081"
WARP_DIR = "/tmp/warp"

_ROUTE_FAIL_THRESHOLD = 3          # consecutive failures before cooldown
_ROUTE_COOLDOWN_SECONDS = 15 * 60
_WARP_RETRY_COOLDOWN_SECONDS = 30 * 60
_WARP_HEALTH_TTL_SECONDS = 10 * 60

_lock = threading.RLock()
_warp_lock = threading.Lock()
_warp_proc = None
_warp_last_attempt = 0.0
_warp_health = {"ok": None, "checked_at": 0.0, "ip": None}
_proxy_index = 0
_route_health: dict[str, dict] = {}
_last_working_route = "none"


# ══════════════════════════════════════════════════════════════
#  Configuration
# ══════════════════════════════════════════════════════════════

def get_proxy_mode() -> str:
    """'auto' | 'custom_proxy' | 'warp' | 'direct'."""
    mode = str(settings_manager.get("proxy_mode", "") or os.getenv("PROXY_MODE", "auto")).strip().lower()
    return mode if mode in ("auto", "custom_proxy", "warp", "direct") else "auto"


def get_custom_proxies() -> list[str]:
    raw: list[str] = []
    sm = settings_manager.get("proxy_urls", [])
    if isinstance(sm, str):
        raw.extend(sm.replace("\n", ",").split(","))
    elif isinstance(sm, list):
        raw.extend(str(p) for p in sm)
    env_raw = os.getenv("PROXY_URLS", "") or os.getenv("TIKTOK_PROXY", "")
    raw.extend(env_raw.replace("\n", ",").split(","))

    out: list[str] = []
    for p in raw:
        p = p.strip()
        if not p:
            continue
        if "://" not in p:
            p = f"http://{p}"
        if p not in out:
            out.append(p)
    return out


def mask_proxy(url: str | None) -> str:
    if not url:
        return "direct"
    try:
        parsed = urlparse(url)
        if parsed.password:
            return url.replace(f":{parsed.password}@", ":****@")
    except Exception:
        pass
    return url


# ══════════════════════════════════════════════════════════════
#  Cloudflare WARP (wireproxy)
# ══════════════════════════════════════════════════════════════

def _warp_enabled() -> bool:
    flag = os.getenv("ENABLE_WARP", "").strip().lower()
    if flag == "false":
        return False
    if flag == "true":
        return True
    # Default: only auto-start on Render, not on developer machines
    return bool(os.getenv("RENDER"))


def is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_ip_via_proxy(proxy_url: str | None = None, timeout: int = 10) -> dict:
    """Return exit IP / country / WARP flag through a proxy (or direct)."""
    start = time.time()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        resp = requests.get(
            "https://www.cloudflare.com/cdn-cgi/trace",
            proxies=proxies,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        latency = int((time.time() - start) * 1000)
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}", "latency_ms": latency}
        trace = dict(line.split("=", 1) for line in resp.text.splitlines() if "=" in line)
        return {
            "ok": True,
            "ip": trace.get("ip", "?"),
            "loc": trace.get("loc", "?"),
            "warp": trace.get("warp", "off"),
            "latency_ms": latency,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:180], "latency_ms": int((time.time() - start) * 1000)}


def _warp_port_open() -> bool:
    return is_port_open("127.0.0.1", 1081, 0.5) or is_port_open("127.0.0.1", 1080, 0.5)


def _warp_tunnel_healthy(force: bool = False) -> bool:
    """Port open AND traffic actually flows (cached for a few minutes)."""
    now = time.time()
    if not force and _warp_health["ok"] is not None and now - _warp_health["checked_at"] < _WARP_HEALTH_TTL_SECONDS:
        return bool(_warp_health["ok"]) and _warp_port_open()
    if not _warp_port_open():
        _warp_health.update(ok=False, checked_at=now, ip=None)
        return False
    probe = probe_ip_via_proxy(DEFAULT_WARP_HTTP, timeout=6)
    _warp_health.update(ok=bool(probe.get("ok")), checked_at=now, ip=probe.get("ip"))
    return bool(probe.get("ok"))


def _download(url: str, dest: str):
    subprocess.run(["curl", "-fsSL", url, "-o", dest], check=True, timeout=60)


def ensure_warp_running() -> bool:
    """Start wireproxy if enabled and not already healthy. Never raises; rate-limited retries."""
    global _warp_proc, _warp_last_attempt

    if _warp_tunnel_healthy():
        return True
    if not _warp_enabled():
        return False

    with _warp_lock:
        if _warp_tunnel_healthy(force=True):
            return True
        if _warp_last_attempt and time.time() - _warp_last_attempt < _WARP_RETRY_COOLDOWN_SECONDS:
            return False
        _warp_last_attempt = time.time()

        try:
            os.makedirs(WARP_DIR, exist_ok=True)
            os_tag = "darwin" if platform.system().lower() == "darwin" else "linux"
            arch_tag = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
            wgcf = "/usr/local/bin/wgcf" if os.path.exists("/usr/local/bin/wgcf") else os.path.join(WARP_DIR, "wgcf")
            wp = "/usr/local/bin/wireproxy" if os.path.exists("/usr/local/bin/wireproxy") else os.path.join(WARP_DIR, "wireproxy")
            conf = os.path.join(WARP_DIR, "wireproxy.conf")

            if not os.path.exists(wp):
                tar = os.path.join(WARP_DIR, "wp.tar.gz")
                _download(f"https://github.com/pufferffish/wireproxy/releases/download/v1.0.9/wireproxy_{os_tag}_{arch_tag}.tar.gz", tar)
                subprocess.run(["tar", "-xzf", tar, "-C", WARP_DIR, "wireproxy"], check=True, timeout=20)
                os.chmod(wp, 0o755)

            if not os.path.exists(conf):
                priv = os.getenv("WARP_PRIVATE_KEY", "").strip()
                if priv:
                    addr = os.getenv("WARP_ADDRESS_IPV4", "172.16.0.2/32").strip()
                    body = (
                        f"[Interface]\nAddress = {addr}\nPrivateKey = {priv}\nDNS = 1.1.1.1\nMTU = 1280\n\n"
                        "[Peer]\nPublicKey = bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=\n"
                        "AllowedIPs = 0.0.0.0/0\n"
                        "Endpoint = engage.cloudflareclient.com:2408\nPersistentKeepalive = 25\n"
                    )
                else:
                    if not os.path.exists(wgcf):
                        _download(f"https://github.com/ViRb3/wgcf/releases/download/v2.2.22/wgcf_2.2.22_{os_tag}_{arch_tag}", wgcf)
                        os.chmod(wgcf, 0o755)
                    if not os.path.exists(os.path.join(WARP_DIR, "wgcf-account.toml")):
                        subprocess.run([wgcf, "register", "--accept-tos"], cwd=WARP_DIR, check=True, timeout=30,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    subprocess.run([wgcf, "generate"], cwd=WARP_DIR, check=True, timeout=20,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    with open(os.path.join(WARP_DIR, "wgcf-profile.conf"), encoding="utf-8") as fh:
                        body = fh.read()
                body += "\n[Socks5]\nBindAddress = 127.0.0.1:1080\n\n[http]\nBindAddress = 127.0.0.1:1081\n"
                with open(conf, "w", encoding="utf-8") as fh:
                    fh.write(body)

            if not _warp_port_open():
                log_fh = open(os.path.join(WARP_DIR, "wireproxy.log"), "a")
                _warp_proc = subprocess.Popen([wp, "-c", conf], cwd=WARP_DIR, stdout=log_fh, stderr=log_fh,
                                              start_new_session=True)
                for _ in range(20):
                    time.sleep(0.25)
                    if _warp_port_open():
                        break

            if _warp_tunnel_healthy(force=True):
                log.info(f"🛡️ Cloudflare WARP route active (exit IP {_warp_health.get('ip')})")
                return True

            log.warning("⚠️ WARP tunnel did not pass traffic (UDP likely blocked). Disabling WARP route for 30 min.")
            if _warp_proc and _warp_proc.poll() is None:
                _warp_proc.terminate()
            _warp_proc = None
        except Exception as e:
            log.warning(f"⚠️ WARP start failed: {e}")
    return False


def get_active_warp_url() -> str | None:
    return DEFAULT_WARP_SOCKS5 if ensure_warp_running() else None


# ══════════════════════════════════════════════════════════════
#  Route selection + health
# ══════════════════════════════════════════════════════════════

def _route_available(name: str) -> bool:
    h = _route_health.get(name)
    return not h or time.time() >= h.get("skip_until", 0)


def record_route_result(route_name: str, success: bool, error: str = ""):
    global _last_working_route
    with _lock:
        h = _route_health.setdefault(route_name, {
            "successes": 0, "failures": 0, "consecutive_failures": 0,
            "skip_until": 0, "last_error": "", "last_checked": None,
        })
        h["last_checked"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if success:
            h["successes"] += 1
            h["consecutive_failures"] = 0
            h["skip_until"] = 0
            h["last_error"] = ""
            _last_working_route = route_name
        else:
            h["failures"] += 1
            h["consecutive_failures"] += 1
            h["last_error"] = str(error)[:200]
            if h["consecutive_failures"] >= _ROUTE_FAIL_THRESHOLD:
                h["skip_until"] = time.time() + _ROUTE_COOLDOWN_SECONDS
                h["consecutive_failures"] = 0
                log.warning(f"⏸️ Route '{route_name}' failing repeatedly — cooling down for {_ROUTE_COOLDOWN_SECONDS // 60} min")


def get_ordered_proxy_routes(include_cooling: bool = False) -> list[dict]:
    """Ordered list of {"name", "proxy", "type"} routes for the current mode."""
    global _proxy_index
    mode = get_proxy_mode()
    customs = get_custom_proxies()
    if customs:
        with _lock:
            start = _proxy_index % len(customs)
            _proxy_index += 1
        customs = customs[start:] + customs[:start]

    custom_routes = [{"name": f"proxy:{mask_proxy(p)}", "proxy": p, "type": "custom"} for p in customs]
    direct = [{"name": "direct", "proxy": None, "type": "direct"}]

    def warp_routes():
        url = get_active_warp_url() if mode != "direct" else None
        return [{"name": "warp", "proxy": url, "type": "warp"}] if url else []

    if mode == "direct":
        routes = direct
    elif mode == "warp":
        routes = warp_routes() + custom_routes + direct
    elif mode == "custom_proxy":
        routes = custom_routes + warp_routes() + direct
    else:  # auto
        routes = custom_routes + warp_routes() + direct

    if include_cooling:
        return routes
    available = [r for r in routes if _route_available(r["name"])]
    return available or routes[-1:]  # always keep at least one route


def get_status_summary() -> dict:
    return {
        "proxy_mode": get_proxy_mode(),
        "warp_enabled": _warp_enabled(),
        "warp_healthy": bool(_warp_health.get("ok")) and _warp_port_open(),
        "warp_exit_ip": _warp_health.get("ip"),
        "custom_proxies": [mask_proxy(p) for p in get_custom_proxies()],
        "last_working_route": _last_working_route,
        "route_health": {
            k: {**v, "cooling_down": time.time() < v.get("skip_until", 0)} for k, v in _route_health.items()
        },
    }


def test_network_layers() -> dict:
    """Probe exit IP for every route (no TikTok traffic)."""
    out = {"direct": probe_ip_via_proxy(None, timeout=8)}
    warp_url = get_active_warp_url()
    out["warp"] = probe_ip_via_proxy(warp_url, timeout=10) if warp_url else {
        "ok": False,
        "status": "disabled" if not _warp_enabled() else "unavailable",
    }
    customs = get_custom_proxies()
    out["custom_proxies"] = [
        {"proxy": mask_proxy(p), **probe_ip_via_proxy(p, timeout=10)} for p in customs[:5]
    ] or {"ok": False, "status": "not_configured"}
    return out
