"""
ticker_sim.py — Drop-in replacement for ticker.py
===================================================

Connects to the Kite Simulator WebSocket instead of Zerodha production.
Everything downstream (database.py, api.py, APScheduler) is unchanged.

Configuration (.env)
--------------------
    KITE_SIM_URL=ws://YOUR_EC2_IP:8765
    KITE_SIM_API_KEY=sim_abc123
    KITE_SIM_ACCESS_TOKEN=sat_xyz789

Usage (api.py)
--------------
    # Replace:
    from ticker import ticker_manager
    # With:
    from ticker_sim import ticker_manager
"""

from __future__ import annotations

import json
import logging
import os
import struct
import threading
import time
from datetime import datetime

import pytz
import websocket
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from database import get_watched_symbols, save_ticks

logger = logging.getLogger(__name__)
IST    = pytz.timezone("Asia/Kolkata")

MARKET_OPEN_HOUR,  MARKET_OPEN_MIN  = 9,  15
MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 15, 30

_ENV = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=_ENV)

SIM_URL          = os.getenv("KITE_SIM_URL",          "ws://localhost:8765")
SIM_API_KEY      = os.getenv("KITE_SIM_API_KEY",      "")
SIM_ACCESS_TOKEN = os.getenv("KITE_SIM_ACCESS_TOKEN", "")

# Build the full authenticated URL
_AUTH_URL = (
    f"{SIM_URL}"
    f"?api_key={SIM_API_KEY}"
    f"&access_token={SIM_ACCESS_TOKEN}"
)

HEADER_LEN  = 2
PKT_HDR_LEN = 2


# ---------------------------------------------------------------------------
# Binary parser (mirrors kite_simulator._pack_one in reverse)
# ---------------------------------------------------------------------------

def _parse(data: bytes) -> list[dict]:
    if len(data) <= 1:
        return []   # heartbeat

    num = struct.unpack_from(">H", data, 0)[0]
    offset = HEADER_LEN
    ticks  = []

    for _ in range(num):
        if offset + PKT_HDR_LEN > len(data):
            break
        pkt_len = struct.unpack_from(">H", data, offset)[0]
        offset += PKT_HDR_LEN
        pkt = data[offset: offset + pkt_len]
        offset += pkt_len

        if len(pkt) < 60:
            continue

        try:
            (token, _, _, last_price, close_price, change,
             volume, buy_qty, sell_qty, o, h, l, c) = struct.unpack_from(
                ">iiiiiiqiiiiii", pkt, 0
            )
            ticks.append({
                "instrument_token": token,
                "last_price":       last_price / 100.0,
                "volume_traded":    volume,
                "buy_quantity":     buy_qty,
                "sell_quantity":    sell_qty,
                "change":           change / 100.0,
                "ohlc": {
                    "open":  o / 100.0,
                    "high":  h / 100.0,
                    "low":   l / 100.0,
                    "close": c / 100.0,
                },
            })
        except struct.error as exc:
            logger.warning("Parse error: %s", exc)

    return ticks


# ---------------------------------------------------------------------------
# SimTickerManager
# ---------------------------------------------------------------------------

