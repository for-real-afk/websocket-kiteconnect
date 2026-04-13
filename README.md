# Kite Simulator

A WebSocket server that mimics Zerodha's KiteTicker binary protocol exactly,
so the Alumnx Quant Lab project works without a paid Kite Connect subscription.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    EC2 Instance                     │
│                                                     │
│  ┌──────────────────────┐  ┌──────────────────────┐ │
│  │  kite_simulator.py   │  │  management_api.py   │ │
│  │  WebSocket :8765     │  │  FastAPI REST :8766  │ │
│  │                      │  │                      │ │
│  │  • Binary tick frames│  │  POST /keys          │ │
│  │  • Auth via URL params│  │  GET  /keys          │ │
│  │  • 10 NSE instruments│  │  POST /keys/:k/token │ │
│  │  • 1s tick rate      │  │  POST /keys/:k/revoke│ │
│  └──────────────────────┘  │  DELETE /keys/:k     │ │
│           ▲                │  GET  /status        │ │
│           │ ws://          │  GET  /instruments   │ │
│           │                └──────────────────────┘ │
│  ┌────────┴─────────────┐         ▲                 │
│  │   Your Project       │         │ HTTP            │
│  │   ticker_sim.py      │─────────┘                 │
│  │   api.py / FastAPI   │  (reads api_keys.json)    │
│  └──────────────────────┘                           │
│           │                                         │
│           ▼  save_ticks()                           │
│        MySQL DB                                     │
└─────────────────────────────────────────────────────┘
```

## Ports to open in EC2 Security Group

| Port | Purpose                      |
|------|------------------------------|
| 22   | SSH                          |
| 8765 | WebSocket simulator          |
| 8766 | Management REST API (FastAPI)|
| 8000 | Your project's FastAPI       |

---

## Quick Start (EC2)

### 1. Clone and deploy

```bash
git clone https://github.com/YOUR_USER/kite-simulator.git ~/kite-simulator
cd ~/kite-simulator
chmod +x scripts/deploy.sh
./scripts/deploy.sh
```

The deploy script:
- Pulls latest code
- Creates a Python venv and installs dependencies
- Installs and starts both systemd services
- Prints the live URLs

### 2. Create your first API key

```bash
# From your local machine or any browser:
curl -X POST http://YOUR_EC2_IP:8766/keys \
     -H "Content-Type: application/json" \
     -d '{"label": "student-deepanshu"}'
```

Response:
```json
{
  "api_key":      "sim_abc123...",
  "access_token": "sat_xyz789...",
  "label":        "student-deepanshu",
  "active":       true,
  "connect_url":  "ws://YOUR_EC2_IP:8765?api_key=sim_abc...&access_token=sat_xyz..."
}
```

⚠️ **Save the `access_token` — it is shown only once.**

### 3. Test the connection

```bash
python simulator/test_client.py \
    --host YOUR_EC2_IP \
    --api-key sim_abc123 \
    --access-token sat_xyz789 \
    --duration 5
```

### 4. Wire into your project

In `.env` (your main project):
```env
KITE_SIM_URL=ws://YOUR_EC2_IP:8765
KITE_SIM_API_KEY=sim_abc123...
KITE_SIM_ACCESS_TOKEN=sat_xyz789...
```

In `api.py`, change **one line**:
```python
# from ticker import ticker_manager        ← real Zerodha
from ticker_sim import ticker_manager      # ← simulator
```

---

## Management API Reference

Open `http://YOUR_EC2_IP:8766/docs` for the interactive Swagger UI.

| Method   | Endpoint                    | Description                        |
|----------|-----------------------------|------------------------------------|
| `POST`   | `/keys`                     | Create api_key + access_token      |
| `GET`    | `/keys`                     | List all keys (tokens hidden)      |
| `POST`   | `/keys/{api_key}/token`     | Issue an additional access_token   |
| `POST`   | `/keys/{api_key}/revoke`    | Deactivate a key immediately       |
| `DELETE` | `/keys/{api_key}`           | Permanently delete a key           |
| `GET`    | `/status`                   | Live connection count + health     |
| `GET`    | `/instruments`              | Simulated instruments + prices     |

---

## WebSocket Protocol

### Connection URL
```
ws://HOST:8765?api_key=sim_xxx&access_token=sat_yyy
```

Invalid credentials → close code `4001`.

### Subscription messages (client → server)
```json
{"a": "subscribe",   "v": [738561, 341249]}
{"a": "unsubscribe", "v": [738561]}
{"a": "mode",        "v": ["quote", [738561]]}
```

### Instrument manifest (server → client, on connect)
```json
{
  "type": "instruments",
  "data": [
    {"instrument_token": 738561, "tradingsymbol": "RELIANCE", "exchange": "NSE"},
    ...
  ]
}
```

### Binary tick frames (server → client, every 1 second)

Follows Zerodha's QUOTE-mode binary format exactly:
- 2-byte header: `uint16` number of packets
- Per packet: `uint16` length prefix + 184-byte body
- Prices stored as `int32` in paise (×100)
- 1-byte heartbeat (`\x00`) every 3 seconds

---

## Simulated Instruments

| Token   | Symbol     | Exchange | Base Price |
|---------|------------|----------|------------|
| 738561  | RELIANCE   | NSE      | ₹2950      |
| 341249  | HDFCBANK   | NSE      | ₹1680      |
| 408065  | INFY       | NSE      | ₹1450      |
| 2953217 | TCS        | NSE      | ₹3350      |
| 1270529 | ICICIBANK  | NSE      | ₹1050      |
| 969473  | SBIN       | NSE      | ₹800       |
| 315393  | WIPRO      | NSE      | ₹460       |
| 1195009 | TATAMOTORS | NSE      | ₹920       |
| 134657  | AXISBANK   | NSE      | ₹1100      |
| 779521  | SUNPHARMA  | NSE      | ₹1600      |

To add more: edit `INSTRUMENTS` in `simulator/kite_simulator.py`.  
Real tokens: `https://api.kite.trade/instruments` (no auth needed).

---

## Updating after a code change

```bash
# On EC2:
cd ~/kite-simulator
./scripts/deploy.sh

# Or manually:
git pull origin main
sudo systemctl restart kite-simulator kite-mgmt-api
```

## Logs

```bash
sudo journalctl -u kite-simulator  -f    # WebSocket server
sudo journalctl -u kite-mgmt-api   -f    # Management API
```

## File Structure

```
kite-simulator/
├── simulator/
│   ├── kite_simulator.py   # WebSocket server (binary tick broadcaster)
│   ├── auth.py             # API key store (JSON-backed)
│   ├── ticker_sim.py       # Drop-in ticker.py replacement for your project
│   └── test_client.py      # CLI test tool
├── management_api.py       # FastAPI key management server
├── scripts/
│   ├── deploy.sh           # EC2 git-pull + restart script
│   ├── kite-simulator.service   # systemd for WebSocket server
│   └── kite-mgmt-api.service    # systemd for management API
├── config/
│   └── .gitkeep            # api_keys.json lives here (git-ignored)
├── requirements.txt
├── .env.example
└── .gitignore
```
