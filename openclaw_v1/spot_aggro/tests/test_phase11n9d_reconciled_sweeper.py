"""Phase 11n-9-d — Reconciled Sweeper.

Sweeps orphan reconciled positions under spot_aggro governance.
Phase 11n-9-e: default FULLY AUTO (SPOT_RECON_SWEEP_EXECUTE defaults
to "1"; set =0 or TRADE_DRY_RUN=1 to disable). Every orchestrator
tick builds + ships the plan through Layer 8 pre_trade_gov.

Locks:
  Decision logic:
    1. big loss (ret ≤ -15%)  → action=sell
    2. big win  (ret ≥ +15%)  → action=sell
    3. dust (value < $1 AND age > 24h) → sell
    4. stale (age ≥ 72h AND |ret| < 2%) → sell
    5. link-eligible win (ret ≥ +3% AND age ≤ 24h) → link
    6. otherwise → keep

  Safety:
    7. is_execute_enabled defaults False.
    8. execute_plan is a no-op in dry-run.

  Integration:
    9. /recon/sweep/plan (GET) and /recon/sweep (POST) registered.
   10. Feature manifest advertises reconciled_sweeper.
   11. Orchestrator runs recon_sweep_plan (or _execute) each tick.
   12. Dashboard has sweep-status-pill + sweep-mode-pill +
       sweep-summary + sweep-actions slots.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    from shared.persistence import state as persist
    persist._initialized = False
    yield


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def test_big_loss_triggers_sell():
    from spot_aggro.governance.reconciled_sweeper import _decide
    action, _ = _decide(value_usd=20.0, live_ret=-0.20, age_h=48)
    assert action == "sell"


def test_big_win_triggers_sell():
    from spot_aggro.governance.reconciled_sweeper import _decide
    action, _ = _decide(value_usd=20.0, live_ret=0.25, age_h=12)
    assert action == "sell"


def test_dust_under_min_notional_tagged_unsellable():
    """Phase 11n-9-e: OKX rejects spot sells below ~$1. Dust gets
    tagged non-sellable instead of a doomed sell."""
    from spot_aggro.governance.reconciled_sweeper import _decide
    action, _ = _decide(value_usd=0.50, live_ret=0.0, age_h=30)
    assert action == "dust_unsellable"


def test_stale_with_flat_ret_triggers_sell():
    from spot_aggro.governance.reconciled_sweeper import _decide
    action, _ = _decide(value_usd=15.0, live_ret=0.01, age_h=80)
    assert action == "sell"


def test_link_eligible_young_winner():
    from spot_aggro.governance.reconciled_sweeper import _decide
    action, _ = _decide(value_usd=20.0, live_ret=0.05, age_h=10)
    assert action == "link"


def test_neutral_kept():
    from spot_aggro.governance.reconciled_sweeper import _decide
    # Value ok, minor loss, not stale yet.
    action, _ = _decide(value_usd=20.0, live_ret=-0.03, age_h=24)
    assert action == "keep"


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

def test_is_execute_enabled_defaults_on(monkeypatch):
    """Phase 11n-9-e: default ON (fully auto)."""
    monkeypatch.delenv("SPOT_RECON_SWEEP_EXECUTE", raising=False)
    monkeypatch.delenv("TRADE_DRY_RUN", raising=False)
    monkeypatch.delenv("SPOT_DRY_RUN", raising=False)
    from spot_aggro.governance import reconciled_sweeper
    assert reconciled_sweeper.is_execute_enabled() is True


def test_is_execute_enabled_off_when_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("SPOT_RECON_SWEEP_EXECUTE", "0")
    from spot_aggro.governance import reconciled_sweeper
    assert reconciled_sweeper.is_execute_enabled() is False


def test_is_execute_enabled_off_on_trade_dry_run(monkeypatch):
    monkeypatch.setenv("TRADE_DRY_RUN", "1")
    from spot_aggro.governance import reconciled_sweeper
    assert reconciled_sweeper.is_execute_enabled() is False


def test_execute_plan_is_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("SPOT_RECON_SWEEP_EXECUTE", "0")
    from spot_aggro.governance import reconciled_sweeper as rs
    p = rs.build_plan()
    p2 = rs.execute_plan(p)
    assert p2 is p  # returned unchanged
    for a in p2.actions:
        assert a.executed is False


# ---------------------------------------------------------------------------
# Endpoints + dashboard + orchestrator
# ---------------------------------------------------------------------------

def test_endpoints_registered():
    from spot_aggro.api import routes
    paths = {r.path for r in routes.router.routes}
    for p in ("/spot_aggro/recon/sweep/plan",
              "/spot_aggro/recon/sweep"):
        assert p in paths, f"{p} not registered"


def test_feature_manifest_has_sweeper():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    assert body["features"]["reconciled_sweeper"] is True


def test_orchestrator_runs_recon_sweep_step():
    from spot_aggro.governance import auto_orchestrator
    tk = auto_orchestrator.run_tick()
    names = [s.name for s in tk.steps]
    # Whether dry-run or execute mode, SOME sweep step must appear.
    assert any(n in names for n in ("recon_sweep_plan", "recon_sweep_execute"))


def test_dashboard_has_sweep_panel_slots():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    for slot in ("sweep-status-pill", "sweep-mode-pill",
                 "sweep-summary", "sweep-actions"):
        assert f'id="{slot}"' in html, f"missing {slot}"
    assert "Sweep Now" in html
