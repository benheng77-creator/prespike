"""
Dynamic universe discovery — fetches every USDT-denominated perp from OKX
and returns them ranked by |z-score on 30d funding| × liquidity × spread.

Replaces the static 16-coin YAML list when cfg.universe_mode == "dynamic".
Cache: 8 hours (per funding settlement cycle). Background refresh job
lives in scheduler.universe_refresh.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Optional

from .config import load as load_cfg


log = logging.getLogger("ops.universe")

_lock = threading.Lock()
_cache: list["DiscoveredCoin"] = []
_cache_ts: int = 0
CACHE_TTL_S = 8 * 3600


# SQLite-backed cache so the engine process and the API/dashboard process
# share the same discovery result. In-memory cache above is still the fast
# path; SQLite is the cross-process source of truth.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS apex_universe_cache (
    id              INTEGER PRIMARY KEY CHECK (id=1),
    refreshed_ts    INTEGER NOT NULL,
    payload_json    TEXT NOT NULL
);
"""

_schema_initialized = False


def _init_schema() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    from ..persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()
    _schema_initialized = True


def _load_from_db() -> tuple[list["DiscoveredCoin"], int]:
    _init_schema()
    from ..persistence import state as persist
    con = persist._connect()
    try:
        r = con.execute("SELECT refreshed_ts, payload_json FROM apex_universe_cache WHERE id=1").fetchone()
    finally:
        con.close()
    if not r:
        return [], 0
    try:
        arr = json.loads(r["payload_json"])
    except Exception:
        return [], 0
    coins = [DiscoveredCoin(**c) for c in arr]
    return coins, int(r["refreshed_ts"])


def _save_to_db(coins: list["DiscoveredCoin"], ts: int) -> None:
    _init_schema()
    from ..persistence import state as persist
    payload = json.dumps([asdict(c) for c in coins], default=str)
    con = persist._connect()
    try:
        con.execute("DELETE FROM apex_universe_cache")
        con.execute("INSERT INTO apex_universe_cache (id, refreshed_ts, payload_json) VALUES (1, ?, ?)", (ts, payload))
        con.commit()
    finally:
        con.close()


@dataclass
class DiscoveredCoin:
    symbol: str            # e.g. "INJ-USDT"
    instrument: str        # OKX perp ID "INJ-USDT-SWAP"
    category: str          # heuristic: meme / l2 / alt_l1 / majors / unknown
    volume_24h_usd: float
    funding_rate: float
    open_interest_usd: float
    spread_bp: float
    score: float           # composite rank score


# ---------------------------------------------------------------------------
# Category heuristic — keep small, sync with config/apex_config.yml
# ---------------------------------------------------------------------------

_KNOWN_CATEGORY = {
    "BTC": "major", "ETH": "major", "SOL": "major",
    "WIF": "meme", "PEPE": "meme", "BONK": "meme", "FLOKI": "meme",
    "SHIB": "meme", "DOGE": "meme", "POPCAT": "meme", "MEW": "meme",
    "ARB": "l2", "OP": "l2", "STRK": "l2", "METIS": "l2", "MANTA": "l2",
    "SUI": "alt_l1", "SEI": "alt_l1", "INJ": "alt_l1", "TIA": "alt_l1",
    "APT": "alt_l1", "ATOM": "alt_l1", "NEAR": "alt_l1", "AVAX": "alt_l1",
    "JTO": "sol_eco", "JUP": "sol_eco", "PYTH": "sol_eco", "RNDR": "sol_eco",
    "ORDI": "brc20", "SATS": "brc20", "RATS": "brc20",
    "ENA": "defi", "RUNE": "defi", "UNI": "defi", "AAVE": "defi",
    "LDO": "defi", "DYDX": "defi", "GMX": "defi",
}


def classify(symbol: str) -> str:
    base = symbol.split("-")[0].upper().split("/")[0]
    return _KNOWN_CATEGORY.get(base, "unknown")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

