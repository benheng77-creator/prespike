"""Opportunity Fabric hygiene patches.

A. counterfactual_replay.replay_one: skip entries whose |realized_ret|
   exceeds OUTLIER_RET_PCT so operator close_all / reconciler artifacts
   don't distort aggregate causal stats.

B. /positions/close_all: reject reason strings that collide with
   engine exit reasons (SL, TP, TRAIL, TIME_STOP, etc). One historical
   close_all?reason=SL call on 2026-04-18 poisoned 11 trades.
"""
from __future__ import annotations

import importlib
import sqlite3
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# A. Outlier filter in counterfactual_replay
# ---------------------------------------------------------------------------

@pytest.fixture
def _iso_cfr(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    monkeypatch.setenv("SPOT_CFR_OUTLIER_PCT", "0.10")
    import spot_aggro.governance.counterfactual_replay as cfr
    importlib.reload(cfr)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS spot_live_variant_entries("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " variant TEXT, symbol TEXT, status TEXT,"
        " notional_usd REAL, realized_pnl_usd REAL,"
        " opened_ts_ms INTEGER, closed_ts_ms INTEGER)"
    )
    con.commit()
    con.close()
    cfr._init_schema()
    yield db, cfr


def _insert(db: Path, variant: str, symbol: str, notional: float,
            realized_pnl: float) -> int:
    con = sqlite3.connect(str(db))
    cur = con.execute(
        "INSERT INTO spot_live_variant_entries("
        " variant, symbol, status, notional_usd, realized_pnl_usd,"
        " opened_ts_ms, closed_ts_ms)"
        " VALUES(?,?,?,?,?,?,?)",
        (variant, symbol, "closed", notional, realized_pnl,
         int(time.time() * 1000) - 3_600_000,
         int(time.time() * 1000)),
    )
    eid = cur.lastrowid
    con.commit()
    con.close()
    return eid


def test_outlier_entry_returns_skip_rationale(_iso_cfr):
    db, cfr = _iso_cfr
    # 14% loss on $5 notional -> beyond 10% threshold.
    eid = _insert(db, "contrarian", "ZOMBIE-USDT", 5.0, -0.70)
    r = cfr.replay_one(eid, "exploratory")
    assert r.causal_delta_bp is None
    assert r.counterfactual_pnl_usd is None
    assert "outlier skipped" in r.rationale


def test_normal_entry_still_replays(_iso_cfr):
    db, cfr = _iso_cfr
    eid = _insert(db, "contrarian", "NORMAL-USDT", 5.0, 0.05)  # +1%
    r = cfr.replay_one(eid, "exploratory")
    assert r.causal_delta_bp is not None
    assert r.counterfactual_pnl_usd is not None


def test_aggregate_stats_reports_outliers_separately(_iso_cfr):
    db, cfr = _iso_cfr
    # 3 normal trades, 2 outliers.
    _insert(db, "contrarian", "A", 5.0, 0.05)
    _insert(db, "contrarian", "B", 5.0, -0.03)
    _insert(db, "contrarian", "C", 5.0, 0.10)
    _insert(db, "contrarian", "D", 5.0, -0.70)   # 14% loss -> outlier
    _insert(db, "contrarian", "E", 5.0, -1.20)   # 24% loss -> outlier
    cfr.replay_all_closed(policies=("exploratory",))
    stats = cfr.aggregate_stats("exploratory")
    assert stats["n"] == 3
    assert stats["n_outliers_excluded"] == 2
    assert abs(stats["outlier_pnl_usd_sum"] - (-1.90)) < 0.01


def test_outlier_threshold_configurable(_iso_cfr, monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    monkeypatch.setenv("SPOT_CFR_OUTLIER_PCT", "0.25")  # 25% threshold
    import spot_aggro.governance.counterfactual_replay as cfr
    importlib.reload(cfr)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS spot_live_variant_entries("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " variant TEXT, symbol TEXT, status TEXT,"
        " notional_usd REAL, realized_pnl_usd REAL,"
        " opened_ts_ms INTEGER, closed_ts_ms INTEGER)"
    )
    con.commit()
    con.close()
    cfr._init_schema()
    # 14% loss now below threshold — should NOT be skipped.
    eid = _insert(db, "c", "X", 5.0, -0.70)
    r = cfr.replay_one(eid, "exploratory")
    assert r.causal_delta_bp is not None


def test_zero_notional_is_not_outlier(_iso_cfr):
    """Guard against divide-by-zero in outlier check."""
    db, cfr = _iso_cfr
    eid = _insert(db, "c", "Z", 0.0, 0.0)
    r = cfr.replay_one(eid, "conservative")
    # Normal path fires (returns zero cf pnl); outlier filter does not
    # trip on zero notional.
    assert r.counterfactual_pnl_usd == 0.0


# ---------------------------------------------------------------------------
# B. close_all reason collision rejection
# ---------------------------------------------------------------------------

def test_close_all_rejects_reserved_reason_sl():
    """The route must refuse reason=SL to prevent operator actions from
    being logged as engine SL fires."""
    from spot_aggro.api.routes import (
        _ENGINE_EXIT_REASONS, spot_aggro_positions_close_all,
    )
    # Admin token bypass not needed — reason check happens after auth.
    # We call with a fake token via monkeypatch on _require_admin? Simpler:
    # directly assert the reserved set contains the known exit reasons.
    assert "SL" in _ENGINE_EXIT_REASONS
    assert "TP" in _ENGINE_EXIT_REASONS
    assert "TRAIL" in _ENGINE_EXIT_REASONS
    assert "TIME_STOP" in _ENGINE_EXIT_REASONS
    assert "COMPOSITE_DECAY" in _ENGINE_EXIT_REASONS
    assert "SPI_DECAY_CONFIRMED" in _ENGINE_EXIT_REASONS
    assert "SWARM_EXIT" in _ENGINE_EXIT_REASONS
    assert "halt" in _ENGINE_EXIT_REASONS
    # Operator-distinct reasons must NOT be in the set.
    assert "operator_close_all" not in _ENGINE_EXIT_REASONS
    assert "operator_rebalance" not in _ENGINE_EXIT_REASONS
    assert "operator_emergency_stop" not in _ENGINE_EXIT_REASONS


def test_close_all_route_returns_error_on_reserved_reason(monkeypatch):
    """End-to-end: route body must reject reserved-reason calls.
    We bypass _require_admin by patching it to no-op so we can focus
    on the reason-collision logic."""
    from spot_aggro.api import routes as r
    monkeypatch.setattr(r, "_require_admin", lambda *a, **k: None)
    resp = r.spot_aggro_positions_close_all(reason="SL", x_ops_token="x")
    assert resp["ok"] is False
    assert resp["error"] == "reserved_reason"
    assert "SL" in resp["detail"]


def test_close_all_route_accepts_operator_reason(monkeypatch):
    """Positive-path: non-reserved reason passes reason-check.
    (Actual close execution is engine-dependent; we assert we DON'T
    get a reserved_reason error.)"""
    from spot_aggro.api import routes as r
    monkeypatch.setattr(r, "_require_admin", lambda *a, **k: None)
    resp = r.spot_aggro_positions_close_all(reason="operator_close_all",
                                            x_ops_token="x")
    # Might fail with engine_not_started / other reasons; the point is
    # the reserved_reason path was NOT taken.
    assert resp.get("error") != "reserved_reason"
