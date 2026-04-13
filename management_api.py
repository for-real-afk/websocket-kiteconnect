"""
management_api.py — REST API for managing Kite Simulator API keys
==================================================================

Endpoints
---------
POST   /keys                  Create a new api_key + access_token
GET    /keys                  List all keys (tokens hidden)
POST   /keys/{api_key}/token  Issue an additional access_token for a key
POST   /keys/{api_key}/revoke Deactivate a key (connections drop immediately)
DELETE /keys/{api_key}        Permanently delete a key

GET    /status                Server health + connected client count
GET    /instruments           List all simulated instruments

Run
---
    uvicorn management_api:app --host 0.0.0.0 --port 8766 --reload

Then open http://YOUR_EC2_IP:8766/docs for the interactive UI.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make sure simulator/ is importable when run from project root
sys.path.insert(0, str(Path(__file__).resolve().parent / "simulator"))

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import auth as auth_store
from kite_simulator import INSTRUMENTS, _connections, _conn_keys, _state

# ---------------------------------------------------------------------------

app = FastAPI(
    title="Kite Simulator — Management API",
    description=(
        "Create and manage API keys for the Kite WebSocket Simulator.\n\n"
        "After creating a key use it to connect:\n"
        "`ws://HOST:8765?api_key=sim_xxx&access_token=sat_yyy`"
    ),
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateKeyRequest(BaseModel):
    label: str = ""


class KeyCreatedResponse(BaseModel):
    api_key:       str
    access_token:  str
    label:         str
    created_at:    str
    active:        bool
    connect_url:   str


class KeySummary(BaseModel):
    api_key:     str
    label:       str
    token_count: int
    created_at:  str
    active:      bool


class NewTokenResponse(BaseModel):
    api_key:      str
    access_token: str


# ---------------------------------------------------------------------------
# Key management endpoints
# ---------------------------------------------------------------------------

@app.post("/keys", response_model=KeyCreatedResponse, tags=["Keys"],
          summary="Create a new API key")
def create_key(body: CreateKeyRequest):
    """
    Creates a new `api_key` + initial `access_token`.

    ⚠️ **The access_token is returned only once.** Store it securely.
    Use `POST /keys/{api_key}/token` to issue additional tokens later.
    """
    record = auth_store.create_key(label=body.label)
    token  = record["access_tokens"][0]

    host = os.getenv("SIM_HOST", "localhost")
    port = os.getenv("SIM_PORT", "8765")

    return KeyCreatedResponse(
        api_key      = record["api_key"],
        access_token = token,
        label        = record["label"],
        created_at   = record["created_at"],
        active       = record["active"],
        connect_url  = f"ws://{host}:{port}?api_key={record['api_key']}&access_token={token}",
    )


@app.get("/keys", response_model=list[KeySummary], tags=["Keys"],
         summary="List all API keys")
def list_keys():
    """Returns all keys. Access tokens are **not** included."""
    return auth_store.list_keys()


@app.post("/keys/{api_key}/token", response_model=NewTokenResponse, tags=["Keys"],
          summary="Issue an additional access_token")
def issue_token(api_key: str):
    """
    Generates a new `access_token` for an existing `api_key`.
    Useful when a student needs a fresh token without creating a new key.
    """
    token = auth_store.issue_token(api_key)
    if token is None:
        raise HTTPException(status_code=404,
                            detail=f"api_key '{api_key}' not found or inactive.")
    return NewTokenResponse(api_key=api_key, access_token=token)


@app.post("/keys/{api_key}/revoke", tags=["Keys"],
          summary="Revoke (deactivate) an API key")
def revoke_key(api_key: str):
    """
    Marks the key as inactive. Existing WebSocket connections using this key
    will be dropped on the next tick cycle (within 1–2 seconds).
    """
    ok = auth_store.revoke_key(api_key)
    if not ok:
        raise HTTPException(status_code=404, detail=f"api_key '{api_key}' not found.")
    return {"api_key": api_key, "status": "revoked"}


@app.delete("/keys/{api_key}", tags=["Keys"],
            summary="Permanently delete an API key")
def delete_key(api_key: str):
    """Removes the key and all its tokens permanently from disk."""
    ok = auth_store.delete_key(api_key)
    if not ok:
        raise HTTPException(status_code=404, detail=f"api_key '{api_key}' not found.")
    return {"api_key": api_key, "status": "deleted"}


# ---------------------------------------------------------------------------
# Status / introspection endpoints
# ---------------------------------------------------------------------------

@app.get("/status", tags=["Info"], summary="Simulator health & stats")
def status():
    """Returns live connection count, instrument count, and server time."""
    return {
        "status":            "running",
        "connected_clients": len(_connections),
        "instruments":       len(_state),
        "server_time_utc":   datetime.now(timezone.utc).isoformat(),
        "websocket_port":    int(os.getenv("SIM_PORT", 8765)),
    }


@app.get("/instruments", tags=["Info"], summary="List simulated instruments")
def list_instruments():
    """Returns the full instrument list with current simulated prices."""
    return [
        {
            "instrument_token": inst["token"],
            "tradingsymbol":    inst["symbol"],
            "exchange":         inst["exchange"],
            "base_price":       inst["price"],
            "last_price":       round(_state[inst["token"]]["last_price"], 2)
                                if inst["token"] in _state else None,
        }
        for inst in INSTRUMENTS
    ]


# ---------------------------------------------------------------------------
# Root redirect to docs
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")
