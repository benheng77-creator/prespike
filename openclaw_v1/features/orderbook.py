"""
Order book imbalance tracker.

Consumes orderbook snapshots from a ccxt.pro `watch_order_book` feed and
maintains a rolling top-N-level bid/ask volume imbalance in [-1, 1]:

    imbalance = (bid_vol_topN - ask_vol_topN) / (bid_vol_topN + ask_vol_topN)

Positive values mean the bid side is thicker (buy pressure), negative values
mean the ask side is thicker (sell pressure). Exposes freshness in [0, 1]
that decays over 60 seconds since the last update.
"""

from __future__ import annotations

import time
from typing import Tuple


class OrderBookTracker:
    def __init__(self, depth_levels: int = 20):
        self.depth_levels = depth_levels
        self._imbalance: float = 0.0
        self._last_update: float = 0.0

    def record_orderbook(self, orderbook: dict) -> None:
        bids = orderbook.get("bids") or []
        asks = orderbook.get("asks") or []
        bids = bids[: self.depth_levels]
        asks = asks[: self.depth_levels]
        if not bids or not asks:
            return

        bid_vol = sum(float(b[1]) for b in bids if len(b) >= 2)
        ask_vol = sum(float(a[1]) for a in asks if len(a) >= 2)
        total = bid_vol + ask_vol
        if total <= 0:
            return

        self._imbalance = (bid_vol - ask_vol) / total
        self._last_update = time.time()

    def current_imbalance(self) -> float:
        return self._imbalance

    def current_freshness(self) -> float:
        if self._last_update == 0.0:
            return 0.0
        age = time.time() - self._last_update
        return max(0.0, 1.0 - age / 60.0)

    def snapshot(self) -> Tuple[float, float]:
        """Returns (imbalance, freshness)."""
        return self.current_imbalance(), self.current_freshness()