async def discover(adapter: Any, *, force: bool = False) -> list[DiscoveredCoin]:
    """Return the ranked universe. Adapter is an OKXUnified instance."""
    global _cache, _cache_ts
    now = int(time.time())
    with _lock:
        if not force and _cache and (now - _cache_ts) < CACHE_TTL_S:
            return list(_cache)
        # Try the cross-process DB cache before making any exchange calls
        db_coins, db_ts = _load_from_db()
        if not force and db_coins and (now - db_ts) < CACHE_TTL_S:
            _cache = db_coins
            _cache_ts = db_ts
            return list(_cache)

    markets = await asyncio.to_thread(adapter._client.load_markets, True)
    candidates = [m for m in markets.values()
                  if isinstance(m, dict)
                  and m.get("swap")
                  and m.get("quote") == "USDT"
                  and m.get("active", True)]

    log.info("universe discovery: %d USDT perps available on OKX", len(candidates))

    # BULK endpoints: one call returns every ticker and every funding rate.
    # This avoids 580 individual requests and the resulting rate-limit storm.
    tickers: dict[str, Any] = {}
    try:
        tickers = await asyncio.to_thread(adapter._client.fetch_tickers,
                                           params={"instType": "SWAP"})
    except Exception as exc:
        log.warning("bulk fetch_tickers failed: %s", exc)

    results: list[DiscoveredCoin] = []
    for m in candidates:
        inst = m["id"] or m["symbol"]
        symbol = m.get("symbol") or inst               # ccxt-style e.g. "INJ/USDT:USDT"
        base = m.get("base") or ""
        base_symbol = f"{base}-USDT" if base else inst.replace("-SWAP", "")
        t = tickers.get(symbol) or tickers.get(inst) or {}
        info = t.get("info") or {}
        last = float(t.get("last") or t.get("close") or info.get("last") or 0)
        bid = float(t.get("bid") or info.get("bidPx") or 0)
        ask = float(t.get("ask") or info.get("askPx") or 0)
        # OKX perps: quoteVolume is None in ccxt; pull from info.volCcy24h (already USD)
        vol24 = float(
            t.get("quoteVolume")
            or info.get("volCcy24h")
            or (float(info.get("vol24h") or 0) * last)
            or 0
        )
        if vol24 <= 100_000:                   # ignore dust (<$100k 24h)
            continue
        spread_bp = ((ask - bid) / last * 1e4) if (last > 0 and bid > 0 and ask > 0) else 0.0
        # Funding rate: ticker info may not carry it — leave 0 and let
        # funding_hunt.rank_universe fetch it when the coin is actually probed.
        rate = 0.0
        for k in ("fundingRate", "nextFundingRate", "lastFundingRate"):
            v = info.get(k)
            if v not in (None, "", "0"):
                try:
                    rate = float(v)
                    break
                except Exception:
                    continue
        score = (abs(rate) * 1e4) * min(1.0, vol24 / 30_000_000)
        if spread_bp > 0:
            score /= (1 + spread_bp / 5)
        # Even without funding data, keep volume-based prelim rank for ranking pool
        if score == 0 and vol24 > 1_000_000:
            score = vol24 / 1e10
        results.append(DiscoveredCoin(
            symbol=base_symbol, instrument=inst,
            category=classify(base_symbol),
            volume_24h_usd=vol24, funding_rate=rate,
            open_interest_usd=0.0, spread_bp=spread_bp, score=score,
        ))

    results.sort(key=lambda c: -c.score)
    with _lock:
        _cache = results
        _cache_ts = now
    try:
        _save_to_db(results, now)
    except Exception:
        log.exception("universe cache persist failed (non-fatal)")
    log.info("universe discovery: %d coins probed successfully (persisted)", len(results))
    return results


def cached_universe() -> list[DiscoveredCoin]:
    """Read-only accessor. Falls through to DB when in-memory is empty so the
    API process sees what the engine process discovered."""
    global _cache, _cache_ts
    with _lock:
        if _cache:
            return list(_cache)
    try:
        coins, ts = _load_from_db()
    except Exception:
        return []
    if coins:
        with _lock:
            _cache = coins
            _cache_ts = ts
        return list(coins)
    return []


def cache_age_s() -> int:
    global _cache_ts
    if not _cache_ts:
        # Try DB for an authoritative timestamp
        try:
            _, ts = _load_from_db()
            if ts:
                _cache_ts = ts
        except Exception:
            return -1
    return max(0, int(time.time()) - _cache_ts) if _cache_ts else -1
