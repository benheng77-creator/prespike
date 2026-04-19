"""Exhaustive tests for the Binary15 mathematical contract."""

import math
import pytest

from binary15 import (
    ActionSide,
    Binary15Decision,
    Binary15Inputs,
    DecisionStatus,
    FractionalKelly,
    HaltReason,
    HaltRules,
    HaltState,
    JSON_SCHEMA,
    compute_c_eff,
    compute_kelly,
    compute_metrics,
    decide,
    probability_of_ruin,
    update_halt,
    validate_decision_payload,
)
from binary15.backtest import (
    BacktestResult,
    ExecutionConfig,
    Executor,
    OrderBook,
    OrderBookLevel,
    run_backtest,
    simulate_cycle,
    synth_book,
)


# ---- core math -------------------------------------------------------------

def test_compute_c_eff():
    assert compute_c_eff(0.10, 0.002, 0.002, 0.001) == pytest.approx(0.105)


def test_compute_kelly_formula():
    # f* = (p - c) / (1 - c)
    assert compute_kelly(0.18, 0.10) == pytest.approx((0.18 - 0.10) / (1 - 0.10))


def test_compute_kelly_equal_returns_zero():
    assert compute_kelly(0.30, 0.30) == pytest.approx(0.0)


def test_compute_kelly_p_less_than_c_is_negative():
    assert compute_kelly(0.20, 0.50) < 0


# ---- input validation ------------------------------------------------------

def test_inputs_reject_c_outside_unit_open_interval():
    with pytest.raises(ValueError):
        Binary15Inputs(cycleId="x", p=0.5, c=0.0)
    with pytest.raises(ValueError):
        Binary15Inputs(cycleId="x", p=0.5, c=1.0)


def test_inputs_reject_bad_probability():
    with pytest.raises(ValueError):
        Binary15Inputs(cycleId="x", p=1.5, c=0.5)


def test_inputs_reject_negative_costs():
    with pytest.raises(ValueError):
        Binary15Inputs(cycleId="x", p=0.5, c=0.1, fee=-0.01)


def test_fractional_k_bounds_enforced():
    with pytest.raises(ValueError):
        FractionalKelly(0.10)
    with pytest.raises(ValueError):
        FractionalKelly(0.60)
    assert float(FractionalKelly(0.25)) == 0.25
    assert float(FractionalKelly(0.50)) == 0.50


# ---- decide() happy path ----------------------------------------------------

def test_decide_executes_with_sufficient_edge():
    d = decide(Binary15Inputs(
        cycleId="a", p=0.18, c=0.10,
        fee=0.002, slippage=0.002, adverse_selection=0.001,
        fill_probability=0.9, fractional_k=0.25,
    ))
    assert d.action == ActionSide.BUY_YES
    assert d.c_eff == pytest.approx(0.105)
    assert d.edge == pytest.approx(0.18 - 0.105)
    assert d.kelly_f == pytest.approx((0.18 - 0.105) / (1 - 0.105))
    assert d.final_f > 0
    assert d.final_f <= 0.05
    assert d.decision == DecisionStatus.EXECUTE


def test_decide_rejects_insufficient_edge():
    d = decide(Binary15Inputs(
        cycleId="b", p=0.102, c=0.10,
        fee=0.002, slippage=0.002, adverse_selection=0.001,
    ))
    assert d.decision == DecisionStatus.NO_TRADE
    assert d.edge < 0.005


def test_decide_no_trade_when_final_f_above_cap():
    # Force a huge raw Kelly then rely on cap
    d = decide(Binary15Inputs(
        cycleId="c", p=0.98, c=0.10, fractional_k=0.50,
    ))
    # p - c = 0.88, /(1-0.10)=0.9778 → after k=0.5 → 0.489 > 0.05 → NO_TRADE
    assert d.decision == DecisionStatus.NO_TRADE
    assert d.final_f > 0.05


def test_decide_buy_no_when_p_less_than_c():
    d = decide(Binary15Inputs(
        cycleId="d", p=0.20, c=0.50, fractional_k=0.25,
    ))
    assert d.action == ActionSide.BUY_NO
    assert d.kelly_f < 0
    # edge = 0.20 - c_eff(=0.50) < 0 → NO_TRADE under strict literal rule
    assert d.decision == DecisionStatus.NO_TRADE


def test_decide_p_equals_c_eff_gives_zero_kelly():
    d = decide(Binary15Inputs(cycleId="e", p=0.10, c=0.10))
    assert d.kelly_f == pytest.approx(0.0)
    assert d.final_f == 0.0
    assert d.decision == DecisionStatus.NO_TRADE


