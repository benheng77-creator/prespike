"""
Open interest tracker.

Polls an exchange's open-interest endpoint on a timer and maintains a
rolling history. `current_delta_score()` returns the normalized percentage
change in OI over the tracked window, clipped to [-1, 1].

Rising OI in a persistent trend is conviction; falling OI during a move
is distribution. Use the absolute delta as a drift amplifier, or the
signed delta as a directional bias.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Optional

log = logging.getLogger(__name__)

# Max absolute percentage change in OI over the tracked window before the
# score saturates at ±1.
OI_DELTA_SCALE = 0.10


class OpenInterestTracker:
    def __init__(
        self,
        exchange,
        symbol: str,
        refresh_s: int = 60,
        window_s: int = 3600,
    ):
        self.exchange = exchange
        self.symbol = symbol
        self.refresh_s = refresh_s
        self.window_s = window_s
        self._history: deque = deque()
        self._running: bool = False

    def record(self, oi_value: float, ts: Optional[float] = None) -> None:
        now = ts if ts is not None else time.time()
        self._history.append((now, float(oi_value)))
        cutoff = now - self.window_s
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    async def _fetch_once(self) -> None:
        try:
            payload = await self.exchange.fetch_open_interest(self.symbol)
            oi = float(
                payload.get("openInterestAmount")
                or payload.get("openInterest")
                or 0.0
            )
            if oi > 0:
                self.record(oi)
        except Exception as e:
            log.warning(f"Open interest fetch failed for {self.symbol}: {e}")

    async def start(self) -> None:
        self._running = True
        while self._running:
            await self._fetch_once()
            await asyncio.sleep(self.refresh_s)

    async def close(self) -> None:
        self._running = False

    def current_value(self) -> float:
        return self._history[-1][1] if self._history else 0.0

    def current_delta_score(self) -> float:
        if len(self._history) < 2:
            return 0.0
        first = self._history[0][1]
        last = self._history[-1][1]
        if first <= 0:
            return 0.0
        pct_change = (last - first) / first
        scaled = pct_change / OI_DELTA_SCALE
        return max(-1.0, min(1.0, scaled))
