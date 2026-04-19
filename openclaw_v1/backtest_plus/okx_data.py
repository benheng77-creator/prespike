"""
OKX historical data fetcher — candles, funding rates, synthetic ticks.

Public endpoints only (no API key needed):
  /api/v5/market/history-candles   → OHLCV
  /api/v5/public/funding-rate-history → funding
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("backtest_plus.okx_data")

OKX_BASE = "https://www.okx.com"


def _get(path: str, params: dict[str, str], retries: int = 3) -> list:
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v)
    url = f"{OKX_BASE}{path}?{qs}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("code") == "0":
                return body.get("data") or []
            log.warning("OKX error: %s", body.get("msg"))
            return []
        except Exception as exc:
            log.warning("OKX fetch attempt %d failed: %s", attempt + 1, exc)
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    return []


@dataclass
class Candle:
    ts: int         # unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float   # base currency


@dataclass
class FundingSnapshot:
    ts: int
    rate: float


@dataclass
class SyntheticTick:
    ts: int
    price: float
    size: float
    side: str       # "BUY" | "SELL"


def fetch_candles(
    inst_id: str = "BTC-USDT-SWAP",
    bar: str = "5m",
    days: int = 90,
    end_ts_ms: Optional[int] = None,
) -> list[Candle]:
    """Fetch OHLCV candles using ccxt (handles OKX auth/headers).

    Falls back to urllib if ccxt is not installed.
    """
    try:
        import ccxt
        return _fetch_candles_ccxt(inst_id, bar, days, end_ts_ms)
    except ImportError:
        return _fetch_candles_urllib(inst_id, bar, days, end_ts_ms)


def _fetch_candles_ccxt(
    inst_id: str, bar: str, days: int, end_ts_ms: Optional[int],
) -> list[Candle]:
    import ccxt
    ex = ccxt.okx({"enableRateLimit": True})
    end = end_ts_ms or int(time.time() * 1000)
    start = end - days * 86_400_000
    all_candles: list[Candle] = []
    since = start
    tf_map = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
    tf = tf_map.get(bar, bar)
    while since < end:
        try:
            rows = ex.fetch_ohlcv(inst_id, timeframe=tf, since=since, limit=300)
        except Exception as exc:
            log.warning("ccxt fetch_ohlcv failed: %s", exc)
            break
        if not rows:
            break
        for r in rows:
            all_candles.append(Candle(int(r[0]), float(r[1]), float(r[2]),
                                      float(r[3]), float(r[4]), float(r[5])))
        since = int(rows[-1][0]) + 1
        time.sleep(0.1)
    all_candles = [c for c in all_candles if start <= c.ts <= end]
    all_candles.sort(key=lambda c: c.ts)
    log.info("Fetched %d candles via ccxt (%s, %s, %d days)", len(all_candles), inst_id, bar, days)
    return all_candles


def _fetch_candles_urllib(
    inst_id: str, bar: str, days: int, end_ts_ms: Optional[int],
) -> list[Candle]:
    end = end_ts_ms or int(time.time() * 1000)
    start = end - days * 86_400_000
    all_candles: list[Candle] = []
    after = ""
    while True:
        params = {"instId": inst_id, "bar": bar, "limit": "100"}
        if after:
            params["after"] = after
        rows = _get("/api/v5/market/history-candles", params)
        if not rows:
            break
        for r in rows:
            ts = int(r[0])
            all_candles.append(Candle(ts, float(r[1]), float(r[2]),
                                      float(r[3]), float(r[4]), float(r[5])))
        oldest_ts = int(rows[-1][0])
        if oldest_ts <= start:
            break
        after = str(oldest_ts)
        time.sleep(0.15)
    all_candles = [c for c in all_candles if c.ts >= start]
    all_candles.sort(key=lambda c: c.ts)
    log.info("Fetched %d candles via urllib (%s, %s, %d days)", len(all_candles), inst_id, bar, days)
    return all_candles


def fetch_funding_history(
    inst_id: str = "BTC-USDT-SWAP",
    days: int = 90,
    end_ts_ms: Optional[int] = None,
) -> list[FundingSnapshot]:
    """Fetch funding rates using ccxt, fallback to urllib."""
    try:
        import ccxt
        return _fetch_funding_ccxt(inst_id, days, end_ts_ms)
    except ImportError:
        return _fetch_funding_urllib(inst_id, days, end_ts_ms)


def _fetch_funding_ccxt(
    inst_id: str, days: int, end_ts_ms: Optional[int],
) -> list[FundingSnapshot]:
    import ccxt
    ex = ccxt.okx({"enableRateLimit": True})
    end = end_ts_ms or int(time.time() * 1000)
    start = end - days * 86_400_000
    all_rates: list[FundingSnapshot] = []
    since = start
    while since < end:
        try:
            rows = ex.fetch_funding_rate_history(inst_id, since=since, limit=100)
        except Exception as exc:
            log.warning("ccxt funding fetch failed: %s", exc)
            break
        if not rows:
            break
        for r in rows:
            ts = int(r.get("timestamp") or r.get("datetime") or 0)
            if isinstance(ts, str):
                ts = int(time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) * 1000)
            rate = float(r.get("fundingRate") or 0)
            all_rates.append(FundingSnapshot(ts, rate))
        since = max(int(r.get("timestamp") or 0) for r in rows) + 1
        time.sleep(0.1)
    all_rates = [f for f in all_rates if start <= f.ts <= end]
    all_rates.sort(key=lambda f: f.ts)
    log.info("Fetched %d funding snapshots via ccxt (%s, %d days)", len(all_rates), inst_id, days)
    return all_rates


def _fetch_funding_urllib(
    inst_id: str, days: int, end_ts_ms: Optional[int],
) -> list[FundingSnapshot]:
    end = end_ts_ms or int(time.time() * 1000)
    start = end - days * 86_400_000
    all_rates: list[FundingSnapshot] = []
    after = ""
    while True:
        params = {"instId": inst_id, "limit": "100"}
        if after:
            params["after"] = after
        rows = _get("/api/v5/public/funding-rate-history", params)
        if not rows:
            break
        for r in rows:
            ts = int(r.get("fundingTime") or 0)
            rate = float(r.get("fundingRate") or 0)
            all_rates.append(FundingSnapshot(ts, rate))
        oldest_ts = min(int(r.get("fundingTime") or 0) for r in rows)
        if oldest_ts <= start:
            break
        after = str(oldest_ts)
        time.sleep(0.15)
    all_rates = [f for f in all_rates if f.ts >= start]
    all_rates.sort(key=lambda f: f.ts)
    log.info("Fetched %d funding snapshots via urllib (%s, %d days)", len(all_rates), inst_id, days)
    return all_rates


def synthesize_ticks_from_candles(
    candles: list[Candle],
    ticks_per_candle: int = 50,
    seed: int = 42,
) -> list[SyntheticTick]:
    """Generate realistic trade ticks from OHLCV candles.

    Within each candle, we distribute volume across ticks following a
    Brownian bridge from open→high→low→close (or open→low→high→close
    depending on whether it's a green or red candle).

    The side assignment is derived from price movement within the candle:
    rising sub-segments → more BUYs, falling → more SELLs. This produces
    realistic CVD behaviour that reflects actual market microstructure.

    LIMITATION: This is a conservative approximation. Real tick-level CVD
    would be more noisy. We document this in the report methodology.
    """
    rng = random.Random(seed)
    all_ticks: list[SyntheticTick] = []
    for candle in candles:
        bar_ms = 5 * 60 * 1000  # 5m default
        vol_per_tick = max(0.001, candle.volume / ticks_per_candle)
        is_green = candle.close >= candle.open

        if is_green:
            path = _brownian_bridge(candle.open, candle.low, candle.high,
                                     candle.close, ticks_per_candle, rng)
        else:
            path = _brownian_bridge(candle.open, candle.high, candle.low,
                                     candle.close, ticks_per_candle, rng)

        for i, price in enumerate(path):
            tick_ts = candle.ts + int(i * bar_ms / ticks_per_candle)
            if i > 0:
                side = "BUY" if price > path[i - 1] else "SELL"
            else:
                side = "BUY" if is_green else "SELL"
            size = vol_per_tick * (0.5 + rng.random())
            all_ticks.append(SyntheticTick(tick_ts, price, size, side))

    log.info("Synthesized %d ticks from %d candles", len(all_ticks), len(candles))
    return all_ticks


def _brownian_bridge(
    start: float, wp1: float, wp2: float, end: float,
    n: int, rng: random.Random,
) -> list[float]:
    """4-point price path: start → waypoint1 → waypoint2 → end."""
    path: list[float] = []
    n1, n2, n3 = n // 3, n // 3, n - 2 * (n // 3)
    for seg_start, seg_end, seg_n in [
        (start, wp1, n1), (wp1, wp2, n2), (wp2, end, n3),
    ]:
        for j in range(seg_n):
            t = j / max(1, seg_n - 1)
            interp = seg_start + (seg_end - seg_start) * t
            noise = rng.gauss(0, abs(seg_end - seg_start) * 0.1 + 1e-6)
            path.append(max(1e-6, interp + noise))
    return path


def get_funding_at(funding_history: list[FundingSnapshot], ts_ms: int) -> float:
    """Return the most recent funding rate at or before ts_ms."""
    best = 0.0
    for f in funding_history:
        if f.ts <= ts_ms:
            best = f.rate
        else:
            break
    return best
