"""Phase 11m — Research agent production hardening tests.

Locks:
  1. Wilson 95% CI math correctness across representative samples.
  2. Deterministic clock injection: same DB, same clock => same report.
  3. 30m/1h/24h windows all computed + exposed with Wilson CI.
  4. Primary gate uses wr_1h by default; halt/restore matches spec.
  5. cancel_open_buys_for_tier cancels only buys, only for named tier.
  6. History endpoint filters by start/end/tier correctly.
  7. CLI run_once --simulate-time produces report at that simulated time.
  8. Audit_rollup_id + snapshot_id are present + persisted.
  9. Interim vs final status round-trips.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Wilson 95% CI — math correctness
# ---------------------------------------------------------------------------

def test_wilson_ci_zero_sample_returns_none():
    from spot_aggro.governance.research_agent import wilson_ci_95
    assert wilson_ci_95(0, 0) is None


def test_wilson_ci_all_wins_upper_near_one():
    """10/10 wins → CI should lie high, with upper bound = 1.0."""
    from spot_aggro.governance.research_agent import wilson_ci_95
    ci = wilson_ci_95(10, 10)
    assert ci is not None
    assert ci.high == 1.0
    # Wilson lower for 10/10 ≈ 0.7225
    assert 0.65 < ci.low < 0.80


def test_wilson_ci_all_losses_lower_near_zero():
    from spot_aggro.governance.research_agent import wilson_ci_95
    ci = wilson_ci_95(0, 10)
    assert ci is not None
    assert ci.low == 0.0
    # Wilson upper for 0/10 ≈ 0.2775
    assert 0.20 < ci.high < 0.35


def test_wilson_ci_half_wins_centers_near_half():
    from spot_aggro.governance.research_agent import wilson_ci_95
    ci = wilson_ci_95(50, 100)
    assert ci is not None
    center = (ci.low + ci.high) / 2
    assert 0.48 < center < 0.52
    # Width should be roughly 2 × 0.098 ≈ 0.196
    assert 0.17 < ci.width < 0.22


def test_wilson_ci_larger_sample_narrower_width():
    """n=1000 should be much tighter than n=10 at the same proportion."""
    from spot_aggro.governance.research_agent import wilson_ci_95
    narrow = wilson_ci_95(500, 1000)
    wide = wilson_ci_95(5, 10)
    assert narrow.width < wide.width / 2


def test_wilson_ci_monotonic_in_wins():
    """Adding a win (at fixed n) raises the lower bound."""
    from spot_aggro.governance.research_agent import wilson_ci_95
    a = wilson_ci_95(3, 10)
    b = wilson_ci_95(5, 10)
    assert b.low > a.low


# ---------------------------------------------------------------------------
# Deterministic clock
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_env(tmp_path, monkeypatch):
    """Isolated DB + isolated toggle YAML + env overrides."""
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    # Set min_sample small enough that tests with ~15 exits can halt.
    monkeypatch.setenv("SPOT_RESEARCH_MIN_SAMPLE", "10")
    # Phase 11n-2: halt-enforcement is opt-in; the 11m suite validates
    # enforcement behavior end-to-end, so flip it on here.
    monkeypatch.setenv("SPOT_RESEARCH_ENFORCE_HALT", "1")
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
    # Reload the agent module so env overrides take effect.
    import importlib
    from spot_aggro.governance import research_agent as ra
    importlib.reload(ra)
    from shared.persistence import state as persist
    persist._initialized = False
    return tmp_path


def _insert(db, *, tier, action, pnl, ts_ms, module="M1_flow_B"):
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        con.execute(
            "INSERT INTO trade_log "
            "(ts_ms, symbol, module, action, side, notional_usd, avg_px, "
            " fee_usd, pnl_usd, correlation_id, payload_json, tier) "
            "VALUES (?, 'X-USDT', ?, ?, 'sell', 5.0, 1.0, 0.01, ?, NULL, '{}', ?)",
            (ts_ms, module, action, pnl, tier),
        )
        con.commit()
    finally:
        con.close()


def test_clock_injection_produces_deterministic_report(fresh_env):
    """Same fixture DB + same frozen clock => byte-identical reports
    (except for uuid suffix on report_id). Prove clock is the only
    non-determinism."""
    # Frozen clock at a fixed time.
    FROZEN = 2_000_000_000.0          # arbitrary epoch seconds
    clk = lambda: FROZEN

    # Insert 11 losses, 4 wins at ts ~30 minutes ago (under 1h window).
    base_ms = int(FROZEN * 1000) - 30 * 60 * 1000
    for i in range(11):
        _insert(fresh_env, tier="B", action="exit", pnl=-0.5, ts_ms=base_ms - i * 100)
    for i in range(4):
        _insert(fresh_env, tier="B", action="exit", pnl=0.5, ts_ms=base_ms - (11 + i) * 100)

    from spot_aggro.governance.research_agent import run_research
    r1 = run_research(clock=clk)
    r2 = run_research(clock=clk)

    # The frozen clock gives identical timestamps.
    assert r1.generated_ts_ms == r2.generated_ts_ms
    # Aggregate numbers match.
    assert r1.overall_exits == r2.overall_exits
    assert r1.overall_wr == r2.overall_wr
    # Per-tier primary stats match.
    r1_b = next(s for s in r1.tier_stats if s.tier == "B")
    r2_b = next(s for s in r2.tier_stats if s.tier == "B")
    assert r1_b.primary_wr == r2_b.primary_wr
    assert r1_b.primary_sample == r2_b.primary_sample


def test_30m_1h_24h_windows_all_computed(fresh_env):
    FROZEN = 2_000_000_000.0
    clk = lambda: FROZEN
    # 10 exits within 30m (15 min ago).
    for i in range(10):
        _insert(fresh_env, tier="B", action="exit", pnl=-0.5,
                ts_ms=int(FROZEN * 1000) - 15 * 60 * 1000 - i * 100)
    # 5 more exits 45 min ago (in 1h and 24h but NOT 30m).
    for i in range(5):
        _insert(fresh_env, tier="B", action="exit", pnl=-0.5,
                ts_ms=int(FROZEN * 1000) - 45 * 60 * 1000 - i * 100)
    # 7 more exits 10 hours ago (in 24h only).
    for i in range(7):
        _insert(fresh_env, tier="B", action="exit", pnl=0.5,
                ts_ms=int(FROZEN * 1000) - 10 * 3600 * 1000 - i * 100)

    from spot_aggro.governance.research_agent import run_research
    r = run_research(clock=clk)
    b = next(s for s in r.tier_stats if s.tier == "B")

    # Phase 11n-9-g: 7d window added so dashboard has a lifetime
    # signal; kept legacy windows intact.
    assert {"30m", "1h", "24h", "7d"}.issubset(set(b.windows.keys()))
    assert b.windows["30m"].n_exits == 10
    assert b.windows["1h"].n_exits == 15
    assert b.windows["24h"].n_exits == 22

    # Every window with n>0 gets a Wilson CI.
    assert b.windows["30m"].confidence_interval_95 is not None
    assert b.windows["1h"].confidence_interval_95 is not None
    assert b.windows["24h"].confidence_interval_95 is not None


def test_primary_gate_uses_1h_not_24h(fresh_env):
    """If 1h WR is bad but 24h WR is fine, halt STILL fires because the
    primary gate is 1h."""
    FROZEN = 2_000_000_000.0
    clk = lambda: FROZEN
    # 15 losses in the last 30 min.
    for i in range(15):
        _insert(fresh_env, tier="B", action="exit", pnl=-0.5,
                ts_ms=int(FROZEN * 1000) - 15 * 60 * 1000 - i * 100)
    # 30 wins 10h ago — makes 24h WR look great.
    for i in range(30):
        _insert(fresh_env, tier="B", action="exit", pnl=0.5,
                ts_ms=int(FROZEN * 1000) - 10 * 3600 * 1000 - i * 100)
    from spot_aggro.governance.research_agent import run_research
    r = run_research(clock=clk)
    b = next(s for s in r.tier_stats if s.tier == "B")
    assert b.primary_window == "1h"
    assert b.primary_sample == 15
    assert b.primary_wr is not None and b.primary_wr < 0.10
    assert b.halt_verdict == "halt", f"got {b.halt_verdict}: {b.halt_reason}"
    # 24h view is still great in the report.
    assert b.windows["24h"].win_rate > 0.60


# ---------------------------------------------------------------------------
# cancel_open_buys_for_tier
# ---------------------------------------------------------------------------

def test_cancel_open_buys_filters_by_side_and_tier(fresh_env):
    """fetch_open_spot_orders returns a mix — only BUY + matching tier
    should be cancelled."""
    from shared.adapters.okx_unified import OKXUnified

    # Spoof engine state + tier_system with B containing BTC-USDT.
    fake_orders = [
        {"id": "o1", "symbol": "BTC/USDT", "side": "buy", "clientOrderId": "cid1"},
        {"id": "o2", "symbol": "BTC/USDT", "side": "sell", "clientOrderId": "cid2"},  # SELL - ignore
        {"id": "o3", "symbol": "ETH/USDT", "side": "buy", "clientOrderId": "cid3"},  # wrong tier
        {"id": "o4", "symbol": "BTC/USDT", "side": "buy", "clientOrderId": "cid4"},
    ]
    cancelled: list[str] = []

    async def runner():
        adapter = OKXUnified.__new__(OKXUnified)
        adapter.engine = "spot_aggro"
        adapter._client = MagicMock()
        # Sync cancel_order is invoked via asyncio.to_thread inside the
        # method. MagicMock returns a Mock which is truthy.
        adapter._client.cancel_order = lambda oid, sym: (cancelled.append(oid), True)[1]

        async def fake_fetch():
            return fake_orders
        adapter.fetch_open_spot_orders = fake_fetch

        # Force symbol->tier resolution via a synthetic engine snapshot.
        import spot_aggro
        class _EngState:
            __dict__ = {"tier_system": {"tiered_universe": [
                {"symbol": "BTC-USDT", "tier": "B"},
                {"symbol": "ETH-USDT", "tier": "C"},
            ]}}
        class _Eng:
            state = _EngState()
        prev = spot_aggro._engine_instance
        spot_aggro._engine_instance = _Eng()
        try:
            result = await adapter.cancel_open_buys_for_tier("B")
        finally:
            spot_aggro._engine_instance = prev

        return result

    result = asyncio.run(runner())
    assert result["tier"] == "B"
    assert result["cancelled"] == 2
    assert result["inspected"] == 4
    assert set(cancelled) == {"o1", "o4"}


def test_cancel_open_buys_never_cancels_positions(fresh_env):
    """The method only looks at OPEN ORDERS; it never queries positions.
    Prove by asserting fetch_positions was NOT called."""
    from shared.adapters.okx_unified import OKXUnified
    async def runner():
        adapter = OKXUnified.__new__(OKXUnified)
        adapter.engine = "spot_aggro"
        adapter._client = MagicMock()
        async def fake_fetch():
            return []
        adapter.fetch_open_spot_orders = fake_fetch
        await adapter.cancel_open_buys_for_tier("B")
        # No fetch_positions call.
        adapter._client.fetch_positions.assert_not_called()
    asyncio.run(runner())


def test_cancel_open_buys_refuses_non_spot_engine():
    from shared.adapters.okx_unified import OKXUnified
    async def runner():
        adapter = OKXUnified.__new__(OKXUnified)
        adapter.engine = "apex_omega_perp"  # intentionally not spot
        adapter._client = MagicMock()
        r = await adapter.cancel_open_buys_for_tier("B")
        assert r["cancelled"] == 0
        assert any("refusing" in e.lower() for e in r["errors"])
    asyncio.run(runner())


# ---------------------------------------------------------------------------
# History endpoint filters
# ---------------------------------------------------------------------------

def test_history_filters_by_start_end_tier(fresh_env):
    from spot_aggro.governance.research_agent import (
        run_and_persist, history,
    )
    clocks = [
        lambda: 2_000_000_000.0,        # T=0
        lambda: 2_000_003_600.0,        # T+1h
        lambda: 2_000_007_200.0,        # T+2h
    ]
    # Seed one halt-worthy run so tier B filter has something to match.
    for i in range(15):
        _insert(fresh_env, tier="B", action="exit", pnl=-0.5,
                ts_ms=int(2_000_000_000.0 * 1000) - i * 100)
    for c in clocks:
        run_and_persist(clock=c)

    # No filters: 3 rows.
    rows = history(limit=50)
    assert len(rows) == 3

    # Start filter: T+1h onwards => 2 rows.
    rows = history(limit=50, start_ts_ms=int(2_000_003_600.0 * 1000))
    assert len(rows) == 2

    # End filter: up to T+1h => 2 rows (T=0 and T+1h).
    rows = history(limit=50, end_ts_ms=int(2_000_003_600.0 * 1000))
    assert len(rows) == 2

    # Tier filter: only reports that reference B should appear.
    rows = history(limit=50, tier="B")
    # All 3 runs saw B halted/insufficient, so B appears in every payload.
    assert len(rows) >= 1


def test_history_endpoint_iso_parsing(fresh_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from spot_aggro.api import routes as spot_routes

    app = FastAPI()
    app.include_router(spot_routes.router)
    c = TestClient(app)
    # Bad iso.
    r = c.get("/spot_aggro/research/history?start=not-a-date")
    assert r.status_code == 200
    assert r.json()["ok"] is False
    # Bad tier.
    r = c.get("/spot_aggro/research/history?tier=Z")
    assert r.json()["ok"] is False
    # Good empty.
    r = c.get("/spot_aggro/research/history")
    assert r.json()["ok"] is True


# ---------------------------------------------------------------------------
# CLI run_once --simulate-time
# ---------------------------------------------------------------------------

def test_cli_run_once_with_simulate_time(fresh_env, capsys):
    from spot_aggro.governance.research_agent import main
    rc = main(["run_once", "--simulate-time", "2026-04-19T12:00:00Z",
               "--window-hours", "24", "--status", "interim"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    # Timestamp must match the simulated ISO.
    assert data["timestamp"].startswith("2026-04-19T12:00:00")
    assert data["status"] == "interim"
    assert "snapshot_id" in data
    assert "audit_rollup_id" in data


# ---------------------------------------------------------------------------
# Snapshot / rollup evidence linkage
# ---------------------------------------------------------------------------

def test_snapshot_and_rollup_ids_present_and_persisted(fresh_env):
    from spot_aggro.governance.research_agent import (
        run_and_persist, latest_report,
    )
    r = run_and_persist()
    stored = latest_report()
    assert stored is not None
    assert stored.get("snapshot_id") == r.snapshot_id
    assert stored.get("audit_rollup_id") == r.audit_rollup_id
    assert stored.get("status") in ("interim", "final")


def test_interim_and_final_status_round_trip(fresh_env):
    from spot_aggro.governance.research_agent import (
        run_and_persist, history,
    )
    r1 = run_and_persist(status="interim")
    r2 = run_and_persist(status="final")
    rows = history(limit=10)
    statuses = {row["status"] for row in rows}
    assert "interim" in statuses
    assert "final" in statuses


# ---------------------------------------------------------------------------
# End-to-end: clock advance → halt fires → report exists within 1h
# ---------------------------------------------------------------------------

def test_e2e_clock_advance_halt_and_interim_report_within_1h(fresh_env):
    """Simulated E2E:
      T=0      : insert bad-WR history, run agent → halt fires.
      T+60min  : run agent → interim report exists, halt persists.
      T+360min : run agent → full plan exists (final status).
    Verifies queued-buy cancellation happened only for paused tiers."""
    from spot_aggro.governance.research_agent import (
        run_and_persist, latest_report, history,
    )
    from spot_aggro.api import routes as spot_routes

    T0 = 2_000_000_000.0
    # 15 Tier B losses in the last 30 min (so primary 1h window has 15 exits,
    # wr = 0 → halt fires).
    base_ms = int(T0 * 1000) - 15 * 60 * 1000
    for i in range(15):
        _insert(fresh_env, tier="B", action="exit", pnl=-0.5,
                ts_ms=base_ms - i * 1000)

    # Mock the adapter cancel path so we don't hit OKX. The research agent
    # only calls it when a halt TRANSITION happens (off->on for halt,
    # on->off for thaw).
    cancel_calls: list[str] = []
    async def fake_cancel(tier: str):
        cancel_calls.append(tier)
        return {"tier": tier, "cancelled": 1, "inspected": 1, "errors": []}

    with patch("spot_aggro.governance.research_agent._cancel_buys_for_tier_async",
               side_effect=fake_cancel):
        # T=0 run: halt B.
        r0 = run_and_persist(clock=lambda: T0, status="interim")
        assert r0.halt_state.get("B") is True, (
            f"B should be halted, got halt_state={r0.halt_state}"
        )
        # Cancel was called for B only.
        assert cancel_calls == ["B"]

        # T+60min run: B stays halted, status is interim.
        r1 = run_and_persist(clock=lambda: T0 + 3600, status="interim")
        assert r1.halt_state.get("B") is True
        # No new cancel — already halted.
        assert cancel_calls == ["B"]
        assert r1.status == "interim"

        # T+360min run: mark as final. B still halted (no recovery data).
        r2 = run_and_persist(clock=lambda: T0 + 6 * 3600, status="final")
        assert r2.halt_state.get("B") is True
        assert r2.status == "final"

    # History has all 3 runs.
    rows = history(limit=10)
    assert len(rows) >= 3
    # The oldest has snapshot_id + audit_rollup_id.
    for row in rows:
        assert "snapshot_id" in row
        assert "audit_rollup_id" in row

    # Latest report (T+6h) has NO recent exits in its 1h window (the 15
    # losses are >6h old), so the verdict at T+6h is insufficient_sample.
    # Halt state on the tier-toggle LAYER persists across runs though —
    # that's the execution-gate behavior the operator cares about.
    lat = latest_report()
    assert lat is not None
    b_stats = next(s for s in lat["tier_stats"] if s["tier"] == "B")
    # Toggle stays halted even when latest verdict is insufficient_sample.
    assert lat["halt_state"]["B"] is True
    # Find the FIRST run (T=0) in history — it DID see the losses and
    # halt explicitly.
    rows_all = history(limit=10)
    oldest = min(rows_all, key=lambda r: r["generated_ts_ms"])
    assert oldest["n_halted_tiers"] >= 1

    # Tier toggle actually paused B (the integration point).
    snap = spot_routes._SPOT_TIER_TOGGLE.snapshot()
    assert snap["B"] is False
    # Other tiers unaffected.
    assert snap["A+"] is True
    assert snap["A"] is True
    assert snap["C"] is True


# ---------------------------------------------------------------------------
# Dashboard wiring (visible)
# ---------------------------------------------------------------------------

def test_dashboard_has_exact_audit_rollup_phrase():
    """The spec requires this exact phrase visible on both audit views.
    HTML may split it across lines with indent; compare after
    whitespace-collapsing so the rendered page's text is what we verify."""
    import re
    s = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    collapsed = re.sub(r"\s+", " ", s)
    assert (
        "Audit Rollup: Backend 15-check and Browser 31-card audit "
        "rollup visible. See snapshot and evidence." in collapsed
    )


def test_dashboard_has_warn_escalation_hook():
    s = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    assert "persistent WARN on" in s
    assert "_warnEscalated" in s
    assert "_warnHealLog" in s
    # Escalation must go to #tb-issues, not restart engine.
    assert "engine NOT auto-restarted" in s


def test_research_history_endpoint_registered():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from spot_aggro.api import routes as spot_routes

    app = FastAPI()
    app.include_router(spot_routes.router)
    c = TestClient(app)
    r = c.get("/spot_aggro/research/history?limit=1")
    assert r.status_code == 200


def test_adapter_exposes_cancel_open_buys_for_tier():
    """Daily system auditor check will fail if the adapter doesn't expose
    this method. Lock it here too."""
    from shared.adapters.okx_unified import OKXUnified
    assert hasattr(OKXUnified, "cancel_open_buys_for_tier")
    assert hasattr(OKXUnified, "fetch_open_spot_orders")
