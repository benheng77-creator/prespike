"""
Real-time order flow derived from the exchange trade tape.

Tracks signed trade volume over a rolling time window and exposes a
normalized flow sentiment in [-1, 1] plus coverage/freshness metrics.
Positive values mean aggressive buyers dominated, negative means
aggressive sellers dominated.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Tuple


class FlowTracker:
    def __init__(self, window_s: int = 300):
        self.window_s = window_s
        self._trades: deque = deque()

    def record_trades(self, trades) -> None:
        now = time.time()
        for t in trades:
            vol = float(t.get("amount") or 0.0)
            if vol == 0.0:
                continue
            signed = vol if t.get("side") == "buy" else -vol
            self._trades.append((now, signed))

        cutoff = now - self.window_s
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()

    def current(self) -> Tuple[float, float, float]:
        """Returns (flow_sent, coverage, freshness)."""
        if not self._trades:
            return 0.0, 0.0, 0.0

        total = sum(abs(v) for _, v in self._trades)
        if total == 0.0:
            return 0.0, 0.0, 0.0

        net = sum(v for _, v in self._trades)
        flow = max(-1.0, min(1.0, net / total))
        coverage = min(1.0, len(self._trades) / 100.0)

        last_ts = self._trades[-1][0]
        age = time.time() - last_ts
        freshness = max(0.0, 1.0 - age / 60.0)

        return flow, coverage, freshness