class SimTickerManager:
    def __init__(self) -> None:
        self._running    = threading.Event()
        self._thread:    threading.Thread | None = None
        self._ws:        websocket.WebSocketApp | None = None
        self._lock       = threading.Lock()
        self._scheduler  = BackgroundScheduler(timezone=IST)
        self._token_map: dict[int, dict] = {}   # token → {symbol, exchange}
        self._map_lock   = threading.Lock()

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    def start_scheduler(self) -> None:
        self._scheduler.add_job(
            self._on_market_open,
            CronTrigger(day_of_week="mon-fri",
                        hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN,
                        timezone=IST),
            id="market_open", replace_existing=True,
        )
        self._scheduler.add_job(
            self._on_market_close,
            CronTrigger(day_of_week="mon-fri",
                        hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN,
                        timezone=IST),
            id="market_close", replace_existing=True,
        )
        self._scheduler.start()
        logger.info("Scheduler started (simulator mode).")

    def stop_scheduler(self) -> None:
        self.stop_polling()
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Polling control
    # ------------------------------------------------------------------

    def start_polling(self) -> str:
        if self._running.is_set():
            return "already_running"

        if not SIM_API_KEY or not SIM_ACCESS_TOKEN:
            raise RuntimeError(
                "KITE_SIM_API_KEY and KITE_SIM_ACCESS_TOKEN must be set in .env.\n"
                "Create a key via: POST http://EC2_IP:8766/keys"
            )

        self._running.set()
        self._thread = threading.Thread(
            target=self._connect_loop, name="sim-ticker", daemon=True
        )
        self._thread.start()
        logger.info("Simulator ticker started → %s", SIM_URL)
        return "started"

    def stop_polling(self) -> str:
        if not self._running.is_set():
            return "not_running"
        self._running.clear()
        with self._lock:
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("Simulator ticker stopped.")
        return "stopped"

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    @property
    def scheduler_running(self) -> bool:
        return self._scheduler.running

    # ------------------------------------------------------------------
    # APScheduler callbacks
    # ------------------------------------------------------------------

    def _on_market_open(self) -> None:
        logger.info("Market open — starting simulator ticker.")
        try:
            self.start_polling()
        except Exception as exc:
            logger.error("Start failed: %s", exc)

    def _on_market_close(self) -> None:
        logger.info("Market close — stopping simulator ticker.")
        self.stop_polling()

    # ------------------------------------------------------------------
    # WebSocket lifecycle
    # ------------------------------------------------------------------

    def _connect_loop(self) -> None:
        while self._running.is_set():
            ws = websocket.WebSocketApp(
                _AUTH_URL,
                on_open    = self._on_open,
                on_message = self._on_message,
                on_error   = self._on_error,
                on_close   = self._on_close,
            )
            with self._lock:
                self._ws = ws
            try:
                ws.run_forever(ping_interval=20, ping_timeout=30)
            except Exception as exc:
                logger.error("WS error: %s", exc)
            if self._running.is_set():
                logger.info("Reconnecting in 5s…")
                time.sleep(5)

    def _on_open(self, ws) -> None:
        logger.info("Connected to Kite Simulator.")
        # Subscribe to tokens we already know about
        tokens = list(self._token_map.keys())
        if tokens:
            ws.send(json.dumps({"a": "subscribe", "v": tokens}))
            ws.send(json.dumps({"a": "mode", "v": ["quote", tokens]}))

    def _on_message(self, ws, data) -> None:
        # Instrument manifest (JSON text frame)
        if isinstance(data, str):
            try:
                msg = json.loads(data)
                if msg.get("type") == "instruments":
                    with self._map_lock:
                        for inst in msg["data"]:
                            self._token_map[inst["instrument_token"]] = {
                                "symbol":   inst["tradingsymbol"],
                                "exchange": inst["exchange"],
                            }
                    logger.info("Token map loaded: %d instruments.", len(self._token_map))
                    # Now subscribe
                    tokens = list(self._token_map.keys())
                    ws.send(json.dumps({"a": "subscribe", "v": tokens}))
                    ws.send(json.dumps({"a": "mode",      "v": ["quote", tokens]}))
            except Exception as exc:
                logger.warning("JSON error: %s", exc)
            return

        # Binary tick frame
        raw_ticks   = _parse(data)
        if not raw_ticks:
            return

        captured_at = datetime.now(IST).replace(tzinfo=None)
        rows        = []

        with self._map_lock:
            for rt in raw_ticks:
                meta = self._token_map.get(rt["instrument_token"])
                if not meta:
                    continue
                ohlc = rt.get("ohlc", {})
                rows.append({
                    "symbol":           meta["symbol"],
                    "exchange":         meta["exchange"],
                    "instrument_token": rt["instrument_token"],
                    "last_price":       rt["last_price"],
                    "volume":           rt.get("volume_traded", 0),
                    "buy_quantity":     rt.get("buy_quantity",  0),
                    "sell_quantity":    rt.get("sell_quantity", 0),
                    "open":             ohlc.get("open"),
                    "high":             ohlc.get("high"),
                    "low":              ohlc.get("low"),
                    "close":            ohlc.get("close"),
                    "change":           rt.get("change"),
                    "captured_at":      captured_at,
                })

        if rows:
            saved = save_ticks(rows)
            logger.debug("Saved %d tick(s).", saved)

    def _on_error(self, ws, err) -> None:
        logger.error("WS error: %s", err)

    def _on_close(self, ws, code, msg) -> None:
        if code == 4001:
            logger.error(
                "Auth rejected (4001). Check KITE_SIM_API_KEY and "
                "KITE_SIM_ACCESS_TOKEN in .env are valid."
            )
        else:
            logger.info("WS closed: code=%s", code)


# Module-level singleton — matches ticker.py interface
ticker_manager = SimTickerManager()
