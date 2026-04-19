"""
Emitter dispatch shape — every emit_* function must produce a row with
the right table name, required tag keys, and field types.
"""
from __future__ import annotations

from typing import Any

import pytest

from spot_aggro.telemetry import emitter


class _FakeSink:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict, dict, int]] = []

    def emit(self, table: str, tags: dict, fields: dict, ts_ns: int) -> None:
        self.rows.append((table, dict(tags), dict(fields), ts_ns))

    def start(self) -> None: ...
    def stop(self, timeout_s: float = 2.0) -> None: ...
    def get_stats(self) -> dict: return {}


@pytest.fixture
def fake_sink(monkeypatch) -> _FakeSink:
    fake = _FakeSink()
    monkeypatch.setattr(emitter, "_sink", fake)
    return fake


def test_emit_trade_shape(fake_sink: _FakeSink) -> None:
    emitter.emit_trade(
        "enter", symbol="INJ-USDT", tier="B", module="M1_flow_B",
        notional_usd=20.0, avg_px=3.31, fee_usd=0.02, pnl_usd=None,
        reason=None, composite=0.62, spi=0.71, correlation_id="c1",
    )
    assert len(fake_sink.rows) == 1
    table, tags, fields, _ts = fake_sink.rows[0]
    assert table == "spot_trades"
    assert tags["action"] == "enter"
    assert tags["symbol"] == "INJ-USDT"
    assert tags["tier"] == "B"
    assert tags["correlation_id"] == "c1"
    assert fields["notional_usd"] == 20.0
    assert fields["composite"] == 0.62


def test_emit_trade_defaults_unknown_tier_to_question(fake_sink: _FakeSink) -> None:
    emitter.emit_trade("skip", symbol="INJ-USDT",
                       reason="INSUFFICIENT_FREE_USDT")
    tags = fake_sink.rows[-1][1]
    assert tags["tier"] == "?"
    assert tags["reason"] == "INSUFFICIENT_FREE_USDT"


@pytest.mark.parametrize("fn,table,kwargs", [
    ("emit_funnel", "spot_funnel", dict(
        window_min=60, scored=100, tier_passed=30,
        consensus_fired=25, consensus_passed=10,
        orders_entered=5, orders_rejected=2, orders_exited=1)),
    ("emit_audit", "spot_audit_swarm", dict(
        symbol="INJ-USDT", verdict="PASS", reason_code=None,
        quorum_size=4, posterior_final=0.78, total_cost_usd=0.003,
        total_latency_ms=1800, adjudicator_invoked=True)),
    ("emit_swarm_cycle", "spot_swarm_cycles", dict(
        layer="fast", duration_ms=2100, agents_ok=5, agents_total=5,
        coins_cycled=5, cost_usd=0.001)),
    ("emit_consensus", "spot_consensus", dict(
        symbol="INJ-USDT", consensus=0.42, conflict=0.31, vetoed=False,
        members_called=5, members_ok=4)),
    ("emit_regime", "spot_regime", dict(
        regime="TRENDING_UP", regime_confidence=0.72)),
    ("emit_execution_cost", "spot_execution_cost", dict(
        symbol="INJ-USDT", notional_usd=20.0, ref_price=3.31,
        fill_price=3.3105, slippage_bp=1.5, half_spread_bp=2.0,
        fee_bp=2.0, maker_or_taker="maker",
        round_trip_cost_bp=13.0, expected_move_bp=40.0)),
    ("emit_coin_memory", "spot_coin_memory_log", dict(
        symbol="INJ-USDT", tier="B", regime="TRENDING_UP",
        n_trades=5, wins=3, win_rate=0.6, sum_pnl=0.4,
        composite_mult=1.05, cooldown_until_ts_ns=0, suppressed=False)),
    ("emit_forensic_run", "spot_forensic_runs", dict(
        report_id="R-1", window_h=2.0, verdict="INSUFFICIENT_DATA",
        n_trades=14, win_rate=0.071, exp_per_trade=-0.05,
        quorum_ok=4, quorum_total=4, cost_usd=0.00001,
        governor_verdict="APPROVED_WITH_WARNINGS", trust_score=0.6)),
    ("emit_kill", "spot_kill_events", dict(
        kind="triggered", reason="DD>8%", dd_pct=0.083, equity_usd=330.0)),
    ("emit_wri", "spot_wri_runs", dict(
        cadence="micro", n_trades=5, win_rate=0.4, total_pnl=-0.1,
        top_cause="bad_entry", confidence="WEAK")),
    ("emit_governor", "spot_governor_runs", dict(
        kind="per_report", report_id="R-1", verdict="APPROVED",
        trust_score=0.88, n_unverifiable=0,
        n_contradictions=0, n_unsupported=0)),
    ("emit_dashboard_truth_issue", "spot_dashboard_truth_issues", dict(
        kind="MISMATCH", card="c-funnel", severity="crit",
        summary_value=29.0, body_value=0.0, age_s=12)),
])
def test_emit_dispatches_to_correct_table(fake_sink: _FakeSink,
                                          fn: str, table: str,
                                          kwargs: dict) -> None:
    getattr(emitter, fn)(**kwargs)
    assert len(fake_sink.rows) == 1
    assert fake_sink.rows[0][0] == table


def test_get_sink_stats_returns_disabled_dict_when_uninitialized(
    monkeypatch,
) -> None:
    monkeypatch.setattr(emitter, "_sink", None)
    s = emitter.get_sink_stats()
    assert s["enabled"] is False
    assert s["sent"] == 0
    assert s["dropped"] == 0
