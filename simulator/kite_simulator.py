"""
kite_simulator.py — Zerodha KiteTicker WebSocket Simulator
===========================================================

Behaves like the real Zerodha KiteTicker across the full trading day:

  Market OPEN  (Mon–Fri 09:15–15:30 IST)
    → Binary QUOTE-mode tick frames every ~1 second
    → Prices follow a random walk with mean-reversion
    → Volume, buy/sell qty update each tick

  Market CLOSED (all other times + weekends)
    → WebSocket connection stays alive (no disconnect)
    → Heartbeat b'\\x00' every 3 seconds keeps the TCP link up
    → JSON status frame every 30 seconds so clients know why ticks stopped:
        {"type": "market_closed", "next_open": "2026-04-14T09:15:00+05:30"}

  Market OPEN transition (exactly 09:15 IST weekdays)
    → Broadcasts {"type": "market_open"} to all connected clients
    → Resets OHLC state (open = last_price, high/low = open, volume = 0)
    → Binary ticks resume immediately

  Market CLOSE transition (exactly 15:30 IST weekdays)
    → Broadcasts {"type": "market_closed", "next_open": "..."}
    → Binary ticks stop; heartbeat-only mode resumes

Authentication
--------------
Clients connect with credentials in the URL query string:

    ws://HOST:8765?api_key=sim_abc&access_token=sat_xyz

Invalid / missing credentials → WebSocket close code 4001.

Binary frame format (QUOTE mode — 184 bytes / instrument)
----------------------------------------------------------
  Header  : uint16  num_packets
  Per pkt : uint16  pkt_length (always 184)
  Bytes 0-3  : int32  instrument_token
  Bytes 4-7  : int32  last_traded_quantity
  Bytes 8-11 : int32  average_traded_price  (x100 paise)
  Bytes 12-15: int32  last_price            (x100 paise)
  Bytes 16-19: int32  close_price           (x100 paise)
  Bytes 20-23: int32  change                (x100)
  Bytes 24-31: int64  volume_traded
  Bytes 32-35: int32  buy_quantity
  Bytes 36-39: int32  sell_quantity
  Bytes 40-43: int32  ohlc.open             (x100 paise)
  Bytes 44-47: int32  ohlc.high             (x100 paise)
  Bytes 48-51: int32  ohlc.low              (x100 paise)
  Bytes 52-55: int32  ohlc.close            (x100 paise)
  Bytes 56-183: zeroed (market depth placeholder)

Run
---
    python kite_simulator.py
    python kite_simulator.py --port 8765 --interval 1.0
    python kite_simulator.py --force-open          # always send ticks (ignore real time)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import struct
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Set
from urllib.parse import parse_qs, urlparse

import pytz
import websockets

import auth as auth_store

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------

MARKET_OPEN_H,  MARKET_OPEN_M  = 9,  15
MARKET_CLOSE_H, MARKET_CLOSE_M = 15, 30


def _market_is_open(force_open: bool = False) -> bool:
    if force_open:
        return True
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_time  = now.replace(hour=MARKET_OPEN_H,  minute=MARKET_OPEN_M,  second=0, microsecond=0)
    close_time = now.replace(hour=MARKET_CLOSE_H, minute=MARKET_CLOSE_M, second=0, microsecond=0)
    return open_time <= now < close_time


def _next_open_dt() -> datetime:
    now  = datetime.now(IST)
    base = now.replace(hour=MARKET_OPEN_H, minute=MARKET_OPEN_M, second=0, microsecond=0)
    if now < base and now.weekday() < 5:
        return base
    candidate = base + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _next_open_iso() -> str:
    return _next_open_dt().isoformat()


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
            "last_price": p,
            "open":       p,
            "high":       p,
            "low":        p,
            "close":      round(p * random.uniform(0.98, 1.02), 2),
            "volume":     0,
            "buy_qty":    random.randint(1_000, 50_000),
            "sell_qty":   random.randint(1_000, 50_000),
        }


def _reset_daily_ohlc() -> None:
    for s in _state.values():
        p = s["last_price"]
        s.update(open=p, high=p, low=p, volume=0)
    logger.info("Daily OHLC state reset for market open.")


# ---------------------------------------------------------------------------
# Price simulation
# ---------------------------------------------------------------------------

def _tick_price(s: dict) -> dict:
    drift     = (s["open"] - s["last_price"]) / max(s["open"], 0.01) * 0.005
    shock     = random.gauss(drift, 0.002)
    new_price = round(max(s["last_price"] * (1 + shock), 0.05), 2)
    s["last_price"] = new_price
    s["high"]       = max(s["high"], new_price)
    s["low"]        = min(s["low"],  new_price)
    s["volume"]    += random.randint(100, 5_000)
    s["buy_qty"]    = random.randint(500, 80_000)
    s["sell_qty"]   = random.randint(500, 80_000)
    s["change"]     = round((new_price - s["close"]) / max(s["close"], 0.01) * 100, 2)
    return s


# ---------------------------------------------------------------------------
# Binary frame builder
# ---------------------------------------------------------------------------

def _pack_one(s: dict) -> bytes:
    p    = lambda v: int(round(v * 100))
    body = struct.pack(
        ">iiiiiiqiiiiii",
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
    body += b"\x00" * (184 - len(body))
    return body


def _build_tick_message(tokens: list[int]) -> bytes:
    packets = [_pack_one(_tick_price(_state[t])) for t in tokens if t in _state]
    if not packets:
        return b""
    msg = struct.pack(">H", len(packets))
    for pkt in packets:
        msg += struct.pack(">H", len(pkt)) + pkt
    return msg


# ---------------------------------------------------------------------------
# Connection registry
# ---------------------------------------------------------------------------

_connections: Dict[Any, Set[int]] = {}
_conn_keys:   Dict[Any, str]      = {}


# ---------------------------------------------------------------------------
# Broadcast helpers
# ---------------------------------------------------------------------------

async def _broadcast_json(payload: dict) -> None:
    msg  = json.dumps(payload)
    dead = []
    for ws in list(_connections):
        try:
            await ws.send(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _connections.pop(ws, None)
        _conn_keys.pop(ws, None)


async def _broadcast_ticks() -> None:
    dead = []
    for ws, tokens in list(_connections.items()):
        if not tokens:
            continue
        try:
            msg = _build_tick_message(list(tokens))
            if msg:
                await ws.send(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _connections.pop(ws, None)
        _conn_keys.pop(ws, None)


async def _broadcast_heartbeat() -> None:
    dead = []
    for ws in list(_connections):
        try:
            await ws.send(b"\x00")
        except Exception:
            dead.append(ws)
    for ws in dead:
        _connections.pop(ws, None)
        _conn_keys.pop(ws, None)


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
# WebSocket handler
# ---------------------------------------------------------------------------

async def _handle(ws: Any, force_open: bool = False) -> None:
    ok, api_key = _authenticate(ws)
    if not ok:
        logger.warning("Rejected connection from %s — bad credentials", ws.remote_address)
        await ws.close(code=4001, reason="Invalid or missing api_key / access_token")
        return

    _connections[ws] = set(_state.keys())
    _conn_keys[ws]   = api_key
    logger.info("Client connected: %s  (total=%d)", ws.remote_address, len(_connections))

    # Instrument manifest
    await ws.send(json.dumps({
        "type": "instruments",
        "data": [
            {"instrument_token": inst["token"],
             "tradingsymbol":    inst["symbol"],
             "exchange":         inst["exchange"]}
            for inst in INSTRUMENTS
        ],
    }))

    # Immediate market status so client knows what to expect
    if _market_is_open(force_open):
        await ws.send(json.dumps({"type": "market_open"}))
    else:
        await ws.send(json.dumps({
            "type":      "market_closed",
            "next_open": _next_open_iso(),
            "message":   "Market is closed. Ticks will resume at next open.",
        }))

    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            try:
                msg    = json.loads(raw)
                action = msg.get("a", "")
                values = msg.get("v", [])
                if action == "subscribe":
                    _connections[ws].update(int(t) for t in values if int(t) in _state)
                elif action == "unsubscribe":
                    _connections[ws].difference_update(int(t) for t in values)
                elif action == "mode":
                    extra = [int(t) for t in (values[1] if len(values) > 1 else [])
                             if int(t) in _state]
                    _connections[ws].update(extra)
            except (json.JSONDecodeError, ValueError):
                pass

    except websockets.exceptions.ConnectionClosed as exc:
        logger.info("Disconnected: %s  code=%s", ws.remote_address, exc.code)
    finally:
        _connections.pop(ws, None)
        _conn_keys.pop(ws, None)
        logger.info("Client removed (total=%d)", len(_connections))


# ---------------------------------------------------------------------------
# Main broadcast loop
# ---------------------------------------------------------------------------

async def _main_loop(interval: float, force_open: bool) -> None:
    HB_INTERVAL     = 3.0
    STATUS_INTERVAL = 30.0

    last_hb     = time.monotonic()
    last_status = time.monotonic()
    prev_open   = _market_is_open(force_open)

    logger.info("Market is %s.", "OPEN" if prev_open else "CLOSED")

    while True:
        await asyncio.sleep(interval)
        now     = time.monotonic()
        is_open = _market_is_open(force_open)

        # Transition: closed → open
        if is_open and not prev_open:
            _reset_daily_ohlc()
            await _broadcast_json({"type": "market_open"})
            logger.info("Market OPENED — ticks resuming (%d client(s)).", len(_connections))

        # Transition: open → closed
        elif not is_open and prev_open:
            await _broadcast_json({
                "type":      "market_closed",
                "next_open": _next_open_iso(),
                "message":   "Market has closed for the day.",
            })
            logger.info("Market CLOSED — heartbeat mode.")

        prev_open = is_open

        if is_open:
            await _broadcast_ticks()
        else:
            # Periodic status frames
            if (now - last_status) >= STATUS_INTERVAL:
                await _broadcast_json({
                    "type":      "market_closed",
                    "next_open": _next_open_iso(),
                    "clients":   len(_connections),
                })
                last_status = now

        # Heartbeat in both modes
        if (now - last_hb) >= HB_INTERVAL:
            await _broadcast_heartbeat()
            last_hb = now


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Zerodha KiteTicker WebSocket Simulator")
    p.add_argument("--host",       default="0.0.0.0")
    p.add_argument("--port",       default=8765,  type=int)
    p.add_argument("--interval",   default=1.0,   type=float)
    p.add_argument("--force-open", action="store_true",
                   help="Always send ticks regardless of IST time (good for local testing)")
    p.add_argument("--log-level",  default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


async def _main(host: str, port: int, interval: float, force_open: bool) -> None:
    _init_state()

    default = auth_store.ensure_default_key()
    if default:
        print("\n" + "=" * 60)
        print("  FIRST-RUN DEFAULT CREDENTIALS (save these now!)")
        print(f"  api_key      = {default['api_key']}")
        print(f"  access_token = {default['access_tokens'][0]}")
        print("=" * 60 + "\n")

    status = "OPEN (forced)" if force_open else ("OPEN" if _market_is_open() else "CLOSED")
    logger.info("Kite Simulator ready — ws://%s:%d  instruments=%d  market=%s",
                host, port, len(_state), status)

    if not force_open and not _market_is_open():
        logger.info("Next market open: %s  (use --force-open to test anytime)", _next_open_iso())

    async def handler(ws: Any) -> None:
        await _handle(ws, force_open=force_open)

    async with websockets.serve(handler, host, port,
                                ping_interval=20, ping_timeout=30, max_size=None):
        await _main_loop(interval, force_open)


def main() -> None:
    args = _args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        asyncio.run(_main(args.host, args.port, args.interval, args.force_open))
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Simulator stopped.")


if __name__ == "__main__":
    main()