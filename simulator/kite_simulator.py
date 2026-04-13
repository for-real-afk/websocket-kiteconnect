"""
kite_simulator.py — Zerodha KiteTicker WebSocket Simulator
===========================================================

Mimics Zerodha's KiteTicker WebSocket binary protocol exactly so that
existing KiteTicker clients (real or patched) can connect without a paid
Kite Connect subscription.

Authentication
--------------
Clients must supply credentials in the WebSocket URL — identical to how
real Kite Connect works:

    ws://HOST:8765?api_key=sim_abc123&access_token=sat_xyz789

The server validates both values against config/api_keys.json.
Connections with missing or invalid credentials are rejected immediately
with WebSocket close code 4001.

Binary frame format — QUOTE mode (184 bytes per instrument)
------------------------------------------------------------
  Bytes   Type     Field
  0-1     uint16   number of packets in this frame
  Then for each packet:
    0-1   uint16   packet length (always 184)
    ---- 184-byte packet body ----
    0-3   int32    instrument_token
    4-7   int32    last_traded_quantity
    8-11  int32    average_traded_price  (× 100, paise)
    12-15 int32    last_price            (× 100, paise)
    16-19 int32    close_price           (× 100, paise)
    20-23 int32    change                (× 100)
    24-31 int64    volume_traded
    32-35 int32    buy_quantity
    36-39 int32    sell_quantity
    40-43 int32    ohlc.open             (× 100, paise)
    44-47 int32    ohlc.high             (× 100, paise)
    48-51 int32    ohlc.low              (× 100, paise)
    52-55 int32    ohlc.close            (× 100, paise)
    56-183         (market depth — zeroed in QUOTE mode)

Heartbeat: b'\\x00' (1 byte) every ~3 seconds.

Subscription messages (JSON from client → server)
--------------------------------------------------
    {"a": "subscribe",   "v": [738561, 341249]}
    {"a": "unsubscribe", "v": [738561]}
    {"a": "mode",        "v": ["quote", [738561]]}

Run
---
    python kite_simulator.py
    python kite_simulator.py --port 8765 --interval 1.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import struct
import sys
import time
from datetime import datetime
from typing import Any, Dict, Set
from urllib.parse import parse_qs, urlparse

import pytz
import websockets

import auth as auth_store

logger = logging.getLogger(__name__)
IST    = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Instrument universe (add more rows to expand)
# Real tokens from: https://api.kite.trade/instruments
# ---------------------------------------------------------------------------

INSTRUMENTS: list[dict] = [
    {"token": 738561,  "symbol": "RELIANCE",   "exchange": "NSE", "price": 2950.0},
    {"token": 341249,  "symbol": "HDFCBANK",   "exchange": "NSE", "price": 1680.0},
    {"token": 408065,  "symbol": "INFY",       "exchange": "NSE", "price": 1450.0},
    {"token": 2953217, "symbol": "TCS",        "exchange": "NSE", "price": 3350.0},
    {"token": 1270529, "symbol": "ICICIBANK",  "exchange": "NSE", "price": 1050.0},
    {"token": 969473,  "symbol": "SBIN",       "exchange": "NSE", "price": 800.0},
    {"token": 315393,  "symbol": "WIPRO",      "exchange": "NSE", "price": 460.0},
    {"token": 1195009, "symbol": "TATAMOTORS", "exchange": "NSE", "price": 920.0},
    {"token": 134657,  "symbol": "AXISBANK",   "exchange": "NSE", "price": 1100.0},
    {"token": 779521,  "symbol": "SUNPHARMA",  "exchange": "NSE", "price": 1600.0},
]

# Mutable per-instrument price state, keyed by instrument_token
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


# ---------------------------------------------------------------------------
# Price simulation — random walk with mean-reversion
# ---------------------------------------------------------------------------

def _tick_price(s: dict) -> dict:
    drift     = (s["open"] - s["last_price"]) / s["open"] * 0.005
    shock     = random.gauss(drift, 0.002)
    new_price = round(max(s["last_price"] * (1 + shock), 0.05), 2)

    s["last_price"] = new_price
    s["high"]       = max(s["high"], new_price)
    s["low"]        = min(s["low"],  new_price)
    s["volume"]    += random.randint(100, 5_000)
    s["buy_qty"]    = random.randint(500, 80_000)
    s["sell_qty"]   = random.randint(500, 80_000)
    s["change"]     = round((new_price - s["close"]) / s["close"] * 100, 2)
    return s


# ---------------------------------------------------------------------------
# Binary frame builder
# ---------------------------------------------------------------------------

def _pack_one(s: dict) -> bytes:
    """Pack one instrument state into a 184-byte QUOTE packet."""
    p = lambda v: int(round(v * 100))          # price → paise integer

    body = struct.pack(
        ">iiiiiiqiiiiii",
        s["token"],
        1,                                      # last_traded_quantity (dummy)
        p(s["last_price"]),                     # average_traded_price
        p(s["last_price"]),                     # last_price
        p(s["close"]),                          # close_price (prev day)
        int(s["change"] * 100),                 # change × 100
        s["volume"],                            # volume_traded (int64)
        s["buy_qty"],
        s["sell_qty"],
        p(s["open"]), p(s["high"]),
        p(s["low"]),  p(s["close"]),            # ohlc
    )
    # body = 60 bytes; pad to 184 (market depth section zeroed)
    body += b"\x00" * (184 - len(body))
    return body   # 184 bytes


def _build_message(tokens: list[int]) -> bytes:
    """
    Build a single WebSocket binary message for N instruments.
    Layout:  [uint16 num_packets]  [uint16 pkt_len][pkt_body] × N
    """
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

# ws → set of subscribed tokens
_connections: Dict[Any, Set[int]] = {}

# ws → api_key (for logging)
_conn_keys: Dict[Any, str] = {}


# ---------------------------------------------------------------------------
# Auth helper — parse query-string from WS path
# ---------------------------------------------------------------------------

def _authenticate(ws: Any) -> tuple[bool, str]:
    """
    Extract api_key and access_token from the WebSocket request URL and
    validate them.  Returns (ok: bool, api_key: str).
    """
    try:
        # websockets v12+: ws.request.path
        # websockets v10/11: ws.path
        raw_path = getattr(ws.request, "path", None) or getattr(ws, "path", "/")
    except AttributeError:
        raw_path = "/"

    qs = parse_qs(urlparse(raw_path).query)

    api_key      = qs.get("api_key",      [""])[0]
    access_token = qs.get("access_token", [""])[0]

    if not api_key or not access_token:
        return False, ""

    return auth_store.validate(api_key, access_token), api_key


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def _handle(ws: Any) -> None:
    ok, api_key = _authenticate(ws)

    if not ok:
        logger.warning("Rejected unauthenticated connection from %s", ws.remote_address)
        await ws.close(code=4001, reason="Invalid or missing api_key / access_token")
        return

    # Subscribe to all instruments by default
    _connections[ws]  = set(_state.keys())
    _conn_keys[ws]    = api_key
    client            = ws.remote_address

    logger.info("Client connected: %s  api_key=%s  (total=%d)",
                client, api_key, len(_connections))

    # Send instrument manifest so client can build token→symbol map
    await ws.send(json.dumps({
        "type": "instruments",
        "data": [
            {
                "instrument_token": inst["token"],
                "tradingsymbol":    inst["symbol"],
                "exchange":         inst["exchange"],
            }
            for inst in INSTRUMENTS
        ],
    }))

    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue   # ignore binary from client
            try:
                msg    = json.loads(raw)
                action = msg.get("a", "")
                values = msg.get("v", [])

                if action == "subscribe":
                    tokens = [int(t) for t in values if int(t) in _state]
                    _connections[ws].update(tokens)

                elif action == "unsubscribe":
                    _connections[ws].difference_update([int(t) for t in values])

                elif action == "mode":
                    # ["quote", [token1, token2]]
                    extra = [int(t) for t in (values[1] if len(values) > 1 else [])
                             if int(t) in _state]
                    _connections[ws].update(extra)

            except (json.JSONDecodeError, ValueError):
                logger.debug("Bad message from %s: %r", client, raw[:80])

    except websockets.exceptions.ConnectionClosed as exc:
        logger.info("Disconnected: %s  code=%s", client, exc.code)
    finally:
        _connections.pop(ws, None)
        _conn_keys.pop(ws,   None)
        logger.info("Client removed: %s  (total=%d)", client, len(_connections))


# ---------------------------------------------------------------------------
# Broadcast loop
# ---------------------------------------------------------------------------

async def _broadcast_loop(interval: float) -> None:
    HB_INTERVAL = 3.0
    last_hb     = time.monotonic()

    while True:
        await asyncio.sleep(interval)

        now    = time.monotonic()
        send_hb = (now - last_hb) >= HB_INTERVAL
        dead   = []

        for ws, tokens in list(_connections.items()):
            if not tokens:
                continue
            try:
                await ws.send(_build_message(list(tokens)))
                if send_hb:
                    await ws.send(b"\x00")      # heartbeat
            except Exception:
                dead.append(ws)

        for ws in dead:
            _connections.pop(ws, None)
            _conn_keys.pop(ws, None)

        if send_hb:
            last_hb = now


# ---------------------------------------------------------------------------
# Daily OHLC reset at 09:15 IST
# ---------------------------------------------------------------------------

async def _daily_reset_loop() -> None:
    from datetime import timedelta
    while True:
        now  = datetime.now(IST)
        nxt  = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if now >= nxt:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        if datetime.now(IST).weekday() < 5:
            for s in _state.values():
                s.update(open=s["last_price"], high=s["last_price"],
                         low=s["last_price"],  volume=0)
            logger.info("Daily OHLC state reset at market open.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Zerodha KiteTicker WebSocket Simulator")
    p.add_argument("--host",      default="0.0.0.0")
    p.add_argument("--port",      default=8765,  type=int)
    p.add_argument("--interval",  default=1.0,   type=float, help="Tick interval (seconds)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


async def _main(host: str, port: int, interval: float) -> None:
    _init_state()

    # Create a default key if no keys exist yet
    default = auth_store.ensure_default_key()
    if default:
        print("\n" + "═" * 60)
        print("  FIRST-RUN DEFAULT CREDENTIALS (save these now!)")
        print(f"  api_key      = {default['api_key']}")
        print(f"  access_token = {default['access_tokens'][0]}")
        print("═" * 60 + "\n")

    logger.info(
        "Kite Simulator ready — ws://%s:%d  instruments=%d  tick=%.1fs",
        host, port, len(_state), interval,
    )

    async with websockets.serve(
        _handle, host, port,
        ping_interval=20,
        ping_timeout=30,
        max_size=None,
    ):
        await asyncio.gather(
            _broadcast_loop(interval),
            _daily_reset_loop(),
        )


def main() -> None:
    args = _args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Windows: use asyncio.run() + KeyboardInterrupt catch.
    # Linux/Mac: same approach works fine too — simpler than add_signal_handler.
    try:
        asyncio.run(_main(args.host, args.port, args.interval))
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Simulator stopped.")


if __name__ == "__main__":
    main()