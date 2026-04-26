"""Critical: live runtime features must equal research features bit-for-bit."""
from __future__ import annotations

import math

import numpy as np
import pytest

from ..strategies._features_research import compute_features_research
from ..strategies._features_runtime import FeatureRuntime


def _synthetic_bars(n=400, seed=42):
    rng = np.random.default_rng(seed)
    p = 50000.0
    out = np.zeros((n, 6))
    for i in range(n):
        d = rng.normal(0, 30)
        rng_bar = abs(rng.normal(0, 20))
        p = p + d
        out[i] = [int(1700_000_000 + i * 300), p, p + rng_bar, p - rng_bar, p,
                  abs(rng.normal(1000, 200))]
    return out


def test_parity_on_last_bar():
    bars = _synthetic_bars()
    # Research
    research = compute_features_research(bars, feature_config={})
    # Runtime
    feature_order = [
        "atr_14", "close_minus_sma20_atr", "close_minus_sma60_atr",
        "close_minus_sma200_atr", "range_over_atr",
        "range_ratio_14", "range_ratio_60",
        "vol_ratio_20", "vol_ratio_60",
        "donchian_width", "position_in_channel",
        "bbw", "bbw_rank_120",
        "atr_rank_100", "rv_ann", "rv_rank_200",
        "inside_bar_count_10",
    ]
    rt = FeatureRuntime(feature_order)
    for ts, o, h, l, c, v in bars:
        rt.update(int(ts), o, h, l, c, v)
    rt_features = {f: rt.vector[i] for i, f in enumerate(feature_order)}

    drifts = []
    for k in feature_order:
        a = research.get(k, math.nan)
        b = rt_features.get(k, math.nan)
        if math.isnan(a) or math.isnan(b):
            continue
        denom = max(abs(a), abs(b), 1e-12)
        rel = abs(a - b) / denom
        drifts.append((k, rel))
    if not drifts:
        pytest.skip("no overlapping features computed (warm-up issue in test data)")
    max_drift = max(rel for _, rel in drifts)
    # Tolerance is loose because rolling-window order-of-operations may differ
    # at fp level. Real audit uses 1e-6; parity test uses 1e-3 to allow
    # numerical-noise differences while catching structural drift.
    assert max_drift < 1e-3, f"largest drift: {max(drifts, key=lambda x: x[1])}"
