"""Phase 11n-9-c — Daily Alpha auto-executor + control-panel reorder.

Locks:
  Executor:
    1. daily_alpha_executor.is_enabled() respects SPOT_ALPHA_AUTO_EXECUTE.
    2. execute_admitted_picks is a no-op when disabled (returns []).
    3. Endpoint /daily_alpha/executions registered.
    4. Feature manifest advertises daily_alpha_executor + pre_trade_gov.
    5. Orchestrator includes a daily_alpha_execute step when executor is
       enabled (no step when disabled).
    6. _already_executed_today is true after a recorded placed execution
       and false otherwise.

  Reorder via CSS order:
    7. c-sysaudit has inline style order:-10 (top of column stack).
    8. c-engine/c-conn/c-today/c-acct all have negative order values
       (above c-alpha/c-research/c-gov which default to order:0).
    9. DOM order unchanged — c-alpha still listed BEFORE c-engine in
       source order (visual reorder only).
"""
from __future__ import annotations

import os
import re
import time
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
# Executor
# ---------------------------------------------------------------------------

def test_is_enabled_defaults_on(monkeypatch):
    """Phase 11n-9-e: executor is fully auto by default."""
    monkeypatch.delenv("SPOT_ALPHA_AUTO_EXECUTE", raising=False)
    monkeypatch.delenv("TRADE_DRY_RUN", raising=False)
    monkeypatch.delenv("SPOT_DRY_RUN", raising=False)
    from spot_aggro.governance import daily_alpha_executor
    assert daily_alpha_executor.is_enabled() is True


def test_is_enabled_off_when_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("SPOT_ALPHA_AUTO_EXECUTE", "0")
    from spot_aggro.governance import daily_alpha_executor
    assert daily_alpha_executor.is_enabled() is False


def test_is_enabled_off_on_trade_dry_run(monkeypatch):
    monkeypatch.setenv("TRADE_DRY_RUN", "1")
    from spot_aggro.governance import daily_alpha_executor
    assert daily_alpha_executor.is_enabled() is False


def test_execute_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("SPOT_ALPHA_AUTO_EXECUTE", "0")
    from spot_aggro.governance import daily_alpha_executor
    assert daily_alpha_executor.execute_admitted_picks() == []


def test_dedupe_tracks_today():
    from spot_aggro.governance import daily_alpha_executor as dae
    # Fresh DB — nothing executed today.
    assert dae._already_executed_today("BTC-USDT", "buy") is False
    # Record a placed execution.
    ex = dae.AlphaExecution(
        authz_id="at-test", ts_ms=int(time.time()*1000),
        symbol="BTC-USDT", side="buy", tier="A+",
        requested_notional=25.0, placed=True, reason="test",
    )
    dae._record_execution(ex)
    assert dae._already_executed_today("BTC-USDT", "buy") is True
    # Other symbol/side still false.
    assert dae._already_executed_today("ETH-USDT", "buy") is False
    assert dae._already_executed_today("BTC-USDT", "sell") is False


def test_executions_today_returns_newest_first():
    from spot_aggro.governance import daily_alpha_executor as dae
    for sym in ("BTC-USDT", "ETH-USDT"):
        ex = dae.AlphaExecution(
            authz_id=f"at-{sym}", ts_ms=int(time.time() * 1000),
            symbol=sym, side="buy", tier="A+",
            requested_notional=25.0, placed=True, reason="test",
        )
        dae._record_execution(ex)
        time.sleep(0.003)
    rows = dae.executions_today()
    assert len(rows) >= 2
    # Newest first.
    assert rows[0]["ts_ms"] >= rows[1]["ts_ms"]


def test_endpoint_registered():
    from spot_aggro.api import routes
    paths = {r.path for r in routes.router.routes}
    assert "/spot_aggro/daily_alpha/executions" in paths


def test_feature_manifest_advertises_executor_and_pregate():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    assert body["features"]["daily_alpha_executor"] is True
    assert body["features"]["pre_trade_gov"] is True


def test_orchestrator_runs_executor_step_by_default(monkeypatch):
    """Phase 11n-9-e: executor runs by default, not only when opted in."""
    monkeypatch.delenv("SPOT_ALPHA_AUTO_EXECUTE", raising=False)
    monkeypatch.delenv("TRADE_DRY_RUN", raising=False)
    monkeypatch.delenv("SPOT_DRY_RUN", raising=False)
    from spot_aggro.governance import auto_orchestrator
    tk = auto_orchestrator.run_tick()
    names = [s.name for s in tk.steps]
    assert "daily_alpha_execute" in names


def test_orchestrator_skips_executor_step_when_disabled(monkeypatch):
    monkeypatch.setenv("SPOT_ALPHA_AUTO_EXECUTE", "0")
    from spot_aggro.governance import auto_orchestrator
    tk = auto_orchestrator.run_tick()
    names = [s.name for s in tk.steps]
    assert "daily_alpha_execute" not in names


# ---------------------------------------------------------------------------
# Reorder via CSS order
# ---------------------------------------------------------------------------

def test_sysaudit_card_has_negative_order():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    m = re.search(
        r'<div class="[^"]*c[^"]*"\s+id="c-sysaudit"\s+style="[^"]*order:\s*(-?\d+)',
        html,
    )
    assert m, "c-sysaudit missing style with order"
    assert int(m.group(1)) < 0


def test_control_panel_cards_all_have_negative_order():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    for cid in ("c-engine", "c-conn", "c-today", "c-acct"):
        m = re.search(
            rf'id="{cid}"\s+style="[^"]*order:\s*(-?\d+)',
            html,
        )
        assert m, f"{cid} missing inline order style"
        assert int(m.group(1)) < 0, (
            f"{cid} order={m.group(1)} should be negative to place above "
            "c-alpha/c-research/c-gov (which default to order:0)"
        )


def test_alpha_card_source_order_unchanged_but_visual_order_is_below():
    """DOM source-order: c-alpha appears BEFORE c-engine (alpha block
    was placed near the top of the grid, control-panel row is in the
    middle of the file). CSS order makes c-engine render earlier."""
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    alpha_pos = html.find('id="c-alpha"')
    engine_pos = html.find('id="c-engine"')
    assert 0 < alpha_pos < engine_pos, (
        f"c-alpha must be before c-engine in DOM source "
        f"(alpha={alpha_pos}, engine={engine_pos})"
    )
    # And the CSS order on c-engine must be negative, overriding
    # c-alpha's default 0.
    m = re.search(r'id="c-engine"\s+style="[^"]*order:\s*(-?\d+)', html)
    assert m and int(m.group(1)) < 0
