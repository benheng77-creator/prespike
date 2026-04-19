"""Unit tests for feature extractors."""

import random

from features.atr import compute_atr
from features.drift import compute_drift_score
from features.momentum import compute_mtf_directions
from features.regime import compute_regime_stats


# ---------- momentum ----------


def test_momentum_insufficient_data():
    assert compute_mtf_directions([100.0, 101.0]) == (0.0, 0.0, 0.0, 0.0)


def test_momentum_positive_trend():
    closes = [100.0 + i for i in range(300)]
    d5, d15, d60, d240 = compute_mtf_directions(closes)
    assert d5 > 0
    assert d240 > 0
    assert all(-1.0 <= d <= 1.0 for d in (d5, d15, d60, d240))


def test_momentum_negative_trend():
    closes = [300.0 - i for i in range(300)]
    d5, d15, d60, d240 = compute_mtf_directions(closes)
    assert d5 < 0
    assert d240 < 0


def test_momentum_flat():
    closes = [100.0] * 300
    assert compute_mtf_directions(closes) == (0.0, 0.0, 0.0, 0.0)


# ---------- drift ----------


def test_drift_insufficient_data():
    assert compute_drift_score([100.0] * 50) == 0.0


def test_drift_stable_series_low():
    random.seed(42)
    closes = [100.0 + random.gauss(0, 0.5) for _ in range(300)]
    drift = compute_drift_score(closes)
    assert 0.0 <= drift <= 1.0
    assert drift < 0.9


def test_drift_regime_shift_high():
    closes = [100.0] * 240 + [100.0 + i * 0.5 for i in range(30)]
    drift = compute_drift_score(closes, short=30, long=240)
    assert drift > 0.5


# ---------- atr ----------


def test_atr_insufficient_data():
    assert compute_atr([1.0, 2.0], [0.5, 1.5], [0.75, 1.75]) == 0.0


def test_atr_constant_unit_range():
    highs = [100.0 + i + 0.5 for i in range(30)]
    lows = [100.0 + i - 0.5 for i in range(30)]
    closes = [100.0 + i for i in range(30)]
    atr = compute_atr(highs, lows, closes, period=14)
    assert atr > 0.0


# ---------- regime ----------


def test_regime_insufficient_data():
    samples, rate = compute_regime_stats([100.0] * 50)
    assert samples == 0.0
    assert rate == 0.5


def test_regime_momentum_perfect_on_monotonic():
    closes = [100.0 + i for i in range(300)]
    samples, rate = compute_regime_stats(closes)
    assert samples > 0
    assert rate > 0.95
