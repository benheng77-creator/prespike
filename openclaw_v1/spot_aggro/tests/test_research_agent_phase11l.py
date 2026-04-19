"""Phase 11l — Win-Rate Research Agent regression tests.

Locks:
  1. run_research() returns a ResearchReport with all 4 canonical tiers.
  2. Halt gate fires only on sufficient sample (>= MIN_SAMPLE_FOR_HALT).
  3. Hysteresis: halted tier thaws only above WR_RESUME_MIN.
  4. Thresholds are env-configurable.
  5. Recommendations list populates with severity + category.
  6. Persistence round-trip.
  7. HTTP routes: GET public; POST admin-only.
  8. Soft-halt goes through the tier-toggle mechanism, NOT direct DB writes.
  9. Reconciled positions / modules are never in the canonical tier stats.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_exits(db, tier: str, wins: int, losses: int, module="M1_flow_B"):
    """Insert `wins` winning exits + `losses` losing exits for one tier."""
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    now_ms = int(time.time() * 1000)
    try:
        for i in range(wins):
            con.execute(
                "INSERT INTO trade_log "
                "(ts_ms, symbol, module, action, side, notional_usd, avg_px, "
                " fee_usd, pnl_usd, correlation_id, payload_json, tier) "
                "VALUES (?, 'X-USDT', ?, 'exit', 'sell', 5.0, 1.0, 0.01, 0.5, NULL, '{}', ?)",
                (now_ms - i * 1000, module, tier),
            )
        for i in range(losses):
            con.execute(
                "INSERT INTO trade_log "
                "(ts_ms, symbol, module, action, side, notional_usd, avg_px, "
                " fee_usd, pnl_usd, correlation_id, payload_json, tier) "
                "VALUES (?, 'X-USDT', ?, 'exit', 'sell', 5.0, 1.0, 0.01, -0.5, NULL, '{}', ?)",
                (now_ms - (wins + i) * 1000, module, tier),
            )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Isolated trades.db + isolated tier-toggle YAML so the test's
    halt-gate flips don't pollute the shipped config.

    Phase 11m — default MIN_SAMPLE_SIZE rose from 10 → 50 (research.yml).
    These 11l tests were written against n=10 semantics; pin the env
    override + reload the module so the module-level constant is 10.
    """
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    monkeypatch.setenv("SPOT_RESEARCH_MIN_SAMPLE", "10")
    # Phase 11n-2: halt enforcement is opt-in; 11l asserts execution-lane
    # flips as a side-effect of halt verdicts, so enable enforcement here.
    monkeypatch.setenv("SPOT_RESEARCH_ENFORCE_HALT", "1")
    # Isolated toggle YAML.
    cfg_path = tmp_path / "tiers.yml"
    cfg_path.write_text(
        yaml.safe_dump({
            "schema_version": "spot.tiers.v1",
            "engine": "spot_aggro",
            "execution": {"A+": True, "A": True, "B": True, "C": True},
        }, sort_keys=False),
        encoding="utf-8",
    )
    from spot_aggro.gates.tier_toggle import TierExecutionToggle
    from spot_aggro.api import routes as spot_routes
    monkeypatch.setattr(spot_routes, "_SPOT_TIER_TOGGLE",
                        TierExecutionToggle(config_path=cfg_path),
                        raising=False)
    # Reload the research agent so MIN_SAMPLE_FOR_HALT picks up the env.
    import importlib
    from spot_aggro.governance import research_agent as ra
    importlib.reload(ra)
    from shared.persistence import state as persist
    persist._initialized = False
    return tmp_path


# ---------------------------------------------------------------------------
# Core agent behavior
# ---------------------------------------------------------------------------

def test_run_research_returns_all_four_canonical_tiers(fresh_db):
    from spot_aggro.governance.research_agent import run_research
    r = run_research(window_h=24)
    tiers = {s.tier for s in r.tier_stats}
    assert tiers == {"A+", "A", "B", "C"}


def test_halt_gate_requires_sufficient_sample(fresh_db):
    """A tier with 2 losses should NOT halt — sample too small."""
    _insert_exits(fresh_db, "B", wins=0, losses=2)
    from spot_aggro.governance.research_agent import run_research
    r = run_research(window_h=24)
    tier_b = next(s for s in r.tier_stats if s.tier == "B")
    assert tier_b.halt_verdict == "insufficient_sample"
    # Toggle must remain ON.
    assert r.halt_state.get("B") is False or r.halt_state.get("B") is None


def test_halt_gate_fires_on_low_wr_with_sufficient_sample(fresh_db):
    """With 15 exits and 2 wins (13% WR << 60%), tier must halt."""
    _insert_exits(fresh_db, "B", wins=2, losses=13)
    from spot_aggro.governance.research_agent import run_research
    r = run_research(window_h=24)
    tier_b = next(s for s in r.tier_stats if s.tier == "B")
    assert tier_b.halt_verdict == "halt", f"expected halt, got {tier_b.halt_verdict}: {tier_b.halt_reason}"
    assert tier_b.win_rate is not None and tier_b.win_rate < 0.60
    # Halt state reflects that the toggle was flipped OFF.
    assert r.halt_state["B"] is True
    # And the tier-toggle layer confirms it.
    from spot_aggro.api import routes as spot_routes
    assert spot_routes._SPOT_TIER_TOGGLE.snapshot()["B"] is False


def test_halt_gate_does_not_fire_above_halt_min(fresh_db):
    """With 12W/3L (80% WR), tier stays allowed."""
    _insert_exits(fresh_db, "A", wins=12, losses=3)
    from spot_aggro.governance.research_agent import run_research
    r = run_research(window_h=24)
    tier_a = next(s for s in r.tier_stats if s.tier == "A")
    assert tier_a.halt_verdict == "allow"
    assert tier_a.win_rate > 0.60


