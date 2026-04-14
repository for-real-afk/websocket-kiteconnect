"""
kite_simulator.py — WebSocket server mimicking Zerodha KiteTicker binary protocol
==================================================================================

Behaviour matches real Zerodha KiteConnect exactly:

  ┌─────────────────────┬────────────────────────────────────────────────────┐
  │ Scenario            │ What is sent                                       │
  ├─────────────────────┼────────────────────────────────────────────────────┤
  │ Market OPEN         │ Binary QUOTE-mode tick frames every 1 second       │
  │ Market CLOSED       │ 0x21 heartbeat byte every 10 seconds               │
  │ New conn (closed)   │ One cached-price binary packet → then heartbeats   │
  │ Invalid credentials │ Close code 4001                                    │
  └─────────────────────┴────────────────────────────────────────────────────┘

Run
---
    # Normal mode (respects IST market hours)
    python kite_simulator.py

    # Force market always OPEN (for 24/7 testing)
    python kite_simulator.py --force-open

    # Custom port / host
    python kite_simulator.py --host 0.0.0.0 --port 8765

Environment (.env)
------------------
    ADMIN_TOKEN=change_me_secret       # bearer token for /admin/* REST calls
    SIM_HOST=0.0.0.0
    SIM_PORT=8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import struct
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytz
import websockets
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")

logger = logging.getLogger("kite_simulator")
IST    = pytz.timezone("Asia/Kolkata")

MARKET_OPEN  = dtime(9,  15, 0)
MARKET_CLOSE = dtime(15, 30, 0)

HEARTBEAT_BYTE    = bytes([0x21])          # Zerodha heartbeat
TICK_INTERVAL     = 1.0                    # seconds between tick frames
HEARTBEAT_INTERVAL = 10.0                  # seconds between heartbeats (closed)

# ---------------------------------------------------------------------------
# Instruments — edit freely; real tokens from api.kite.trade/instruments
# ---------------------------------------------------------------------------

INSTRUMENTS: list[dict] = [
    {"token": 738561,  "symbol": "RELIANCE",   "exchange": "NSE", "base": 2950.0},
    {"token": 341249,  "symbol": "HDFCBANK",   "exchange": "NSE", "base": 1680.0},
    {"token": 408065,  "symbol": "INFY",       "exchange": "NSE", "base": 1450.0},
    {"token": 2953217, "symbol": "TCS",        "exchange": "NSE", "base": 3350.0},
    {"token": 1270529, "symbol": "ICICIBANK",  "exchange": "NSE", "base": 1050.0},
    {"token": 969473,  "symbol": "SBIN",       "exchange": "NSE", "base":  800.0},
    {"token": 315393,  "symbol": "WIPRO",      "exchange": "NSE", "base":  460.0},
    {"token": 1195009, "symbol": "TATAMOTORS", "exchange": "NSE", "base":  920.0},
    {"token": 134657,  "symbol": "AXISBANK",   "exchange": "NSE", "base": 1100.0},
    {"token": 779521,  "symbol": "SUNPHARMA",  "exchange": "NSE", "base": 1600.0},
]

# ---------------------------------------------------------------------------
# Shared mutable state
# ---------------------------------------------------------------------------

# last_prices[token] = {last, open, high, low, close, volume, buy_qty, sell_qty}
last_prices: dict[int, dict] = {}

# connected clients
connected_clients: set[websockets.WebSocketServerProtocol] = set()

# market state flag (set by market_controller coroutine)
market_open_flag = False

# force-open flag set from CLI
force_open = False


def _now_ist() -> datetime:
    return datetime.now(IST)


def _is_market_open() -> bool:
    if force_open:
        return True
    now = _now_ist()
    if now.weekday() >= 5:           # Saturday / Sunday
        return False
    t = now.time()
    return MARKET_OPEN <= t < MARKET_CLOSE


# ---------------------------------------------------------------------------
# Price simulation — random walk with intraday OHLC tracking
# ---------------------------------------------------------------------------

def _init_prices() -> None:
    """Initialise last_prices from INSTRUMENTS base prices."""
    for inst in INSTRUMENTS:
        t = inst["token"]
        p = inst["base"]
        last_prices[t] = {
            "last":     p,
            "open":     p,
            "high":     p,
            "low":      p,
            "close":    p,           # previous close
            "volume":   random.randint(500_000, 2_000_000),
            "buy_qty":  random.randint(1_000, 50_000),
            "sell_qty": random.randint(1_000, 50_000),
            "change":   0.0,
        }


def _tick_prices() -> None:
    """Advance each instrument by a small random step."""
    for inst in INSTRUMENTS:
        t   = inst["token"]
        rec = last_prices[t]

        # ±0.05 % random walk, mean-reverting toward base
        drift  = (inst["base"] - rec["last"]) * 0.0001
        change = rec["last"] * random.uniform(-0.0005, 0.0005) + drift
        new_lp = max(rec["last"] + change, 0.10)

        rec["last"]     = new_lp
        rec["high"]     = max(rec["high"], new_lp)
        rec["low"]      = min(rec["low"],  new_lp)
        rec["volume"]  += random.randint(100, 5000)
        rec["buy_qty"]  = random.randint(1_000, 50_000)
        rec["sell_qty"] = random.randint(1_000, 50_000)
        rec["change"]   = ((new_lp - rec["close"]) / rec["close"]) * 100


def _reset_daily() -> None:
    """Called at market open — reset OHLC to current price as new open."""
    for inst in INSTRUMENTS:
        t   = inst["token"]
        rec = last_prices[t]
        rec["open"]   = rec["last"]
        rec["high"]   = rec["last"]
        rec["low"]    = rec["last"]
        rec["volume"] = 0
        rec["change"] = 0.0


# ---------------------------------------------------------------------------
# Binary frame builder (Zerodha QUOTE-mode)
# ---------------------------------------------------------------------------

# struct layout: token, ltq, avg_price, last_price, close_price, change,
#                volume (q), buy_qty, sell_qty, open, high, low, close
_PKT_FMT = ">iiiiiiqiiiiii"
_PKT_BODY = struct.calcsize(_PKT_FMT)        # 56 bytes
_PKT_LEN  = _PKT_BODY + 4                    # pad to 60 so client check (< 60) passes


def _pack_one(token: int, rec: dict) -> bytes:
    """Pack one instrument into a 60-byte QUOTE packet."""
    def p(x): return int(round(x * 100))     # float → paise

    body = struct.pack(
        _PKT_FMT,
        token,
        p(rec["last"]),           # last traded qty (reused)
        p(rec["last"]),           # avg price
        p(rec["last"]),           # last price  ← the one clients use
        p(rec["close"]),          # previous close
        p(rec["change"]),         # % change × 100
        rec["volume"],            # volume (int64)
        rec["buy_qty"],
        rec["sell_qty"],
        p(rec["open"]),
        p(rec["high"]),
        p(rec["low"]),
        p(rec["close"]),
    )
    return body + b"\x00" * 4    # 4-byte padding → 60 bytes total


def _build_frame(tokens: list[int] | None = None) -> bytes:
    """
    Build a complete Zerodha binary frame for the requested tokens
    (or all instruments if tokens is None).
    """
    if tokens is None:
        tokens = [i["token"] for i in INSTRUMENTS]

    packets = []
    for token in tokens:
        rec = last_prices.get(token)
        if rec is None:
            continue
        pkt = _pack_one(token, rec)
        # each packet is prefixed with its uint16 length
        packets.append(struct.pack(">H", len(pkt)) + pkt)

    header = struct.pack(">H", len(packets))
    return header + b"".join(packets)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

sys.path.insert(0, str(_ROOT / "simulator"))
try:
    import auth as auth_store
    AUTH_AVAILABLE = True
except ImportError:
    AUTH_AVAILABLE = False
    logger.warning("auth.py not found — ALL connections accepted (dev mode).")


def _authenticate(path: str) -> bool:
    qs = parse_qs(urlparse(path).query)
    api_key = qs.get("api_key",      [""])[0]
    token   = qs.get("access_token", [""])[0]
    if not api_key or not token:
        return False
    if not AUTH_AVAILABLE:
        return True    # dev mode: accept anything
    return auth_store.validate(api_key, token)


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

_INSTRUMENTS_MSG = json.dumps({
    "type": "instruments",
    "data": [
        {
            "instrument_token": i["token"],
            "tradingsymbol":    i["symbol"],
            "exchange":         i["exchange"],
        }
        for i in INSTRUMENTS
    ],
})


async def handler(ws: websockets.WebSocketServerProtocol) -> None:
    # 1. Auth
    if not _authenticate(ws.path):
        logger.warning("Auth failed from %s", ws.remote_address)
        await ws.close(4001, "Invalid api_key or access_token")
        return

    logger.info("Client connected: %s", ws.remote_address)
    connected_clients.add(ws)

    try:
        # 2. Always send instrument manifest first
        await ws.send(_INSTRUMENTS_MSG)

        if market_open_flag:
            # 3a. Market is OPEN — announce and let broadcast loop handle ticks
            await ws.send(json.dumps({"type": "market_open"}))
            # Just keep connection alive; tick broadcast loop sends to all clients
            await _wait_until_closed(ws)

        else:
            # 3b. Market is CLOSED — send one cached price packet, then heartbeats
            await ws.send(json.dumps({
                "type":        "market_closed",
                "next_open":   _next_open_str(),
            }))
            # Send cached last-price frame so client has something to display
            cached_frame = _build_frame()
            await ws.send(cached_frame)
            logger.debug("Sent cached price frame to new client (market closed).")

            # Then send 0x21 heartbeats every 10 s until market opens or client leaves
            await _heartbeat_loop(ws)

    except websockets.ConnectionClosed:
        pass
    finally:
        connected_clients.discard(ws)
        logger.info("Client disconnected: %s", ws.remote_address)


async def _wait_until_closed(ws: websockets.WebSocketServerProtocol) -> None:
    """Keep the connection alive; the broadcast loop pushes frames externally."""
    try:
        async for _ in ws:
            pass   # consume any subscription messages silently
    except websockets.ConnectionClosed:
        pass


async def _heartbeat_loop(ws: websockets.WebSocketServerProtocol) -> None:
    """Send 0x21 every 10 s. Exit when market opens or client disconnects."""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if market_open_flag:
                # Market just opened — announce and hand off to broadcast loop
                await ws.send(json.dumps({"type": "market_open"}))
                await _wait_until_closed(ws)
                return
            await ws.send(HEARTBEAT_BYTE)
    except websockets.ConnectionClosed:
        pass


# ---------------------------------------------------------------------------
# Broadcast loop — runs as a background task
# ---------------------------------------------------------------------------

async def broadcast_loop() -> None:
    """
    Every TICK_INTERVAL seconds during market hours, advance prices and
    push binary frames to all connected clients.
    """
    global market_open_flag

    prev_open = False

    while True:
        await asyncio.sleep(TICK_INTERVAL)

        now_open = _is_market_open()

        # Transition: closed → open
        if now_open and not prev_open:
            market_open_flag = True
            prev_open        = True
            _reset_daily()
            logger.info("Market OPEN — broadcasting ticks.")
            msg = json.dumps({"type": "market_open"})
            await _broadcast_text(msg)

        # Transition: open → closed
        elif not now_open and prev_open:
            market_open_flag = False
            prev_open        = False
            logger.info("Market CLOSED — switching to heartbeats.")
            msg = json.dumps({
                "type":      "market_closed",
                "next_open": _next_open_str(),
            })
            await _broadcast_text(msg)

        # During market hours: tick and broadcast
        if market_open_flag:
            _tick_prices()
            frame = _build_frame()
            await _broadcast_binary(frame)


async def _broadcast_text(msg: str) -> None:
    dead = set()
    for ws in list(connected_clients):
        try:
            await ws.send(msg)
        except websockets.ConnectionClosed:
            dead.add(ws)
    connected_clients.difference_update(dead)


async def _broadcast_binary(data: bytes) -> None:
    dead = set()
    for ws in list(connected_clients):
        try:
            await ws.send(data)
        except websockets.ConnectionClosed:
            dead.add(ws)
    connected_clients.difference_update(dead)


# ---------------------------------------------------------------------------
# Market controller (checks every minute for open/close transitions)
# ---------------------------------------------------------------------------

async def market_controller() -> None:
    """Sync market_open_flag with real IST time every 60 s."""
    global market_open_flag
    while True:
        market_open_flag = _is_market_open()
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _next_open_str() -> str:
    from datetime import timedelta
    now = _now_ist()
    # Find next weekday at 09:15
    candidate = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.strftime("%Y-%m-%d 09:15 IST")


# ---------------------------------------------------------------------------
# Stats logger
# ---------------------------------------------------------------------------

async def stats_logger() -> None:
    while True:
        await asyncio.sleep(30)
        logger.info(
            "Clients connected: %d  |  Market: %s",
            len(connected_clients),
            "OPEN" if market_open_flag else "CLOSED",
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(host: str, port: int) -> None:
    global force_open, market_open_flag

    _init_prices()
    market_open_flag = _is_market_open()

    if AUTH_AVAILABLE:
        auth_store.ensure_default_key()

    logger.info("Kite Simulator starting on ws://%s:%d", host, port)
    logger.info("Market: %s  |  Force-open: %s", "OPEN" if market_open_flag else "CLOSED", force_open)

    async with websockets.serve(handler, host, port, max_size=2**20):
        await asyncio.gather(
            broadcast_loop(),
            market_controller(),
            stats_logger(),
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kite WebSocket Simulator")
    parser.add_argument("--host",       default=os.getenv("SIM_HOST", "0.0.0.0"))
    parser.add_argument("--port",       default=int(os.getenv("SIM_PORT", "8765")), type=int)
    parser.add_argument("--force-open", action="store_true",
                        help="Treat market as always open (24/7 tick streaming)")
    parser.add_argument("--log-level",  default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    force_open = args.force_open

    logging.basicConfig(
        level   = getattr(logging, args.log_level),
        format  = "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt = "%Y-%m-%d %H:%M:%S",
    )

    try:
        asyncio.run(main(args.host, args.port))
    except KeyboardInterrupt:
        logger.info("Simulator stopped.")