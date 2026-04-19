"""Phase 11n-9-y — Gate Enforcement + Contradiction Freeze + Layer 1
Economic Truth regression locks.

Contract: the engine entry path must NEVER reach the exchange adapter
without (a) consulting is_entry_frozen() and (b) consulting
pre_trade_gov.authorize_trade() and honoring its verdict. A pick with
passed=0 must raise GateBlocked via enforce_authorized(). The
regression test plants that state and asserts the exception fires.

Also locks:
  * net_pnl column present in trade_log
  * Layer 1 Wilson expectancy module imports + returns expected shape
  * Sample-confidence floors: below floor → 'insufficient_sample'
  * Contradiction Freeze module imports + is_entry_frozen() O(1)
  * Freeze singleton state schema + history schema
  * ACK flow: mismatch rejects, verbatim match releases
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# net_pnl column
# ---------------------------------------------------------------------------

@pytest.fixture
def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    # Force schema init
    from shared.persistence import state as persist
    persist._initialized = False
    persist.init_schema()
    yield db


def test_net_pnl_column_exists(_isolated_db):
    con = sqlite3.connect(str(_isolated_db))
    cols = [r[1] for r in con.execute("PRAGMA table_info(trade_log)")]
    con.close()
    assert "net_pnl" in cols
    assert "slippage_usd" in cols


def test_backfill_net_pnl_on_migration(_isolated_db):
    con = sqlite3.connect(str(_isolated_db))
    # Simulate pre-migration row: net_pnl=NULL, pnl_usd=1.0, fee_usd=0.1
    con.execute(
        "INSERT INTO trade_log(ts_ms, symbol, module, action, pnl_usd, fee_usd) "
        "VALUES(?, 'X-USDT', 'M1_scalp_C', 'exit', 1.0, 0.1)",
        (int(time.time()*1000),),
    )
    con.commit()
    con.close()
    # Re-run migration
    from shared.persistence.state import _migrate_trade_log_net_pnl_column
    con = sqlite3.connect(str(_isolated_db))
    _migrate_trade_log_net_pnl_column(con)
    con.commit()
    r = con.execute("SELECT pnl_usd, fee_usd, net_pnl FROM trade_log").fetchone()
    con.close()
    assert r[0] == 1.0
    assert r[1] == 0.1
    assert abs(r[2] - 0.9) < 1e-9   # 1.0 - 0.1 - 0 = 0.9


# ---------------------------------------------------------------------------
# Layer 1 — Economic Truth Governor
# ---------------------------------------------------------------------------

def test_economic_truth_gov_imports():
    from spot_aggro.governance import economic_truth_gov as etg
    for name in ("run_once", "latest", "history", "wilson_wr", "SAMPLE_FLOOR"):
        assert hasattr(etg, name)


def test_wilson_wr_shape():
    from spot_aggro.governance.economic_truth_gov import wilson_wr
    # 0 of 0: no information
    p, l, u = wilson_wr(0, 0)
    assert p == 0 and l == 0 and u == 0
    # 10 of 10: upper bounded at 1.0
    p, l, u = wilson_wr(10, 10)
    assert p == 1.0
    assert u == 1.0
    assert l < 1.0      # Wilson lower bound is strictly < 1 at any finite n
    # Symmetric case
    p, l, u = wilson_wr(5, 10)
    assert abs(p - 0.5) < 1e-9
    assert l < 0.5 < u


def test_economic_truth_sample_floors(_isolated_db):
    from spot_aggro.governance.economic_truth_gov import run_once
    r = run_once()
    # Empty DB ⇒ all cells are insufficient or absent
    assert r.overall_verdict in ("insufficient_sample", "ok")
    assert r.window_n_exits == 0


def test_economic_truth_fail_on_negative_upper_bound(_isolated_db):
    """Plant 60 losing exits on tier B (above tier floor=50). Expect
    tier B cell to flip to 'fail' because even Wilson upper bound of WR
    cannot save negative expectancy."""
    from spot_aggro.governance.economic_truth_gov import run_once
    con = sqlite3.connect(str(_isolated_db))
    ts = int(time.time() * 1000)
    for i in range(60):
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action, tier, "
            "notional_usd, pnl_usd, fee_usd, slippage_usd, net_pnl) "
            "VALUES(?, ?, 'M1_flow_B', 'exit', 'B', 10, -0.05, 0.001, 0.0, -0.051)",
            (ts + i, f"SYM{i % 3}-USDT"),
        )
    con.commit()
    con.close()
    r = run_once()
    tier_b_cells = [c for c in r.cells if c.segment_kind == "tier" and c.segment_key == "B"]
    assert len(tier_b_cells) == 1
    assert tier_b_cells[0].n == 60
    assert tier_b_cells[0].verdict == "fail"
    assert tier_b_cells[0].expectancy_upper < 0


def test_economic_truth_insufficient_sample_does_not_fail(_isolated_db):
    """Tier with only 10 losing exits is under the tier-floor (50), so
    verdict must be insufficient_sample, never fail, even if every
    trade is red."""
    from spot_aggro.governance.economic_truth_gov import run_once
    con = sqlite3.connect(str(_isolated_db))
    ts = int(time.time() * 1000)
    for i in range(10):
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action, tier, "
            "notional_usd, pnl_usd, fee_usd, slippage_usd, net_pnl) "
            "VALUES(?, ?, 'M1_flow_B', 'exit', 'B', 10, -0.05, 0.001, 0.0, -0.051)",
            (ts + i, "X-USDT"),
        )
    con.commit()
    con.close()
    r = run_once()
    tier_b = [c for c in r.cells if c.segment_kind == "tier" and c.segment_key == "B"]
    assert len(tier_b) == 1
    assert tier_b[0].verdict == "insufficient_sample"


# ---------------------------------------------------------------------------
# Contradiction Freeze
# ---------------------------------------------------------------------------

def test_contradiction_freeze_imports():
    from spot_aggro.governance import contradiction_freeze as cf
    for name in ("is_entry_frozen", "tick", "register_trigger", "ack",
                 "current_state", "history", "EntryFrozen"):
        assert hasattr(cf, name)


def test_entry_frozen_default_false_on_fresh_db(_isolated_db):
    from spot_aggro.governance.contradiction_freeze import (
        is_entry_frozen, current_state,
    )
    assert is_entry_frozen() is False
    s = current_state()
    assert s["frozen"] == 0


def test_register_trigger_sets_freeze(_isolated_db):
    from spot_aggro.governance.contradiction_freeze import (
        register_trigger, is_entry_frozen, current_state,
    )
    register_trigger("T6", "gate_bypass", "Layer 2 detected bypass")
    assert is_entry_frozen() is True
    s = current_state()
    assert s["frozen"] == 1
    assert s["primary_cause"] == "T6"


def test_ack_rejects_mismatched_cause(_isolated_db):
    from spot_aggro.governance.contradiction_freeze import register_trigger, ack
    register_trigger("T6", "gate_bypass", "Layer 2 detected bypass")
    ok, msg = ack("WRONG_CAUSE")
    assert ok is False


def test_ack_accepts_verbatim_cause(_isolated_db):
    from spot_aggro.governance.contradiction_freeze import (
        register_trigger, ack, is_entry_frozen,
    )
    register_trigger("T6", "gate_bypass", "Layer 2 detected bypass")
    assert is_entry_frozen() is True
    ok, msg = ack("T6")
    assert ok is True
    assert is_entry_frozen() is False


# ---------------------------------------------------------------------------
# Gate enforcement — GateBlocked exception
# ---------------------------------------------------------------------------

def test_gate_blocked_raised_on_passed_false():
    from spot_aggro.governance.pre_trade_gov import (
        GateBlocked, enforce_authorized, TradeAuthorization,
    )
    authz = TradeAuthorization(
        authz_id="test-1", ts_ms=int(time.time()*1000),
        symbol="X-USDT", side="buy", tier="C",
        source="engine_entry:M1_scalp_C",
        passed=False, score=0.2,
        checklist=[], rejection_reason="projected_wr_meets_target",
        target_proj_wr=0.70,
    )
    with pytest.raises(GateBlocked) as exc_info:
        enforce_authorized(authz)
    assert exc_info.value.authz.symbol == "X-USDT"
    assert "projected_wr_meets_target" in str(exc_info.value)


def test_gate_blocked_not_raised_on_passed_true():
    from spot_aggro.governance.pre_trade_gov import (
        enforce_authorized, TradeAuthorization,
    )
    authz = TradeAuthorization(
        authz_id="test-2", ts_ms=int(time.time()*1000),
        symbol="X-USDT", side="buy", tier="C",
        source="engine_entry:M1_scalp_C",
        passed=True, score=0.9,
        checklist=[], rejection_reason=None,
        target_proj_wr=0.70,
    )
    # Must not raise
    enforce_authorized(authz)


# ---------------------------------------------------------------------------
# Engine wiring — string-level locks
# ---------------------------------------------------------------------------

def _engine_src() -> str:
    # tests/ -> spot_aggro/ -> engine.py
    return (Path(__file__).resolve().parent.parent / "engine.py").read_text(encoding="utf-8")


def test_engine_entry_path_calls_is_entry_frozen():
    src = _engine_src()
    # Both entry paths (M1-style + BLITZ) must read the freeze flag.
    assert src.count("is_entry_frozen()") >= 2, (
        "engine.py must call is_entry_frozen() in every entry path"
    )


def test_engine_entry_path_calls_authorize_trade_with_engine_entry_source():
    src = _engine_src()
    # The pre-trade gate must be consulted with an engine_entry source
    # in at least two places (M1/M3 module entries + BLITZ).
    assert src.count('source=f"engine_entry:') + src.count('source="engine_entry:') >= 2
