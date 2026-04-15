"""
kite_client.py — Plug-and-play client for the Kite Simulator
=============================================================

Works exactly like a real Zerodha KiteTicker:
  - Connects to the simulator WebSocket
  - Receives binary tick frames every second
  - Parses OHLCV data from each frame
  - Saves everything to MySQL (same schema as the main project)
  - Handles market open/close transitions automatically
  - Auto-reconnects if the connection drops

Setup (one time)
----------------
1. pip install websocket-client pymysql dbutils python-dotenv pytz

2. Copy .env.client.example to .env and fill in:
      KITE_SIM_URL          = ws://13.126.130.56:8765
      KITE_SIM_API_KEY      = sim_xxx
      KITE_SIM_ACCESS_TOKEN = sat_yyy
      MYSQL_URL             = mysql+pymysql://user:pass@host:3306/dbname

3. python kite_client.py

That's it. Data flows into your MySQL every second during market hours.
Outside market hours the connection stays alive — ticks resume automatically
when the market opens.

To run without MySQL (just print ticks to console):
    python kite_client.py --no-db --print
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import struct
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import pytz
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
IST    = pytz.timezone("Asia/Kolkata")

SIM_URL          = os.getenv("KITE_SIM_URL",          "ws://localhost:8765")
SIM_API_KEY      = os.getenv("KITE_SIM_API_KEY",      "")
SIM_ACCESS_TOKEN = os.getenv("KITE_SIM_ACCESS_TOKEN", "")
MYSQL_URL        = os.getenv("MYSQL_URL",              "")

AUTH_URL = f"{SIM_URL}?api_key={SIM_API_KEY}&access_token={SIM_ACCESS_TOKEN}"

# ---------------------------------------------------------------------------
# Binary parser — mirrors kite_simulator._pack_one in reverse
# ---------------------------------------------------------------------------

def parse_tick_frame(data: bytes) -> list[dict]:
    """Parse a Zerodha QUOTE-mode binary frame into a list of tick dicts."""
    if len(data) <= 1:
        return []   # heartbeat byte

    try:
        num_packets = struct.unpack_from(">H", data, 0)[0]
    except struct.error:
        return []

    ticks  = []
    offset = 2   # skip 2-byte header

    for _ in range(num_packets):
        if offset + 2 > len(data):
            break
        pkt_len = struct.unpack_from(">H", data, offset)[0]
        offset += 2
        pkt = data[offset: offset + pkt_len]
        offset += pkt_len

        if len(pkt) < 60:
            continue

        try:
            (token, ltq, avg_price, last_price, close_price,
             change, volume, buy_qty, sell_qty,
             o, h, l, c) = struct.unpack_from(">iiiiiiqiiiiii", pkt, 0)

            ticks.append({
                "instrument_token": token,
                "last_price":       last_price  / 100.0,
                "average_price":    avg_price   / 100.0,
                "volume":           volume,
                "buy_quantity":     buy_qty,
                "sell_quantity":    sell_qty,
                "change":           change      / 100.0,
                "ohlc": {
                    "open":  o / 100.0,
                    "high":  h / 100.0,
                    "low":   l / 100.0,
                    "close": c / 100.0,
                },
            })
        except struct.error:
            continue

    return ticks


# ---------------------------------------------------------------------------
# MySQL — identical schema to the main project's stock_data table
# ---------------------------------------------------------------------------

_pool      = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool:
        return _pool
    with _pool_lock:
        if _pool:
            return _pool
        if not MYSQL_URL:
            return None
        try:
            import pymysql
            from dbutils.pooled_db import PooledDB
            from urllib.parse import urlparse
            p = urlparse(MYSQL_URL)
            _pool = PooledDB(
                creator      = pymysql,
                mincached    = 1,
                maxcached    = 3,
                maxconnections = 5,
                blocking     = True,
                host         = p.hostname,
                port         = p.port or 3306,
                user         = p.username,
                password     = p.password,
                database     = p.path.lstrip("/"),
                connect_timeout = 10,
                cursorclass  = pymysql.cursors.DictCursor,
            )
            logger.info("MySQL pool created → %s:%s/%s",
                        p.hostname, p.port or 3306, p.path.lstrip("/"))
            _ensure_table()
        except Exception as exc:
            logger.error("MySQL pool failed: %s", exc)
            _pool = None
    return _pool


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS stock_data (
    id               BIGINT       AUTO_INCREMENT PRIMARY KEY,
    instrument_token INT          NOT NULL,
    symbol           VARCHAR(50)  NOT NULL DEFAULT '',
    exchange         VARCHAR(20)  NOT NULL DEFAULT 'NSE',
    timestamp        DATETIME     NOT NULL,
    open             DECIMAL(12,4),
    high             DECIMAL(12,4),
    low              DECIMAL(12,4),
    close            DECIMAL(12,4),
    volume           BIGINT,
    UNIQUE KEY uq_token_ts (instrument_token, timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def _ensure_table():
    pool = _get_pool()
    if not pool:
        return
    conn = pool.connection()
    cur  = conn.cursor()
    cur.execute(_CREATE_TABLE)
    conn.commit()
    cur.close()
    conn.close()


def save_ticks_to_db(ticks: list[dict], token_map: dict) -> int:
    pool = _get_pool()
    if not pool or not ticks:
        return 0

    saved = 0
    now   = datetime.now(IST).replace(tzinfo=None)
    try:
        conn = pool.connection()
        cur  = conn.cursor()
        for t in ticks:
            token = t["instrument_token"]
            meta  = token_map.get(token, {})
            ohlc  = t.get("ohlc", {})
            cur.execute(
                """
                INSERT IGNORE INTO stock_data
                    (instrument_token, symbol, exchange, timestamp,
                     open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    token,
                    meta.get("symbol", str(token)),
                    meta.get("exchange", "NSE"),
                    now,
                    ohlc.get("open"),
                    ohlc.get("high"),
                    ohlc.get("low"),
                    ohlc.get("close"),
                    t.get("volume", 0),
                ),
            )
            saved += cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
    except Exception as exc:
        logger.error("DB save error: %s", exc)
    return saved


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------

