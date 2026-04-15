# Kite Simulator — Zerodha KiteConnect WebSocket Simulator

A production-ready WebSocket server that **mimics Zerodha's KiteConnect binary protocol exactly** — so you can build, test, and train on real market data pipelines without a paid Zerodha subscription.

Runs on AWS EC2. Streams live simulated tick data for 10 NSE instruments every second during market hours.

---

## Table of Contents

- [What It Does](#what-it-does)
- [Architecture](#architecture)
- [Quick Start — Get Your Credentials](#quick-start--get-your-credentials)
- [Connecting Your Client](#connecting-your-client)
- [Save Data to MySQL](#save-data-to-mysql)
- [Save Data to CSV / JSON / Excel](#save-data-to-csv--json--excel)
- [Integrate with Your FastAPI Project](#integrate-with-your-fastapi-project)
- [Simulated Instruments](#simulated-instruments)
- [WebSocket Protocol Reference](#websocket-protocol-reference)
- [Market Hours Behaviour](#market-hours-behaviour)
- [Troubleshooting](#troubleshooting)
- [Admin — Managing Keys](#admin--managing-keys)

---

## What It Does

| Scenario | Behaviour |
|---|---|
| Market OPEN (Mon–Fri 09:15–15:30 IST) | Binary QUOTE-mode tick frames every 1 second |
| Market CLOSED | `0x21` heartbeat byte every 10 seconds — connection stays alive |
| New connection while closed | One cached price packet sent immediately, then heartbeats |
| Invalid credentials | Close code `4001` — same as real Zerodha |

This is a **drop-in replacement** for `KiteTicker`. Change one import line in your project and everything works.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        EC2 Instance                          │
│                                                              │
│  ┌─────────────────────────┐  ┌────────────────────────────┐ │
│  │   kite_simulator.py     │  │    management_api.py       │ │
│  │   WebSocket :8765       │  │    FastAPI REST :8766      │ │
│  │                         │  │                            │ │
│  │  • Binary tick frames   │  │  POST /keys                │ │
│  │  • Auth via URL params  │  │  GET  /keys                │ │
│  │  • 10 NSE instruments   │  │  POST /keys/:k/token       │ │
│  │  • 1s tick rate         │  │  POST /keys/:k/revoke      │ │
│  │  • Heartbeat on close   │  │  DELETE /keys/:k           │ │
│  └─────────────────────────┘  │  GET  /status              │ │
│            ▲                  │  GET  /instruments         │ │
│            │ WebSocket        └────────────────────────────┘ │
└────────────┼─────────────────────────────────────────────────┘
             │
    ┌────────┴──────────────────────────┐
    │        Your Machine / Project     │
    │                                   │
    │  kite_client.py  → MySQL          │
    │  save_ticks.py   → CSV/JSON/Excel │
    │  ticker_sim.py   → FastAPI project│
    └───────────────────────────────────┘
```

---

## Quick Start — Get Your Credentials

### Step 1 — Request your API key

Send a POST request to the management API to generate your personal credentials:

```bash
curl -X POST http://13.126.130.56:8766/keys \
     -H "Content-Type: application/json" \
     -d '{"label": "your-name"}'
```

Or open the Swagger UI in your browser:
```
http://13.126.130.56:8766/docs
```
Click **POST /keys → Try it out → Execute**.

### Step 2 — Save your credentials

You will receive a response like this:

```json
{
  "api_key":      "sim_abc123...",
  "access_token": "sat_xyz789...",
  "label":        "your-name",
  "active":       true,
  "connect_url":  "ws://13.126.130.56:8765?api_key=sim_abc...&access_token=sat_xyz..."
}
```

> **Important:** Save the `access_token` immediately. It is shown **only once**. If you lose it, request a new one via `POST /keys/{api_key}/token`.

### Step 3 — Verify the connection

Install the test dependency if you haven't already:

```bash
pip install websocket-client
```

Run the test client:

```bash
python test_client.py \
  --host 13.126.130.56 \
  --api-key sim_abc123... \
  --access-token sat_xyz789... \
  --duration 10
```

Expected output:

```
Connecting to ws://13.126.130.56:8765 ...

  [manifest] 10 instruments registered

  RELIANCE     last=   2951.23  O=  2950.00  H=  2958.10  L=  2948.30  C=  2950.00  vol=   124,500  chg=  +0.04%
  HDFCBANK     last=   1681.45  O=  1680.00  H=  1687.20  L=  1679.10  C=  1680.00  vol=    89,200  chg=  +0.09%
  ...

Received 100 ticks over 10.0s.
✓ Simulator is working correctly with auth.
```

---

## Connecting Your Client

### Minimum working client (Python)

```python
import json, struct, websocket

API_KEY      = "sim_abc123..."
ACCESS_TOKEN = "sat_xyz789..."
URL = f"ws://13.126.130.56:8765?api_key={API_KEY}&access_token={ACCESS_TOKEN}"

def parse_frame(data):
    if len(data) <= 1:
        return []   # heartbeat
    num = struct.unpack_from(">H", data, 0)[0]
    offset, ticks = 2, []
    for _ in range(num):
        if offset + 2 > len(data): break
        plen = struct.unpack_from(">H", data, offset)[0]; offset += 2
        pkt  = data[offset: offset + plen];               offset += plen
        if len(pkt) < 60: continue
        tok, _, _, lp, cp, chg, vol, bq, sq, o, h, l, c = struct.unpack_from(
            ">iiiiiiqiiiiii", pkt, 0)
        ticks.append({
            "token": tok,
            "last":  lp / 100,
            "open":  o  / 100,
            "high":  h  / 100,
            "low":   l  / 100,
            "close": c  / 100,
            "volume": vol,
            "change": chg / 100,
        })
    return ticks

token_map = {}

def on_message(ws, data):
    if isinstance(data, str):
        msg = json.loads(data)
        if msg["type"] == "instruments":
            for i in msg["data"]:
                token_map[i["instrument_token"]] = i["tradingsymbol"]
        elif msg["type"] == "market_open":
            print("Market is OPEN — ticks flowing")
        elif msg["type"] == "market_closed":
            print(f"Market CLOSED. Next open: {msg.get('next_open')}")
        return
    for t in parse_frame(data):
        sym = token_map.get(t["token"], t["token"])
        print(f"{sym}: ₹{t['last']:.2f}  vol={t['volume']:,}")

def on_error(ws, err):
    print(f"Error: {err}")

def on_close(ws, code, msg):
    if code == 4001:
        print("AUTH FAILED — check your api_key and access_token")
    else:
        print(f"Disconnected: {code}")

ws = websocket.WebSocketApp(URL,
    on_message=on_message,
    on_error=on_error,
    on_close=on_close)
ws.run_forever(ping_interval=30, ping_timeout=20)
```

> **Note:** Always use `ping_interval > ping_timeout`. The rule is `ping_interval=30, ping_timeout=20`.

---

## Save Data to MySQL

### Prerequisites

```bash
pip install websocket-client pymysql dbutils python-dotenv pytz
```

### Setup

Create a `.env` file:

```env
KITE_SIM_URL=ws://13.126.130.56:8765
KITE_SIM_API_KEY=sim_abc123...
KITE_SIM_ACCESS_TOKEN=sat_xyz789...
MYSQL_URL=mysql+pymysql://username:password@localhost:3306/your_database
```

### Run

```bash
# Stream ticks + print to console + save to MySQL
python kite_client.py --print

# Save to MySQL silently
python kite_client.py

# Test without MySQL (console only)
python kite_client.py --print --no-db
```

The client automatically creates a `stock_data` table with this schema:

```sql
CREATE TABLE stock_data (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    instrument_token INT          NOT NULL,
    symbol           VARCHAR(50),
    exchange         VARCHAR(20),
    timestamp        DATETIME     NOT NULL,
    open             DECIMAL(12,4),
    high             DECIMAL(12,4),
    low              DECIMAL(12,4),
    close            DECIMAL(12,4),
    volume           BIGINT,
    UNIQUE KEY uq_token_ts (instrument_token, timestamp)
);
```

### Query in MySQL Workbench

```sql
-- Latest ticks for all symbols
SELECT symbol, timestamp, open, high, low, close, volume
FROM stock_data
ORDER BY timestamp DESC
LIMIT 100;

-- OHLCV for a specific symbol
SELECT timestamp, open, high, low, close, volume
FROM stock_data
WHERE symbol = 'RELIANCE'
ORDER BY timestamp DESC
LIMIT 50;

-- Daily summary
SELECT
    symbol,
    DATE(timestamp)   AS date,
    MIN(open)         AS day_open,
    MAX(high)         AS day_high,
    MIN(low)          AS day_low,
    MAX(close)        AS day_close,
    SUM(volume)       AS total_volume
FROM stock_data
GROUP BY symbol, DATE(timestamp)
ORDER BY date DESC, symbol;
```

---

## Save Data to CSV / JSON / Excel

Use this standalone script — no MySQL needed:

```python
# save_ticks.py
import json, csv, struct, websocket
from datetime import datetime

API_KEY      = "sim_abc123..."
ACCESS_TOKEN = "sat_xyz789..."
URL          = f"ws://13.126.130.56:8765?api_key={API_KEY}&access_token={ACCESS_TOKEN}"

SAVE_FORMAT  = "csv"        # change to "json" or "excel"
MAX_TICKS    = 500          # how many ticks to collect before saving

rows      = []
token_map = {}

def parse_frame(data):
    if len(data) <= 1: return []
    num = struct.unpack_from(">H", data, 0)[0]
    offset, ticks = 2, []
    for _ in range(num):
        if offset + 2 > len(data): break
        plen = struct.unpack_from(">H", data, offset)[0]; offset += 2
        pkt  = data[offset: offset + plen];               offset += plen
        if len(pkt) < 60: continue
        tok, _, _, lp, cp, chg, vol, bq, sq, o, h, l, c = struct.unpack_from(
            ">iiiiiiqiiiiii", pkt, 0)
        ticks.append({"token": tok, "last": lp/100, "open": o/100,
                      "high": h/100, "low": l/100, "close": c/100,
                      "volume": vol, "change": chg/100})
    return ticks

def save_and_exit(ws):
    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\nSaving {len(rows)} rows...")

    if SAVE_FORMAT == "csv":
        fname = f"ticks_{ts}.csv"
        with open(fname, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)

    elif SAVE_FORMAT == "json":
        fname = f"ticks_{ts}.json"
        with open(fname, "w") as f:
            json.dump(rows, f, indent=2)

    elif SAVE_FORMAT == "excel":
        import pandas as pd
        fname = f"ticks_{ts}.xlsx"
        pd.DataFrame(rows).to_excel(fname, index=False)

    print(f"Saved → {fname}")
    ws.close()

def on_message(ws, data):
    if isinstance(data, str):
        msg = json.loads(data)
        if msg.get("type") == "instruments":
            for i in msg["data"]:
                token_map[i["instrument_token"]] = i["tradingsymbol"]
        return
    for t in parse_frame(data):
        rows.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol":    token_map.get(t["token"], str(t["token"])),
            "last":      t["last"], "open": t["open"], "high": t["high"],
            "low":       t["low"],  "close": t["close"],
            "volume":    t["volume"], "change": t["change"],
        })
    print(f"  Collected {len(rows)} ticks", end="\r")
    if len(rows) >= MAX_TICKS:
        save_and_exit(ws)

ws = websocket.WebSocketApp(URL, on_message=on_message)
ws.run_forever(ping_interval=30, ping_timeout=20)
```

Install dependencies:

```bash
# For CSV/JSON only:
pip install websocket-client

# For Excel:
pip install websocket-client pandas openpyxl
```

Run:

```bash
python save_ticks.py
```

---

## Integrate with Your FastAPI Project

If you have an existing FastAPI project that uses Zerodha's real `KiteTicker`, switch to the simulator with **one line change**.

### Step 1 — Add to your `.env`

```env
KITE_SIM_URL=ws://13.126.130.56:8765
KITE_SIM_API_KEY=sim_abc123...
KITE_SIM_ACCESS_TOKEN=sat_xyz789...
```

### Step 2 — Change one import in `api.py`

```python
# BEFORE (real Zerodha — requires paid subscription)
from ticker import ticker_manager

# AFTER (simulator — free)
from ticker_sim import ticker_manager
```

Everything else — APScheduler, database saving, OHLCV parsing — is identical.

---

## Simulated Instruments

| Token | Symbol | Exchange | Base Price |
|---|---|---|---|
| 738561 | RELIANCE | NSE | ₹2950 |
| 341249 | HDFCBANK | NSE | ₹1680 |
| 408065 | INFY | NSE | ₹1450 |
| 2953217 | TCS | NSE | ₹3350 |
| 1270529 | ICICIBANK | NSE | ₹1050 |
| 969473 | SBIN | NSE | ₹800 |
| 315393 | WIPRO | NSE | ₹460 |
| 1195009 | TATAMOTORS | NSE | ₹920 |
| 134657 | AXISBANK | NSE | ₹1100 |
| 779521 | SUNPHARMA | NSE | ₹1600 |

Prices follow a mean-reverting random walk — they drift around the base price realistically.

---

## WebSocket Protocol Reference

### Connection URL

```
ws://13.126.130.56:8765?api_key=sim_xxx&access_token=sat_yyy
```

Invalid credentials → close code **4001**.

### Messages sent by server (server → client)

**On connect — instrument manifest (JSON):**
```json
{
  "type": "instruments",
  "data": [
    {"instrument_token": 738561, "tradingsymbol": "RELIANCE", "exchange": "NSE"},
    ...
  ]
}
```

**At market open (JSON):**
```json
{"type": "market_open"}
```

**At market close (JSON):**
```json
{"type": "market_closed", "next_open": "2026-04-15 09:15 IST"}
```

**During market hours — binary tick frame (every 1 second):**
```
[2-byte header: uint16 number of packets]
  [2-byte uint16 packet length]
  [60-byte packet body per instrument]
  ...
```

Each 60-byte packet body (struct format `>iiiiiiqiiiiii`):

| Field | Type | Notes |
|---|---|---|
| instrument_token | int32 | |
| last_traded_qty | int32 | paise × 100 |
| average_price | int32 | paise × 100 |
| last_price | int32 | paise × 100 |
| close_price | int32 | paise × 100 |
| change | int32 | % × 100 |
| volume | int64 | |
| buy_quantity | int32 | |
| sell_quantity | int32 | |
| open | int32 | paise × 100 |
| high | int32 | paise × 100 |
| low | int32 | paise × 100 |
| close | int32 | paise × 100 |

**During market closed — heartbeat (binary, every 10 seconds):**
```
0x21
```

### Subscription messages (client → server)

The server auto-subscribes all clients to all instruments on connect. You can also send:

```json
{"a": "subscribe",   "v": [738561, 341249]}
{"a": "unsubscribe", "v": [738561]}
{"a": "mode",        "v": ["quote", [738561]]}
```

---

## Market Hours Behaviour

| Time (IST) | Day | Behaviour |
|---|---|---|
| 09:15 – 15:30 | Mon–Fri | Binary tick frames every 1 second |
| 15:30 – 09:15 | Mon–Fri | `0x21` heartbeat every 10 seconds |
| All day | Sat–Sun | `0x21` heartbeat every 10 seconds |
| Any time | Any | New connections get one cached price frame first |

The connection **never drops** outside market hours. This is identical to real Zerodha behaviour.

---

## Troubleshooting

### "Invalid api_key or access_token" (close code 4001)

Your credentials are wrong. Do not use the placeholder values `sim_xxx` / `sat_yyy` — those are examples. Use the real values you received when you ran `POST /keys`.

To get a fresh token for your existing key:

```bash
curl -X POST http://13.126.130.56:8766/keys/YOUR_API_KEY/token
```

### "Ensure ping_interval > ping_timeout"

Your `run_forever` call has the values backwards. Use:

```python
ws.run_forever(ping_interval=30, ping_timeout=20)
```

### "Port could not be cast to integer value as '8765:8765'"

You passed `--host 13.126.130.56:8765` — the port is included automatically. Pass only the IP:

```bash
python test_client.py --host 13.126.130.56 --api-key ... --access-token ...
```

### No ticks received outside 09:15–15:30 IST

This is expected behaviour. The connection stays alive but only heartbeat bytes are sent. To receive ticks 24/7 for testing, ask the admin to enable `--force-open` mode.

### Connection keeps dropping and reconnecting

Check that your `on_error` handler is not seeing code `4001`. If it is, your credentials are invalid. If code is different, it is a network issue — the client will auto-reconnect.

---

## Admin — Managing Keys

> This section is for the server admin only. Users do not need access to port 8766.

### Create a key for a new user

```bash
curl -X POST http://localhost:8766/keys \
     -H "Content-Type: application/json" \
     -d '{"label": "student-name"}'
```

### List all keys

```bash
curl http://localhost:8766/keys
```

### Issue a new token for an existing key

```bash
curl -X POST http://localhost:8766/keys/sim_abc123.../token
```

### Revoke access immediately

```bash
curl -X POST http://localhost:8766/keys/sim_abc123.../revoke
```

### Permanently delete a key

```bash
curl -X DELETE http://localhost:8766/keys/sim_abc123...
```

### Enable 24/7 tick streaming (force-open mode)

Edit the service file:

```bash
sudo nano /etc/systemd/system/kite-simulator.service
```

Add `--force-open` to `ExecStart`:

```ini
ExecStart=.../python kite_simulator.py --host 0.0.0.0 --port 8765 --force-open --log-level INFO
```

Then reload:

```bash
sudo systemctl daemon-reload
sudo systemctl restart kite-simulator
```

### View live server logs

```bash
sudo journalctl -u kite-simulator -f
sudo journalctl -u kite-mgmt-api  -f
```

---

## Required Python Packages

```bash
pip install websocket-client pytz python-dotenv pymysql dbutils pandas openpyxl
```

---

*Server hosted at `13.126.130.56` · WebSocket `:8765` · Management API `:8766/docs`*
