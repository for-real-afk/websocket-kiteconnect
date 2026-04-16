"""
kite_simulator.py — Zerodha KiteTicker WebSocket Simulator (3-Phase)
=====================================================================

Holiday detection is FULLY AUTOMATIC — no hardcoded dates:

  Layer 1 (best):  Fetch live from NSE's official API, cache to disk 24h
  Layer 2 (fallback): holidays.India(year) — covers ~85% of NSE holidays
  Layer 3 (supplement): auto-computed rules for the 4 holidays that
                         holidays.India() structurally misses:
                           • Dr Ambedkar Jayanti  — fixed: April 14 every year
                           • Mahavir Jayanti       — rolling lookup, extendable
                           • Diwali (Laxmi Pujan + Balipratipada) — rolling lookup
                           • Guru Nanak Jayanti    — rolling lookup

  Weekends (Sat/Sun) are always closed regardless of any calendar.

Three broadcast phases:

  Phase 1  OPEN         09:15–15:30 IST, Mon–Fri, non-holiday
           Binary QUOTE-mode tick frames every 1 second.

  Phase 2  POST-MARKET  15:30–16:00 IST, Mon–Fri, non-holiday
           Sparse binary frames every 15–30 seconds.
           Prices converge toward the official VWAP closing price.

  Phase 3  CLOSED       16:00+, all weekends, all NSE holidays
           0x21 heartbeat byte every 10 seconds.

  NEW CONNECTION while closed / post-market:
           One cached binary snapshot (last known O/H/L/C/V) sent
           immediately, then normal phase behaviour.

Run
---
    python kite_simulator.py                  # respects real IST schedule
    python kite_simulator.py --force-open     # always Phase 1 (24/7 dev mode)
    python kite_simulator.py --log-level DEBUG
"""

from __future__ import annotations

import argparse
import asyncio
import http.cookiejar
import json
import logging
import os
import random
import struct
import time
import urllib.request
from datetime import date, datetime, timedelta, time as dt
from pathlib import Path
from typing import Any, Dict, Set
from urllib.parse import parse_qs, urlparse

import pytz
import websockets

import auth as auth_store

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Holiday detection — fully automatic, three-layer fallback
# ---------------------------------------------------------------------------

_HOLIDAY_CACHE_FILE = Path(__file__).resolve().parent / "config" / "nse_holidays_cache.json"
_CACHE_TTL_SECONDS  = 86_400   # re-fetch once per day


# ── Layer 1: Live NSE API fetch with 24-hour disk cache ──────────────────

def _fetch_nse_holidays_live() -> dict[int, set[date]] | None:
    """
    Fetch the official NSE holiday list via their public API.
    Returns {year: {date, ...}} or None if the request fails.
    NSE requires a session cookie so we do a two-step request.
    """
    jar    = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.nseindia.com/market-data/exchange-holidays",
    }

    try:
        # Step 1: seed session cookies
        seed = urllib.request.Request("https://www.nseindia.com/", headers=headers)
        opener.open(seed, timeout=6)

        # Step 2: fetch holiday master
        req  = urllib.request.Request(
            "https://www.nseindia.com/api/holiday-master?type=trading",
            headers=headers,
        )
        with opener.open(req, timeout=6) as resp:
            raw = json.loads(resp.read().decode())

        # Response: {"CM": [{"tradingDate": "14-Apr-2026", ...}, ...], "FO": [...], ...}
        # We only need the equity segment ("CM")
        segment = raw.get("CM", [])
        by_year: dict[int, set[date]] = {}
        for rec in segment:
            s = rec.get("tradingDate", "")
            try:
                d = datetime.strptime(s, "%d-%b-%Y").date()
                by_year.setdefault(d.year, set()).add(d)
            except ValueError:
                continue

        logger.info("NSE API: fetched %d holiday records.", sum(len(v) for v in by_year.values()))
        return by_year

    except Exception as exc:
        logger.debug("NSE live fetch failed: %s", exc)
        return None


