#!/usr/bin/env python3
"""
Proxy Scraper Telegram Bot v2 — Production Grade
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Real features (not placeholders):
  • HTTP / SOCKS4 / SOCKS5 actual testing via aiohttp-socks
  • Auto protocol detection (tries HTTP→SOCKS5→SOCKS4)
  • HTML table scraping (free-proxy-list.net style)
  • GeoIP country + anonymity level lookup
  • Throttled progress (respects Telegram 30 edits/min limit)
  • asyncio.Semaphore concurrency (not naive batches)
  • Retry once on timeout
  • Per-user settings (timeout, concurrency, test URL)
  • Multi-format export: TXT / CSV / JSON
  • Dedup + valid IP validation
  • Concurrent multi-source collection
  • Real error messages (not generic failures)
"""

import asyncio
import aiohttp
import csv
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from aiohttp_socks import ProxyConnector, ProxyType
from bs4 import BeautifulSoup
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.error import RetryAfter, BadRequest

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("proxybot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ─── Defaults ────────────────────────────────────────────────────────────────
DEFAULT_TIMEOUT   = 10        # seconds per proxy test
DEFAULT_CONCUR    = 80        # simultaneous connections
DEFAULT_TEST_URL  = "http://httpbin.org/ip"
RETRY_TIMEOUT     = 15        # seconds for retry pass
PROGRESS_MIN_GAP  = 3.0       # minimum seconds between progress edits
GEO_BATCH         = 40        # concurrent geoip lookups

# ─── Free Proxy Sources ───────────────────────────────────────────────────────
FREE_SOURCES: dict[str, dict] = {
    "ProxyScrape HTTP": {
        "url": "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=5000&country=all",
        "type": "http", "parser": "text",
    },
    "ProxyScrape SOCKS4": {
        "url": "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks4&timeout=5000",
        "type": "socks4", "parser": "text",
    },
    "ProxyScrape SOCKS5": {
        "url": "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5&timeout=5000",
        "type": "socks5", "parser": "text",
    },
    "TheSpeedX HTTP": {
        "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "type": "http", "parser": "text",
    },
    "TheSpeedX SOCKS4": {
        "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
        "type": "socks4", "parser": "text",
    },
    "TheSpeedX SOCKS5": {
        "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        "type": "socks5", "parser": "text",
    },
    "ShiftyTR HTTP": {
        "url": "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
        "type": "http", "parser": "text",
    },
    "ShiftyTR HTTPS": {
        "url": "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt",
        "type": "http", "parser": "text",
    },
    "MuRongPIG HTTP": {
        "url": "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt",
        "type": "http", "parser": "text",
    },
    "MuRongPIG SOCKS5": {
        "url": "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt",
        "type": "socks5", "parser": "text",
    },
    "GeoNode HTTP": {
        "url": "https://proxylist.geonode.com/api/proxy-list?limit=200&page=1&sort_by=lastChecked&sort_type=desc&protocols=http,https",
        "type": "auto", "parser": "geonode_json",
    },
    "ProxyList.to": {
        "url": "https://www.proxy-list.download/api/v1/get?type=http",
        "type": "http", "parser": "text",
    },
    "Free-Proxy-List.net": {
        "url": "https://free-proxy-list.net/",
        "type": "auto", "parser": "html_table",
    },
    "SSL-Proxies.org": {
        "url": "https://www.sslproxies.org/",
        "type": "http", "parser": "html_table",
    },
}

# ─── Data Classes ────────────────────────────────────────────────────────────
@dataclass
class ProxyResult:
    proxy: str                       # ip:port
    protocol: str = "unknown"        # http / socks4 / socks5 / unknown
    alive: bool   = False
    response_ms: Optional[int] = None
    country: str  = ""
    country_flag: str = ""
    city: str     = ""
    isp: str      = ""
    anonymity: str = ""              # transparent / anonymous / elite
    error: str    = ""

    def to_row(self) -> list:
        return [
            self.proxy, self.protocol,
            "✅" if self.alive else "❌",
            f"{self.response_ms}ms" if self.response_ms else "-",
            self.country, self.city, self.isp, self.anonymity,
        ]


@dataclass
class UserSettings:
    timeout: int   = DEFAULT_TIMEOUT
    concur: int    = DEFAULT_CONCUR
    test_url: str  = DEFAULT_TEST_URL
    geo_lookup: bool = True
    export_fmt: str  = "txt"          # txt / csv / json


# ─── IP / Proxy Utilities ─────────────────────────────────────────────────────
_PROXY_RE = re.compile(
    r'\b((?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}'
    r'(?:25[0-5]|2[0-4]\d|[01]?\d\d?)):'
    r'(\d{2,5})\b'
)
_PRIVATE_RANGES = [
    re.compile(r'^10\.'),
    re.compile(r'^127\.'),
    re.compile(r'^172\.(1[6-9]|2\d|3[01])\.'),
    re.compile(r'^192\.168\.'),
]

def _is_private(ip: str) -> bool:
    return any(r.match(ip) for r in _PRIVATE_RANGES)

def extract_proxies(text: str, hint_type: str = "auto") -> list[tuple[str, str]]:
    """
    Returns list of (proxy_str, type_hint) from raw text.
    Validates ports, drops private IPs, deduplicates.
    """
    seen = set()
    out  = []
    for ip, port_str in _PROXY_RE.findall(text):
        port = int(port_str)
        if not (1 <= port <= 65535):
            continue
        if _is_private(ip):
            continue
        key = f"{ip}:{port}"
        if key not in seen:
            seen.add(key)
            out.append((key, hint_type))
    return out


def extract_proxies_geonode(data: dict) -> list[tuple[str, str]]:
    """Parse GeoNode JSON API response."""
    out = []
    for item in data.get("data", []):
        ip   = item.get("ip", "")
        port = item.get("port", "")
        if not ip or not port:
            continue
        proxy = f"{ip}:{port}"
        # pick highest protocol priority
        protocols = item.get("protocols", ["http"])
        ptype = "socks5" if "socks5" in protocols else (
                "socks4" if "socks4" in protocols else "http")
        out.append((proxy, ptype))
    return out


def extract_proxies_html_table(html: str, hint_type: str) -> list[tuple[str, str]]:
    """Parse free-proxy-list.net style HTML tables."""
    soup = BeautifulSoup(html, "lxml")
    out  = []
    seen = set()

    # Strategy 1: look for textarea (some sites put raw list there)
    for ta in soup.find_all("textarea"):
        proxies = extract_proxies(ta.get_text(), hint_type)
        for p in proxies:
            if p[0] not in seen:
                seen.add(p[0])
                out.append(p)

    # Strategy 2: standard <table> rows
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows[1:]:  # skip header
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) >= 2:
                ip, port_str = cols[0], cols[1]
                if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                    try:
                        port = int(port_str)
                        if 1 <= port <= 65535 and not _is_private(ip):
                            key = f"{ip}:{port}"
                            if key not in seen:
                                seen.add(key)
                                # column 4 is usually type on free-proxy-list
                                ptype = hint_type
                                if len(cols) > 4:
                                    t = cols[4].lower()
                                    if "socks5" in t:
                                        ptype = "socks5"
                                    elif "socks4" in t:
                                        ptype = "socks4"
                                out.append((key, ptype))
                    except ValueError:
                        pass
    return out


