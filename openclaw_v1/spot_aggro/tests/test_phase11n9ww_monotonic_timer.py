"""Phase 11n-9-ww — monotonic trade-timer anchor regression.

Repeated calls must return the SAME anchor_ts_ms until a real CDV
trade opens. The earlier implementation reset to now_ms every call
when spot_live_variant_entries was missing, causing the UI counter
to reset on every dashboard refresh.
"""
from __future__ import annotations

import importlib
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def _iso_timer_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    monkeypatch.setenv("FEATURE_CONTRARIAN_DEEPVALUE_PANEL", "1")
    # Reload modules so the new env is picked up.
    for m in (
        "spot_aggro.governance.variant_trip_wire",
        "spot_aggro.api.routes_strategy_cdv",
    ):
        mod = importlib.import_module(m)
        importlib.reload(mod)
    yield db


def test_anchor_persists_across_calls(_iso_timer_db):
    """Two calls with no trade activity must return the same anchor."""
    from spot_aggro.api.routes_strategy_cdv import _build_trade_timer
    now = int(time.time() * 1000)
    r1 = _build_trade_timer(now)
    anchor1 = r1["ticking_from_ts_ms"]
    # Later call with advanced clock must keep the anchor stable.
    r2 = _build_trade_timer(now + 5_000)
    assert r2["ticking_from_ts_ms"] == anchor1, (
        f"anchor moved {anchor1} -> {r2['ticking_from_ts_ms']} between calls"
    )
    # elapsed_ms_server must advance.
    assert r2["elapsed_ms_server"] > r1["elapsed_ms_server"]


def test_anchor_advances_when_new_trade_opens(_iso_timer_db):
    """Anchor must jump forward when a new CDV trade is recorded."""
    from spot_aggro.api.routes_strategy_cdv import _build_trade_timer
    db = _iso_timer_db
    now = int(time.time() * 1000)

    # Seed anchor with no trades.
    _build_trade_timer(now)

    # Simulate a new CDV trade opening 100s later.
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS spot_live_variant_entries("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " variant TEXT, status TEXT,"
        " notional_usd REAL, realized_pnl_usd REAL,"
        " opened_ts_ms INTEGER, closed_ts_ms INTEGER)"
    )
    trade_ts = now + 100_000
    con.execute(
        "INSERT INTO spot_live_variant_entries("
        " variant, status, notional_usd, opened_ts_ms)"
        " VALUES(?,?,?,?)",
        ("contrarian", "open", 25.0, trade_ts),
    )
    con.commit()
    con.close()

    r2 = _build_trade_timer(now + 105_000)
    assert r2["ticking_from_ts_ms"] == trade_ts, (
        f"anchor should advance to trade open time, got {r2['ticking_from_ts_ms']} vs {trade_ts}"
    )
    assert "trade_opened:contrarian" in r2["anchor_reason"]


def test_operator_reset_moves_anchor_to_now(_iso_timer_db):
    """POST /trade_timer/reset must re-anchor to now."""
    from spot_aggro.api.routes_strategy_cdv import (
        _build_trade_timer, cdv_trade_timer_reset,
    )
    now = int(time.time() * 1000)
    r1 = _build_trade_timer(now)
    anchor1 = r1["ticking_from_ts_ms"]
    # Reset.
    reset_resp = cdv_trade_timer_reset(
        x_cdv_role="strategy:contrarian_deepvalue_admin",
        x_ops_token=None,   # role header alone is sufficient in test
    )
    assert reset_resp["ok"] is True
    # Subsequent call reads the new anchor.
    r2 = _build_trade_timer(now + 1_000)
    assert r2["ticking_from_ts_ms"] >= anchor1, "reset anchor went backwards"
    assert r2["anchor_reason"] == "operator_reset"


def test_anchor_survives_module_reload(_iso_timer_db):
    """Anchor must survive a process restart — it's persisted to disk."""
    from spot_aggro.api.routes_strategy_cdv import _build_trade_timer
    now = int(time.time() * 1000)
    r1 = _build_trade_timer(now)
    anchor1 = r1["ticking_from_ts_ms"]
    # Simulate restart by reloading the module.
    import spot_aggro.api.routes_strategy_cdv as m
    importlib.reload(m)
    r2 = m._build_trade_timer(now + 3_000)
    assert r2["ticking_from_ts_ms"] == anchor1, (
        "anchor lost on module reload — not persisted to disk"
    )