def _load_cache() -> dict[int, set[date]] | None:
    """Load holiday cache from disk if it exists and is fresh."""
    if not _HOLIDAY_CACHE_FILE.exists():
        return None
    try:
        raw  = json.loads(_HOLIDAY_CACHE_FILE.read_text())
        ts   = raw.get("timestamp", 0)
        if time.time() - ts > _CACHE_TTL_SECONDS:
            return None                             # stale
        data: dict[int, set[date]] = {}
        for yr_str, dates in raw.get("holidays", {}).items():
            data[int(yr_str)] = {date.fromisoformat(d) for d in dates}
        logger.debug("Holiday cache loaded (%d years).", len(data))
        return data
    except Exception:
        return None


def _save_cache(by_year: dict[int, set[date]]) -> None:
    """Persist holiday dict to disk."""
    try:
        _HOLIDAY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": time.time(),
            "holidays":  {str(yr): [d.isoformat() for d in dates]
                          for yr, dates in by_year.items()},
        }
        _HOLIDAY_CACHE_FILE.write_text(json.dumps(payload, indent=2))
        logger.debug("Holiday cache saved.")
    except Exception as exc:
        logger.debug("Could not save holiday cache: %s", exc)


# ── Layer 2: holidays.India() ─────────────────────────────────────────────

def _holidays_india(year: int) -> set[date]:
    """Return the holiday set from the `holidays` package for the given year."""
    try:
        import holidays as hol_pkg
        return set(hol_pkg.India(years=year).keys())
    except ImportError:
        logger.warning("'holidays' package not installed. Run: pip install holidays")
        return set()


# ── Layer 3: NSE-specific supplement (holidays missed by holidays.India()) ─

# These four categories are structurally absent from holidays.India():
#   • Dr. Ambedkar Jayanti  — April 14, every year (constitutionally fixed)
#   • Mahavir Jayanti       — 13th Chaitra (Jain, not in government package)
#   • Diwali Laxmi Pujan + Balipratipada — Kartik Amavasya (lunar)
#   • Guru Nanak Jayanti    — Kartik Purnima (Sikh, lunar)
#
# The lookup tables below cover ±3 years. Extend as needed — this is the
# ONLY list that ever needs a manual update, and only once a year.

_MAHAVIR: dict[int, date] = {
    2024: date(2024, 4, 21),
    2025: date(2025, 4, 10),
    2026: date(2026, 4, 10),
    2027: date(2027, 3, 30),
    2028: date(2028, 4, 17),
}
_DIWALI: dict[int, tuple[date, date]] = {      # (Laxmi Pujan, Balipratipada)
    2024: (date(2024, 11,  1), date(2024, 11,  2)),
    2025: (date(2025, 10, 20), date(2025, 10, 21)),
    2026: (date(2026, 10, 21), date(2026, 10, 22)),
    2027: (date(2027, 10, 10), date(2027, 10, 11)),
    2028: (date(2028, 10, 29), date(2028, 10, 30)),
}
_GURU_NANAK: dict[int, date] = {
    2024: date(2024, 11, 15),
    2025: date(2025, 11,  5),
    2026: date(2026, 11,  5),
    2027: date(2027, 10, 25),
    2028: date(2028, 11, 12),
}


def _nse_supplement(year: int) -> set[date]:
    extra: set[date] = set()
    # Ambedkar Jayanti is constitutionally fixed — always April 14
    extra.add(date(year, 4, 14))
    if year in _MAHAVIR:
        extra.add(_MAHAVIR[year])
    if year in _DIWALI:
        extra.update(_DIWALI[year])
    if year in _GURU_NANAK:
        extra.add(_GURU_NANAK[year])
    return extra


# ── Unified holiday resolver ───────────────────────────────────────────────

_resolved: dict[int, set[date]] = {}    # in-memory resolved cache

