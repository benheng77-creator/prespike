"""
Regime statistics for the Bayesian-shrunk win-rate input.

Computes how often a simple trailing-return momentum prediction correctly
called the next-`horizon` bar over the most recent `lookback` bars. The
return value is fed directly to the decision engine as (Samples, Regime),
and a fixed prior (ShrinkN, BaseWR) is layered on top inside the engine.
"""

from __future__ import annotations

from typing import Sequence, Tuple


def compute_regime_stats(
    closes: Sequence[float],
    lookback: int = 100,
    horizon: int = 5,
    signal_window: int = 5,
) -> Tuple[float, float]:
    min_required = lookback + horizon + signal_window
    if len(closes) < min_required:
        return 0.0, 0.5

    hits = 0
    total = 0
    start = len(closes) - lookback - horizon
    end = len(closes) - horizon

    for i in range(start, end):
        if i - signal_window < 0:
            continue
        if closes[i - signal_window] <= 0 or closes[i] <= 0:
            continue

        recent_return = closes[i] - closes[i - signal_window]
        future_return = closes[i + horizon] - closes[i]

        pred = 1 if recent_return > 0 else -1 if recent_return < 0 else 0
        actual = 1 if future_return > 0 else -1 if future_return < 0 else 0

        if pred == 0:
            continue
        total += 1
        if pred == actual:
            hits += 1

    if total == 0:
        return 0.0, 0.5

    return float(total), hits / total
