"""
Perpetual funding rate tracker.

On perp exchanges (Binance futures, Bybit, OKX, etc.), funding is paid every
few hours from longs → shorts (positive) or shorts → longs (negative). It is
a near-pure positioning signal: persistently high positive funding usually
means longs are crowded and vulnerable to a flush; persistently negative
funding the reverse.

`current_score()` returns a normalized funding in [-1, 1], where ±1 maps to
roughly a 0.1%-per-period funding rate (the upper bound for healthy markets).
Treat the score as *contrarian* in live blending: positive funding nudges
FlowSent down (bearish short-term), negative funding nudges it up.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# Roughly the typical max absolute funding rate per 8-hour period on major
# perps before it's considered extreme. Used to rescale raw rates to [-1, 1].
FUNDING_SCALE = 0.001


class FundingTracker:
    def __init__(self, exchange, symbol: str, refresh_s: int = 60):
        self.exchange = exchange
        self.symbol = symbol
        self.refresh_s = refresh_s
        self._last_rate: float = 0.0
        self._last_refresh: float = 0.0
        self._running: bool = False

    def record(self, rate: float) -> None:
        self._last_rate = float(rate)
        self._last_refresh = time.time()

    async def _fetch_once(self) -> None:
        try:
            payload = await self.exchange.fetch_funding_rate(self.symbol)
            rate = float(payload.get("fundingRate") or 0.0)
            self.record(rate)
        except Exception as e:
            log.warning(f"Funding rate fetch failed for {self.symbol}: {e}")

    async def start(self) -> None:
        self._running = True
        while self._running:
            await self._fetch_once()
            await asyncio.sleep(self.refresh_s)

    async def close(self) -> None:
        self._running = False

    def current_rate(self) -> float:
        return self._last_rate

    def current_score(self) -> float:
        capped = max(-FUNDING_SCALE, min(FUNDING_SCALE, self._last_rate))
        return capped / FUNDING_SCALE

    def current_freshness(self) -> float:
        if self._last_refresh == 0.0:
            return 0.0
        age = time.time() - self._last_refresh
        return max(0.0, 1.0 - age / (self.refresh_s * 4))
