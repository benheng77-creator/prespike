"""Unit tests for the live-only feature trackers: funding, order book, OI."""

import time

from features.funding import FundingTracker
from features.open_interest import OpenInterestTracker
from features.orderbook import OrderBookTracker


# ---------- FundingTracker ----------


def test_funding_initial_neutral():
    f = FundingTracker(exchange=None, symbol="BTC/USDT")
    assert f.current_rate() == 0.0
    assert f.current_score() == 0.0
    assert f.current_freshness() == 0.0


def test_funding_record_and_score():
    f = FundingTracker(exchange=None, symbol="BTC/USDT")
    f.record(0.0005)  # half of FUNDING_SCALE
    assert abs(f.current_rate() - 0.0005) < 1e-12
    assert abs(f.current_score() - 0.5) < 1e-6


def test_funding_score_clamped_at_one():
    f = FundingTracker(exchange=None, symbol="BTC/USDT")
    f.record(0.005)  # way above cap
    assert f.current_score() == 1.0
    f.record(-0.01)
    assert f.current_score() == -1.0


def test_funding_freshness_positive_after_record():
    f = FundingTracker(exchange=None, symbol="BTC/USDT", refresh_s=60)
    f.record(0.0001)
    assert f.current_freshness() > 0.0


# ---------- OrderBookTracker ----------


def test_orderbook_initial_neutral():
    ob = OrderBookTracker()
    assert ob.current_imbalance() == 0.0
    assert ob.current_freshness() == 0.0


def test_orderbook_empty_snapshot_ignored():
    ob = OrderBookTracker()
    ob.record_orderbook({"bids": [], "asks": []})
    assert ob.current_imbalance() == 0.0


def test_orderbook_buy_pressure_positive_imbalance():
    ob = OrderBookTracker(depth_levels=5)
    ob.record_orderbook(
        {
            "bids": [[100.0, 30.0], [99.0, 30.0], [98.0, 20.0]],
            "asks": [[101.0, 10.0], [102.0, 10.0]],
        }
    )
    # bid = 80, ask = 20, imbalance = 60/100 = 0.6
    assert abs(ob.current_imbalance() - 0.6) < 1e-9


def test_orderbook_sell_pressure_negative_imbalance():
    ob = OrderBookTracker(depth_levels=5)
    ob.record_orderbook(
        {
            "bids": [[100.0, 5.0], [99.0, 5.0]],
            "asks": [[101.0, 40.0], [102.0, 10.0]],
        }
    )
    # bid = 10, ask = 50, imbalance = -40/60 = -0.6667
    assert abs(ob.current_imbalance() - (-40.0 / 60.0)) < 1e-9


def test_orderbook_depth_levels_limit():
    ob = OrderBookTracker(depth_levels=2)
    # 3 levels on each side, but only top 2 are counted
    ob.record_orderbook(
        {
            "bids": [[100, 10], [99, 10], [98, 1000]],
            "asks": [[101, 10], [102, 10], [103, 1000]],
        }
    )
    # Within top 2 levels, bid_vol = ask_vol = 20 → imbalance = 0
    assert abs(ob.current_imbalance()) < 1e-9


# ---------- OpenInterestTracker ----------


def test_oi_initial_neutral():
    oi = OpenInterestTracker(exchange=None, symbol="BTC/USDT")
    assert oi.current_value() == 0.0
    assert oi.current_delta_score() == 0.0


def test_oi_single_record_still_zero_delta():
    oi = OpenInterestTracker(exchange=None, symbol="BTC/USDT")
    oi.record(1_000_000.0)
    assert oi.current_value() == 1_000_000.0
    assert oi.current_delta_score() == 0.0  # need ≥2 points


def test_oi_rising_returns_positive_delta():
    oi = OpenInterestTracker(
        exchange=None, symbol="BTC/USDT", window_s=3600
    )
    now = time.time()
    oi.record(1_000_000.0, ts=now - 1800)
    oi.record(1_050_000.0, ts=now)
    # +5% change → 5% / 10% cap = 0.5
    assert oi.current_delta_score() > 0.4
    assert oi.current_delta_score() <= 1.0


def test_oi_falling_returns_negative_delta():
    oi = OpenInterestTracker(
        exchange=None, symbol="BTC/USDT", window_s=3600
    )
    now = time.time()
    oi.record(1_000_000.0, ts=now - 1800)
    oi.record(950_000.0, ts=now)
    assert oi.current_delta_score() < -0.4
    assert oi.current_delta_score() >= -1.0


def test_oi_delta_clipped_at_bounds():
    oi = OpenInterestTracker(
        exchange=None, symbol="BTC/USDT", window_s=3600
    )
    now = time.time()
    oi.record(1_000_000.0, ts=now - 1800)
    oi.record(5_000_000.0, ts=now)  # 400% change
    assert oi.current_delta_score() == 1.0


def test_oi_window_evicts_old_samples():
    oi = OpenInterestTracker(
        exchange=None, symbol="BTC/USDT", window_s=60
    )
    now = time.time()
    oi.record(1_000_000.0, ts=now - 300)  # older than window
    oi.record(1_000_000.0, ts=now - 30)
    oi.record(1_050_000.0, ts=now)
    assert len(oi._history) == 2  # old sample dropped