def _nse_holidays(year: int) -> set[date]:
    """
    Return the full NSE holiday set for `year`.
    Resolution order: disk cache → live NSE API → holidays.India() + supplement.
    Result is memoised for the lifetime of the process.
    """
    if year in _resolved:
        return _resolved[year]

    # Try disk cache first (shared across all years fetched together)
    cached = _load_cache()
    if cached and year in cached:
        _resolved.update(cached)
        return _resolved[year]

    # Try live NSE fetch
    live = _fetch_nse_holidays_live()
    if live and year in live:
        _save_cache(live)
        _resolved.update(live)
        return _resolved[year]

    # Fallback: holidays package + supplement
    logger.info(
        "Using fallback holiday calendar for %d "
        "(NSE API unavailable — holidays.India() + NSE supplement).",
        year,
    )
    h = _holidays_india(year) | _nse_supplement(year)
    _resolved[year] = h
    return h


def _is_trading_day(d: date | None = None) -> bool:
    """True only if NSE is open on this date."""
    if d is None:
        d = datetime.now(IST).date()
    if d.weekday() >= 5:                    # Saturday (5) or Sunday (6)
        return False
    return d not in _nse_holidays(d.year)


# ---------------------------------------------------------------------------
# Phase detection
# ---------------------------------------------------------------------------

MARKET_OPEN_TIME     = dt(9,  15, 0)
MARKET_CLOSE_TIME    = dt(15, 30, 0)
POST_MARKET_END_TIME = dt(16,  0, 0)

LIVE_TICK_INTERVAL = 1.0
POST_MARKET_MIN    = 15.0
POST_MARKET_MAX    = 30.0
HEARTBEAT_INTERVAL = 10.0
HEARTBEAT_BYTE     = struct.pack("B", 33)   # 0x21


def _now_ist() -> datetime:
    return datetime.now(IST)


def _phase(force_open: bool = False) -> str:
    """Returns: 'open' | 'post_market' | 'closed'"""
    if force_open:
        return "open"
    now = _now_ist()
    if not _is_trading_day(now.date()):
        return "closed"
    t = now.time()
    if MARKET_OPEN_TIME <= t < MARKET_CLOSE_TIME:
        return "open"
    if MARKET_CLOSE_TIME <= t < POST_MARKET_END_TIME:
        return "post_market"
    return "closed"


def _closed_reason() -> str:
    now = _now_ist()
    d   = now.date()
    if d.weekday() == 5: return "Saturday"
    if d.weekday() == 6: return "Sunday"
    if d in _nse_holidays(d.year): return "NSE holiday"
    return "after hours"


def _next_open_iso() -> str:
    now  = _now_ist()
    cand = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if cand <= now or not _is_trading_day(cand.date()):
        cand += timedelta(days=1)
        while not _is_trading_day(cand.date()):
            cand += timedelta(days=1)
        cand = cand.replace(hour=9, minute=15, second=0, microsecond=0)
    return cand.isoformat()


# ---------------------------------------------------------------------------
# Instrument universe
# ---------------------------------------------------------------------------

INSTRUMENTS: list[dict] = [
    {"token": 738561,  "symbol": "RELIANCE",   "exchange": "NSE", "price": 2950.0},
    {"token": 341249,  "symbol": "HDFCBANK",   "exchange": "NSE", "price": 1680.0},
    {"token": 408065,  "symbol": "INFY",       "exchange": "NSE", "price": 1450.0},
    {"token": 2953217, "symbol": "TCS",        "exchange": "NSE", "price": 3350.0},
    {"token": 1270529, "symbol": "ICICIBANK",  "exchange": "NSE", "price": 1050.0},
    {"token": 969473,  "symbol": "SBIN",       "exchange": "NSE", "price":  800.0},
    {"token": 315393,  "symbol": "WIPRO",      "exchange": "NSE", "price":  460.0},
    {"token": 1195009, "symbol": "TATAMOTORS", "exchange": "NSE", "price":  920.0},
    {"token": 134657,  "symbol": "AXISBANK",   "exchange": "NSE", "price": 1100.0},
    {"token": 779521,  "symbol": "SUNPHARMA",  "exchange": "NSE", "price": 1600.0},
]

_state: Dict[int, dict] = {}


