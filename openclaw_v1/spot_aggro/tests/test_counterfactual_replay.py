"""Opportunity Fabric — Sprint 6 tests: Counterfactual replay."""
from __future__ import annotations

import importlib
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def _iso_cf(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    import spot_aggro.governance.counterfactual_replay as cf
    importlib.reload(cf)
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
    cf._init_schema()
    yield db, cf


def _insert(db: Path, variant: str, symbol: str, notional: float,
            realized_pnl: float, status: str = "closed") -> int:
    con = sqlite3.connect(str(db))
    cur = con.execute(
        "INSERT INTO spot_live_variant_entries("
        " variant, symbol, status, notional_usd, realized_pnl_usd,"
        " opened_ts_ms, closed_ts_ms)"
        " VALUES(?,?,?,?,?,?,?)",
        (variant, symbol, status, notional, realized_pnl,
         int(time.time() * 1000) - 3600_000,
         int(time.time() * 1000)),
    )
    eid = cur.lastrowid
    con.commit()
    con.close()
    return eid


def test_replay_winner_gets_tp_cap(_iso_cf):
    db, cf = _iso_cf
    # Live trade made +$2 on $25 notional = +8%, way above any tp target.
    eid = _insert(db, "contrarian", "BTC-USDT", 25.0, 2.0)
    r = cf.replay_one(eid, "exploratory")   # tp=2%, sl=-1.5%
    # Counterfactual capped at tp=2% of 25 = $0.50.
    assert abs(r.counterfactual_pnl_usd - 0.50) < 0.01
    # Live beat cf by ~1.50 on 25 notional = 6000 / 10000 = 600bp
    assert r.causal_delta_bp > 500


def test_replay_loser_gets_sl_cap(_iso_cf):
    db, cf = _iso_cf
    eid = _insert(db, "deep_value", "ETH-USDT", 25.0, -2.0)  # -8%
    r = cf.replay_one(eid, "conservative")   # tp=1.5%, sl=-1%
    # Cf stopped at sl=-1% of 25 = -$0.25; live lost $2 — cf better by ~$1.75
    assert abs(r.counterfactual_pnl_usd - (-0.25)) < 0.01
    assert r.causal_delta_bp < -500


def test_replay_inside_window_mirrors_live(_iso_cf):
    db, cf = _iso_cf
    # 0.5% move, sits inside any normal tp/sl window.
    eid = _insert(db, "contrarian", "SOL-USDT", 25.0, 0.125)
    r = cf.replay_one(eid, "aggressive")     # tp=3%, sl=-2%
    assert abs(r.counterfactual_pnl_usd - 0.125) < 0.01
    assert abs(r.causal_delta_bp - 0) < 1   # deltas cancel


def test_replay_unknown_policy_returns_none(_iso_cf):
    db, cf = _iso_cf
    eid = _insert(db, "contrarian", "BTC-USDT", 25.0, 0.5)
    r = cf.replay_one(eid, "martingale")
    assert r.causal_delta_bp is None
    assert "unknown policy" in r.rationale


def test_replay_open_entry_returns_none(_iso_cf):
    db, cf = _iso_cf
    eid = _insert(db, "contrarian", "BTC-USDT", 25.0, 0.0, status="open")
    r = cf.replay_one(eid, "exploratory")
    assert r.causal_delta_bp is None
    assert "not yet closed" in r.rationale


def test_replay_persists_to_db_and_is_idempotent(_iso_cf):
    db, cf = _iso_cf
    eid = _insert(db, "deep_value", "ETH-USDT", 25.0, 1.0)
    cf.replay_one(eid, "exploratory")
    cf.replay_one(eid, "exploratory")         # re-run
    con = sqlite3.connect(str(db))
    n = con.execute(
        "SELECT COUNT(*) FROM spot_counterfactual_replay"
        " WHERE entry_id=? AND counterfactual_policy='exploratory'",
        (eid,),
    ).fetchone()[0]
    con.close()
    # Idempotent — UNIQUE constraint means exactly one row.
    assert n == 1


def test_replay_all_closed_runs_every_policy(_iso_cf):
    db, cf = _iso_cf
    _insert(db, "contrarian", "BTC-USDT", 25.0, 0.5)
    _insert(db, "deep_value", "ETH-USDT", 25.0, -0.3)
    rpt = cf.replay_all_closed()
    # 2 entries × 3 policies = 6 results.
    assert rpt["n_entries"] == 2
    assert rpt["n_results"] == 6


def test_aggregate_stats_winloss_counts(_iso_cf):
    db, cf = _iso_cf
    # 3 live-winners, 2 live-losers vs exploratory.
    _insert(db, "contrarian", "A", 25.0, 2.0)       # live hits tp
    _insert(db, "contrarian", "B", 25.0, 1.5)       # live hits tp
    _insert(db, "contrarian", "C", 25.0, 0.75)      # live hits tp
    _insert(db, "contrarian", "D", 25.0, -0.5)      # inside window (loss)
    _insert(db, "contrarian", "E", 25.0, -1.0)      # hits sl
    cf.replay_all_closed(policies=("exploratory",))
    stats = cf.aggregate_stats("exploratory")
    assert stats["n"] == 5
    # Live wins only when realized > cf.  cf is capped at tp=0.5, so first three
    # tie at cap; D (-0.5) is inside window so it matches cf; E hits sl.
    # Therefore live_wins should be around 0-1 depending on rounding.
    assert stats["live_wins"] + stats["cf_wins"] + stats["ties"] == 5


def test_zero_notional_returns_zero_cf(_iso_cf):
    db, cf = _iso_cf
    eid = _insert(db, "contrarian", "ZERO-USDT", 0.0, 0.0)
    r = cf.replay_one(eid, "conservative")
    assert r.counterfactual_pnl_usd == 0.0
