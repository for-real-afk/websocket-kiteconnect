# Kite Simulator — Sharing Guide for Peers

## What Your Peers Get

A live WebSocket endpoint that behaves **exactly like Zerodha KiteConnect**:

| Scenario | What is sent |
|---|---|
| Market OPEN (Mon–Fri 09:15–15:30 IST) | Binary QUOTE-mode tick frames every 1 second |
| Market CLOSED | `0x21` heartbeat byte every 10 seconds |
| New connection while closed | One cached price packet → then heartbeats |
| Invalid credentials | Close code `4001` (same as real Zerodha) |

---

## Step 1 — Deploy on EC2

```bash
# SSH into your EC2
ssh -i your-key.pem ubuntu@YOUR_EC2_IP

# Clone and deploy
git clone https://github.com/YOUR_USER/kite-simulator.git ~/kite-simulator
cd ~/kite-simulator
chmod +x scripts/deploy.sh
./scripts/deploy.sh
```

### EC2 Security Group — open these ports

| Port | Purpose |
|------|---------|
| 22   | SSH |
| 8765 | WebSocket simulator (what peers connect to) |
| 8766 | Management REST API (your admin panel) |

---

## Step 2 — Generate a key for each peer

### Via curl (your terminal):
```bash
curl -X POST http://YOUR_EC2_IP:8766/keys \
     -H "Content-Type: application/json" \
     -d '{"label": "student-deepanshu"}'
```

### Via browser:
Open `http://YOUR_EC2_IP:8766/docs` → click `POST /keys` → try it out.

### Response (save this — token shown ONCE):
```json
{
  "api_key":      "sim_abc123...",
  "access_token": "sat_xyz789...",
  "connect_url":  "ws://YOUR_EC2_IP:8765?api_key=sim_abc...&access_token=sat_xyz..."
}
```

**Share only the `connect_url` with the peer.** Keep the management API private.

---

## What You Send Each Peer (copy-paste this)

```
Kite Simulator Credentials
──────────────────────────
WebSocket URL : ws://13.126.130.56:8765?api_key=sim_XXX&access_token=sat_YYY

This mimics Zerodha KiteConnect exactly.
Ticks flow Mon–Fri 09:15–15:30 IST.
Outside hours: heartbeat byte every 10s (connection stays alive).

Quick test:
  python test_client.py --host 13.126.130.56 --api-key sim_XXX --access-token sat_YYY --duration 10

Save to MySQL:
  Set in .env → python kite_client.py --print

Save to CSV / JSON / Excel:
  See examples below.
```

---

## How Peers Can Use It

### Option A — Save to MySQL (kite_client.py already does this)
```bash
# .env
KITE_SIM_URL=ws://13.126.130.56:8765
KITE_SIM_API_KEY=sim_abc123
KITE_SIM_ACCESS_TOKEN=sat_xyz789
MYSQL_URL=mysql+pymysql://user:pass@localhost:3306/stocks

python kite_client.py
```

---

### Option B — Save to CSV / JSON / Excel

```python
# save_ticks.py — standalone, no MySQL needed
import json, csv, struct, websocket, os
from datetime import datetime

URL = "ws://13.126.130.56:8765?api_key=sim_XXX&access_token=sat_YYY"
SAVE_FORMAT = "csv"   # "csv" | "json" | "excel"

rows = []
token_map = {}

def parse_frame(data):
    import struct
    if len(data) <= 1:
        return []
    num = struct.unpack_from(">H", data, 0)[0]
    offset, ticks = 2, []
    for _ in range(num):
        if offset + 2 > len(data): break
        plen = struct.unpack_from(">H", data, offset)[0]; offset += 2
        pkt  = data[offset: offset + plen];               offset += plen
        if len(pkt) < 60: continue
        tok, _, _, lp, cp, chg, vol, bq, sq, o, h, l, c = struct.unpack_from(">iiiiiiqiiiiii", pkt, 0)
        ticks.append({"token": tok, "last": lp/100, "open": o/100, "high": h/100,
                      "low": l/100, "close": c/100, "volume": vol, "change": chg/100})
    return ticks

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
            "symbol":    token_map.get(t["token"], t["token"]),
            **{k: t[k] for k in ["last","open","high","low","close","volume","change"]}
        })
    print(f"Collected {len(rows)} ticks", end="\r")

def on_close(ws, code, msg):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if SAVE_FORMAT == "csv":
        fname = f"ticks_{ts}.csv"
        with open(fname, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader(); w.writerows(rows)
        print(f"\nSaved {len(rows)} rows → {fname}")

    elif SAVE_FORMAT == "json":
        fname = f"ticks_{ts}.json"
        with open(fname, "w") as f:
            json.dump(rows, f, indent=2)
        print(f"\nSaved {len(rows)} rows → {fname}")

    elif SAVE_FORMAT == "excel":
        import pandas as pd
        fname = f"ticks_{ts}.xlsx"
        pd.DataFrame(rows).to_excel(fname, index=False)
        print(f"\nSaved {len(rows)} rows → {fname}")

ws = websocket.WebSocketApp(URL, on_message=on_message, on_close=on_close)
ws.run_forever()
```

---

### Option C — Connect via MySQL Workbench

1. Peer runs `kite_client.py` (saves to MySQL)
2. Open MySQL Workbench → connect to their DB
3. Query:
```sql
SELECT symbol, timestamp, open, high, low, close, volume
FROM stock_data
ORDER BY timestamp DESC
LIMIT 100;
```

---

## For 24/7 Testing (outside market hours)

```bash
# On EC2 — restart simulator in force-open mode
sudo systemctl stop kite-simulator

# Edit the service file
sudo nano /etc/systemd/system/kite-simulator.service
# Change: ExecStart=...kite_simulator.py
# To:     ExecStart=...kite_simulator.py --force-open

sudo systemctl daemon-reload
sudo systemctl start kite-simulator
```

Or run directly:
```bash
python kite_simulator.py --force-open
```

---

## Managing Keys (your admin panel)

```bash
# List all active keys
curl http://YOUR_EC2_IP:8766/keys

# Issue a fresh token for an existing key
curl -X POST http://YOUR_EC2_IP:8766/keys/sim_abc123/token

# Revoke access immediately (e.g. course ends)
curl -X POST http://YOUR_EC2_IP:8766/keys/sim_abc123/revoke

# Permanently delete
curl -X DELETE http://YOUR_EC2_IP:8766/keys/sim_abc123
```

Or use Swagger UI: `http://YOUR_EC2_IP:8766/docs`

---

## Verify a peer's connection works

```bash
python test_client.py \
  --host 13.126.130.56 \
  --api-key sim_abc123 \
  --access-token sat_xyz789 \
  --duration 10
```

Expected output:
```
[manifest] 10 instruments registered

  RELIANCE     last=   2951.23  O=  2950.00  H=  2958.10  L=  2948.30  ...
  HDFCBANK     last=   1681.45  O=  1680.00  H=  1687.20  L=  1679.10  ...
  ...

Received 100 ticks over 10.0s.
✓ Simulator is working correctly with auth.
```
