"""
Exchange factory — pick the right ccxt client from env, using the
per-exchange key slots populated via the Ops UI / .env.

All exchanges supported: binance, coinbase, okx, cryptocom,
independentreserve (and moomoo, which is a separate code path entirely —
see core.exchange_moomoo).

Additive: existing core.exchange.ExecutionEngine continues to work with
its own bootstrap. This module is what you call when you want the
factory to pick up new credentials without touching any existing code.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)


# Map a friendly id → (ccxt module id, env var key prefix)
EXCHANGES: dict[str, dict[str, Any]] = {
    "binance":            {"ccxt": "binance",            "prefix": None},          # uses API_KEY / API_SECRET
    "coinbase":           {"ccxt": "coinbase",           "prefix": "COINBASE",     "passphrase": True},
    "okx":                {"ccxt": "okx",                "prefix": "OKX",          "passphrase": True},
    "cryptocom":          {"ccxt": "cryptocom",          "prefix": "CRYPTOCOM",    "passphrase": False},
    "independentreserve": {"ccxt": "independentreserve", "prefix": "INDEPENDENTRESERVE", "passphrase": False},
}


def available() -> list[str]:
    return list(EXCHANGES.keys())


def _get(env_key: str) -> Optional[str]:
    v = os.environ.get(env_key)
    return v.strip() if v and v.strip() else None


def credentials_for(exchange_id: str) -> dict[str, Optional[str]]:
    """
    Return raw credentials for the given exchange id. Values are None when
    not configured. Useful for health checks before attempting a client.
    """
    meta = EXCHANGES.get(exchange_id.lower())
    if meta is None:
        return {"apiKey": None, "secret": None, "password": None}
    prefix = meta["prefix"]
    if prefix is None:
        return {"apiKey": _get("API_KEY"), "secret": _get("API_SECRET"), "password": None}
    return {
        "apiKey":   _get(f"{prefix}_API_KEY"),
        "secret":   _get(f"{prefix}_API_SECRET"),
        "password": _get(f"{prefix}_PASSPHRASE") if meta.get("passphrase") else None,
    }


def is_configured(exchange_id: str) -> bool:
    c = credentials_for(exchange_id)
    if c["apiKey"] is None or c["secret"] is None:
        return False
    meta = EXCHANGES.get(exchange_id.lower(), {})
    if meta.get("passphrase") and c["password"] is None:
        return False
    return True


def active_exchange_id() -> str:
    return (os.environ.get("ACTIVE_EXCHANGE") or "binance").strip().lower()


def build_client(exchange_id: Optional[str] = None, *, sandbox: bool = False) -> Any:
    """
    Build and return a configured ccxt client.

    Raises RuntimeError when credentials are missing — callers should
    check `is_configured()` first if they need to fall back to paper.
    """
    try:
        import ccxt  # type: ignore
    except Exception as e:  # pragma: no cover — env without ccxt
        raise RuntimeError(f"ccxt not installed: {e}")

    eid = (exchange_id or active_exchange_id()).lower()
    meta = EXCHANGES.get(eid)
    if meta is None:
        raise RuntimeError(f"unsupported exchange: {eid}")

    cls = getattr(ccxt, meta["ccxt"], None)
    if cls is None:
        raise RuntimeError(f"ccxt has no client for {meta['ccxt']}")

    creds = credentials_for(eid)
    if not is_configured(eid):
        raise RuntimeError(f"{eid} is not fully configured — fill keys in Ops › Keys")

    opts: dict[str, Any] = {
        "apiKey": creds["apiKey"],
        "secret": creds["secret"],
        "enableRateLimit": True,
    }
    if creds["password"] is not None:
        opts["password"] = creds["password"]
    client = cls(opts)
    if sandbox and hasattr(client, "set_sandbox_mode"):
        try:
            client.set_sandbox_mode(True)
        except Exception:
            pass
    return client


def summary() -> dict[str, Any]:
    """Per-exchange configured flag + currently active id — for /status."""
    return {
        "active": active_exchange_id(),
        "configured": {eid: is_configured(eid) for eid in EXCHANGES},
        "supported": list(EXCHANGES.keys()),
    }