class KiteSimClient:
    """
    Drop-in replacement for Zerodha's KiteTicker.
    Connect once, receive ticks forever (market hours + after hours).
    """

    def __init__(self, print_ticks: bool = False, use_db: bool = True):
        self._print      = print_ticks
        self._use_db     = use_db and bool(MYSQL_URL)
        self._token_map: dict[int, dict] = {}   # token → {symbol, exchange}
        self._running    = threading.Event()
        self._ws         = None
        self._lock       = threading.Lock()

        # Stats
        self.ticks_received = 0
        self.ticks_saved    = 0
        self.market_open    = False

    def start(self):
        """Start the client in the background. Blocks until KeyboardInterrupt."""
        if not SIM_API_KEY or not SIM_ACCESS_TOKEN:
            raise RuntimeError(
                "KITE_SIM_API_KEY and KITE_SIM_ACCESS_TOKEN must be set.\n"
                "Check your .env file."
            )
        self._running.set()
        logger.info("Connecting to %s", SIM_URL)
        self._connect_loop()

    def stop(self):
        self._running.clear()
        with self._lock:
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass

    def _connect_loop(self):
        import websocket
        while self._running.is_set():
            ws = websocket.WebSocketApp(
                AUTH_URL,
                on_open    = self._on_open,
                on_message = self._on_message,
                on_error   = self._on_error,
                on_close   = self._on_close,
            )
            with self._lock:
                self._ws = ws
            ws.run_forever(ping_interval=30, ping_timeout=20)
            if self._running.is_set():
                logger.info("Reconnecting in 5s…")
                time.sleep(5)

    def _on_open(self, ws):
        logger.info("Connected to Kite Simulator.")

    def _on_message(self, ws, data):
        # --- JSON text frames ---
        if isinstance(data, str):
            try:
                msg  = json.loads(data)
                kind = msg.get("type", "")

                if kind == "instruments":
                    for inst in msg.get("data", []):
                        self._token_map[inst["instrument_token"]] = {
                            "symbol":   inst["tradingsymbol"],
                            "exchange": inst["exchange"],
                        }
                    logger.info("Instruments loaded: %d symbols", len(self._token_map))
                    # Subscribe to everything
                    tokens = list(self._token_map.keys())
                    ws.send(json.dumps({"a": "subscribe", "v": tokens}))
                    ws.send(json.dumps({"a": "mode", "v": ["quote", tokens]}))

                elif kind == "market_open":
                    self.market_open = True
                    logger.info("Market OPEN — ticks flowing.")

                elif kind == "market_closed":
                    self.market_open = False
                    logger.info("Market CLOSED. Next open: %s",
                                msg.get("next_open", "unknown"))
            except Exception as exc:
                logger.debug("JSON parse error: %s", exc)
            return

        # --- Binary tick frames ---
        ticks = parse_tick_frame(data)
        if not ticks:
            return   # heartbeat

        self.ticks_received += len(ticks)

        if self._print:
            ts = datetime.now(IST).strftime("%H:%M:%S")
            for t in ticks:
                sym  = self._token_map.get(t["instrument_token"], {}).get("symbol", t["instrument_token"])
                ohlc = t.get("ohlc", {})
                print(
                    f"[{ts}] {sym:<12} "
                    f"LTP={t['last_price']:>10.2f}  "
                    f"O={ohlc.get('open',0):>10.2f}  "
                    f"H={ohlc.get('high',0):>10.2f}  "
                    f"L={ohlc.get('low',0):>10.2f}  "
                    f"C={ohlc.get('close',0):>10.2f}  "
                    f"Vol={t['volume']:>10,}  "
                    f"Chg={t['change']:>+7.2f}%"
                )

        if self._use_db:
            saved = save_ticks_to_db(ticks, self._token_map)
            self.ticks_saved += saved

    def _on_error(self, ws, err):
        if "4001" in str(err):
            logger.error("AUTH FAILED — check your api_key and access_token.")
            self._running.clear()
        else:
            logger.error("WebSocket error: %s", err)

    def _on_close(self, ws, code, msg):
        if code == 4001:
            logger.error("Rejected: invalid credentials (code 4001).")
            self._running.clear()
        else:
            logger.info("Disconnected (code=%s). Will reconnect.", code)