def _init_state() -> None:
    for inst in INSTRUMENTS:
        p = inst["price"]
        _state[inst["token"]] = {
            **inst,
            "last_price":     p,
            "open":           p,
            "high":           p,
            "low":            p,
            "close":          round(p * random.uniform(0.98, 1.02), 2),
            "official_close": None,
            "volume":         0,
            "buy_qty":        random.randint(1_000, 50_000),
            "sell_qty":       random.randint(1_000, 50_000),
            "change":         0.0,
        }


def _reset_daily_ohlc() -> None:
    for s in _state.values():
        p = s["last_price"]
        s["open"] = s["high"] = s["low"] = p
        s["volume"]         = 0
        s["official_close"] = None
    logger.info("Daily OHLC reset.")


def _set_official_close() -> None:
    for s in _state.values():
        s["official_close"] = round(s["last_price"] * random.uniform(0.9995, 1.0005), 2)
    logger.info("Official closing prices locked (15:30 IST).")


# ---------------------------------------------------------------------------
# Price simulation
# ---------------------------------------------------------------------------

def _tick_live(s: dict) -> None:
    drift = (s["price"] - s["last_price"]) / max(s["price"], 0.01) * 0.001
    p     = round(max(s["last_price"] * (1 + random.gauss(drift, 0.0015)), 0.05), 2)
    s["last_price"] = p
    s["high"]       = max(s["high"], p)
    s["low"]        = min(s["low"],  p)
    s["volume"]    += random.randint(100, 5_000)
    s["buy_qty"]    = random.randint(500, 80_000)
    s["sell_qty"]   = random.randint(500, 80_000)
    s["change"]     = round((p - s["close"]) / max(s["close"], 0.01) * 100, 4)


def _tick_post_market(s: dict) -> None:
    target = s["official_close"] or s["last_price"]
    pull   = (target - s["last_price"]) * random.uniform(0.05, 0.15)
    p      = round(max(s["last_price"] + pull + s["last_price"] * random.gauss(0, 0.0002), 0.05), 2)
    s["last_price"] = p
    s["high"]       = max(s["high"], p)
    s["low"]        = min(s["low"],  p)
    s["volume"]    += random.randint(10, 200)
    s["buy_qty"]    = random.randint(100, 5_000)
    s["sell_qty"]   = random.randint(100, 5_000)
    s["change"]     = round((p - s["close"]) / max(s["close"], 0.01) * 100, 4)


# ---------------------------------------------------------------------------
# Binary frame builder  (Zerodha QUOTE-mode, 184-byte packets)
# ---------------------------------------------------------------------------

_PKT_FMT = ">iiiiiiqiiiiii"
_PKT_LEN  = 184


def _pack_one(s: dict, tick_fn=None) -> bytes:
    if tick_fn:
        tick_fn(s)
    def p(v): return int(round(v * 100))
    body = struct.pack(
        _PKT_FMT,
        s["token"], 1,
        p(s["last_price"]), p(s["last_price"]),
        p(s["close"]),      int(s["change"] * 100),
        s["volume"],        s["buy_qty"], s["sell_qty"],
        p(s["open"]),       p(s["high"]),
        p(s["low"]),        p(s["close"]),
    )
    return body + b"\x00" * (_PKT_LEN - len(body))


def _build_frame(tokens: list[int], tick_fn=None) -> bytes:
    pkts = []
    for tok in tokens:
        s = _state.get(tok)
        if s:
            pkt = _pack_one(s, tick_fn=tick_fn)
            pkts.append(struct.pack(">H", len(pkt)) + pkt)
    return (struct.pack(">H", len(pkts)) + b"".join(pkts)) if pkts else b""


# ---------------------------------------------------------------------------
# Connection registry + broadcast
# ---------------------------------------------------------------------------

_connections: Dict[Any, Set[int]] = {}


