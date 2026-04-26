"""
Speed-first incremental feature pipeline for live runtime.

Per-bar update is O(1) per feature using ring-buffered rolling state.
NO pandas in hot path. NO dict-based feature storage at runtime — features
are written into a pre-allocated flat np.ndarray indexed by feature order.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np


@dataclass(slots=True)
class FeatureSpec:
    name: str
    fn: Callable                      # incremental update fn
    state_size: int                   # ring buffer size needed
    args: tuple = ()


class RingBuffer:
    """Pre-allocated O(1) rolling buffer of fixed length."""
    __slots__ = ("_buf", "_idx", "_full", "_n")

    def __init__(self, n: int):
        self._buf = np.zeros(n, dtype=np.float64)
        self._idx = 0
        self._full = False
        self._n = n

    def push(self, v: float) -> None:
        self._buf[self._idx] = v
        self._idx = (self._idx + 1) % self._n
        if self._idx == 0:
            self._full = True

    def view(self) -> np.ndarray:
        if self._full:
            return np.concatenate((self._buf[self._idx:], self._buf[:self._idx]))
        return self._buf[:self._idx]

    @property
    def is_full(self) -> bool:
        return self._full

    @property
    def n(self) -> int:
        return self._n


class IncrementalATR:
    __slots__ = ("_n", "_tr_buf", "_prev_close", "_atr")

    def __init__(self, n: int):
        self._n = n
        self._tr_buf = RingBuffer(n)
        self._prev_close = math.nan
        self._atr = math.nan

    def update(self, h: float, l: float, c: float) -> float:
        if math.isnan(self._prev_close):
            tr = h - l
        else:
            tr = max(h - l, abs(h - self._prev_close), abs(l - self._prev_close))
        self._tr_buf.push(tr)
        self._prev_close = c
        if self._tr_buf.is_full:
            self._atr = float(np.mean(self._tr_buf.view()))
        return self._atr


class IncrementalSMAStd:
    __slots__ = ("_buf", "_sum", "_sumsq", "_n")

    def __init__(self, n: int):
        self._buf = RingBuffer(n)
        self._sum = 0.0
        self._sumsq = 0.0
        self._n = n

    def update(self, v: float) -> tuple[float, float]:
        if self._buf.is_full:
            old = self._buf._buf[self._buf._idx]   # next-to-be-overwritten
            self._sum -= old
            self._sumsq -= old * old
        self._buf.push(v)
        self._sum += v
        self._sumsq += v * v
        if self._buf.is_full:
            mean = self._sum / self._n
            var = max(0.0, self._sumsq / self._n - mean * mean)
            return mean, math.sqrt(var)
        return math.nan, math.nan


class FeatureRuntime:
    """Owns per-bar incremental state for the full feature set.

    Speed: per-bar update touches only the rolling state of each feature.
    Final emission writes into a pre-allocated np.ndarray sized by
    feature_order length.
    """

    def __init__(self, feature_order: list[str]):
        self.feature_order = feature_order
        self._n_features = len(feature_order)
        self._out = np.zeros(self._n_features, dtype=np.float64)
        # Indicators
        self._atr14 = IncrementalATR(14)
        self._sma20 = IncrementalSMAStd(20)
        self._sma60 = IncrementalSMAStd(60)
        self._sma200 = IncrementalSMAStd(200)
        self._rng_avg14 = IncrementalSMAStd(14)
        self._rng_avg60 = IncrementalSMAStd(60)
        self._vol_avg20 = IncrementalSMAStd(20)
        self._vol_avg60 = IncrementalSMAStd(60)
        # Percentile-rank windows
        self._atr_window_100 = RingBuffer(100)
        self._bbw_window_120 = RingBuffer(120)
        self._rv_window_200 = RingBuffer(200)
        # Highs/lows for Donchian
        self._dh20 = RingBuffer(20)
        self._dl20 = RingBuffer(20)
        # Log returns
        self._prev_close = math.nan
        self._logret_buf30 = RingBuffer(30)
        # Inside-bar tracking
        self._prev_h = math.nan
        self._prev_l = math.nan
        self._inside_count_10 = RingBuffer(10)

    # ------------------------------------------------------------------
    def update(self, ts: int, o: float, h: float, l: float, c: float, v: float):
        out = self._out
        out.fill(math.nan)
        idx = {n: i for i, n in enumerate(self.feature_order)}

        atr = self._atr14.update(h, l, c)
        m20, s20 = self._sma20.update(c)
        m60, s60 = self._sma60.update(c)
        m200, _ = self._sma200.update(c)
        ravg14, _ = self._rng_avg14.update(h - l)
        ravg60, _ = self._rng_avg60.update(h - l)
        vavg20, _ = self._vol_avg20.update(v)
        vavg60, _ = self._vol_avg60.update(v)

        self._dh20.push(h)
        self._dl20.push(l)
        if self._dh20.is_full:
            d_high = float(np.max(self._dh20.view()))
            d_low = float(np.min(self._dl20.view()))
        else:
            d_high = d_low = math.nan

        # Log return
        if not math.isnan(self._prev_close) and self._prev_close > 0 and c > 0:
            lr = math.log(c / self._prev_close)
        else:
            lr = math.nan
        self._prev_close = c
        if not math.isnan(lr):
            self._logret_buf30.push(lr)

        # Realized vol annualized (per-period × sqrt(periods/year))
        if self._logret_buf30.is_full:
            rv = float(np.std(self._logret_buf30.view(), ddof=0)) * math.sqrt(525600 / 5)
            self._rv_window_200.push(rv)
        else:
            rv = math.nan

        # BBW
        if not math.isnan(s20) and m20 > 0:
            bbw = (4.0 * s20) / m20
            self._bbw_window_120.push(bbw)
        else:
            bbw = math.nan

        # ATR percentile-rank window
        if not math.isnan(atr):
            self._atr_window_100.push(atr)

        # Inside bar
        if not math.isnan(self._prev_h) and h < self._prev_h and l > self._prev_l:
            self._inside_count_10.push(1.0)
        else:
            self._inside_count_10.push(0.0)
        self._prev_h, self._prev_l = h, l

        def _set(name: str, val: float):
            i = idx.get(name)
            if i is not None:
                out[i] = val if (val is not None and math.isfinite(val)) else math.nan

        _set("atr_14", atr)
        _set("close_minus_sma20_atr", (c - m20) / atr if atr and not math.isnan(m20) else math.nan)
        _set("close_minus_sma60_atr", (c - m60) / atr if atr and not math.isnan(m60) else math.nan)
        _set("close_minus_sma200_atr", (c - m200) / atr if atr and not math.isnan(m200) else math.nan)
        _set("range_over_atr", (h - l) / atr if atr else math.nan)
        _set("range_ratio_14", (h - l) / ravg14 if ravg14 else math.nan)
        _set("range_ratio_60", (h - l) / ravg60 if ravg60 else math.nan)
        _set("vol_ratio_20", v / vavg20 if vavg20 else math.nan)
        _set("vol_ratio_60", v / vavg60 if vavg60 else math.nan)

        # Donchian width
        if not math.isnan(d_high) and not math.isnan(d_low) and (d_high + d_low) > 0:
            dcw = (d_high - d_low) / ((d_high + d_low) / 2.0)
            _set("donchian_width", dcw)
            _set("position_in_channel",
                 (c - d_low) / (d_high - d_low) if d_high > d_low else 0.5)
        # BBW + percentile rank
        _set("bbw", bbw)
        if self._bbw_window_120.is_full and not math.isnan(bbw):
            arr = self._bbw_window_120.view()
            rank = float(np.sum(arr <= bbw)) / arr.size
            _set("bbw_rank_120", rank)

        # ATR percentile rank
        if self._atr_window_100.is_full and not math.isnan(atr):
            arr = self._atr_window_100.view()
            rank = float(np.sum(arr <= atr)) / arr.size
            _set("atr_rank_100", rank)

        # RV percentile rank
        if self._rv_window_200.is_full and not math.isnan(rv):
            arr = self._rv_window_200.view()
            rank = float(np.sum(arr <= rv)) / arr.size
            _set("rv_ann", rv)
            _set("rv_rank_200", rank)

        # Inside-bar density
        if self._inside_count_10.is_full:
            _set("inside_bar_count_10", float(np.sum(self._inside_count_10.view())))

        return out  # caller consumes; do NOT mutate

    @property
    def vector(self) -> np.ndarray:
        return self._out

    def is_warm(self) -> bool:
        return self._rv_window_200.is_full and self._bbw_window_120.is_full
