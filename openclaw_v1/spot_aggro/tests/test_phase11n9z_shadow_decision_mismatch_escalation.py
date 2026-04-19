"""Phase 11n-9-z regression locks.

Steps 6-9 coverage:
  6. Shadow scorer tables + comparison runner
  7. Layer 2 decision quality (decile + rank monotonicity) + GateBypass
  8. Card-truth mismatch detector M1-M7
  9. Contradiction-freeze escalation ladder + /gov/root_cause/ack
"""
from __future__ import annotations

import sqlite3
import time

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    from shared.persistence import state as persist
    persist._initialized = False
    persist.init_schema()
    # Pre-create spot_pre_trade_authorizations (lazily built by
    # pre_trade_gov on first real call; tests want to insert rows
    # directly so we seed the table upfront).
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS spot_pre_trade_authorizations("
        " authz_id TEXT PRIMARY KEY,"
        " ts_ms INTEGER NOT NULL,"
        " symbol TEXT NOT NULL,"
        " side TEXT NOT NULL,"
        " tier TEXT,"
        " source TEXT,"
        " passed INTEGER NOT NULL,"
        " score REAL,"
        " rejection TEXT,"
        " payload_json TEXT)"
    )
    con.commit()
    con.close()
    yield db


# ===========================================================================
# STEP 6 — Shadow scorer
# ===========================================================================

def test_shadow_tables_created_on_first_write(_isolated_db):
    from spot_aggro.governance.shadow_scorer import record_shadow_authz
    record_shadow_authz(
        live_authz_id="live-1",
        symbol="X-USDT", side="buy", tier="B",
        live_score=0.3, live_passed=True,
    )
    con = sqlite3.connect(str(_isolated_db))
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'shadow_%'"
    )}
    con.close()
    for expected in (
        "shadow_pre_trade_authorizations",
        "shadow_trade_log",
        "shadow_comparison_verdicts",
    ):
        assert expected in names, f"missing table: {expected}"


def test_shadow_inverts_score(_isolated_db):
    from spot_aggro.governance.shadow_scorer import record_shadow_authz
    record_shadow_authz(
        live_authz_id="live-2",
        symbol="X-USDT", side="buy", tier="B",
        live_score=0.3, live_passed=True,
    )
    con = sqlite3.connect(str(_isolated_db))
    r = con.execute(
        "SELECT live_score, shadow_score, live_passed, shadow_passed "
        "FROM shadow_pre_trade_authorizations"
    ).fetchone()
    con.close()
    assert r[0] == pytest.approx(0.3)
    assert r[1] == pytest.approx(-0.3)
    assert r[2] == 1
    assert r[3] == 0    # shadow_score = -0.3 < 0 → rejected


def test_shadow_comparison_insufficient_below_200(_isolated_db):
    from spot_aggro.governance.shadow_scorer import run_comparison
    ab = run_comparison()
    assert ab.promotion_verdict == "insufficient"


def test_shadow_comparison_no_promote_on_losing_B(_isolated_db):
    """Plant 210 matched authz + exits. BOTH A and B admit on this
    fixture so the 200-exit floor is cleared for both sides. Every
    matched exit is losing, so B expectancy is negative and the
    promotion decision returns 'no_promote'."""
    from spot_aggro.governance.shadow_scorer import (
        record_shadow_authz, run_comparison,
    )
    con = sqlite3.connect(str(_isolated_db))
    base_ts = int(time.time() * 1000)
    # Half A-passed, half B-passed — each side gets 210 admitted picks.
    # live_score=+0.5 → live_passed=True, shadow=-0.5 → rejected.
    # live_score=-0.5 → live_passed=False, shadow=+0.5 → passed.
    for i in range(210):
        record_shadow_authz(
            live_authz_id=f"live-A-{i}",
            symbol=f"S{i % 3}-USDT", side="buy", tier="B",
            live_score=0.5, live_passed=True,
        )
    for i in range(210):
        record_shadow_authz(
            live_authz_id=f"live-B-{i}",
            symbol=f"S{i % 3}-USDT", side="buy", tier="B",
            live_score=-0.5, live_passed=False,
        )
    # Plant matched losing exits after the authz window.
    for i in range(420):
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action, tier,"
            " notional_usd, pnl_usd, fee_usd, slippage_usd, net_pnl)"
            " VALUES(?, ?, 'M1_flow_B', 'exit', 'B', 10, -0.05, 0.001,"
            " 0.0, -0.051)",
            (base_ts + 1000 + i * 1000, f"S{i % 3}-USDT"),
        )
    con.commit()
    con.close()
    ab = run_comparison()
    assert ab.promotion_verdict == "no_promote", (
        f"expected no_promote, got {ab.promotion_verdict!r}; "
        f"reason={ab.reason}; A.n={ab.window_n_a} B.n={ab.window_n_b}"
    )


