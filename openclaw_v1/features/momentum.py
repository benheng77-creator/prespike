"""
Multi-timeframe directional features for the decision engine.

Given a sequence of closing prices sampled at 1-minute intervals, compute
normalized directional scores for 5m / 15m / 60m / 240m windows. Each score
is tanh(z) where z is (window_return) / (per-bar stdev * sqrt(window)), so
the output is naturally bounded in (-1, 1) and scales with both direction
and conviction.
"""

from __future__ import annotations

import math
from typing import Sequence, Tuple


def _directional(closes: Sequence[float], window: int) -> float:
    if len(closes) < window + 1:
        return 0.0

    recent = list(closes[-(window + 1):])
    returns = [
        (recent[i + 1] - recent[i]) / recent[i]
        for i in range(len(recent) - 1)
        if recent[i] > 0
    ]
    if not returns:
        return 0.0

    window_return = (recent[-1] - recent[0]) / recent[0]
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / len(returns)
    std = math.sqrt(var)
    if std == 0.0:
        return 0.0

    z = window_return / (std * math.sqrt(window))
    return max(-1.0, min(1.0, math.tanh(z)))


def compute_mtf_directions(
    closes: Sequence[float],
) -> Tuple[float, float, float, float]:
    """Return (D5, D15, D60, D240) from a sequence of 1-minute closes."""
    return (
        _directional(closes, 5),
        _directional(closes, 15),
        _directional(closes, 60),
        _directional(closes, 240),
    )
