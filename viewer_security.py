"""IP blocklist + automatic vulnerability-scan blocking for the Flask app.

Manual list: ``blocked_ips.json`` (override with ``RESOURCE_BLOCKED_IPS_JSON``).
Auto list: ``auto_blocked_ips.json`` (override with ``RESOURCE_AUTO_BLOCK_JSON``).

Two triggers:
1. Instant block — request path/query matches a known probe pattern
   (MCP, router vulns, JWT hunting, secret/config disclosure, etc.).
   First hit is blocked immediately with 403 and the IP is persisted.
2. Rate block — too many 404s in a short window (dir fuzzing /
   enumeration). Tracked in memory, persisted on trigger.

Local/private IPs are never auto-blocked so you can't lock yourself out.
"""

import ipaddress
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

BLOCKED_IPS_JSON = Path(
    os.getenv("RESOURCE_BLOCKED_IPS_JSON", APP_DIR / "blocked_ips.json")
).resolve()
AUTO_BLOCK_JSON = Path(
    os.getenv("RESOURCE_AUTO_BLOCK_JSON", APP_DIR / "auto_blocked_ips.json")
).resolve()

_lock = threading.Lock()
_404_hits = defaultdict(deque)  # ip -> deque[timestamps]
_auto_cache = {"mtime": 0, "data": {}}

# Never auto-block these (you, LAN, docker).
TRUSTED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
]

# path+query (lowercased) substrings/regex that mean "vulnerability scan".
# Kept as substrings for speed; regex only where needed.
SUSPICIOUS_SUBSTRINGS = [
    # MCP / AI endpoint hunting (your log: /api/mcp, /mcp/, /mcp, /sse)
    "/api/mcp", "/mcp", "/sse", "/api/sse",
    # Router / firmware vuln probes (your log)
    "/cgi-bin/authlogin.cgi", "/cgi-bin/", "currentsetting.htm",
    "zld_product_spec", "/ext-js/",
    "/api/session/properties",
    # JWT / token hunting (your log: /v404/exec?jwt=)
    "/v404/", "?jwt=", "jwt=", "bearer ",
    # Config / secret disclosure
    ".env", ".git/", ".svn/", ".hg/", ".aws/", "id_rsa",
    "wp-config", "config.php", "appsettings.json", "web.config",
    # Admin / DB panels
    "phpmyadmin", "pma/", "adminer", "wp-admin", "wp-login",
    "jmx-console", "manager/html", "druid/", "actuator/",
    "graphql", ".well-known/security.txt",
    # IoT / camera / router classics
    "boaform/", "goform/", "gponform/", "hudson", "axis-cgi/",
    "videostream.cgi", "snapshot.cgi",
    # Shellshock / traversal / injection probes
    "../", "..%2f", "%2e%2e", "/etc/passwd", "/proc/self",
    "nessus", "nikto", "sqlmap", "masscan",
    ".php?", ".asp?", ".aspx?", ".jsp?",
]

