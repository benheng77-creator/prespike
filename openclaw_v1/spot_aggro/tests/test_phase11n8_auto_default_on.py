"""Phase 11n-8 — Auto Orchestrator defaults ON + card moved to grid bottom.

Locks:
  1. server.py starts the auto-orchestrator unless SPOT_AUTO_ORCHESTRATOR=0.
     (Flipped from opt-in to opt-out: "100% auto" default.)
  2. server.py fires a first tick on a daemon thread immediately after
     start so the dashboard has verdicts within ~5s.
  3. Research thresholds payload exposes `enforce_halt` so the dashboard
     can label the card "advisory" vs "ENFORCING".
  4. Dashboard c-auto card lives AFTER every other c-* card in the
     grid (moved to bottom per 100%-auto placement).
  5. Dashboard c-auto uses class="c span3" at the new location (full
     row at the bottom for the control-plane card).
  6. STATUS pill label is descriptive (AUTO ON / IDLE / etc.), not
     "STATUS · ERROR" on every edge case.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def test_server_defaults_auto_orchestrator_on():
    src = (REPO / "openclaw_v1" / "server.py").read_text(encoding="utf-8")
    # The env-gate check must read "0" (meaning: disable only if explicitly 0).
    # Old behavior compared to "1" (opt-in). Flip confirmed.
    assert re.search(
        r'SPOT_AUTO_ORCHESTRATOR"?,\s*"1"\s*\)\s*\.strip\(\)\s*==\s*"0"',
        src,
    ), "server.py must default auto-orchestrator ON (disable only on ==0)"


def test_server_fires_first_tick_on_startup():
    src = (REPO / "openclaw_v1" / "server.py").read_text(encoding="utf-8")
    assert "_first_tick" in src, (
        "server.py must schedule a first tick after starting the "
        "background loop so the dashboard has verdicts immediately"
    )
    assert "daemon=True" in src
    assert "run_tick()" in src


def test_research_thresholds_surface_enforce_halt(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    from shared.persistence import state as persist
    persist._initialized = False
    import importlib
    from spot_aggro.governance import research_agent as ra
    importlib.reload(ra)
    r = ra.run_research(clock=lambda: 2_000_000_000.0, status="interim")
    th = r.to_dict()["thresholds"]
    assert "enforce_halt" in th
    assert isinstance(th["enforce_halt"], bool)


def test_dashboard_auto_card_is_last_card_in_grid():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    # Slice from the start of the grid to its closing comment.
    tab_start = html.find('id="tab-dash"')
    tab_end = html.find("/tab-dash", tab_start)
    assert tab_start > 0 and tab_end > tab_start
    grid = html[tab_start:tab_end]
    # Find every top-level card id in order of appearance.
    card_ids = re.findall(r'<div[^>]+id="(c-[a-z0-9-]+)"', grid)
    assert card_ids, "no cards found in #tab-dash grid"
    assert card_ids[-1] == "c-auto", (
        "c-auto must be the LAST card in the dashboard grid "
        f"(actual last card: {card_ids[-1]!r}; full order: {card_ids})"
    )


def test_dashboard_auto_card_is_span3_at_bottom():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<div class="([^"]+)" id="c-auto"', html)
    assert m, "c-auto card not found"
    cls = m.group(1).split()
    assert "span3" in cls, (
        f"c-auto should span the full grid row at the bottom; got class={cls}"
    )


def test_dashboard_status_pill_labels_are_descriptive():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    # The old "STATUS · ERROR" label was misleading. The new ones must
    # name WHAT is happening.
    assert "STATUS · AUTO ON" in html or "STATUS · IDLE" in html, (
        "STATUS pill should use descriptive labels (AUTO ON / IDLE), "
        "not a generic ERROR"
    )
    # "STATUS · ERROR" should no longer appear as a hard label.
    assert "STATUS · ERROR" not in html, (
        "Remove the misleading 'STATUS · ERROR' pill text"
    )
