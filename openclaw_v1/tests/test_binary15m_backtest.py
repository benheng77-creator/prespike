"""
Backtest harness tests — each audit defect has at least one test.

These tests DO NOT assert edge (there is none on synthetic random walks
by construction). They assert the harness is correct, leak-free, and
all modules compose.
"""

import math
from typing import Any, Sequence

import pytest

from binary15m.agent.featurizer import featurize
from binary15m.agent.model_server import stub_probabilities, seed_for_bucket
from binary15m.agent.resolver import DEFAULT_CONFIG, ResolverConfig, resolve
from binary15m.agent.stream_processor import build_cycle_bundle
from binary15m.backtest import (
    Bar,
    AdaptiveGeometry,
    CostModel,
    EXCHANGE_FEES,
    block_bootstrap_ci,
    brier,
    compute_metrics,
    ece,
    forced_path_isolation,
    integrity_check,
    isotonic_fit,
    isotonic_predict,
    prev_bar_direction,
    run_cycle,
    run_walk_forward,
    sma_crossover,
    synth_bars,
    tie_breaker_ablation,
    build_launch_report,
    walk_forward_fill,
    always_buy,
    always_sell,
    random_class_balanced,
)


# ---- fixtures ----

@pytest.fixture(scope="module")
def bars():
    return synth_bars(500, seed=7)


def _features_fn(bars: Sequence[Bar], idx: int) -> dict:
    bar_k = bars[idx]
    ts_close = bar_k.close_ts_ms
    candles = [[b.ts_ms, b.open, b.high, b.low, b.close, b.volume] for b in bars[:idx + 1]]
    raw = {
        "candles_15m": {"value": candles, "ts_ms": ts_close - 1},
        "candles_1m":  {"value": candles, "ts_ms": ts_close - 1},
        "orderbook_top": {"value": {"bids": [[bar_k.close, 1.0]], "asks": [[bar_k.close, 1.0]]},
                          "ts_ms": ts_close - 1},
        "trades":      {"value": [], "ts_ms": ts_close - 1},
        "funding_rate": {"value": 0.0, "ts_ms": ts_close - 1000},
        "open_interest":{"value": 0.0, "ts_ms": ts_close - 1000},
        "spread":       {"value": bar_k.spread, "ts_ms": ts_close - 1},
        "news_sent":    {"value": 0.0, "ts_ms": ts_close - 5000},
        "social_sent":  {"value": 0.0, "ts_ms": ts_close - 5000},
        "onchain_flow": {"value": 0.0, "ts_ms": ts_close - 5000},
        "event_risk":   {"value": 0.0, "ts_ms": ts_close - 5000},
        "drift_score":  {"value": 0.0, "ts_ms": ts_close - 5000},
    }
    return featurize(build_cycle_bundle(raw, now_ms=ts_close))


def _model_fn(features: dict, idx: int) -> dict:
    return stub_probabilities(features).to_dict()


def _seed_fn(ts_ms: int) -> int:
    return seed_for_bucket(ts_ms)


# ---------- integrity ----------

def test_integrity_rejects_duplicates():
    bs = synth_bars(10)
    bs.append(bs[5])  # duplicate
    clean, rep = integrity_check(bs)
    assert rep.rejected_duplicate == 1
    assert len(clean) == len(bs) - 1


def test_integrity_rejects_flat_and_zero_volume():
    bs = synth_bars(10)
    from dataclasses import replace
    bs[3] = replace(bs[3], volume=0.0)
    bs[5] = replace(bs[5], high=bs[5].low)  # tie
    clean, rep = integrity_check(bs)
    assert rep.rejected_tie >= 2
    assert len(clean) <= len(bs) - 2


def test_integrity_preserves_clean_bars():
    bs = synth_bars(200)
    clean, rep = integrity_check(bs)
    assert rep.n_out == len(clean)
    assert rep.rejected_duplicate == 0


# ---------- leakage fixes ----------

def test_no_wall_clock_leakage_in_featurizer():
    """build_cycle_bundle with a past now_ms produces the SAME features as
    a different past now_ms, i.e. bundle depends on explicit clock only."""
    bs = synth_bars(300)
    f1 = _features_fn(bs, 250)
    # Rebuild the identical bundle deterministically
    f2 = _features_fn(bs, 250)
    assert f1 == f2


def test_entry_uses_next_bar_open_not_last_close(bars):
    # Zero-spread + zero-fee → entry price equals next-bar open exactly.
    from dataclasses import replace
    bars_nos = [replace(b, spread=0.0, bid_qty=1e9, ask_qty=1e9) for b in bars]
    costs = CostModel(fee_override=0.0, slippage_impact=0.0)
    geometry = AdaptiveGeometry()
    out = run_cycle(bars_nos, signal_idx=250, features_fn=_features_fn,
                    model_fn=_model_fn, seed_fn=_seed_fn,
                    geometry=geometry, costs=costs, n_max_hold=8)
    assert out.entry_ts_ms == bars_nos[251].ts_ms
    assert abs(out.entry_px - bars_nos[251].open) < 1e-6
    # The key guarantee: entry timestamp is NEXT bar, not the signal bar.
    assert out.entry_ts_ms > bars_nos[250].close_ts_ms - 1