# ─── HTTP Fetch ───────────────────────────────────────────────────────────────
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*",
}

async def fetch(url: str, timeout: int = 20,
                return_json: bool = False) -> Optional[str | dict]:
    try:
        async with aiohttp.ClientSession(headers=_HEADERS) as s:
            async with s.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                ssl=False,
                allow_redirects=True,
            ) as r:
                if r.status != 200:
                    logger.warning(f"HTTP {r.status} for {url}")
                    return None
                if return_json:
                    return await r.json(content_type=None)
                return await r.text(errors="replace")
    except Exception as e:
        logger.warning(f"fetch({url}): {e}")
        return None


# ─── Proxy Testing ────────────────────────────────────────────────────────────
async def _test_http(proxy: str, test_url: str, timeout: int) -> Optional[int]:
    """Test HTTP proxy. Returns ms or None."""
    t0 = time.perf_counter()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                test_url,
                proxy=f"http://{proxy}",
                timeout=aiohttp.ClientTimeout(total=timeout),
                ssl=False,
            ) as r:
                if r.status in (200, 204):
                    return int((time.perf_counter() - t0) * 1000)
    except Exception:
        pass
    return None


async def _test_socks(proxy: str, ptype: ProxyType,
                      test_url: str, timeout: int) -> Optional[int]:
    """Test SOCKS4/5 proxy using aiohttp-socks. Returns ms or None."""
    ip, port = proxy.rsplit(":", 1)
    t0 = time.perf_counter()
    try:
        connector = ProxyConnector(
            proxy_type=ptype,
            host=ip,
            port=int(port),
            rdns=True,
        )
        async with aiohttp.ClientSession(connector=connector) as s:
            async with s.get(
                test_url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                ssl=False,
            ) as r:
                if r.status in (200, 204):
                    return int((time.perf_counter() - t0) * 1000)
    except Exception:
        pass
    return None


async def test_proxy(
    proxy: str,
    hint_type: str,
    test_url: str,
    timeout: int,
    sem: asyncio.Semaphore,
) -> ProxyResult:
    """
    Test a proxy, auto-detecting protocol if hint is 'auto' or 'unknown'.
    Returns a ProxyResult.
    """
    result = ProxyResult(proxy=proxy, protocol=hint_type)

    async with sem:
        # Build ordered list of protocols to try
        if hint_type in ("http", "https"):
            order = [("http", None)]
        elif hint_type == "socks4":
            order = [("socks4", ProxyType.SOCKS4)]
        elif hint_type == "socks5":
            order = [("socks5", ProxyType.SOCKS5)]
        else:  # auto / unknown — try all three
            order = [
                ("http",   None),
                ("socks5", ProxyType.SOCKS5),
                ("socks4", ProxyType.SOCKS4),
            ]

        for proto, ptype in order:
            if ptype is None:
                ms = await _test_http(proxy, test_url, timeout)
            else:
                ms = await _test_socks(proxy, ptype, test_url, timeout)

            if ms is not None:
                result.alive       = True
                result.protocol    = proto
                result.response_ms = ms
                return result

        # Retry once with longer timeout if completely unknown
        if hint_type in ("auto", "unknown"):
            ms = await _test_http(proxy, test_url, RETRY_TIMEOUT)
            if ms is not None:
                result.alive       = True
                result.protocol    = "http"
                result.response_ms = ms

    return result


