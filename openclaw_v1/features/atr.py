"""
Average True Range over a rolling window of 1-minute OHLC bars.

ATR is used to size stops and targets proportional to realised volatility,
so that `RRTrue` in the decision engine is derived from the market's own
movement scale instead of a fixed percentage.
"""

from __future__ import annotations

from typing import Sequence


def compute_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float:
    n = len(closes)
    if n < period + 1 or len(highs) != n or len(lows) != n:
        return 0.0

    trs = []
    for i in range(n - period, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)

    return sum(trs) / len(trs)