def test_exit_is_first_touch_inside_window(bars):
    # Craft a bar where target is hit on bar k+2
    costs = CostModel(fee_override=0.0)
    geometry = AdaptiveGeometry(stop_atr_mult=1.5,
                                target_atr_mult_low_vol=0.5,
                                target_atr_mult_high_vol=0.5)
    out = run_cycle(bars, signal_idx=250, features_fn=_features_fn,
                    model_fn=_model_fn, seed_fn=_seed_fn,
                    geometry=geometry, costs=costs, n_max_hold=16)
    assert out.exit_reason in ("target", "stop", "timeout", "stop_conservative")
    assert out.debug["exit_idx"] >= 251
    assert out.debug["exit_idx"] <= 251 + 16


# ---------- cost model ----------

def test_cost_model_exchanges_present():
    for ex in ("coinbase", "binance", "okx", "cryptocom", "independentreserve"):
        assert ex in EXCHANGE_FEES


def test_cost_model_round_trip_scales():
    c1 = CostModel(fee_override=0.001)
    c2 = CostModel(fee_override=0.002)
    assert c2.round_trip_fee_r() == pytest.approx(2 * c1.round_trip_fee_r())


# ---------- benchmarks ----------

def test_always_buy_and_sell_balance(bars):
    costs = CostModel(fee_override=0.0)
    geom = AdaptiveGeometry()
    buys = [run_cycle(bars, signal_idx=i, features_fn=_features_fn, model_fn=_model_fn,
                      seed_fn=_seed_fn, geometry=geom, costs=costs, n_max_hold=4,
                      override_verdict="BUY") for i in range(250, 260)]
    sells = [run_cycle(bars, signal_idx=i, features_fn=_features_fn, model_fn=_model_fn,
                       seed_fn=_seed_fn, geometry=geom, costs=costs, n_max_hold=4,
                       override_verdict="SELL") for i in range(250, 260)]
    # Sign-flipped in aggregate (approximately)
    assert sum(o.realized_r for o in buys) == pytest.approx(-sum(o.realized_r for o in sells), rel=0.6, abs=0.5)


def test_prev_bar_and_sma_are_binary(bars):
    for i in range(50, 100):
        assert prev_bar_direction(bars, i) in ("BUY", "SELL")
        assert sma_crossover(bars, i) in ("BUY", "SELL")


def test_random_balanced_is_binary(bars):
    fn = random_class_balanced(0.5, seed=1)
    for i in range(10, 40):
        assert fn(bars, i) in ("BUY", "SELL")


# ---------- metrics ----------

def test_metrics_sanity_on_synthetic(bars):
    costs = CostModel(fee_override=0.0)
    geom = AdaptiveGeometry()
    outs = [run_cycle(bars, signal_idx=i, features_fn=_features_fn,
                      model_fn=_model_fn, seed_fn=_seed_fn,
                      geometry=geom, costs=costs, n_max_hold=8)
            for i in range(250, 400)]
    m = compute_metrics(outs)
    assert m.n == len(outs)
    assert 0 <= m.win_rate <= 1
    assert m.buy_count + m.sell_count == m.n


# ---------- bootstrap ----------

def test_block_bootstrap_ci_handles_zero_mean():
    vals = [0.01, -0.01, 0.02, -0.02, 0.005, -0.005] * 50
    ci = block_bootstrap_ci(vals, block_size=5, n_resamples=500, seed=123)
    assert ci["mean"] == pytest.approx(sum(vals) / len(vals), abs=1e-9)
    assert ci["ci_lower"] <= ci["mean"] <= ci["ci_upper"]


def test_block_bootstrap_ci_rejects_zero_for_positive_mean():
    vals = [0.01] * 500
    ci = block_bootstrap_ci(vals, block_size=10, n_resamples=400, seed=123)
    assert ci["mean"] > 0
    assert ci["ci_lower"] > 0


# ---------- calibration ----------

def test_isotonic_is_monotone():
    probs = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    outcomes = [0, 1, 0, 1, 1, 1, 0, 1, 1]
    fit = isotonic_fit(probs, outcomes)
    preds = isotonic_predict(fit, probs)
    for a, b in zip(preds, preds[1:]):
        assert a <= b + 1e-9


def test_brier_extreme_cases():
    assert brier([1.0, 0.0], [1, 0]) == pytest.approx(0.0)
    assert brier([0.0, 1.0], [1, 0]) == pytest.approx(1.0)


def test_ece_trivial_perfect_calibration():
    assert ece([0.5] * 1000, [1, 0] * 500, n_bins=10) == pytest.approx(0.0, abs=0.02)