# ─── GeoIP Lookup ─────────────────────────────────────────────────────────────
async def geo_lookup_batch(results: list[ProxyResult]) -> None:
    """
    Fill country / isp / anonymity on alive proxies using ip-api.com batch API.
    Modifies in-place. ip-api.com free: 100 IPs per batch, 45 req/min.
    """
    alive = [r for r in results if r.alive]
    if not alive:
        return

    # Build IP list (deduplicated by proxy)
    ips = [r.proxy.rsplit(":", 1)[0] for r in alive]
    ip_to_results: dict[str, list[ProxyResult]] = {}
    for r in alive:
        ip = r.proxy.rsplit(":", 1)[0]
        ip_to_results.setdefault(ip, []).append(r)

    FLAGS = {
        "AF": "🇦🇫", "AL": "🇦🇱", "DZ": "🇩🇿", "AO": "🇦🇴", "AR": "🇦🇷",
        "AM": "🇦🇲", "AU": "🇦🇺", "AT": "🇦🇹", "AZ": "🇦🇿", "BD": "🇧🇩",
        "BE": "🇧🇪", "BR": "🇧🇷", "BG": "🇧🇬", "CA": "🇨🇦", "CL": "🇨🇱",
        "CN": "🇨🇳", "CO": "🇨🇴", "HR": "🇭🇷", "CZ": "🇨🇿", "DK": "🇩🇰",
        "EG": "🇪🇬", "ET": "🇪🇹", "FI": "🇫🇮", "FR": "🇫🇷", "DE": "🇩🇪",
        "GH": "🇬🇭", "GR": "🇬🇷", "HK": "🇭🇰", "HU": "🇭🇺", "IN": "🇮🇳",
        "ID": "🇮🇩", "IR": "🇮🇷", "IQ": "🇮🇶", "IL": "🇮🇱", "IT": "🇮🇹",
        "JP": "🇯🇵", "JO": "🇯🇴", "KZ": "🇰🇿", "KE": "🇰🇪", "KR": "🇰🇷",
        "KW": "🇰🇼", "LB": "🇱🇧", "LY": "🇱🇾", "MY": "🇲🇾", "MX": "🇲🇽",
        "MD": "🇲🇩", "MA": "🇲🇦", "MZ": "🇲🇿", "MM": "🇲🇲", "NP": "🇳🇵",
        "NL": "🇳🇱", "NZ": "🇳🇿", "NG": "🇳🇬", "NO": "🇳🇴", "PK": "🇵🇰",
        "PA": "🇵🇦", "PE": "🇵🇪", "PH": "🇵🇭", "PL": "🇵🇱", "PT": "🇵🇹",
        "RO": "🇷🇴", "RU": "🇷🇺", "SA": "🇸🇦", "RS": "🇷🇸", "SG": "🇸🇬",
        "ZA": "🇿🇦", "ES": "🇪🇸", "LK": "🇱🇰", "SE": "🇸🇪", "CH": "🇨🇭",
        "TW": "🇹🇼", "TZ": "🇹🇿", "TH": "🇹🇭", "TN": "🇹🇳", "TR": "🇹🇷",
        "UA": "🇺🇦", "AE": "🇦🇪", "GB": "🇬🇧", "US": "🇺🇸", "UZ": "🇺🇿",
        "VN": "🇻🇳", "YE": "🇾🇪", "ZM": "🇿🇲", "ZW": "🇿🇼",
    }

    # Send batches of 100
    unique_ips = list(ip_to_results.keys())
    sem = asyncio.Semaphore(3)  # max 3 concurrent batch calls

    async def lookup_chunk(chunk: list[str]) -> None:
        payload = [{"query": ip, "fields": "status,countryCode,city,isp,proxy,query"}
                   for ip in chunk]
        async with sem:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        "http://ip-api.com/batch",
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as r:
                        if r.status != 200:
                            return
                        data = await r.json()
                        for item in data:
                            ip = item.get("query", "")
                            if item.get("status") == "success":
                                cc   = item.get("countryCode", "")
                                flag = FLAGS.get(cc, "🌐")
                                city = item.get("city", "")
                                isp  = item.get("isp", "")
                                anon = "anonymous" if item.get("proxy") else "transparent"
                                for res in ip_to_results.get(ip, []):
                                    res.country      = cc
                                    res.country_flag = flag
                                    res.city         = city
                                    res.isp          = isp[:30]
                                    res.anonymity    = anon
            except Exception as e:
                logger.warning(f"GeoIP batch error: {e}")

    tasks = []
    for i in range(0, len(unique_ips), 100):
        tasks.append(lookup_chunk(unique_ips[i:i+100]))
    if tasks:
        await asyncio.gather(*tasks)