SUSPICIOUS_RE = re.compile(
    r"(\.php($|[/.?])|\.asp($|[/.?])|\.jspx?($|[/.?])"
    r"|/console/|/debug/|/status\.json"
    r"|exec(\?|/|$)|shell(\?|/|$)|cmd=|command=)",
    re.IGNORECASE,
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_trusted(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_NETWORKS)


def load_blocklist(path=None):
    """Load the manual blocklist. Never raises; returns (ips set, cidr list, enabled)."""
    target = Path(path) if path else BLOCKED_IPS_JSON
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set(), [], True
    if not isinstance(data, dict):
        return set(), [], True
    enabled = data.get("enabled", True)
    if not enabled:
        return set(), [], False

    ips = set()
    raw_ips = data.get("blocked_ips", [])
    if isinstance(raw_ips, list):
        for entry in raw_ips:
            if isinstance(entry, str):
                candidate = entry.strip()
            elif isinstance(entry, dict):
                candidate = str(entry.get("ip") or "").strip()
            else:
                continue
            if candidate:
                ips.add(candidate)

    networks = []
    raw_cidrs = data.get("blocked_cidrs", [])
    if isinstance(raw_cidrs, list):
        for entry in raw_cidrs:
            if isinstance(entry, str):
                candidate = entry.strip()
            elif isinstance(entry, dict):
                candidate = str(entry.get("cidr") or "").strip()
            else:
                continue
            if not candidate:
                continue
            try:
                networks.append(ipaddress.ip_network(candidate, strict=False))
            except ValueError:
                continue
    return ips, networks, True


def load_auto_config():
    """Auto-block tuning from blocked_ips.json. Never raises."""
    defaults = {
        "enabled": True,
        "not_found_threshold": 10,
        "not_found_window_s": 60,
    }
    try:
        with open(BLOCKED_IPS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = data.get("auto_block", {})
        if isinstance(cfg, dict):
            for key in defaults:
                if key in cfg:
                    defaults[key] = cfg[key]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    # Extra patterns appended via config: {"auto_block_patterns": [...]}
    try:
        with open(BLOCKED_IPS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        extra = data.get("auto_block_patterns", [])
        if isinstance(extra, list):
            return defaults, [str(p).lower() for p in extra if str(p).strip()]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return defaults, []


def load_auto_blocks():
    """Load persisted auto blocks {ip: info}. Cached by mtime. Never raises."""
    try:
        mtime = AUTO_BLOCK_JSON.stat().st_mtime
    except OSError:
        return {}
    if mtime == _auto_cache["mtime"]:
        return _auto_cache["data"]
    try:
        with open(AUTO_BLOCK_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _auto_cache["data"]
    if not isinstance(data, dict):
        data = {}
    _auto_cache["mtime"] = mtime
    _auto_cache["data"] = data
    return data


def auto_block_ip(ip, reason, path=""):
    """Persist an auto-block. Returns True if newly added."""
    ip = (ip or "").strip()
    if not ip or is_trusted(ip):
        return False
    now = _now_iso()
    with _lock:
        try:
            with open(AUTO_BLOCK_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        existing = data.get(ip)
        if isinstance(existing, dict):
            existing["hits"] = int(existing.get("hits") or 1) + 1
            existing["last_seen"] = now
            existing["last_reason"] = reason
            paths = existing.get("paths") or []
            if path and path not in paths:
                paths.append(path[-200:])
            existing["paths"] = paths[-10:]
            data[ip] = existing
            is_new = False
        else:
            data[ip] = {
                "reason": reason,
                "first_seen": now,
                "last_seen": now,
                "last_reason": reason,
                "hits": 1,
                "paths": [path[-200:]] if path else [],
            }
            is_new = True
        try:
            AUTO_BLOCK_JSON.parent.mkdir(parents=True, exist_ok=True)
            with open(AUTO_BLOCK_JSON, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            try:
                _auto_cache["mtime"] = AUTO_BLOCK_JSON.stat().st_mtime
            except OSError:
                pass
            _auto_cache["data"] = data
        except OSError:
            return False
    print(f"[security] auto-blocked {ip}: {reason} ({path})")
    return is_new


def unblock_auto_ip(ip):
    ip = (ip or "").strip()
    with _lock:
        try:
            with open(AUTO_BLOCK_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
        if ip not in data:
            return False
        del data[ip]
        try:
            with open(AUTO_BLOCK_JSON, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            _auto_cache["data"] = data
        except OSError:
            return False
    _404_hits.pop(ip, None)
    return True


def match_suspicious(full_path, extra_patterns=()):
    """Return matched pattern string, or None."""
    target = (full_path or "").lower()
    for pat in SUSPICIOUS_SUBSTRINGS:
        if pat in target:
            return pat
    for pat in extra_patterns:
        if pat and pat in target:
            return pat
    if SUSPICIOUS_RE.search(target):
        return "suspicious-regex"
    return None


def client_ips_from_request(request):
    """Collect candidate client IPs: X-Forwarded-For chain + remote_addr."""
    candidates = []
    forwarded = request.headers.get("X-Forwarded-For", "")
    for part in forwarded.split(","):
        part = part.strip()
        if part:
            candidates.append(part)
    real_ip = request.headers.get("X-Real-IP", "").strip()
    if real_ip:
        candidates.append(real_ip)
    if request.remote_addr:
        candidates.append(request.remote_addr.strip())
    return candidates


def connection_ip(request):
    """The real TCP peer — used as the auto-block key (XFF is spoofable)."""
    return (request.remote_addr or "").strip()


def is_blocked(client_ip, blocked_ips, blocked_networks, auto_blocks=None):
    if not client_ip:
        return False
    if client_ip in blocked_ips:
        return True
    if auto_blocks and client_ip in auto_blocks:
        return True
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(addr in net for net in blocked_networks)


def _record_404(ip, threshold, window_s):
    """Track a 404; return True if the rate threshold just tripped."""
    now = time.monotonic()
    with _lock:
        dq = _404_hits[ip]
        dq.append(now)
        while dq and now - dq[0] > window_s:
            dq.popleft()
        return len(dq) >= threshold


def register_ip_blocking(flask_app):
    @flask_app.before_request
    def _block_denied_ips():
        from flask import jsonify, request

        blocked_ips, blocked_networks, enabled = load_blocklist()
        auto_blocks = load_auto_blocks()
        auto_cfg, extra_patterns = load_auto_config()
        for candidate in client_ips_from_request(request):
            if is_blocked(candidate, blocked_ips, blocked_networks, auto_blocks):
                return jsonify({"ok": False, "message": "Forbidden"}), 403
        if not enabled:
            return None

        # Instant block on known probe signatures.
        try:
            full = request.full_path  # path + query string
        except Exception:
            full = request.path
        matched = match_suspicious(full, extra_patterns)
        if matched and auto_cfg.get("enabled", True):
            ip = connection_ip(request)
            if ip and not is_trusted(ip):
                auto_block_ip(ip, f"vulnerability-probe pattern: {matched}", full)
                return jsonify({"ok": False, "message": "Forbidden"}), 403
        return None

    @flask_app.after_request
    def _track_enumeration(response):
        from flask import request

        try:
            auto_cfg, _ = load_auto_config()
            if not auto_cfg.get("enabled", True):
                return response
            if response.status_code != 404:
                return response
            # Don't count probes already handled, or our own traffic.
            if request.path.startswith("/security/"):
                return response
            ip = connection_ip(request)
            if not ip or is_trusted(ip):
                return response
            if _record_404(
                ip,
                int(auto_cfg.get("not_found_threshold", 10)),
                int(auto_cfg.get("not_found_window_s", 60)),
            ):
                auto_block_ip(
                    ip,
                    f"enumeration: {auto_cfg.get('not_found_threshold', 10)}x 404 "
                    f"in {auto_cfg.get('not_found_window_s', 60)}s",
                    request.path,
                )
        except Exception:
            pass
        return response

    @flask_app.get("/security/blocks")
    def security_blocks():
        from flask import jsonify

        blocked_ips, blocked_networks, _ = load_blocklist()
        return jsonify({
            "ok": True,
            "manual_ips": sorted(blocked_ips),
            "manual_cidrs": [str(n) for n in blocked_networks],
            "auto": load_auto_blocks(),
        }), 200

    @flask_app.post("/security/unblock")
    def security_unblock():
        from flask import jsonify, request

        data = request.get_json(silent=True) or request.form
        ip = str(data.get("ip") or "").strip()
        if not ip:
            return jsonify({"ok": False, "message": "Missing ip"}), 400
        if unblock_auto_ip(ip):
            return jsonify({"ok": True, "message": f"Unblocked {ip}"}), 200
        return jsonify({"ok": False, "message": "IP not in auto-block list"}), 404
