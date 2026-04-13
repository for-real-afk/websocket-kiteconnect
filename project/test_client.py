"""
test_client.py — Verify the simulator is sending valid authenticated frames.

Usage
-----
    python test_client.py --api-key sim_xxx --access-token sat_yyy
    python test_client.py --api-key sim_xxx --access-token sat_yyy --host 1.2.3.4
    python test_client.py --api-key sim_xxx --access-token sat_yyy --duration 10
"""

from __future__ import annotations

import argparse
import json
import struct
import time

import websocket

HEADER_LEN  = 2
PKT_HDR_LEN = 2


def parse_binary(data: bytes) -> list[dict]:
    if len(data) <= 1:
        return []
    num    = struct.unpack_from(">H", data, 0)[0]
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
        (token, _, _, last_price, close_price, change,
         volume, buy_qty, sell_qty, o, h, l, c) = struct.unpack_from(
            ">iiiiiiqiiiiii", pkt, 0
        )
        ticks.append({
            "token":      token,
            "last_price": last_price / 100.0,
            "open":       o / 100.0, "high": h / 100.0,
            "low":        l / 100.0, "close": c / 100.0,
            "volume":     volume,    "change": change / 100.0,
        })
    return ticks


def main():
    parser = argparse.ArgumentParser(description="Kite Simulator test client")
    parser.add_argument("--host",         default="localhost")
    parser.add_argument("--port",         default=8765, type=int)
    parser.add_argument("--api-key",      required=True)
    parser.add_argument("--access-token", required=True)
    parser.add_argument("--duration",     default=5.0, type=float)
    args = parser.parse_args()

    url      = (f"ws://{args.host}:{args.port}"
                f"?api_key={args.api_key}&access_token={args.access_token}")
    deadline = time.monotonic() + args.duration
    received = []
    token_map: dict[int, str] = {}

    print(f"\nConnecting to {url}  (listening {args.duration}s) …\n")

    def on_open(ws):
        pass   # server subscribes us to all instruments automatically

    def on_message(ws, data):
        if isinstance(data, str):
            try:
                msg = json.loads(data)
                if msg.get("type") == "instruments":
                    for inst in msg["data"]:
                        token_map[inst["instrument_token"]] = inst["tradingsymbol"]
                    print(f"  [manifest] {len(token_map)} instruments registered\n")
            except Exception:
                pass
            return

        ticks = parse_binary(data)
        for t in ticks:
            sym = token_map.get(t["token"], str(t["token"]))
            received.append(t)
            print(
                f"  {sym:<12} "
                f"last={t['last_price']:>10.2f}  "
                f"O={t['open']:>10.2f}  H={t['high']:>10.2f}  "
                f"L={t['low']:>10.2f}  C={t['close']:>10.2f}  "
                f"vol={t['volume']:>10,}  chg={t['change']:>+7.2f}%"
            )

        if time.monotonic() >= deadline:
            ws.close()

    def on_error(ws, err):
        print(f"\n  ERROR: {err}")

    def on_close(ws, code, msg):
        if code == 4001:
            print(f"\n  REJECTED: Invalid api_key or access_token (code 4001)")
        else:
            print(f"\n  Connection closed: code={code}")

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open, on_message=on_message,
        on_error=on_error, on_close=on_close,
    )
    ws.run_forever()

    print(f"\n{'─' * 60}")
    print(f"Received {len(received)} ticks over {args.duration}s.")
    if received:
        print("✓ Simulator is working correctly with auth.")
    else:
        print("✗ No ticks — check credentials and that simulator is running.")


if __name__ == "__main__":
    main()