# ===========================================================================
# STEP 7 — Layer 2 Decision Quality
# ===========================================================================

def test_decision_quality_insufficient_below_floor(_isolated_db):
    from spot_aggro.governance.decision_quality_gov import run_once
    v = run_once()
    assert v.overall_verdict in ("insufficient", "ok")


def test_decision_quality_detects_inversion(_isolated_db):
    """Plant 20 exits in one cell where high scores -> negative pnl
    (inverted scorer). Expect verdict='inverted' for that cell."""
    import json
    from spot_aggro.governance.decision_quality_gov import run_once
    con = sqlite3.connect(str(_isolated_db))
    base_ts = int(time.time() * 1000)
    for i in range(20):
        # Inverted: higher score (i) -> more negative pnl
        score = i / 20.0
        pnl = -0.01 * i    # higher score → bigger loss
        authz_id = f"a-{i}"
        con.execute(
            "INSERT INTO spot_pre_trade_authorizations("
            "authz_id, ts_ms, symbol, side, tier, source, passed, score,"
            " payload_json) VALUES(?, ?, ?, 'buy', 'B', 'engine', 1, ?, '{}')",
            (authz_id, base_ts + i * 1000, f"SYM-USDT", score),
        )
        # authz_id stamped in exit payload so the Layer 2 matcher can
        # pair by correlation instead of timestamp-heuristic.
        payload = json.dumps({
            "regime": "trend_up", "tier": "B", "authz_id": authz_id,
        })
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action, tier,"
            " notional_usd, pnl_usd, fee_usd, slippage_usd, net_pnl,"
            " payload_json)"
            " VALUES(?, 'SYM-USDT', 'M1_flow_B', 'exit', 'B', 10, ?,"
            " 0.001, 0.0, ?, ?)",
            (base_ts + i * 1000 + 60000, pnl, pnl - 0.001, payload),
        )
    con.commit()
    con.close()
    v = run_once()
    inverted_cells = [c for c in v.cells if c.verdict == "inverted"]
    assert inverted_cells, (
        f"expected inverted cell; got: "
        f"{[(c.cell_key, c.verdict, c.rho) for c in v.cells]}"
    )


def test_gate_bypass_exception():
    from spot_aggro.governance.decision_quality_gov import GateBypass
    exc = GateBypass("authz-x", "X-USDT", "bypass detail")
    assert "X-USDT" in str(exc)
    assert exc.authz_id == "authz-x"


def test_raise_gate_bypass_triggers_freeze(_isolated_db):
    from spot_aggro.governance.decision_quality_gov import (
        raise_gate_bypass, GateBypass,
    )
    from spot_aggro.governance.contradiction_freeze import is_entry_frozen
    with pytest.raises(GateBypass):
        raise_gate_bypass("authz-1", "X-USDT", "test")
    assert is_entry_frozen() is True


# ===========================================================================
# STEP 8 — Card-truth mismatch M1-M7
# ===========================================================================

def test_mismatch_m2_equity_writer_stale(_isolated_db):
    """Plant an equity_marks row older than 300s → M2 should fire."""
    from spot_aggro.governance.card_truth_mismatch import run_once
    con = sqlite3.connect(str(_isolated_db))
    stale_ts = int(time.time() * 1000) - 400 * 1000   # 400s old
    con.execute(
        "INSERT INTO equity_marks(ts_ms, equity_usd, peak_usd,"
        " drawdown_pct, positions_open) VALUES(?, 100, 100, 0, 0)",
        (stale_ts,),
    )
    con.commit()
    con.close()
    scan = run_once()
    rule_ids = [f.rule_id for f in scan.findings]
    assert "M2" in rule_ids


def test_mismatch_m7_gate_bypass_rate(_isolated_db):
    """Plant 60 authz rows with pass_rate < 1% + 60 entries → M7."""
    from spot_aggro.governance.card_truth_mismatch import run_once
    con = sqlite3.connect(str(_isolated_db))
    ts = int(time.time() * 1000)
    # 60 rejections
    for i in range(60):
        con.execute(
            "INSERT INTO spot_pre_trade_authorizations("
            "authz_id, ts_ms, symbol, side, tier, source, passed, score,"
            " payload_json) VALUES(?,?,?, 'buy', 'B', 'e', 0, 0.3, '{}')",
            (f"a-{i}", ts - i * 1000, "X-USDT"),
        )
    # 60 entries (bypass indicator)
    for i in range(60):
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action,"
            " tier, notional_usd) VALUES(?, 'X-USDT', 'M1_flow_B',"
            " 'enter', 'B', 10)",
            (ts - i * 1000,),
        )
    con.commit()
    con.close()
    scan = run_once()
    rule_ids = [f.rule_id for f in scan.findings]
    assert "M7" in rule_ids