# ─── Source Fetcher ───────────────────────────────────────────────────────────
async def collect_from_source(name: str, cfg: dict) -> list[tuple[str, str]]:
    """Fetch and parse proxies from one source. Returns [(proxy, type), ...]."""
    url    = cfg["url"]
    parser = cfg["parser"]
    ptype  = cfg["type"]
    proxies: list[tuple[str, str]] = []

    if parser == "text":
        content = await fetch(url)
        if content:
            proxies = extract_proxies(content, ptype)

    elif parser == "geonode_json":
        data = await fetch(url, return_json=True)
        if isinstance(data, dict):
            proxies = extract_proxies_geonode(data)
        elif isinstance(data, str):
            # fallback text parse if content-type wrong
            proxies = extract_proxies(data, ptype)

    elif parser == "html_table":
        html = await fetch(url)
        if html:
            proxies = extract_proxies_html_table(html, ptype)

    logger.info(f"Source [{name}]: {len(proxies)} proxies found")
    return proxies


async def collect_all_sources(
    names: Optional[list[str]] = None,
) -> list[tuple[str, str]]:
    """Concurrently fetch all (or selected) sources. Returns deduplicated list."""
    targets = {k: v for k, v in FREE_SOURCES.items()
               if names is None or k in names}

    tasks = [collect_from_source(n, c) for n, c in targets.items()]
    results = await asyncio.gather(*tasks)

    seen: set[str] = set()
    merged: list[tuple[str, str]] = []
    for batch in results:
        for proxy, ptype in batch:
            if proxy not in seen:
                seen.add(proxy)
                merged.append((proxy, ptype))

    return merged


async def scrape_url_for_proxies(url: str) -> list[tuple[str, str]]:
    """
    Smart scrape: try JSON → HTML table → plain text in order.
    Returns [(proxy, type), ...].
    """
    content = await fetch(url)
    if content is None:
        return []

    # Try JSON
    try:
        data = json.loads(content)
        if isinstance(data, dict) and "data" in data:
            proxies = extract_proxies_geonode(data)
            if proxies:
                return proxies
    except (json.JSONDecodeError, ValueError):
        pass

    # Try HTML table
    if "<table" in content.lower() or "<html" in content.lower():
        proxies = extract_proxies_html_table(content, "auto")
        if proxies:
            return proxies

    # Fallback: plain text
    return extract_proxies(content, "auto")


# ─── Test Runner ─────────────────────────────────────────────────────────────
async def run_tests(
    proxies: list[tuple[str, str]],
    settings: UserSettings,
    progress_cb=None,
) -> list[ProxyResult]:
    """
    Test all proxies concurrently using a semaphore.
    Calls progress_cb(done, total, alive_count) periodically.
    """
    sem     = asyncio.Semaphore(settings.concur)
    results = []
    done    = 0
    alive   = 0
    lock    = asyncio.Lock()
    last_cb = [0.0]

    async def _test_one(proxy: str, ptype: str) -> ProxyResult:
        nonlocal done, alive
        r = await test_proxy(proxy, ptype, settings.test_url, settings.timeout, sem)
        async with lock:
            done += 1
            if r.alive:
                alive += 1
            results.append(r)
            now = time.monotonic()
            if progress_cb and (now - last_cb[0] >= PROGRESS_MIN_GAP or done == len(proxies)):
                last_cb[0] = now
                await progress_cb(done, len(proxies), alive)
        return r

    tasks = [_test_one(p, t) for p, t in proxies]
    await asyncio.gather(*tasks)
    return results


# ─── Export Builders ─────────────────────────────────────────────────────────
def build_txt(results: list[ProxyResult], alive_only: bool = True) -> str:
    pool = [r for r in results if r.alive] if alive_only else results
    pool = sorted(pool, key=lambda r: r.response_ms or 99999)
    alive_count = sum(1 for r in results if r.alive)
    rate = alive_count / len(results) * 100 if results else 0
    lines = [
        "# Proxy List — Generated by @ProxyScraperBot",
        f"# Date    : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"# Working : {alive_count} / {len(results)}  ({rate:.1f}%)",
        "",
    ]
    for r in pool:
        if r.alive:
            meta = f"# {r.protocol.upper()} | {r.response_ms}ms | {r.country_flag}{r.country} | {r.anonymity}"
            lines.append(f"{r.proxy}  {meta}")
        else:
            if not alive_only:
                lines.append(f"# DEAD {r.proxy}")
    return "\n".join(lines)


def build_csv(results: list[ProxyResult], alive_only: bool = True) -> str:
    pool = [r for r in results if r.alive] if alive_only else results
    pool = sorted(pool, key=lambda r: r.response_ms or 99999)
    buf  = io.StringIO()
    w    = csv.writer(buf)
    w.writerow(["proxy", "protocol", "alive", "response_ms",
                "country", "city", "isp", "anonymity"])
    for r in pool:
        w.writerow([
            r.proxy, r.protocol, r.alive,
            r.response_ms or "", r.country,
            r.city, r.isp, r.anonymity,
        ])
    return buf.getvalue()