def test_thaw_gate_fires_when_halted_tier_recovers(fresh_db):
    """Pre-halt a tier, then insert a winning streak, rerun agent — tier
    must thaw when WR climbs above resume threshold."""
    # Pre-halt manually.
    from spot_aggro.api import routes as spot_routes
    spot_routes._SPOT_TIER_TOGGLE.set_enabled(
        "C", False, actor="test", note="pre-halt", persist=True,
    )
    # Insert winning streak (12 wins, 3 losses = 80% WR, >= 50% resume).
    _insert_exits(fresh_db, "C", wins=12, losses=3)
    from spot_aggro.governance.research_agent import run_research
    r = run_research(window_h=24)
    tier_c = next(s for s in r.tier_stats if s.tier == "C")
    assert tier_c.halt_verdict == "thaw", f"got {tier_c.halt_verdict}: {tier_c.halt_reason}"
    # Toggle is back ON.
    assert spot_routes._SPOT_TIER_TOGGLE.snapshot()["C"] is True


def test_reconciled_modules_excluded_from_canonical_stats(fresh_db):
    """M_reconciled exits must NOT count toward canonical WR — they're
    cleanup, not strategy."""
    _insert_exits(fresh_db, "B", wins=5, losses=0, module="M1_flow_B")
    _insert_exits(fresh_db, "?", wins=0, losses=20, module="M_reconciled_lowconf")
    from spot_aggro.governance.research_agent import run_research
    r = run_research(window_h=24)
    tier_b = next(s for s in r.tier_stats if s.tier == "B")
    # 5W/0L only; the 20 reconciled losses must not drag this.
    assert tier_b.n_exits == 5
    assert tier_b.n_wins == 5


def test_env_thresholds_override(monkeypatch, fresh_db):
    """Re-import the agent with SPOT_RESEARCH_WR_HALT_MIN=0.75 — tier with
    70% WR must now halt."""
    monkeypatch.setenv("SPOT_RESEARCH_WR_HALT_MIN", "0.75")
    monkeypatch.setenv("SPOT_RESEARCH_WR_RESUME_MIN", "0.65")
    # Force re-import so the module re-reads env.
    import importlib
    from spot_aggro.governance import research_agent as ra
    importlib.reload(ra)
    assert abs(ra.WR_HALT_MIN - 0.75) < 1e-6
    assert abs(ra.WR_RESUME_MIN - 0.65) < 1e-6
    # Insert 7W/3L = 70% WR, >= 10 sample but < new halt threshold.
    _insert_exits(fresh_db, "A", wins=7, losses=3)
    r = ra.run_research(window_h=24)
    tier_a = next(s for s in r.tier_stats if s.tier == "A")
    assert tier_a.halt_verdict == "halt"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_run_and_persist_round_trip(fresh_db):
    from spot_aggro.governance.research_agent import (
        run_and_persist, latest_report, history,
    )
    r = run_and_persist(window_h=24)
    stored = latest_report()
    assert stored is not None
    assert stored["report_id"] == r.report_id
    assert stored["window_h"] == 24
    hist = history(limit=5)
    assert len(hist) >= 1
    assert hist[0]["report_id"] == r.report_id


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

def test_research_latest_is_public(fresh_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from spot_aggro.api import routes as spot_routes

    app = FastAPI()
    app.include_router(spot_routes.router)
    c = TestClient(app)
    r = c.get("/spot_aggro/research/latest")
    assert r.status_code == 200
    assert "ok" in r.json()


def test_research_run_requires_admin(fresh_db, monkeypatch):
    monkeypatch.setenv("OPS_ADMIN_TOKEN", "test-token-research")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from spot_aggro.api import routes as spot_routes

    app = FastAPI()
    app.include_router(spot_routes.router)
    c = TestClient(app)

    # No token → 401.
    r = c.post("/spot_aggro/research/run")
    assert r.status_code == 401
    # Wrong token → 401.
    r = c.post("/spot_aggro/research/run",
               headers={"X-Ops-Token": "wrong"})
    assert r.status_code == 401
    # Correct token → 200 with report.
    r = c.post("/spot_aggro/research/run",
               headers={"X-Ops-Token": "test-token-research"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "report" in body
    assert body["report"]["thresholds"]["wr_halt_min"] > 0


def test_build_endpoint_advertises_research_agent_feature():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from spot_aggro.api import routes as spot_routes

    app = FastAPI()
    app.include_router(spot_routes.router)
    c = TestClient(app)
    r = c.get("/spot_aggro/build")
    assert r.json()["features"].get("research_agent") is True


# ---------------------------------------------------------------------------
# Dashboard wiring
# ---------------------------------------------------------------------------

def test_dashboard_has_research_card_and_handlers():
    s = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    assert 'id="c-research"' in s
    assert 'id="research-wr-pill"' in s
    assert 'id="research-tier-grid"' in s
    assert "fetchResearch" in s
    assert "/spot_aggro/research/latest" in s
    assert "runResearchAgent" in s
    assert "/spot_aggro/research/run" in s


def test_dashboard_has_self_heal_pass():
    s = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    assert "selfHealWarnCards" in s
    assert "setInterval(selfHealWarnCards" in s


def test_server_schedules_hourly_research():
    src = (REPO / "openclaw_v1" / "server.py").read_text(encoding="utf-8")
    assert "_start_spot_aggro_research_agent" in src
    assert "research_agent import run_and_persist" in src
    # 1h interval.
    assert "3600" in src