def test_mismatch_writes_to_table(_isolated_db):
    """Any mismatch should persist; latest_findings returns it."""
    from spot_aggro.governance.card_truth_mismatch import run_once, latest_findings
    con = sqlite3.connect(str(_isolated_db))
    stale_ts = int(time.time() * 1000) - 400 * 1000
    con.execute(
        "INSERT INTO equity_marks(ts_ms, equity_usd, peak_usd,"
        " drawdown_pct, positions_open) VALUES(?, 100, 100, 0, 0)",
        (stale_ts,),
    )
    con.commit()
    con.close()
    run_once()
    findings = latest_findings(limit=10)
    assert findings
    assert any(f["rule_id"] == "M2" for f in findings)


def test_mismatch_triggers_t4_on_multiple(_isolated_db):
    """≥ 2 mismatches in one scan must register T4 on the freeze."""
    from spot_aggro.governance.card_truth_mismatch import run_once
    from spot_aggro.governance.contradiction_freeze import is_entry_frozen
    con = sqlite3.connect(str(_isolated_db))
    stale_ts = int(time.time() * 1000) - 400 * 1000
    con.execute(
        "INSERT INTO equity_marks(ts_ms, equity_usd, peak_usd,"
        " drawdown_pct, positions_open) VALUES(?, 100, 100, 0, 0)",
        (stale_ts,),
    )
    # Also plant M7: lots of rejects + lots of entries
    ts = int(time.time() * 1000)
    for i in range(60):
        con.execute(
            "INSERT INTO spot_pre_trade_authorizations("
            "authz_id, ts_ms, symbol, side, tier, source, passed, score,"
            " payload_json) VALUES(?,?,?, 'buy', 'B', 'e', 0, 0.3, '{}')",
            (f"a-{i}", ts - i * 1000, "X-USDT"),
        )
    for i in range(60):
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action,"
            " tier, notional_usd) VALUES(?, 'X-USDT', 'M1_flow_B',"
            " 'enter', 'B', 10)",
            (ts - i * 1000,),
        )
    con.commit()
    con.close()
    scan = run_once()
    assert len(scan.findings) >= 2
    # Freeze should now be active
    assert is_entry_frozen() is True


# ===========================================================================
# STEP 9 — Escalation ladder + /gov/root_cause/ack
# ===========================================================================

def test_time_in_freeze_zero_when_not_frozen(_isolated_db):
    from spot_aggro.governance.contradiction_freeze import _time_in_freeze_ms
    assert _time_in_freeze_ms() == 0


def test_time_in_freeze_increments(_isolated_db):
    from spot_aggro.governance.contradiction_freeze import (
        register_trigger, _time_in_freeze_ms,
    )
    register_trigger("T6", "test", "test")
    time.sleep(0.05)
    assert _time_in_freeze_ms() > 0


def test_escalation_ladder_rungs_exist():
    """Ladder rung constants must be defined in strictly ascending
    order: T+0, T+15, T+30, T+60."""
    from spot_aggro.governance.contradiction_freeze import (
        _LADDER_T_WARN_MS, _LADDER_T_P1_MS,
        _LADDER_T_FORENSIC_MS, _LADDER_T_KILL_MS,
    )
    assert _LADDER_T_WARN_MS == 0
    assert _LADDER_T_P1_MS == 15 * 60 * 1000
    assert _LADDER_T_FORENSIC_MS == 30 * 60 * 1000
    assert _LADDER_T_KILL_MS == 60 * 60 * 1000


def test_root_cause_ack_alias_registered():
    """Both /gov/contradiction_freeze/ack and /gov/root_cause/ack
    must exist as POST routes on the spot router."""
    from spot_aggro.api.routes import router
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/spot_aggro/gov/contradiction_freeze/ack" in paths
    assert "/spot_aggro/gov/root_cause/ack" in paths


def test_new_gov_routes_registered():
    """All phase-z endpoints must be on the spot router."""
    from spot_aggro.api.routes import router
    paths = {getattr(r, "path", "") for r in router.routes}
    expected = {
        "/spot_aggro/gov/economic_truth",
        "/spot_aggro/gov/economic_truth/run",
        "/spot_aggro/gov/contradiction_freeze",
        "/spot_aggro/gov/contradiction_freeze/ack",
        "/spot_aggro/gov/root_cause/ack",
        "/spot_aggro/gov/decision_quality",
        "/spot_aggro/gov/decision_quality/run",
        "/spot_aggro/gov/card_truth_mismatch",
        "/spot_aggro/gov/card_truth_mismatch/run",
        "/spot_aggro/gov/shadow_scorer",
        "/spot_aggro/gov/shadow_scorer/run",
    }
    missing = expected - paths
    assert not missing, f"missing routes: {missing}"