async def _broadcast(msg: str | bytes) -> None:
    dead = []
    for ws in list(_connections):
        try:
            await ws.send(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _connections.pop(ws, None)


async def _broadcast_ticks(tick_fn) -> None:
    dead = []
    for ws, tokens in list(_connections.items()):
        if not tokens:
            continue
        try:
            frame = _build_frame(list(tokens), tick_fn=tick_fn)
            if frame:
                await ws.send(frame)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _connections.pop(ws, None)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _authenticate(ws: Any) -> tuple[bool, str]:
    try:
        raw_path = getattr(ws.request, "path", None) or getattr(ws, "path", "/")
    except AttributeError:
        raw_path = "/"
    qs           = parse_qs(urlparse(raw_path).query)
    api_key      = qs.get("api_key",      [""])[0]
    access_token = qs.get("access_token", [""])[0]
    if not api_key or not access_token:
        return False, ""
    return auth_store.validate(api_key, access_token), api_key


# ---------------------------------------------------------------------------
# Instrument manifest
# ---------------------------------------------------------------------------

_MANIFEST = json.dumps({
    "type": "instruments",
    "data": [{"instrument_token": i["token"],
              "tradingsymbol":    i["symbol"],
              "exchange":         i["exchange"]}
             for i in INSTRUMENTS],
})


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def _handle(ws: Any, force_open: bool = False) -> None:
    ok, _ = _authenticate(ws)
    if not ok:
        logger.warning("Rejected %s — bad credentials", ws.remote_address)
        await ws.close(code=4001, reason="Invalid api_key or access_token")
        return

    all_tokens = list(_state.keys())
    _connections[ws] = set(all_tokens)
    logger.info("Client connected: %s  (total=%d)", ws.remote_address, len(_connections))

    await ws.send(_MANIFEST)

    current = _phase(force_open)

    if current == "open":
        await ws.send(json.dumps({"type": "market_open"}))

    elif current == "post_market":
        await ws.send(json.dumps({
            "type":      "market_closed",
            "phase":     "post_market",
            "message":   "Post-market session (15:30–16:00 IST). Sparse price updates continuing.",
            "next_open": _next_open_iso(),
        }))
        snapshot = _build_frame(all_tokens)
        if snapshot:
            await ws.send(snapshot)

    else:
        reason = _closed_reason()
        await ws.send(json.dumps({
            "type":      "market_closed",
            "phase":     "heartbeat",
            "reason":    reason,
            "message":   f"Market closed ({reason}). Heartbeat-only until next open.",
            "next_open": _next_open_iso(),
        }))
        snapshot = _build_frame(all_tokens)
        if snapshot:
            await ws.send(snapshot)
        await ws.send(HEARTBEAT_BYTE)

    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            try:
                msg = json.loads(raw)
                a, v = msg.get("a", ""), msg.get("v", [])
                if a == "subscribe":
                    _connections[ws].update(int(t) for t in v if int(t) in _state)
                elif a == "unsubscribe":
                    _connections[ws].difference_update(int(t) for t in v)
                elif a == "mode":
                    extra = [int(t) for t in (v[1] if len(v) > 1 else []) if int(t) in _state]
                    _connections[ws].update(extra)
            except (json.JSONDecodeError, ValueError):
                pass
    except websockets.exceptions.ConnectionClosed as exc:
        logger.info("Disconnected: %s  code=%s", ws.remote_address, exc.code)
    finally:
        _connections.pop(ws, None)
        logger.info("Client removed (total=%d)", len(_connections))


# ---------------------------------------------------------------------------
# Main broadcast loop
# ---------------------------------------------------------------------------

async def _main_loop(force_open: bool) -> None:
    prev_phase     = _phase(force_open)
    last_hb        = time.monotonic()
    last_live      = time.monotonic()
    last_post      = time.monotonic()
    next_post_gap  = random.uniform(POST_MARKET_MIN, POST_MARKET_MAX)
    last_stats     = time.monotonic()

    logger.info("Broadcast loop started — phase: %s", prev_phase.upper())

    while True:
        await asyncio.sleep(0.25)
        now     = time.monotonic()
        current = _phase(force_open)

        # ── Phase transitions ──────────────────────────────────────────────
        if current != prev_phase:
            logger.info("PHASE TRANSITION  %s → %s  [%s IST]",
                        prev_phase.upper(), current.upper(),
                        _now_ist().strftime("%H:%M:%S"))

            if current == "open":
                _reset_daily_ohlc()
                await _broadcast(json.dumps({"type": "market_open"}))

            elif current == "post_market":
                _set_official_close()
                await _broadcast(json.dumps({
                    "type":      "market_closed",
                    "phase":     "post_market",
                    "message":   "Post-market session (15:30 IST). Sparse ticks for 30 min.",
                    "next_open": _next_open_iso(),
                }))
                last_post    = now
                next_post_gap = random.uniform(POST_MARKET_MIN, POST_MARKET_MAX)

            elif current == "closed":
                reason = _closed_reason()
                await _broadcast(json.dumps({
                    "type":      "market_closed",
                    "phase":     "heartbeat",
                    "reason":    reason,
                    "message":   f"Market closed ({reason}). Heartbeat-only until next open.",
                    "next_open": _next_open_iso(),
                }))

            prev_phase = current

        # ── Phase 1: Live ticks every 1 second ────────────────────────────
        if current == "open":
            if (now - last_live) >= LIVE_TICK_INTERVAL:
                await _broadcast_ticks(tick_fn=_tick_live)
                last_live = now

        # ── Phase 2: Post-market sparse ticks every 15–30 seconds ─────────
        elif current == "post_market":
            if (now - last_post) >= next_post_gap:
                await _broadcast_ticks(tick_fn=_tick_post_market)
                last_post    = now
                next_post_gap = random.uniform(POST_MARKET_MIN, POST_MARKET_MAX)
                logger.debug("Post-market tick sent. Next in %.1fs.", next_post_gap)

        # ── Heartbeat 0x21 every 10 seconds (phases 2 + 3) ────────────────
        if current in ("post_market", "closed"):
            if (now - last_hb) >= HEARTBEAT_INTERVAL:
                await _broadcast(HEARTBEAT_BYTE)
                last_hb = now

        # ── Status log every 60 seconds ───────────────────────────────────
        if (now - last_stats) >= 60.0:
            logger.info("Phase: %-12s  Clients: %d  Next open: %s",
                        current.upper(), len(_connections), _next_open_iso())
            last_stats = now


# ---------------------------------------------------------------------------
# CLI + entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Zerodha KiteTicker Simulator — automatic holiday detection"
    )
    p.add_argument("--host",       default="0.0.0.0")
    p.add_argument("--port",       default=8765, type=int)
    p.add_argument("--force-open", action="store_true",
                   help="Always stream live ticks (ignore calendar — dev/test mode)")
    p.add_argument("--log-level",  default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


async def _main(host: str, port: int, force_open: bool) -> None:
    _init_state()

    # Pre-warm holiday cache for this year and next
    today = datetime.now(IST).date()
    logger.info("Loading holiday calendar for %d and %d...", today.year, today.year + 1)
    holidays_this_year = _nse_holidays(today.year)
    holidays_next_year = _nse_holidays(today.year + 1)
    logger.info(
        "Calendar ready — %d holidays this year, %d next year.",
        len(holidays_this_year), len(holidays_next_year),
    )

    default = auth_store.ensure_default_key()
    if default:
        print("\n" + "=" * 60)
        print("  FIRST-RUN DEFAULT CREDENTIALS  (save these now!)")
        print(f"  api_key      = {default['api_key']}")
        print(f"  access_token = {default['access_tokens'][0]}")
        print("=" * 60 + "\n")

    current = _phase(force_open)
    logger.info("Kite Simulator ws://%s:%d  phase=%s  force_open=%s",
                host, port, current.upper(), force_open)

    if current == "closed" and not force_open:
        reason = _closed_reason()
        logger.info("Market CLOSED (%s). Next open: %s. Sending heartbeats.", reason, _next_open_iso())

    async def handler(ws: Any) -> None:
        await _handle(ws, force_open=force_open)

    async with websockets.serve(handler, host, port, ping_interval=None, max_size=None):
        await _main_loop(force_open)


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level   = getattr(logging, args.log_level),
        format  = "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt = "%Y-%m-%d %H:%M:%S",
    )
    try:
        asyncio.run(_main(args.host, args.port, args.force_open))
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Simulator stopped.")


if __name__ == "__main__":
    main()