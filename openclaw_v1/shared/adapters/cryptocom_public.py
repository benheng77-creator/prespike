"""Phase 11n-9-qq — Crypto.com public-API adapter (read-only).

Option B scope: comparison feed ONLY. No auth, no orders, no balance
polling, no execution integration. Fetches ticker + orderbook-depth
from Crypto.com's public REST endpoints for parallel visibility
against OKX.

Source: https://exchange-docs.crypto.com/exchange/v1/rest-ws/
Public endpoints used:
  GET /v1/public/get-ticker?instrument_name=ENA_USDT
  GET /v1/public/get-book?instrument_name=ENA_USDT&depth=20

Symbol format: BASE_USDT (underscore). OKX uses BASE-USDT (hyphen).
This module owns the translation. Never calls private endpoints.
Never places orders. Never reads balances.

Fail-open: any HTTP/parse error returns None. Logged once, not raised.
Caller (exchange_comparison_feed) decides how to handle absent data.
"""
from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)

CDC_BASE_URL = "https://api.crypto.com/exchange/v1"
CDC_HTTP_TIMEOUT_S = 4.0
CDC_USER_AGENT = "spot-aggro/cdc-readonly/1.0"


def _okx_to_cdc_symbol(okx_symbol: str) -> str:
    """OKX ENA-USDT -> CDC ENA_USDT."""
    return okx_symbol.replace("-", "_")


def _cdc_to_okx_symbol(cdc_symbol: str) -> str:
    """CDC ENA_USDT -> OKX ENA-USDT."""
    return cdc_symbol.replace("_", "-")


def _http_get_json(url: str) -> dict[str, Any] | None:
    """Sync HTTP GET returning parsed JSON, or None on any failure."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": CDC_USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=CDC_HTTP_TIMEOUT_S) as resp:
            if resp.getcode() != 200:
                return None
            import json as _json
            return _json.loads(resp.read(65536))
    except Exception as exc:
        log.debug("cdc http_get failed %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------

@dataclass
class CdcTicker:
    symbol: str                 # canonical OKX-style (ENA-USDT)
    last: float
    bid: float
    ask: float
    spread_bp: float
    volume_24h: float | None = None
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CdcDepthSnapshot:
    symbol: str
    bids: list[tuple[float, float]]   # (price, size) descending
    asks: list[tuple[float, float]]   # (price, size) ascending
    bid_depth_usd: float               # sum of top N bid notional
    ask_depth_usd: float               # sum of top N ask notional
    top_depth_usd: float               # min(bid, ask) — conservative fill estimate
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bids"] = [list(b) for b in self.bids[:5]]    # cap serialized depth
        d["asks"] = [list(a) for a in self.asks[:5]]
        return d


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_ticker_sync(okx_symbol: str) -> CdcTicker | None:
    """Fetch current ticker for a symbol. Sync version for threadpool usage.
    Returns None if the symbol is unknown to CDC or request fails.

    Note: CDC v1 endpoint is `get-tickers` (plural) — the singular form
    returns HTTP 404. Docs occasionally reference the singular; the
    working v1 Exchange-API path uses plural.
    """
    cdc_sym = _okx_to_cdc_symbol(okx_symbol)
    url = (
        f"{CDC_BASE_URL}/public/get-tickers"
        f"?instrument_name={urllib.parse.quote(cdc_sym)}"
    )
    body = _http_get_json(url)
    if not body:
        return None
    # CDC response shape: {"code":0, "result":{"data":[{"i":"ENA_USDT", "a":"0.1167", "b":"0.1166", "k":"0.1168", "v":"..."}...]}}
    try:
        if body.get("code") not in (0, "0"):
            return None
        rows = body.get("result", {}).get("data", [])
        if not rows:
            return None
        row = rows[0] if isinstance(rows, list) else rows
        last = float(row.get("a") or 0)
        bid = float(row.get("b") or 0)
        ask = float(row.get("k") or 0)
        vol = row.get("v")
        volume_24h = float(vol) if vol is not None else None
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
        spread_bp = (
            (ask - bid) / mid * 10_000.0
            if mid > 0 and ask > bid else 0.0
        )
        return CdcTicker(
            symbol=okx_symbol,
            last=last, bid=bid, ask=ask,
            spread_bp=round(spread_bp, 2),
            volume_24h=volume_24h,
        )
    except (TypeError, ValueError, KeyError) as exc:
        log.debug("cdc ticker parse failed %s: %s", okx_symbol, exc)
        return None


def fetch_depth_sync(
    okx_symbol: str, levels: int = 20,
) -> CdcDepthSnapshot | None:
    """Fetch orderbook top `levels` bids + asks, aggregate USD depth."""
    cdc_sym = _okx_to_cdc_symbol(okx_symbol)
    url = (
        f"{CDC_BASE_URL}/public/get-book"
        f"?instrument_name={urllib.parse.quote(cdc_sym)}"
        f"&depth={int(levels)}"
    )
    body = _http_get_json(url)
    if not body:
        return None
    try:
        if body.get("code") not in (0, "0"):
            return None
        rows = body.get("result", {}).get("data", [])
        if not rows:
            return None
        row = rows[0] if isinstance(rows, list) else rows
        bids_raw = row.get("bids") or []
        asks_raw = row.get("asks") or []
        # CDC format: [[price, size, n_orders], ...]
        bids = [
            (float(b[0]), float(b[1]))
            for b in bids_raw[:levels]
            if len(b) >= 2
        ]
        asks = [
            (float(a[0]), float(a[1]))
            for a in asks_raw[:levels]
            if len(a) >= 2
        ]
        bid_depth_usd = sum(p * q for p, q in bids)
        ask_depth_usd = sum(p * q for p, q in asks)
        top_depth_usd = min(bid_depth_usd, ask_depth_usd)
        return CdcDepthSnapshot(
            symbol=okx_symbol, bids=bids, asks=asks,
            bid_depth_usd=round(bid_depth_usd, 2),
            ask_depth_usd=round(ask_depth_usd, 2),
            top_depth_usd=round(top_depth_usd, 2),
        )
    except (TypeError, ValueError, KeyError) as exc:
        log.debug("cdc depth parse failed %s: %s", okx_symbol, exc)
        return None


async def fetch_ticker(okx_symbol: str) -> CdcTicker | None:
    return await asyncio.to_thread(fetch_ticker_sync, okx_symbol)


async def fetch_depth(okx_symbol: str, levels: int = 20) -> CdcDepthSnapshot | None:
    return await asyncio.to_thread(fetch_depth_sync, okx_symbol, levels)


async def fetch_ticker_and_depth(
    okx_symbol: str, levels: int = 20,
) -> tuple[CdcTicker | None, CdcDepthSnapshot | None]:
    """Fetch both concurrently. Each returns independently; partial
    results allowed (e.g. ticker OK + depth fail)."""
    t_task = fetch_ticker(okx_symbol)
    d_task = fetch_depth(okx_symbol, levels=levels)
    t, d = await asyncio.gather(t_task, d_task, return_exceptions=True)
    return (
        t if isinstance(t, CdcTicker) else None,
        d if isinstance(d, CdcDepthSnapshot) else None,
    )


def is_reachable() -> bool:
    """Quick liveness probe against get-tickers?instrument_name=BTC_USDT.
    Used by canary."""
    url = f"{CDC_BASE_URL}/public/get-tickers?instrument_name=BTC_USDT"
    body = _http_get_json(url)
    if not body:
        return False
    return body.get("code") in (0, "0")
