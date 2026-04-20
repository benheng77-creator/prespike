"""Pull historical 1m candles from OKX + Crypto.com public REST.

Both endpoints are free + unauthenticated. Candle shape normalized to:
    [ts_ms, open, high, low, close, volume_base]

Cached to disk under openclaw_v1/runtime/backtest/cache/<exchange>_<sym>_<tf>.json
Re-run loads from cache unless force_refresh=True.

Rate-limited: sleeps between paged calls to stay under exchange limits.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


OKX_BASE = "https://www.okx.com"
CDC_BASE = "https://api.crypto.com/exchange"

REPO_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = REPO_ROOT / "runtime" / "backtest" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Symbol naming — OKX uses BTC-USDT, Crypto.com uses BTC_USDT
# ---------------------------------------------------------------------------

def _cdc_instrument(sym: str) -> str:
    return sym.replace("-", "_")


def _http_get_json(url: str, timeout: float = 10.0) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "opp-fabric-backtest/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None


# ---------------------------------------------------------------------------
# OKX candles
# ---------------------------------------------------------------------------

def fetch_okx_candles(
    symbol: str,
    bar: str = "1m",
    total_bars: int = 10_000,
    rate_limit_s: float = 0.25,
) -> list[list[float]]:
    """Pull up to total_bars 1m candles from OKX using /history-candles
    (up to 100 per call, backwards-paged).

    Returns: list of [ts_ms, open, high, low, close, vol_base]
    Sorted oldest-first.
    """
    rows: list[list[float]] = []
    after_ms = ""                    # empty = newest; sets to oldest ts each loop
    per_call = 100                    # OKX max for history-candles
    calls = max(1, total_bars // per_call + 1)
    for _ in range(calls):
        url = (
            f"{OKX_BASE}/api/v5/market/history-candles"
            f"?instId={symbol}&bar={bar}&limit={per_call}"
        )
        if after_ms:
            url += f"&after={after_ms}"
        body = _http_get_json(url)
        if not body or str(body.get("code")) != "0":
            break
        data = body.get("data") or []
        if not data:
            break
        # OKX returns newest-first: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        for r in data:
            ts = int(r[0])
            rows.append([ts, float(r[1]), float(r[2]),
                         float(r[3]), float(r[4]), float(r[5])])
        after_ms = str(data[-1][0])   # oldest in this batch -> page further back
        time.sleep(rate_limit_s)
        if len(rows) >= total_bars:
            break
    # Sort oldest-first, dedupe on ts.
    by_ts: dict[int, list[float]] = {}
    for r in rows:
        by_ts[int(r[0])] = r
    return sorted(by_ts.values(), key=lambda x: x[0])


# ---------------------------------------------------------------------------
# Crypto.com candles
# ---------------------------------------------------------------------------

def fetch_cdc_candles(
    symbol: str,
    timeframe: str = "1m",
    count: int = 1000,
    rate_limit_s: float = 0.25,
) -> list[list[float]]:
    """Pull 1m candles from Crypto.com /exchange/v1/public/get-candlestick.

    Single call returns up to count bars (max ~1000). No pagination param
    documented; caller loops with end_ts to get more if needed.

    Returns: list of [ts_ms, open, high, low, close, vol_base]
    """
    rows: list[list[float]] = []
    end_ts = int(time.time() * 1000)
    inst = _cdc_instrument(symbol)
    max_calls = 15                    # safety
    for _ in range(max_calls):
        url = (
            f"{CDC_BASE}/v1/public/get-candlestick"
            f"?instrument_name={inst}&timeframe={timeframe}&count={count}"
            f"&end_ts={end_ts}"
        )
        body = _http_get_json(url)
        if not body or str(body.get("code")) != "0":
            break
        data = (body.get("result") or {}).get("data") or []
        if not data:
            break
        for r in data:
            # CDC returns dicts with t/o/h/l/c/v keys.
            ts = int(r["t"])
            rows.append([
                ts, float(r["o"]), float(r["h"]),
                float(r["l"]), float(r["c"]),
                float(r.get("v") or 0),
            ])
        # Oldest row timestamp for next page.
        oldest_ts = min(int(r["t"]) for r in data)
        if oldest_ts >= end_ts:
            break
        end_ts = oldest_ts - 1
        time.sleep(rate_limit_s)
    by_ts: dict[int, list[float]] = {}
    for r in rows:
        by_ts[int(r[0])] = r
    return sorted(by_ts.values(), key=lambda x: x[0])


# ---------------------------------------------------------------------------
# Cache wrappers
# ---------------------------------------------------------------------------

def _cache_path(exchange: str, symbol: str, bar: str) -> Path:
    return CACHE_DIR / f"{exchange}_{symbol.replace('-','').replace('_','')}_{bar}.json"


@dataclass
class CandleSet:
    exchange: str
    symbol: str
    bar: str
    rows: list[list[float]]

    @property
    def n(self) -> int:
        return len(self.rows)

    @property
    def first_ts_ms(self) -> int | None:
        return int(self.rows[0][0]) if self.rows else None

    @property
    def last_ts_ms(self) -> int | None:
        return int(self.rows[-1][0]) if self.rows else None


def load_or_fetch(
    exchange: str,
    symbol: str,
    bar: str = "1m",
    total_bars: int = 10_000,
    force_refresh: bool = False,
) -> CandleSet:
    path = _cache_path(exchange, symbol, bar)
    if path.exists() and not force_refresh:
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            return CandleSet(exchange=exchange, symbol=symbol, bar=bar,
                             rows=blob.get("rows") or [])
        except Exception:
            pass

    if exchange == "okx":
        rows = fetch_okx_candles(symbol, bar=bar, total_bars=total_bars)
    elif exchange == "cdc":
        rows = fetch_cdc_candles(symbol, timeframe=bar, count=1000)
    else:
        raise ValueError(f"unknown exchange {exchange!r}")

    blob = {
        "exchange": exchange, "symbol": symbol, "bar": bar,
        "fetched_ts_ms": int(time.time() * 1000),
        "n": len(rows), "rows": rows,
    }
    path.write_text(json.dumps(blob), encoding="utf-8")
    return CandleSet(exchange=exchange, symbol=symbol, bar=bar, rows=rows)