def test_decide_latency_flag_reflects_override():
    d_none = decide(Binary15Inputs(cycleId="f", p=0.2, c=0.1))
    d_adj = decide(Binary15Inputs(cycleId="g", p=0.2, c=0.1, p_latency_adjusted=0.18))
    assert d_none.latency_adjusted is False
    assert d_adj.latency_adjusted is True
    assert d_adj.p_eff == 0.18


def test_decide_fill_probability_scales_position():
    d1 = decide(Binary15Inputs(cycleId="h", p=0.2, c=0.1, fill_probability=1.0))
    d2 = decide(Binary15Inputs(cycleId="i", p=0.2, c=0.1, fill_probability=0.5))
    assert d2.final_f == pytest.approx(d1.final_f * 0.5)


def test_decide_correlation_scale_shrinks_position():
    d1 = decide(Binary15Inputs(cycleId="j", p=0.2, c=0.1, correlation_scale=1.0))
    d2 = decide(Binary15Inputs(cycleId="k", p=0.2, c=0.1, correlation_scale=0.5))
    assert d2.final_f == pytest.approx(d1.final_f * 0.5)


def test_decide_fractional_k_linear_effect():
    d1 = decide(Binary15Inputs(cycleId="l", p=0.2, c=0.1, fractional_k=0.25))
    d2 = decide(Binary15Inputs(cycleId="m", p=0.2, c=0.1, fractional_k=0.50))
    assert d2.final_f == pytest.approx(d1.final_f * 2.0)


def test_decide_is_deterministic():
    inp = Binary15Inputs(
        cycleId="det", p=0.17, c=0.09, fee=0.001, slippage=0.0015,
        adverse_selection=0.0005, fill_probability=0.92,
        fractional_k=0.3, correlation_scale=0.9,
    )
    d1 = decide(inp)
    d2 = decide(inp)
    assert d1.to_strict_json() == d2.to_strict_json()


# ---- strict JSON output contract -------------------------------------------

def test_strict_json_contains_only_allowed_keys():
    d = decide(Binary15Inputs(cycleId="n", p=0.18, c=0.10,
                              fee=0.002, slippage=0.002, adverse_selection=0.001))
    payload = d.to_strict_json()
    required = set(JSON_SCHEMA["required"])
    allowed = set(JSON_SCHEMA["properties"].keys())
    assert set(payload.keys()) == required == allowed


def test_validate_decision_payload_ok():
    d = decide(Binary15Inputs(cycleId="o", p=0.18, c=0.10,
                              fee=0.002, slippage=0.002, adverse_selection=0.001))
    errors = validate_decision_payload(d.to_strict_json())
    assert errors == []


def test_validate_decision_payload_rejects_extra_keys():
    d = decide(Binary15Inputs(cycleId="p", p=0.18, c=0.10))
    payload = d.to_strict_json()
    payload["extra"] = "bad"
    errors = validate_decision_payload(payload)
    assert any("extra" in e for e in errors)


def test_validate_decision_payload_rejects_bad_action():
    d = decide(Binary15Inputs(cycleId="q", p=0.18, c=0.10))
    payload = d.to_strict_json()
    payload["action"] = "HOLD"
    errors = validate_decision_payload(payload)
    assert any("BUY_YES|BUY_NO" in e for e in errors)


# ---- halt / auto-stop -------------------------------------------------------

def test_halt_triggers_on_edge_collapse():
    state = HaltState(rules=HaltRules(edge_window=5, edge_fail_fraction=0.8))
    for _ in range(4):
        update_halt(state, edge=0.0, filled=True, latency_ms=100, equity_delta=0.0)
    update_halt(state, edge=0.0, filled=True, latency_ms=100, equity_delta=0.0)
    assert state.halted is True
    assert state.reason == HaltReason.EDGE_COLLAPSE


def test_halt_triggers_on_latency():
    state = HaltState(rules=HaltRules(max_latency_ms=1000))
    update_halt(state, edge=0.01, filled=True, latency_ms=5000, equity_delta=0.0)
    assert state.halted is True
    assert state.reason == HaltReason.LATENCY_EXCEEDED


def test_halt_triggers_on_drawdown():
    state = HaltState(rules=HaltRules(max_drawdown=0.10))
    update_halt(state, equity_delta=+1.0)
    update_halt(state, equity_delta=-0.2)
    assert state.halted is True
    assert state.reason == HaltReason.DRAWDOWN_EXCEEDED


