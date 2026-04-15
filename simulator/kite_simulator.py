"""
kite_simulator.py — Zerodha KiteTicker WebSocket Simulator (3-Phase)
=====================================================================

Three phases, exactly like real Zerodha:

  Phase 1  OPEN        09:15–15:30 IST Mon–Fri (non-holiday)
           Binary QUOTE-mode tick frames every 1 second.

  Phase 2  POST-MARKET 15:30–16:00 IST Mon–Fri (non-holiday)
           Sparse binary packets every 15-30 seconds.
           Prices converge toward the official closing price.

  Phase 3  CLOSED      16:00 onward, ALL weekends, ALL NSE holidays
           0x21 heartbeat byte every 10 seconds. No price data.

  NEW CONNECTION while closed / post-market:
           One cached binary snapshot sent immediately (last known prices),
           then normal phase behaviour.

Run
---
    python kite_simulator.py                  # respects real IST market hours
    python kite_simulator.py --force-open     # always Phase 1 (for 24/7 testing)
    python kite_simulator.py --log-level DEBUG
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import struct
import time
from datetime import date, datetime, timedelta, timezone, time as dt
from typing import Any, Dict, Set
from urllib.parse import parse_qs, urlparse

import pytz
import websockets

import auth as auth_store

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# NSE Holiday Calendar  (add future years as needed)
# Source: NSE India official holiday list
# ---------------------------------------------------------------------------

NSE_HOLIDAYS: set[date] = {
    # 2025
    date(2025, 1, 26),   # Republic Day
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr
    date(2025, 4, 10),   # Shri Ram Navami
    date(2025, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Gandhi Jayanti / Dussehra
    date(2025, 10, 20),  # Diwali Laxmi Pujan
    date(2025, 10, 21),  # Diwali Balipratipada
    date(2025, 11, 5),   # Prakash Gurpurb Sri Guru Nanak Dev Ji
    date(2025, 12, 25),  # Christmas

    # 2026
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 25),   # Holi
    date(2026, 4, 2),    # Shri Ram Navami
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 10),   # Mahavir Jayanti
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 10, 21),  # Diwali Laxmi Pujan
    date(2026, 10, 22),  # Diwali Balipratipada
    date(2026, 11, 5),   # Guru Nanak Jayanti
    date(2026, 12, 25),  # Christmas
}

# ---------------------------------------------------------------------------
# Phase timing constants
# ---------------------------------------------------------------------------

MARKET_OPEN_TIME     = dt(9,  15, 0)
MARKET_CLOSE_TIME    = dt(15, 30, 0)
POST_MARKET_END_TIME = dt(16,  0, 0)

LIVE_TICK_INTERVAL   = 1.0    # seconds between live ticks (Phase 1)
POST_MARKET_MIN      = 15.0   # min seconds between post-market ticks (Phase 2)
POST_MARKET_MAX      = 30.0   # max seconds between post-market ticks (Phase 2)
HEARTBEAT_INTERVAL   = 10.0   # seconds between 0x21 heartbeats (Phase 3)

HEARTBEAT_BYTE = struct.pack("B", 33)   # 0x21


# ---------------------------------------------------------------------------
# Phase detection  — the single source of truth
# ---------------------------------------------------------------------------

def _now_ist() -> datetime:
    return datetime.now(IST)


def _is_trading_day(d: date | None = None) -> bool:
    """True if NSE is open on this date (not weekend, not holiday)."""
    if d is None:
        d = _now_ist().date()
    if d.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    if d in NSE_HOLIDAYS:
        return False
    return True


def _phase(force_open: bool = False) -> str:
    """
    Returns exactly one of:  'open' | 'post_market' | 'closed'

    This is called every loop iteration — it is THE gate that decides
    what data gets broadcast.  force_open bypasses the calendar check.
    """
    if force_open:
        return "open"

    now = _now_ist()

    # Weekend or holiday → always closed, no ticks
    if not _is_trading_day(now.date()):
        return "closed"

    t = now.time()

    if MARKET_OPEN_TIME <= t < MARKET_CLOSE_TIME:
        return "open"

    if MARKET_CLOSE_TIME <= t < POST_MARKET_END_TIME:
        return "post_market"

    return "closed"


def _next_open_iso() -> str:
    """ISO timestamp of the next market open (skips weekends + holidays)."""
    now       = _now_ist()
    candidate = now.replace(hour=9, minute=15, second=0, microsecond=0)
    # If today's open is still in the future and today is a trading day, use it
    if candidate > now and _is_trading_day(candidate.date()):
        return candidate.isoformat()
    # Otherwise advance day by day until we find a trading day
    candidate += timedelta(days=1)
    while not _is_trading_day(candidate.date()):
        candidate += timedelta(days=1)
    candidate = candidate.replace(hour=9, minute=15, second=0, microsecond=0)
    return candidate.isoformat()


def _is_holiday_or_weekend(d: date | None = None) -> bool:
    return not _is_trading_day(d)


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
    logger.info("Daily OHLC reset for market open.")


def _set_official_close() -> None:
    """Lock VWAP-simulated closing prices at 15:30."""
    for s in _state.values():
        s["official_close"] = round(s["last_price"] * random.uniform(0.9995, 1.0005), 2)
    logger.info("Official closing prices locked in (15:30 IST).")


# ---------------------------------------------------------------------------
# Price simulation
# ---------------------------------------------------------------------------

def _tick_live(s: dict) -> None:
    drift = (s["price"] - s["last_price"]) / max(s["price"], 0.01) * 0.001
    shock = random.gauss(drift, 0.0015)
    p     = round(max(s["last_price"] * (1 + shock), 0.05), 2)
    s["last_price"] = p
    s["high"]       = max(s["high"], p)
    s["low"]        = min(s["low"],  p)
    s["volume"]    += random.randint(100, 5_000)
    s["buy_qty"]    = random.randint(500, 80_000)
    s["sell_qty"]   = random.randint(500, 80_000)
    s["change"]     = round((p - s["close"]) / max(s["close"], 0.01) * 100, 4)


def _tick_post_market(s: dict) -> None:
    """Gentle convergence toward official close, very sparse volume."""
    target = s["official_close"] or s["last_price"]
    pull   = (target - s["last_price"]) * random.uniform(0.05, 0.15)
    noise  = s["last_price"] * random.gauss(0, 0.0002)
    p      = round(max(s["last_price"] + pull + noise, 0.05), 2)
    s["last_price"] = p
    s["high"]       = max(s["high"], p)
    s["low"]        = min(s["low"],  p)
    s["volume"]    += random.randint(10, 200)      # tiny end-of-day lots
    s["buy_qty"]    = random.randint(100, 5_000)
    s["sell_qty"]   = random.randint(100, 5_000)
    s["change"]     = round((p - s["close"]) / max(s["close"], 0.01) * 100, 4)


# ---------------------------------------------------------------------------
# Binary frame builder  (Zerodha QUOTE-mode, 184-byte packets)
# ---------------------------------------------------------------------------

_PKT_FMT = ">iiiiiiqiiiiii"
_PKT_LEN  = 184   # padded to 184 bytes to match real Zerodha wire format


def _pack_one(s: dict, tick_fn=None) -> bytes:
    if tick_fn:
        tick_fn(s)

    def p(v): return int(round(v * 100))

    body = struct.pack(
        _PKT_FMT,
        s["token"],
        1,
        p(s["last_price"]),
        p(s["last_price"]),
        p(s["close"]),
        int(s["change"] * 100),
        s["volume"],
        s["buy_qty"],
        s["sell_qty"],
        p(s["open"]), p(s["high"]),
        p(s["low"]),  p(s["close"]),
    )
    body += b"\x00" * (_PKT_LEN - len(body))
    return body


def _build_frame(tokens: list[int], tick_fn=None) -> bytes:
    packets = []
    for token in tokens:
        s = _state.get(token)
        if s:
            pkt = _pack_one(s, tick_fn=tick_fn)
            packets.append(struct.pack(">H", len(pkt)) + pkt)
    if not packets:
        return b""
    return struct.pack(">H", len(packets)) + b"".join(packets)


# ---------------------------------------------------------------------------
# Connection registry
# ---------------------------------------------------------------------------

_connections: Dict[Any, Set[int]] = {}   # ws → subscribed token set


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
# Instrument manifest JSON (built once at startup)
# ---------------------------------------------------------------------------

_MANIFEST = json.dumps({
    "type": "instruments",
    "data": [{"instrument_token": i["token"],
              "tradingsymbol":    i["symbol"],
              "exchange":         i["exchange"]}
             for i in INSTRUMENTS],
})


# ---------------------------------------------------------------------------
# WebSocket connection handler
# ---------------------------------------------------------------------------

async def _handle(ws: Any, force_open: bool = False) -> None:
    ok, api_key = _authenticate(ws)
    if not ok:
        logger.warning("Rejected %s — bad credentials", ws.remote_address)
        await ws.close(code=4001, reason="Invalid api_key or access_token")
        return

    all_tokens = list(_state.keys())
    _connections[ws] = set(all_tokens)
    logger.info("Client connected: %s  (total=%d)", ws.remote_address, len(_connections))

    # 1. Always send instrument manifest
    await ws.send(_MANIFEST)

    current = _phase(force_open)
    now_ist = _now_ist()

    # 2. Phase-aware greeting + initial data
    if current == "open":
        await ws.send(json.dumps({"type": "market_open"}))

    elif current == "post_market":
        await ws.send(json.dumps({
            "type":      "market_closed",
            "phase":     "post_market",
            "message":   "Post-market session (15:30–16:00 IST). Sparse price updates continuing.",
            "next_open": _next_open_iso(),
        }))
        # Send snapshot so client isn't blank on connect
        snapshot = _build_frame(all_tokens)
        if snapshot:
            await ws.send(snapshot)

    else:
        # CLOSED — weekend, holiday, or after 16:00
        reason = "weekend" if now_ist.weekday() >= 5 else \
                 "holiday" if _is_holiday_or_weekend(now_ist.date()) else \
                 "after hours"
        await ws.send(json.dumps({
            "type":      "market_closed",
            "phase":     "heartbeat",
            "reason":    reason,
            "message":   f"Market closed ({reason}). Heartbeat-only until next open.",
            "next_open": _next_open_iso(),
        }))
        # ONE cached snapshot with last known prices (day's O/H/L/C/V)
        snapshot = _build_frame(all_tokens)
        if snapshot:
            logger.debug("Sent cached snapshot to new client (closed — %s).", reason)
            await ws.send(snapshot)
        # Immediate heartbeat — don't make client wait 10s
        await ws.send(HEARTBEAT_BYTE)

    # 3. Stay alive — handle subscription messages from client
    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            try:
                msg = json.loads(raw)
                a   = msg.get("a", "")
                v   = msg.get("v", [])
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
    prev_phase       = _phase(force_open)
    last_hb          = time.monotonic()
    last_live_tick   = time.monotonic()
    last_post_tick   = time.monotonic()
    next_post_gap    = random.uniform(POST_MARKET_MIN, POST_MARKET_MAX)
    last_stats       = time.monotonic()

    logger.info(
        "Broadcast loop started — phase: %s%s",
        prev_phase.upper(),
        "  [force-open mode]" if force_open else "",
    )

    while True:
        await asyncio.sleep(0.25)   # fine poll — transitions detected within 250ms
        now     = time.monotonic()
        current = _phase(force_open)

        # ── Phase transition ───────────────────────────────────────────────

        if current != prev_phase:
            logger.info(
                "PHASE TRANSITION  %s → %s  (%s IST)",
                prev_phase.upper(), current.upper(),
                _now_ist().strftime("%H:%M:%S"),
            )

            if current == "open":
                _reset_daily_ohlc()
                await _broadcast(json.dumps({"type": "market_open"}))

            elif current == "post_market":
                _set_official_close()
                await _broadcast(json.dumps({
                    "type":      "market_closed",
                    "phase":     "post_market",
                    "message":   "Post-market session started (15:30 IST). Sparse ticks for 30 min.",
                    "next_open": _next_open_iso(),
                }))
                last_post_tick = now
                next_post_gap  = random.uniform(POST_MARKET_MIN, POST_MARKET_MAX)

            elif current == "closed":
                now_ist = _now_ist()
                reason  = "weekend" if now_ist.weekday() >= 5 else \
                          "holiday" if _is_holiday_or_weekend(now_ist.date()) else \
                          "after hours"
                await _broadcast(json.dumps({
                    "type":      "market_closed",
                    "phase":     "heartbeat",
                    "reason":    reason,
                    "message":   f"Market closed ({reason}). Heartbeat-only until next open.",
                    "next_open": _next_open_iso(),
                }))

            prev_phase = current

        # ── Phase 1: Live binary ticks every 1 second ─────────────────────

        if current == "open":
            if (now - last_live_tick) >= LIVE_TICK_INTERVAL:
                await _broadcast_ticks(tick_fn=_tick_live)
                last_live_tick = now

        # ── Phase 2: Post-market sparse binary ticks every 15–30 seconds ──

        elif current == "post_market":
            if (now - last_post_tick) >= next_post_gap:
                await _broadcast_ticks(tick_fn=_tick_post_market)
                last_post_tick = now
                next_post_gap  = random.uniform(POST_MARKET_MIN, POST_MARKET_MAX)
                logger.debug("Post-market tick sent. Next in %.1fs.", next_post_gap)

        # ── Phase 3 (and Phase 2): Heartbeat 0x21 every 10 seconds ────────
        # NOTE: Phase 1 does NOT send heartbeats — tick frames keep connection alive

        if current in ("post_market", "closed"):
            if (now - last_hb) >= HEARTBEAT_INTERVAL:
                await _broadcast(HEARTBEAT_BYTE)
                last_hb = now

        # ── Periodic status log every 60 seconds ──────────────────────────

        if (now - last_stats) >= 60.0:
            logger.info(
                "Status — phase: %-12s  clients: %d  next_open: %s",
                current.upper(), len(_connections), _next_open_iso(),
            )
            last_stats = now


# ---------------------------------------------------------------------------
# CLI + entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Zerodha KiteTicker Simulator (3-phase: open / post-market / closed)"
    )
    p.add_argument("--host",       default="0.0.0.0")
    p.add_argument("--port",       default=8765, type=int)
    p.add_argument("--force-open", action="store_true",
                   help="Always stream live ticks regardless of IST time / holidays")
    p.add_argument("--log-level",  default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


async def _main(host: str, port: int, force_open: bool) -> None:
    _init_state()

    default = auth_store.ensure_default_key()
    if default:
        print("\n" + "=" * 60)
        print("  FIRST-RUN DEFAULT CREDENTIALS  (save these now!)")
        print(f"  api_key      = {default['api_key']}")
        print(f"  access_token = {default['access_tokens'][0]}")
        print("=" * 60 + "\n")

    current = _phase(force_open)
    now_ist = _now_ist()

    logger.info(
        "Kite Simulator (3-phase) ws://%s:%d  phase=%s  force_open=%s",
        host, port, current.upper(), force_open,
    )

    if current == "closed" and not force_open:
        reason = "weekend" if now_ist.weekday() >= 5 else \
                 "holiday" if _is_holiday_or_weekend(now_ist.date()) else \
                 "after hours"
        logger.info("Market is CLOSED (%s). Next open: %s", reason, _next_open_iso())
        logger.info("Sending heartbeat 0x21 every %.0fs. Use --force-open to stream ticks.", HEARTBEAT_INTERVAL)

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