"""
Regime-drift detector for the rolling price series.

Standard-error-normalized rolling mean-shift: compare the short-window
mean return to the long-window mean return, normalize by the standard
error of the difference (not the raw stdev), and squash through tanh.
Output lives in [0, 1] where higher values indicate the short window has
drifted statistically away from the long-run distribution.

This is a tractable baseline. Production systems typically use ADWIN,
Page-Hinkley, or a formal change-point test — swap this implementation
once you want stronger guarantees.
"""

from __future__ import annotations

import math
from typing import Sequence


def compute_drift_score(
    closes: Sequence[float],
    short: int = 30,
    long: int = 240,
) -> float:
    if len(closes) < long + 1:
        return 0.0

    window = list(closes[-(long + 1):])
    returns = [
        (window[i + 1] - window[i]) / window[i]
        for i in range(len(window) - 1)
        if window[i] > 0
    ]
    if len(returns) < short:
        return 0.0

    n_long = len(returns)
    short_mean = sum(returns[-short:]) / short
    long_mean = sum(returns) / n_long
    long_var = sum((r - long_mean) ** 2 for r in returns) / n_long
    long_std = math.sqrt(long_var)
    if long_std == 0.0:
        return 0.0

    se_diff = long_std * math.sqrt(1.0 / short + 1.0 / n_long)
    if se_diff == 0.0:
        return 0.0

    z = abs(short_mean - long_mean) / se_diff
    # tanh(z/3): a ~3-sigma shift maps to ~0.76, leaving headroom above
    return max(0.0, min(1.0, math.tanh(z / 3.0)))
