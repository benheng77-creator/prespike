"""Phase 11n-9-bb — Swarm prefilter regression locks.

Cuts LLM cost by gating every swarm call on (ready_to_trade AND
admitted-tier-for-symbol AND no-freeze). Engine currently pays for
~25,400 calls/day on 40 coins; after this layer it pays for only the
admitted universe (seed: ENA + DOT). Expected cost: ~$1/day.
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
    yield db


# ---------------------------------------------------------------------------
# should_call_llm decisions
# ---------------------------------------------------------------------------

def test_should_call_llm_blocks_when_not_ready(_isolated_db):
    from spot_aggro.swarm.prefilter import should_call_llm
    ok, reason = should_call_llm("ENA-USDT", layer="fast")
    # Fresh DB: readiness is False by default (C1 unmet)
    assert ok is False
    assert "engine_not_ready" in reason


def test_should_call_llm_allows_when_all_conditions_met(_isolated_db, monkeypatch):
    """Simulate ready_to_trade=True + ENA is admitted (seed) + no freeze.
    Expect allow=True."""
    from spot_aggro.governance.trade_readiness import evaluate
    from spot_aggro.governance.economic_truth_gov import run_once as l1
    monkeypatch.setenv("SIGN_FLIP_COMMIT", "abc")
    l1()
    evaluate()
    from spot_aggro.swarm.prefilter import should_call_llm
    ok, reason = should_call_llm("ENA-USDT", layer="fast")
    assert ok is True, f"expected allow; reason={reason}"
    assert "admitted_tiers=C" in reason


def test_should_call_llm_blocks_non_admitted_symbol(_isolated_db, monkeypatch):
    """Even when ready, a coin that isn't in the admitted universe
    should not cost LLM calls."""
    from spot_aggro.governance.trade_readiness import evaluate
    from spot_aggro.governance.economic_truth_gov import run_once as l1
    monkeypatch.setenv("SIGN_FLIP_COMMIT", "abc")
    l1()
    evaluate()
    from spot_aggro.swarm.prefilter import should_call_llm
    ok, reason = should_call_llm("SOL-USDT", layer="fast")
    assert ok is False
    assert "no_admitted_tier_for_SOL-USDT" in reason


def test_should_call_llm_bypass_always_allows(_isolated_db):
    """bypass=True is for maintenance hooks (sysaudit etc.) that must
    run even when the engine is halted."""
    from spot_aggro.swarm.prefilter import should_call_llm
    ok, reason = should_call_llm("SOL-USDT", layer="sysaudit", bypass=True)
    assert ok is True
    assert reason == "bypass"


# ---------------------------------------------------------------------------
# filter_coin_list
# ---------------------------------------------------------------------------

def test_filter_coin_list_drops_non_admitted(_isolated_db, monkeypatch):
    from spot_aggro.governance.trade_readiness import evaluate
    from spot_aggro.governance.economic_truth_gov import run_once as l1
    monkeypatch.setenv("SIGN_FLIP_COMMIT", "abc")
    l1()
    evaluate()
    from spot_aggro.swarm.prefilter import filter_coin_list
    universe = [
        {"symbol": "ENA-USDT"},    # admitted
        {"symbol": "DOT-USDT"},    # admitted
        {"symbol": "SOL-USDT"},    # not admitted
        {"symbol": "INJ-USDT"},    # not admitted
        {"symbol": "PEPE-USDT"},   # not admitted
    ]
    filtered, skipped = filter_coin_list(universe, layer="heavy")
    syms = {c["symbol"] for c in filtered}
    assert syms == {"ENA-USDT", "DOT-USDT"}
    assert skipped == 3


def test_filter_coin_list_bypass_keeps_all(_isolated_db):
    from spot_aggro.swarm.prefilter import filter_coin_list
    universe = [{"symbol": "SOL-USDT"}, {"symbol": "INJ-USDT"}]
    filtered, skipped = filter_coin_list(universe, layer="heavy", bypass=True)
    assert len(filtered) == 2
    assert skipped == 0


# ---------------------------------------------------------------------------
# Engine + swarm runner wiring (string-level locks)
# ---------------------------------------------------------------------------

def _engine_src() -> str:
    return (Path(__file__).resolve().parent.parent / "engine.py").read_text(encoding="utf-8")


def _runner_src() -> str:
    return (Path(__file__).resolve().parent.parent / "swarm" / "runner.py").read_text(encoding="utf-8")


def test_engine_m1_calls_prefilter_before_consensus():
    """The engine's M1 path must import + call should_call_llm BEFORE
    run_consensus."""
    src = _engine_src()
    # should_call_llm must appear before run_consensus in file order
    idx_pre = src.find("should_call_llm(")
    idx_llm = src.find("llm_consensus.run_consensus(")
    assert idx_pre > 0, "engine must import should_call_llm"
    assert idx_pre < idx_llm, "prefilter must gate BEFORE the LLM call"


def test_engine_blitz_calls_prefilter_before_consensus():
    """Both engine entry paths (M1 + BLITZ) must have the prefilter."""
    src = _engine_src()
    assert src.count("should_call_llm(") >= 2, (
        "both entry paths must call should_call_llm"
    )


def test_swarm_heavy_layer_filters_universe():
    src = _runner_src()
    # _run_heavy must import filter_coin_list
    assert "from .prefilter import filter_coin_list" in src
    assert 'filter_coin_list(rankings, layer="heavy")' in src


def test_swarm_standard_layer_filters_universe():
    src = _runner_src()
    assert 'filter_coin_list(coins, layer="standard")' in src


def test_swarm_fast_layer_filters_universe():
    src = _runner_src()
    assert 'filter_coin_list(coins, layer="fast")' in src


# ---------------------------------------------------------------------------
# Cost telemetry endpoint
# ---------------------------------------------------------------------------

def test_llm_cost_24h_route_registered():
    from spot_aggro.api.routes import router
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/spot_aggro/gov/llm_cost_24h" in paths


def test_llm_cost_24h_returns_shape(_isolated_db):
    """Empty DB — endpoint returns 0 cost, 0 calls, empty list."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from spot_aggro.api import routes as spot_routes
    app = FastAPI()
    app.include_router(spot_routes.router)
    # Need to seed llm_cost table (lazily built); create minimal row.
    con = sqlite3.connect(str(_isolated_db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS llm_cost("
        " ts_ms INTEGER, provider TEXT, model TEXT,"
        " cost_usd REAL, latency_ms INTEGER, ok INTEGER)"
    )
    con.commit()
    con.close()
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/spot_aggro/gov/llm_cost_24h")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "total_cost_usd" in body
    assert "total_calls" in body
    assert "projected_monthly_usd" in body
    assert "by_provider_model" in body


# ---------------------------------------------------------------------------
# Feature manifest
# ---------------------------------------------------------------------------

def test_feature_flags_advertised():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    assert body["features"]["swarm_prefilter"] is True
    assert body["features"]["llm_cost_telemetry"] is True
