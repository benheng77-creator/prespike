"""
Canonical feature module.

This is the SAME math as the runtime, expressed in vectorized form. The
audit gate imports compute_features_research(), runs it on the full bar
window, and compares to runtime output. Bit-for-bit parity is required
(within fp tolerance).

Speed posture: vectorized numpy, no pandas. Used for both research training
and audit recompute.
"""
from __future__ import annotations

import math

import numpy as np


def _atr(h, l, c, n: int) -> np.ndarray:
    prev_c = np.concatenate(([math.nan], c[:-1]))
    tr = np.maximum.reduce([
        h - l,
        np.abs(h - prev_c),
        np.abs(l - prev_c),
    ])
    out = np.full(c.size, math.nan)
    if c.size >= n:
        # rolling mean
        c_tr = np.cumsum(np.where(np.isfinite(tr), tr, 0.0))
        for i in range(n - 1, c.size):
            out[i] = (c_tr[i] - (c_tr[i - n] if i >= n else 0.0)) / n
    return out


def _rolling_mean_std(x, n: int):
    m = np.full(x.size, math.nan)
    s = np.full(x.size, math.nan)
    for i in range(n - 1, x.size):
        win = x[i - n + 1:i + 1]
        m[i] = np.mean(win)
        s[i] = np.std(win, ddof=0)
    return m, s


def _rolling_max(x, n: int):
    out = np.full(x.size, math.nan)
    for i in range(n - 1, x.size):
        out[i] = np.max(x[i - n + 1:i + 1])
    return out


def _rolling_min(x, n: int):
    out = np.full(x.size, math.nan)
    for i in range(n - 1, x.size):
        out[i] = np.min(x[i - n + 1:i + 1])
    return out


def _percentile_rank_last(x: np.ndarray, w: int) -> float:
    if x.size < w:
        return math.nan
    win = x[-w:]
    if not np.all(np.isfinite(win)):
        return math.nan
    return float(np.sum(win <= win[-1])) / w


def compute_features_research(bars: np.ndarray, feature_config: dict) -> dict[str, float]:
    """bars: shape (N, 6) [ts, o, h, l, c, v]. Returns dict of last-bar features.

    Used by audit gate to independently recompute live features.
    """
    if bars.shape[0] < 200:
        return {}
    o = bars[:, 1].astype(np.float64)
    h = bars[:, 2].astype(np.float64)
    l = bars[:, 3].astype(np.float64)
    c = bars[:, 4].astype(np.float64)
    v = bars[:, 5].astype(np.float64)

    atr14 = _atr(h, l, c, 14)
    m20, s20 = _rolling_mean_std(c, 20)
    m60, _ = _rolling_mean_std(c, 60)
    m200, _ = _rolling_mean_std(c, 200)
    rng = h - l
    rng14, _ = _rolling_mean_std(rng, 14)
    rng60, _ = _rolling_mean_std(rng, 60)
    vol20, _ = _rolling_mean_std(v, 20)
    vol60, _ = _rolling_mean_std(v, 60)
    dh20 = _rolling_max(h, 20)
    dl20 = _rolling_min(l, 20)

    # log returns + RV
    log_c = np.log(np.where(c > 0, c, math.nan))
    lr = np.diff(log_c, prepend=math.nan)
    # RV over 30 bars, annualized for 5m bars (525600 mins/yr / 5 mins per bar)
    rv = np.full(c.size, math.nan)
    for i in range(30, c.size):
        win = lr[i - 30 + 1:i + 1]
        rv[i] = np.std(win, ddof=0) * math.sqrt(525600 / 5)

    # BBW
    bbw = np.full(c.size, math.nan)
    for i in range(c.size):
        if np.isfinite(s20[i]) and np.isfinite(m20[i]) and m20[i] > 0:
            bbw[i] = 4.0 * s20[i] / m20[i]

    # Inside bars
    inside = np.zeros(c.size)
    for i in range(1, c.size):
        if h[i] < h[i - 1] and l[i] > l[i - 1]:
            inside[i] = 1.0

    last = c.size - 1
    out: dict[str, float] = {}

    def _set(k, v):
        if v is None or not np.isfinite(v):
            out[k] = math.nan
        else:
            out[k] = float(v)

    _set("atr_14", atr14[last])
    if np.isfinite(atr14[last]) and atr14[last] > 0:
        _set("close_minus_sma20_atr", (c[last] - m20[last]) / atr14[last])
        _set("close_minus_sma60_atr", (c[last] - m60[last]) / atr14[last])
        _set("close_minus_sma200_atr", (c[last] - m200[last]) / atr14[last])
        _set("range_over_atr", rng[last] / atr14[last])
    if np.isfinite(rng14[last]) and rng14[last] > 0:
        _set("range_ratio_14", rng[last] / rng14[last])
    if np.isfinite(rng60[last]) and rng60[last] > 0:
        _set("range_ratio_60", rng[last] / rng60[last])
    if np.isfinite(vol20[last]) and vol20[last] > 0:
        _set("vol_ratio_20", v[last] / vol20[last])
    if np.isfinite(vol60[last]) and vol60[last] > 0:
        _set("vol_ratio_60", v[last] / vol60[last])
    if np.isfinite(dh20[last]) and np.isfinite(dl20[last]):
        mid = (dh20[last] + dl20[last]) / 2.0
        if mid > 0:
            _set("donchian_width", (dh20[last] - dl20[last]) / mid)
        if dh20[last] > dl20[last]:
            _set("position_in_channel",
                 (c[last] - dl20[last]) / (dh20[last] - dl20[last]))
    _set("bbw", bbw[last])
    _set("bbw_rank_120", _percentile_rank_last(bbw, 120))
    _set("atr_rank_100", _percentile_rank_last(atr14, 100))
    _set("rv_ann", rv[last])
    _set("rv_rank_200", _percentile_rank_last(rv, 200))
    if c.size >= 10:
        _set("inside_bar_count_10", float(np.sum(inside[-10:])))
    return out