# ---------------------------------------------------------------------------
# Stats printer
# ---------------------------------------------------------------------------

def _print_stats(client: KiteSimClient):
    while True:
        time.sleep(30)
        logger.info(
            "Stats — ticks received: %d  saved to DB: %d  market: %s",
            client.ticks_received,
            client.ticks_saved,
            "OPEN" if client.market_open else "CLOSED",
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Kite Simulator client — receives ticks and saves to MySQL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--print",  action="store_true", dest="print_ticks",
                        help="Print every tick to console")
    parser.add_argument("--no-db",  action="store_true",
                        help="Skip MySQL — useful for testing without a database")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level    = getattr(logging, args.log_level),
        format   = "%(asctime)s %(levelname)-8s %(message)s",
        datefmt  = "%Y-%m-%d %H:%M:%S",
    )

    use_db = not args.no_db
    if use_db and not MYSQL_URL:
        logger.warning("MYSQL_URL not set — running without database (add it to .env)")
        use_db = False

    if use_db:
        logger.info("MySQL enabled — ticks will be saved to stock_data table.")
    else:
        logger.info("No-DB mode — ticks will not be saved.")

    client = KiteSimClient(print_ticks=args.print_ticks, use_db=use_db)

    # Background stats thread
    t = threading.Thread(target=_print_stats, args=(client,), daemon=True)
    t.start()

    try:
        client.start()
    except KeyboardInterrupt:
        logger.info("Stopping…")
        client.stop()
    except RuntimeError as exc:
        logger.error(str(exc))


if __name__ == "__main__":
    main()