def build_json(results: list[ProxyResult], alive_only: bool = True) -> str:
    pool = [r for r in results if r.alive] if alive_only else results
    pool = sorted(pool, key=lambda r: r.response_ms or 99999)
    data = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "alive": sum(1 for r in results if r.alive),
        "proxies": [asdict(r) for r in pool],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def make_export(results: list[ProxyResult], fmt: str,
                alive_only: bool = True) -> tuple[bytes, str, str]:
    """Returns (bytes, filename_suffix, mime_type)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = "alive" if alive_only else "all"
    if fmt == "csv":
        content = build_csv(results, alive_only)
        return content.encode(), f"proxies_{tag}_{ts}.csv", "text/csv"
    elif fmt == "json":
        content = build_json(results, alive_only)
        return content.encode(), f"proxies_{tag}_{ts}.json", "application/json"
    else:
        content = build_txt(results, alive_only)
        return content.encode(), f"proxies_{tag}_{ts}.txt", "text/plain"


# ─── Summary Formatter ────────────────────────────────────────────────────────
def fmt_summary(results: list[ProxyResult], source: str = "") -> str:
    alive = [r for r in results if r.alive]
    dead  = len(results) - len(alive)
    rate  = len(alive) / len(results) * 100 if results else 0
    emoji = "🟢" if rate >= 50 else ("🟡" if rate >= 20 else "🔴")

    times = [r.response_ms for r in alive if r.response_ms]
    avg_t = round(sum(times) / len(times)) if times else 0
    min_t = min(times, default=0)
    max_t = max(times, default=0)

    # Protocol breakdown
    by_proto: dict[str, int] = {}
    for r in alive:
        by_proto[r.protocol] = by_proto.get(r.protocol, 0) + 1
    proto_str = "  ".join(
        f"`{p.upper()}:{c}`" for p, c in sorted(by_proto.items())
    )

    # Country top-3
    by_country: dict[str, int] = {}
    for r in alive:
        if r.country:
            by_country[r.country] = by_country.get(r.country, 0) + 1
    top_cc = sorted(by_country.items(), key=lambda x: -x[1])[:3]
    cc_str = "  ".join(
        f"{r.country_flag}{r.country}:{c}"
        for cc, c in top_cc
        for r in [next((x for x in alive if x.country == cc), None)]
        if r
    ) or "—"

    lines = [
        f"📊 *Results{(' — ' + source) if source else ''}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"✅ Alive:  `{len(alive)}`",
        f"❌ Dead:   `{dead}`",
        f"📦 Total:  `{len(results)}`",
        f"{emoji} Rate:   `{rate:.1f}%`",
        "━━━━━━━━━━━━━━━━━━━━",
        f"⚡ Avg:    `{avg_t}ms`",
        f"🚀 Best:   `{min_t}ms`",
        f"🐢 Worst:  `{max_t}ms`",
    ]
    if proto_str:
        lines += ["━━━━━━━━━━━━━━━━━━━━", f"🔌 Protocols: {proto_str}"]
    if cc_str != "—":
        lines += [f"🌍 Top countries: {cc_str}"]

    # Top 5 fastest
    top5 = sorted(alive, key=lambda r: r.response_ms or 99999)[:5]
    if top5:
        lines += ["━━━━━━━━━━━━━━━━━━━━", "🏆 *Top 5 Fastest*"]
        for r in top5:
            lines.append(
                f"`{r.proxy}` ⚡{r.response_ms}ms "
                f"{r.country_flag} `{r.protocol.upper()}`"
            )
    return "\n".join(lines)


# ─── Throttled Edit Helper ────────────────────────────────────────────────────
async def safe_edit(msg: Message, text: str, **kwargs) -> None:
    """Edit message, silently swallow 'not modified' and retry on flood."""
    try:
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, **kwargs)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.5)
        try:
            await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, **kwargs)
        except Exception:
            pass
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug(f"safe_edit BadRequest: {e}")
    except Exception as e:
        logger.debug(f"safe_edit error: {e}")


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    filled = int(done / total * width) if total else 0
    return "▓" * filled + "░" * (width - filled)


# ─── Per-user Settings ────────────────────────────────────────────────────────
_USER_SETTINGS: dict[int, UserSettings] = {}

def get_settings(uid: int) -> UserSettings:
    return _USER_SETTINGS.setdefault(uid, UserSettings())


# ─── Core Pipeline ───────────────────────────────────────────────────────────
async def pipeline_scrape_test(
    chat_id: int,
    status_msg: Message,
    context: ContextTypes.DEFAULT_TYPE,
    proxies: list[tuple[str, str]],
    source_label: str,
    settings: UserSettings,
) -> None:
    """Full pipeline: test → geo → export → send files."""
    total = len(proxies)
    start_ts = time.monotonic()

    # ── Phase 1: test ──────────────────────────────────────────────
    async def on_progress(done: int, tot: int, alive: int) -> None:
        pct = int(done / tot * 100)
        bar = _progress_bar(done, tot)
        elapsed = int(time.monotonic() - start_ts)
        await safe_edit(
            status_msg,
            f"⚙️ *Testing Proxies...*\n\n"
            f"`{bar}` `{pct}%`\n"
            f"Tested: `{done}/{tot}` | ✅ Alive: `{alive}`\n"
            f"⏱ Elapsed: `{elapsed}s`",
        )

    await safe_edit(
        status_msg,
        f"⚙️ *Testing {total} proxies...*\n\n"
        f"`{'░' * 10}` `0%`\n"
        f"Concurrency: `{settings.concur}` | Timeout: `{settings.timeout}s`",
    )
    results = await run_tests(proxies, settings, progress_cb=on_progress)

    alive_results = [r for r in results if r.alive]

    # ── Phase 2: GeoIP ────────────────────────────────────────────
    if settings.geo_lookup and alive_results:
        await safe_edit(
            status_msg,
            f"🌍 *GeoIP lookup for {len(alive_results)} alive proxies...*",
        )
        await geo_lookup_batch(results)

    # ── Phase 3: Summary ──────────────────────────────────────────
    summary = fmt_summary(results, source=source_label)
    total_elapsed = int(time.monotonic() - start_ts)
    summary += f"\n\n⏱ *Total time:* `{total_elapsed}s`"

    if not alive_results:
        await safe_edit(status_msg, f"😞 *No working proxies found*\n\n{summary}")
        return

    await safe_edit(status_msg, summary)

    # ── Phase 4: Export ───────────────────────────────────────────
    fmt = settings.export_fmt
    alive_bytes, alive_fname, _ = make_export(results, fmt, alive_only=True)
    all_bytes, all_fname, _     = make_export(results, fmt, alive_only=False)

    await context.bot.send_document(
        chat_id=chat_id,
        document=io.BytesIO(alive_bytes),
        filename=alive_fname,
        caption=(
            f"✅ *Working Proxies* — `{len(alive_results)}/{len(results)}`\n"
            f"Source: {source_label}"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

    # Send "all" file only if there are dead proxies worth saving
    if len(results) > len(alive_results) and len(results) <= 2000:
        await context.bot.send_document(
            chat_id=chat_id,
            document=io.BytesIO(all_bytes),
            filename=all_fname,
            caption=f"📋 All proxies (alive + dead) — `{len(results)}` total",
            parse_mode=ParseMode.MARKDOWN,
        )


# ─── Handlers ────────────────────────────────────────────────────────────────

def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 URL Scrape", callback_data="mode:scrape"),
            InlineKeyboardButton("📋 Test List",  callback_data="mode:test"),
        ],
        [
            InlineKeyboardButton("🆓 Free Sources",  callback_data="free:menu"),
            InlineKeyboardButton("⚙️ Settings",      callback_data="settings:menu"),
        ],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *Proxy Scraper Bot v2*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "URL ပေးရုံဖြင့် proxy auto-scrape၊ test ပြီး file ထုတ်ပေးသော bot\n\n"
        "✅ HTTP / SOCKS4 / SOCKS5 real testing\n"
        "🌍 GeoIP country + ISP info\n"
        "📁 TXT / CSV / JSON export\n"
        "⚙️ Adjustable timeout & concurrency\n"
        "🗂️ HTML table + JSON + plaintext scraping\n\n"
        "URL ကို တိုက်ရိုက်ပို့လည်း ရသည် 👇"
    )
    await update.message.reply_text(
        text, reply_markup=_main_keyboard(), parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Commands*\n"
        "━━━━━━━━━━━━━\n"
        "/start — main menu\n"
        "/scrape `<url>` — URL မှ scrape + test\n"
        "/test — proxy list paste mode\n"
        "/free — free source selector\n"
        "/settings — bot settings\n"
        "/help — ဤ message\n\n"
        "📌 *Quick Usage*\n"
        "• URL တိုက်ရိုက်ပို့ → scrape & test\n"
        "• `IP:PORT` lines ပို့ → test only\n"
        "• `.txt` / `.csv` upload → test\n\n"
        "💡 *Supported Sites*\n"
        "free-proxy-list.net, sslproxies.org,\n"
        "raw GitHub lists, GeoNode API,\n"
        "ProxyScrape API, and plain text pages"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_scrape(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: `/scrape <url>`\nExample: `/scrape https://free-proxy-list.net`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    url = context.args[0]
    await _do_scrape_url(update.message, context, url)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "test"
    await update.message.reply_text(
        "📋 *Test Mode*\n\nProxy list ကို paste လုပ်ပါ (`IP:PORT` format, တစ်ကြောင်းစီ):",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🆓 *Free Sources*\n\nSource ရွေးပါ:",
        reply_markup=_free_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s   = get_settings(uid)
    await update.message.reply_text(
        _settings_text(s),
        reply_markup=_settings_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─── Settings UI ─────────────────────────────────────────────────────────────
def _settings_text(s: UserSettings) -> str:
    return (
        "⚙️ *Settings*\n"
        "━━━━━━━━━━━━━\n"
        f"⏱ Timeout:     `{s.timeout}s`\n"
        f"⚡ Concurrency: `{s.concur}`\n"
        f"🌐 Test URL:    `{s.test_url}`\n"
        f"🌍 GeoIP:       `{'on' if s.geo_lookup else 'off'}`\n"
        f"📁 Export fmt:  `{s.export_fmt.upper()}`\n"
    )


def _settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏱ Timeout",      callback_data="set:timeout"),
            InlineKeyboardButton("⚡ Concurrency",  callback_data="set:concur"),
        ],
        [
            InlineKeyboardButton("🌐 Test URL",     callback_data="set:testurl"),
            InlineKeyboardButton("🌍 GeoIP Toggle", callback_data="set:geotoggle"),
        ],
        [
            InlineKeyboardButton("📄 TXT",  callback_data="set:fmt:txt"),
            InlineKeyboardButton("📊 CSV",  callback_data="set:fmt:csv"),
            InlineKeyboardButton("🔷 JSON", callback_data="set:fmt:json"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="back:main")],
    ])


# ─── Free Sources Keyboard ────────────────────────────────────────────────────
def _free_keyboard() -> InlineKeyboardMarkup:
    rows = []
    keys = list(FREE_SOURCES.keys())
    for i in range(0, len(keys), 2):
        row = [InlineKeyboardButton(f"📡 {keys[i]}", callback_data=f"src:{i}")]
        if i + 1 < len(keys):
            row.append(InlineKeyboardButton(f"📡 {keys[i+1]}", callback_data=f"src:{i+1}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("🔄 Collect ALL Sources", callback_data="src:ALL")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="back:main")])
    return InlineKeyboardMarkup(rows)


# ─── Callback Handler ─────────────────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q  = update.callback_query
    d  = q.data
    uid = q.from_user.id
    await q.answer()

    # ── Navigation ────────────────────────────────────────────────
    if d == "back:main":
        await q.edit_message_text(
            "🤖 *Proxy Scraper Bot v2*\n\nMode ရွေးပါ 👇",
            reply_markup=_main_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif d == "help":
        await q.edit_message_text(
            "📖 Use /help for full help.",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── Mode selection ────────────────────────────────────────────
    elif d == "mode:scrape":
        context.user_data["mode"] = "scrape"
        await q.edit_message_text(
            "🌐 *URL Scrape Mode*\n\nProxy list ပါသည့် URL ကို ပို့ပါ:\n\n"
            "Example:\n"
            "`https://free-proxy-list.net`\n"
            "`https://raw.githubusercontent.com/.../http.txt`",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif d == "mode:test":
        context.user_data["mode"] = "test"
        await q.edit_message_text(
            "📋 *Test Mode*\n\n`IP:PORT` list ကို paste လုပ်ပါ:",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── Free sources ──────────────────────────────────────────────
    elif d == "free:menu":
        await q.edit_message_text(
            "🆓 *Free Sources*\n\nSource ရွေးပါ:",
            reply_markup=_free_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif d.startswith("src:"):
        key = d[4:]
        source_keys = list(FREE_SOURCES.keys())
        chat_id = q.message.chat.id
        settings = get_settings(uid)

        if key == "ALL":
            await q.edit_message_text(
                "⏳ *Collecting all free sources...*\n\n"
                f"Fetching {len(FREE_SOURCES)} sources simultaneously...",
                parse_mode=ParseMode.MARKDOWN,
            )
            proxies = await collect_all_sources()
            await q.edit_message_text(
                f"✅ `{len(proxies)}` unique proxies collected from all sources.\n"
                f"⚙️ Testing with concurrency `{settings.concur}`...",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            idx  = int(key)
            name = source_keys[idx]
            cfg  = FREE_SOURCES[name]
            await q.edit_message_text(
                f"⏳ Fetching *{name}*...",
                parse_mode=ParseMode.MARKDOWN,
            )
            proxies = await collect_from_source(name, cfg)
            if not proxies:
                await q.edit_message_text(
                    f"❌ *{name}* မှ proxy မတွေ့ပါ.\n"
                    "Source offline ဖြစ်နေနိုင်သည်။",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            label = name
            await q.edit_message_text(
                f"✅ `{len(proxies)}` proxies found from *{name}*.\n"
                f"⚙️ Testing...",
                parse_mode=ParseMode.MARKDOWN,
            )

        label = "All Free Sources" if key == "ALL" else source_keys[int(key)]
        await pipeline_scrape_test(
            chat_id, q.message, context, proxies, label, settings,
        )

    # ── Settings ──────────────────────────────────────────────────
    elif d == "settings:menu":
        s = get_settings(uid)
        await q.edit_message_text(
            _settings_text(s),
            reply_markup=_settings_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif d == "set:geotoggle":
        s = get_settings(uid)
        s.geo_lookup = not s.geo_lookup
        await q.edit_message_text(
            _settings_text(s),
            reply_markup=_settings_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif d.startswith("set:fmt:"):
        fmt = d.split(":")[-1]
        s = get_settings(uid)
        s.export_fmt = fmt
        await q.edit_message_text(
            _settings_text(s),
            reply_markup=_settings_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif d == "set:timeout":
        context.user_data["awaiting"] = "timeout"
        await q.edit_message_text(
            "⏱ *Timeout Setting*\n\nSeconds ထည့်ပါ (3–30):\nExample: `10`",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif d == "set:concur":
        context.user_data["awaiting"] = "concur"
        await q.edit_message_text(
            "⚡ *Concurrency Setting*\n\nConcurrent connections (10–200):\nExample: `80`",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif d == "set:testurl":
        context.user_data["awaiting"] = "testurl"
        await q.edit_message_text(
            "🌐 *Test URL Setting*\n\nHTTP test URL ထည့်ပါ:\n"
            "Example: `http://httpbin.org/ip`",
            parse_mode=ParseMode.MARKDOWN,
        )


# ─── Message Handler ──────────────────────────────────────────────────────────
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg     = update.message
    uid     = msg.from_user.id
    text    = msg.text or ""
    mode    = context.user_data.get("mode", "auto")
    awaiting = context.user_data.get("awaiting")
    settings = get_settings(uid)

    # ── Settings input ────────────────────────────────────────────
    if awaiting:
        context.user_data.pop("awaiting")
        if awaiting == "timeout":
            try:
                v = int(text.strip())
                settings.timeout = max(3, min(30, v))
                await msg.reply_text(
                    f"✅ Timeout: `{settings.timeout}s`",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except ValueError:
                await msg.reply_text("❌ Invalid number")
        elif awaiting == "concur":
            try:
                v = int(text.strip())
                settings.concur = max(10, min(200, v))
                await msg.reply_text(
                    f"✅ Concurrency: `{settings.concur}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except ValueError:
                await msg.reply_text("❌ Invalid number")
        elif awaiting == "testurl":
            url = text.strip()
            if url.startswith("http"):
                settings.test_url = url
                await msg.reply_text(
                    f"✅ Test URL: `{url}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                await msg.reply_text("❌ http:// သို့မဟုတ် https:// ဖြင့်စပါ")
        return

    # ── File upload ───────────────────────────────────────────────
    if msg.document:
        doc = msg.document
        if not doc.file_name:
            await msg.reply_text("❌ Unknown file type")
            return
        ext = doc.file_name.rsplit(".", 1)[-1].lower()
        if ext not in ("txt", "csv"):
            await msg.reply_text("⚠️ .txt or .csv ဖိုင်သာ လက်ခံသည်")
            return
        tg_file = await doc.get_file()
        raw     = await tg_file.download_as_bytearray()
        content = raw.decode("utf-8", errors="ignore")
        proxies = extract_proxies(content, "auto")
        if not proxies:
            await msg.reply_text("❌ `IP:PORT` format proxy မတွေ့ပါ", parse_mode=ParseMode.MARKDOWN)
            return
        status = await msg.reply_text(
            f"📂 `{doc.file_name}` — `{len(proxies)}` proxies found\n⚙️ Testing...",
            parse_mode=ParseMode.MARKDOWN,
        )
        await pipeline_scrape_test(
            msg.chat.id, status, context, proxies, doc.file_name, settings,
        )
        return

    # ── URL detection ─────────────────────────────────────────────
    url_match = re.search(r'https?://\S+', text)
    if url_match or mode == "scrape":
        url = url_match.group(0) if url_match else text.strip()
        if not url.startswith("http"):
            await msg.reply_text("❌ http:// or https:// ဖြင့်စသော URL ပေးပါ")
            return
        context.user_data["mode"] = "auto"
        await _do_scrape_url(msg, context, url)
        return

    # ── Proxy list ────────────────────────────────────────────────
    proxies = extract_proxies(text, "auto")
    if proxies or mode == "test":
        context.user_data["mode"] = "auto"
        if not proxies:
            await msg.reply_text(
                "⚠️ Proxy မတွေ့ပါ. Format: `IP:PORT`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        status = await msg.reply_text(
            f"📋 `{len(proxies)}` proxies detected. Testing...",
            parse_mode=ParseMode.MARKDOWN,
        )
        await pipeline_scrape_test(
            msg.chat.id, status, context, proxies, "Custom List", settings,
        )
        return

    # ── Fallback ──────────────────────────────────────────────────
    await msg.reply_text(
        "❓ URL သို့မဟုတ် `IP:PORT` list ပေးပါ.\n\nMode ရွေးချယ်ပါ 👇",
        reply_markup=_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _do_scrape_url(
    msg: Message,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
) -> None:
    uid      = msg.from_user.id if msg.from_user else 0
    settings = get_settings(uid)

    status = await msg.reply_text(
        f"🔍 *Scraping:*\n`{url[:80]}`\n\n⏳ Fetching...",
        parse_mode=ParseMode.MARKDOWN,
    )
    proxies = await scrape_url_for_proxies(url)

    if not proxies:
        await safe_edit(
            status,
            f"❌ *Proxy မတွေ့ပါ*\n\n"
            f"URL: `{url[:80]}`\n\n"
            "Possible reasons:\n"
            "• Site is down or blocked\n"
            "• No `IP:PORT` format found\n"
            "• JavaScript-rendered page (use /free sources instead)",
        )
        return

    await safe_edit(
        status,
        f"✅ `{len(proxies)}` proxies scraped\n"
        f"🔍 Source: `{url[:60]}`\n"
        f"⚙️ Testing with `{settings.concur}` concurrent...",
    )
    await pipeline_scrape_test(
        msg.chat.id, status, context, proxies,
        url[:50], settings,
    )


# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("❌  BOT_TOKEN မသတ်မှတ်ရသေး!")
        print("    export BOT_TOKEN='8784196407:AAERxmoqgkeZ96yJYsRS653SfWGWQkSSAis'")
        return

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("scrape",   cmd_scrape))
    app.add_handler(CommandHandler("test",     cmd_test))
    app.add_handler(CommandHandler("free",     cmd_free))
    app.add_handler(CommandHandler("settings", cmd_settings))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.Document.ALL,
        on_message,
    ))

    print("🤖 Proxy Scraper Bot v2 started")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
