"""
auth.py — API key store for the Kite Simulator.

Keys are persisted in config/api_keys.json as:
{
    "<api_key>": {
        "access_tokens": ["tok_abc", "tok_xyz"],
        "label": "student-deepanshu",
        "created_at": "2026-04-13T10:00:00",
        "active": true
    }
}

Thread-safety: a module-level RLock guards every read/write so the
WebSocket broadcast thread and the FastAPI management thread never race.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Storage location — resolved relative to THIS file's directory
# ---------------------------------------------------------------------------

_REPO_ROOT   = Path(__file__).resolve().parent.parent          # project root
_KEYS_FILE   = _REPO_ROOT / "config" / "api_keys.json"
_lock        = threading.RLock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load() -> dict:
    """Read keys file; return empty dict if missing."""
    if not _KEYS_FILE.exists():
        return {}
    try:
        return json.loads(_KEYS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read %s: %s", _KEYS_FILE, exc)
        return {}


def _save(data: dict) -> None:
    """Write keys dict to disk atomically (write-then-rename)."""
    _KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _KEYS_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(_KEYS_FILE)
    except OSError as exc:
        logger.error("Failed to write %s: %s", _KEYS_FILE, exc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_key(label: str = "") -> dict:
    """
    Generate a new api_key + one initial access_token.
    Returns the full record including the plaintext tokens (only shown once).
    """
    api_key      = "sim_" + secrets.token_hex(16)
    access_token = "sat_" + secrets.token_hex(24)

    record = {
        "access_tokens": [access_token],
        "label":         label or f"key-{api_key[-6:]}",
        "created_at":    _now_iso(),
        "active":        True,
    }

    with _lock:
        data = _load()
        data[api_key] = record
        _save(data)

    logger.info("Created API key %s (%s)", api_key, record["label"])
    return {"api_key": api_key, **record}


def issue_token(api_key: str) -> Optional[str]:
    """
    Generate a new access_token for an existing api_key.
    Returns None if the api_key doesn't exist or is inactive.
    """
    with _lock:
        data = _load()
        rec  = data.get(api_key)
        if not rec or not rec.get("active"):
            return None

        new_token = "sat_" + secrets.token_hex(24)
        rec["access_tokens"].append(new_token)
        _save(data)

    logger.info("Issued new token for %s", api_key)
    return new_token


def revoke_key(api_key: str) -> bool:
    """Deactivate an API key (tokens stop working immediately)."""
    with _lock:
        data = _load()
        if api_key not in data:
            return False
        data[api_key]["active"] = False
        _save(data)

    logger.info("Revoked API key %s", api_key)
    return True


def delete_key(api_key: str) -> bool:
    """Permanently delete an API key and all its tokens."""
    with _lock:
        data = _load()
        if api_key not in data:
            return False
        del data[api_key]
        _save(data)

    logger.info("Deleted API key %s", api_key)
    return True


def list_keys() -> list[dict]:
    """Return all keys (tokens are NOT included for security)."""
    with _lock:
        data = _load()

    result = []
    for api_key, rec in data.items():
        result.append({
            "api_key":       api_key,
            "label":         rec.get("label", ""),
            "token_count":   len(rec.get("access_tokens", [])),
            "created_at":    rec.get("created_at", ""),
            "active":        rec.get("active", False),
        })
    return result


def validate(api_key: str, access_token: str) -> bool:
    """
    Return True if api_key is active AND access_token belongs to it.
    Called on every incoming WebSocket connection — must be fast.
    """
    with _lock:
        data = _load()
        rec  = data.get(api_key)

    if not rec or not rec.get("active"):
        return False
    return access_token in rec.get("access_tokens", [])


def ensure_default_key() -> Optional[dict]:
    """
    If no keys exist at all, create one default key so the server is
    immediately usable after a fresh deploy.  Returns the new record
    (so the startup log can print the credentials), or None if keys
    already exist.
    """
    with _lock:
        data = _load()
        if data:
            return None

    record = create_key(label="default")
    logger.warning(
        "No API keys found — created default key.\n"
        "  api_key      = %s\n"
        "  access_token = %s\n"
        "  Save these now — access_token is not shown again.",
        record["api_key"],
        record["access_tokens"][0],
    )
    return record