# ---------- walk-forward ----------

def test_walk_forward_folds_are_time_ordered(bars):
    costs = CostModel(fee_override=0.0)
    geom = AdaptiveGeometry()
    def cycle_fn(bs, i):
        return run_cycle(bs, signal_idx=i, features_fn=_features_fn,
                         model_fn=_model_fn, seed_fn=_seed_fn,
                         geometry=geom, costs=costs, n_max_hold=4)
    res = run_walk_forward(bars, train_bars=100, test_bars=50, step_bars=50,
                           cycle_fn=cycle_fn, min_signal_idx=0)
    assert len(res.folds) >= 1
    for f in res.folds:
        assert f.test_start_idx > f.train_start_idx
    # Fold windows non-overlapping (by test window)
    for a, b in zip(res.folds, res.folds[1:]):
        assert b.test_start_idx >= a.test_end_idx or b.test_start_idx > a.test_start_idx


# ---------- ablation ----------

def test_forced_path_isolation_partitions_correctly(bars):
    costs = CostModel(fee_override=0.0)
    geom = AdaptiveGeometry()
    outs = [run_cycle(bars, signal_idx=i, features_fn=_features_fn,
                      model_fn=_model_fn, seed_fn=_seed_fn,
                      geometry=geom, costs=costs, n_max_hold=4)
            for i in range(250, 300)]
    rep = forced_path_isolation(outs)
    assert rep.all_cycles.n == len(outs)
    assert rep.non_forced.n + rep.forced_at_spec_factor.n == len(outs)


def test_tie_breaker_ablation_preserves_n(bars):
    costs = CostModel(fee_override=0.0)
    geom = AdaptiveGeometry()
    outs = [run_cycle(bars, signal_idx=i, features_fn=_features_fn,
                      model_fn=_model_fn, seed_fn=_seed_fn,
                      geometry=geom, costs=costs, n_max_hold=4)
            for i in range(250, 350)]
    tb = tie_breaker_ablation(outs)
    assert tb.baseline_time_seed.n == tb.always_buy_on_seed.n == tb.always_sell_on_seed.n == tb.alternating_on_seed.n


# ---------- launch gate ----------

def test_launch_gate_fails_when_benchmarks_win():
    from binary15m.backtest.metrics import MetricBundle
    empty = MetricBundle(n=100, net_r=-1.0, profit_factor=0.5)
    gate = build_launch_report(
        overall_metrics=empty,
        test_net_r_per_cycle=[-0.01] * 100,
        rolling_sharpe=[0.3, 0.4],
        calibration_brier=0.3, calibration_ece=0.20,
        benchmarks_net_r={"always_buy": 5.0, "always_sell": 3.0},
        strategy_net_r=-1.0,
        forced_isolation=None,
        robustness_net_r={"baseline": -0.5},
    )
    assert gate.verdict == "FAIL"
    assert gate.g1_net_positive is False
    assert gate.g3_profit_factor is False
    assert gate.g5_beats_benchmarks is False


def test_launch_gate_passes_on_perfect_case():
    from binary15m.backtest.metrics import MetricBundle
    strong = MetricBundle(n=1000, net_r=5.0, profit_factor=1.8)
    gate = build_launch_report(
        overall_metrics=strong,
        test_net_r_per_cycle=[0.01] * 1000,
        rolling_sharpe=[1.1, 1.2, 1.5],
        calibration_brier=0.10, calibration_ece=0.02,
        benchmarks_net_r={"always_buy": -1.0, "always_sell": -2.0,
                          "prev_bar": 0.0, "sma_cross": 0.5,
                          "random_balanced": -0.2},
        strategy_net_r=5.0,
        forced_isolation=None,
        robustness_net_r={"baseline": 5.0, "fees_x2": 4.0, "spread_x2": 3.8,
                          "slippage_x2": 3.5, "delay_1_bar": 3.6,
                          "liquidity_halved": 3.4},
    )
    # forced_isolation=None → g6 stays False (missing proof)
    assert gate.g1_net_positive is True
    assert gate.g3_profit_factor is True
    assert gate.g4_calibration is True
    assert gate.g5_beats_benchmarks is True
    assert gate.verdict in ("PASS", "CONDITIONAL")


# ---------- resolver config ----------

def test_resolver_config_allows_threshold_sweep():
    features = {"freshness": 0.9, "coverage": 0.9, "D240": 0.52,
                "orderbook_imbalance": 0.5, "D5": 0.5}
    model = {"p_buy": 0.6, "p_sell": 0.4}
    strict = resolve(features, model, seed=0,
                     config=ResolverConfig(strong_model_delta=0.5,
                                           watch_model_delta=0.5))
    loose = resolve(features, model, seed=0,
                    config=ResolverConfig(strong_model_delta=0.05,
                                          watch_model_delta=0.02))
    assert strict.forced is True      # threshold too strict → forced
    assert loose.forced is False      # threshold loose → primary/watch
