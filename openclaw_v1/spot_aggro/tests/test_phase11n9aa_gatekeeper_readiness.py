"""Phase 11n-9-aa regression locks.

Covers:
  * Universe gatekeeper (step 14): auto-admit + auto-deprecate
  * Trade readiness flag (mechanical release)
  * Engine entry guard: EngineNotReady + cell-not-admitted skips
  * /spot_aggro/start honors readiness flag (409 when not ready)
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    from shared.persistence import state as persist
    persist._initialized = False
    persist.init_schema()
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS spot_pre_trade_authorizations("
        " authz_id TEXT PRIMARY KEY, ts_ms INTEGER NOT NULL, symbol TEXT,"
        " side TEXT, tier TEXT, source TEXT, passed INTEGER, score REAL,"
        " rejection TEXT, payload_json TEXT)"
    )
    con.commit()
    con.close()
    yield db


# ===========================================================================
# Universe gatekeeper
# ===========================================================================

def test_gatekeeper_seeds_tier_c_ena_dot(_isolated_db):
    """On first init, Tier-C + ENA/DOT are auto-admitted as seed."""
    from spot_aggro.governance.universe_gatekeeper import admitted_cells
    cells = admitted_cells()
    keys = {f"{c['cell_kind']}|{c['cell_key']}" for c in cells}
    assert "tier_symbol|C|ENA-USDT" in keys
    assert "tier_symbol|C|DOT-USDT" in keys


def test_gatekeeper_auto_deprecates_wilson_upper_negative(_isolated_db):
    """Plant ENA Tier-C with 60 consistently-losing exits. The
    auto-deprecation pass must flip state to deprecated_auto."""
    from spot_aggro.governance.universe_gatekeeper import (
        run_tick, is_cell_admitted,
    )
    con = sqlite3.connect(str(_isolated_db))
    ts = int(time.time() * 1000)
    for i in range(60):
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action, tier,"
            " notional_usd, pnl_usd, fee_usd, slippage_usd, net_pnl)"
            " VALUES(?, 'ENA-USDT', 'M1_scalp_C', 'exit', 'C', 10,"
            " -0.05, 0.001, 0.0, -0.051)",
            (ts + i,),
        )
    con.commit()
    con.close()
    # Before: ENA Tier-C is seeded as admitted
    assert is_cell_admitted("tier_symbol", "C|ENA-USDT") is True
    run_tick()
    # After: Wilson_upper is still < 0 on 60 losses → auto-deprecated
    assert is_cell_admitted("tier_symbol", "C|ENA-USDT") is False


def test_gatekeeper_auto_admits_wilson_lower_positive(_isolated_db):
    """Plant SYM-USDT Tier-B with 60 winners at +$0.10 each. Expect
    auto-admission because Wilson_lower > 0."""
    from spot_aggro.governance.universe_gatekeeper import (
        run_tick, is_cell_admitted,
    )
    con = sqlite3.connect(str(_isolated_db))
    ts = int(time.time() * 1000)
    # Not-seeded cell: SYM-USDT tier B
    for i in range(60):
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action, tier,"
            " notional_usd, pnl_usd, fee_usd, slippage_usd, net_pnl)"
            " VALUES(?, 'SYM-USDT', 'M1_flow_B', 'exit', 'B', 10, 0.10,"
            " 0.001, 0.0, 0.099)",
            (ts + i,),
        )
    con.commit()
    con.close()
    assert is_cell_admitted("tier_symbol", "B|SYM-USDT") is False
    run_tick()
    assert is_cell_admitted("tier_symbol", "B|SYM-USDT") is True


def test_gatekeeper_deprecated_cell_does_not_reauto_admit(_isolated_db):
    """A cell in deprecated_auto stays deprecated even if future data
    looks profitable — fresh out-of-sample data reset must be operator-
    initiated."""
    from spot_aggro.governance.universe_gatekeeper import (
        run_tick, is_cell_admitted, all_admissions,
    )
    con = sqlite3.connect(str(_isolated_db))
    ts = int(time.time() * 1000)
    # Insert 60 losers to force auto-deprecate on ENA tier C
    for i in range(60):
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action, tier,"
            " notional_usd, pnl_usd, fee_usd, slippage_usd, net_pnl)"
            " VALUES(?, 'ENA-USDT', 'M1_scalp_C', 'exit', 'C', 10,"
            " -0.05, 0.001, 0.0, -0.051)",
            (ts + i,),
        )
    con.commit()
    run_tick()
    # Now flood with wins
    for i in range(60):
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action, tier,"
            " notional_usd, pnl_usd, fee_usd, slippage_usd, net_pnl)"
            " VALUES(?, 'ENA-USDT', 'M1_scalp_C', 'exit', 'C', 10, 0.20,"
            " 0.001, 0.0, 0.199)",
            (ts + 1000 + i,),
        )
    con.commit()
    con.close()
    run_tick()
    adms = {(a.cell_kind, a.cell_key): a for a in all_admissions()}
    ena = adms.get(("tier_symbol", "C|ENA-USDT"))
    assert ena is not None
    assert ena.state == "deprecated_auto"
    assert is_cell_admitted("tier_symbol", "C|ENA-USDT") is False


# ===========================================================================
# Trade readiness
# ===========================================================================

def test_trade_readiness_starts_not_ready(_isolated_db):
    from spot_aggro.governance.trade_readiness import evaluate
    t = evaluate()
    assert t.ready is False
    # At minimum C1 (shadow not promoted) should be unmet on a fresh DB
    assert "C1_shadow_promoted_or_signflip" in t.unmet


def test_trade_readiness_becomes_ready_with_sign_flip_commit(_isolated_db, monkeypatch):
    """Simulate: SIGN_FLIP_COMMIT env var set + no freeze + admitted
    cells exist + fresh Layer 1 verdict with no admitted-fail cells.
    Expect ready=True."""
    from spot_aggro.governance.trade_readiness import evaluate
    from spot_aggro.governance.economic_truth_gov import run_once as l1_run
    monkeypatch.setenv("SIGN_FLIP_COMMIT", "abc123")
    # Trigger Layer 1 so its latest verdict is recent (within 10 min)
    l1_run()
    t = evaluate()
    # C2 (no freeze), C3 (seeded cells), C5 (fresh layer 1) should be met.
    # C4 might also be met on empty DB.
    assert "C1_shadow_promoted_or_signflip" not in t.unmet
    assert "C3_universe_has_admitted_cells" not in t.unmet
    # Final ready depends on C4+C5 which should pass on empty DB
    assert t.ready is True, f"unmet: {t.unmet}; details: {t.details}"


def test_is_ready_to_trade_fail_closed_on_unknown_state(_isolated_db):
    """Never-evaluated DB: is_ready_to_trade returns False."""
    from spot_aggro.governance.trade_readiness import is_ready_to_trade
    assert is_ready_to_trade() is False


def test_assert_ready_or_raise_when_not_ready(_isolated_db):
    from spot_aggro.governance.trade_readiness import (
        assert_ready_or_raise, EngineNotReady,
    )
    with pytest.raises(EngineNotReady) as exc_info:
        assert_ready_or_raise()
    assert exc_info.value.unmet  # at least one unmet condition


# ===========================================================================
# Engine-path wiring (string-level locks)
# ===========================================================================

def _engine_src() -> str:
    return (Path(__file__).resolve().parent.parent / "engine.py").read_text(encoding="utf-8")


def test_engine_reads_is_ready_to_trade():
    src = _engine_src()
    # Both the M1 path and the BLITZ path must guard on readiness.
    assert src.count("is_ready_to_trade()") >= 2


def test_engine_reads_is_cell_admitted():
    src = _engine_src()
    assert src.count("is_cell_admitted(") >= 2


# ===========================================================================
# Route registration
# ===========================================================================

def test_new_endpoints_registered():
    from spot_aggro.api.routes import router
    paths = {getattr(r, "path", "") for r in router.routes}
    for p in (
        "/spot_aggro/gov/trade_readiness",
        "/spot_aggro/gov/trade_readiness/run",
        "/spot_aggro/gov/universe",
        "/spot_aggro/gov/universe/run",
    ):
        assert p in paths, f"missing route: {p}"


def test_start_endpoint_honors_readiness_flag(_isolated_db, monkeypatch):
    """Hit POST /spot_aggro/start via TestClient — expect 409 when
    readiness flag is False (fresh DB = never evaluated)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from spot_aggro.api import routes as spot_routes
    app = FastAPI()
    app.include_router(spot_routes.router)
    monkeypatch.setenv("OPS_ADMIN_TOKEN", "test-token")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post(
        "/spot_aggro/start",
        headers={"X-Ops-Token": "test-token"},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["detail"]["error"] == "engine_not_ready_to_trade"
    assert body["detail"]["unmet"]   # non-empty list