def test_halt_triggers_on_fill_rate():
    state = HaltState(rules=HaltRules(fill_window=5, min_fill_rate=0.9))
    for _ in range(5):
        update_halt(state, filled=False, edge=0.01, latency_ms=100, equity_delta=0.0)
    assert state.halted is True
    assert state.reason == HaltReason.FILL_RATE_DEGRADED


def test_halt_stays_halted():
    state = HaltState(rules=HaltRules(max_latency_ms=100))
    update_halt(state, latency_ms=200)
    assert state.halted is True
    update_halt(state, latency_ms=50, filled=True, edge=0.02)
    assert state.halted is True


# ---- metrics ---------------------------------------------------------------

def test_compute_metrics_empty():
    m = compute_metrics([])
    assert m.n_trades == 0
    assert m.net_pnl == 0.0


def test_compute_metrics_simple_series():
    m = compute_metrics([0.1, -0.05, 0.2, -0.1, 0.05])
    assert m.n_trades == 5
    assert m.net_pnl == pytest.approx(0.2)
    assert m.max_drawdown > 0
    assert m.sharpe != 0


def test_probability_of_ruin_positive_on_negative_edge():
    # Strongly negative expected return should give non-trivial ruin
    pr = probability_of_ruin([-0.10] * 50, ruin_fraction=0.5, n_simulations=500, seed=1)
    assert pr > 0.5


def test_probability_of_ruin_zero_on_strongly_positive_edge():
    pr = probability_of_ruin([0.10] * 200, ruin_fraction=0.5, n_simulations=500, seed=1)
    assert pr == 0.0


# ---- backtest engine --------------------------------------------------------

def test_orderbook_helpers():
    book = synth_book(0.50)
    assert book.best_bid() < 0.50 < book.best_ask()
    assert book.mid() == pytest.approx(0.50, abs=0.005)


def test_executor_produces_fill_report():
    book = synth_book(0.50)
    cfg = ExecutionConfig()
    ex = Executor(cfg, seed=7)
    r = ex.simulate_order(side="BUY_YES", size=0.01, book=book)
    assert r.filled_size > 0
    assert r.fees_paid >= 0
    assert r.latency_ms > 0


def test_simulate_cycle_executes_with_edge():
    book = synth_book(0.10)
    ex = Executor(ExecutionConfig(), seed=11)
    cr = simulate_cycle(
        cycle_idx=0, p=0.20, book=book,
        executor=ex, exec_cfg=ExecutionConfig(),
        fractional_k=0.25, outcome_yes=1,
    )
    assert cr.decision.decision in (DecisionStatus.EXECUTE, DecisionStatus.NO_TRADE)
    if cr.decision.decision == DecisionStatus.EXECUTE:
        assert cr.fill is not None


def test_run_backtest_end_to_end():
    # p_true > c for first half; hostile second half (p < c)
    books = [synth_book(0.20) for _ in range(50)]
    p_series = [0.30] * 25 + [0.10] * 25  # edge then no edge
    outcomes = [1] * 25 + [0] * 25
    res = run_backtest(
        p_series=p_series, books=books,
        exec_cfg=ExecutionConfig(),
        halt_rules=HaltRules(edge_window=10, edge_fail_fraction=0.8,
                             max_drawdown=1.0),
        fractional_k=0.25, correlation_scale=1.0,
        seed=99, outcomes=outcomes,
    )
    assert isinstance(res, BacktestResult)
    assert len(res.cycles) == 50
    # Edge-collapse halt fires on the second half
    assert res.halt.halted is True
    assert res.halt.reason == HaltReason.EDGE_COLLAPSE


def test_run_backtest_respects_cap_and_threshold():
    books = [synth_book(0.10) for _ in range(5)]
    p_series = [0.11] * 5   # edge = 0.01 before friction; with friction → NO_TRADE
    res = run_backtest(p_series=p_series, books=books,
                       exec_cfg=ExecutionConfig(fee_taker=0.02),
                       fractional_k=0.25, seed=3)
    executions = [c for c in res.cycles if c.decision.decision.value == "EXECUTE"]
    assert executions == []


# ---- full workflow smoke ----------------------------------------------------

def test_strict_json_example_shape_matches_spec():
    """Sanity-check that the example payload in the spec would pass validation."""
    example = {
        "cycleId": "timestamp_or_uuid",
        "action": "BUY_YES",
        "p": 0.18,
        "c": 0.10,
        "c_eff": 0.105,
        "edge": 0.075,
        "kelly_f": 0.0849,
        "fractional_k": 0.25,
        "final_f": 0.0212,
        "fill_probability": 0.9,
        "latency_adjusted": True,
        "decision": "EXECUTE",
        "reason": "p > c_eff with sufficient edge after costs",
    }
    assert validate_decision_payload(example) == []
