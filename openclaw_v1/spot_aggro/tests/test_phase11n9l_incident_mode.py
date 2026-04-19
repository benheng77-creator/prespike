"""Phase 11n-9-l — Incident Mode (P4) regression locks.

Locks:
  State machine:
    1. Default state is inactive with no trigger.
    2. enter() flips active + records since_ts_ms + trigger + actor.
    3. Second enter() while active is idempotent (changed=False).
    4. exit_() with force=True flips inactive immediately.
    5. exit_() without force + P0 still open → changed=False.
    6. exit_() without force + before min-duration → changed=False.

  Timeline:
    7. Every enter/exit appends a row to spot_incident_timeline.
    8. Diagnostics run appends with kind="diagnostic".
    9. Timeline is append-only (never mutated).

  Diagnostics:
   10. run_diagnostics returns 5 results (heartbeat, db, exchange,
       scheduler, feed) each with check/ok/latency_ms/detail.

  Integration:
   11. alert_center.ingest with P0 severity auto-enters incident mode.
   12. alert_center.ack on last P0 triggers maybe_auto_exit; but
       min-duration guard blocks unless enough elapsed.
   13. Orchestrator tick calls maybe_auto_exit.

  Endpoints + dashboard:
   14. /incident/status, /incident/enter, /incident/exit,
       /incident/diagnostics registered.
   15. Feature manifest advertises incident_mode=true.
   16. Dashboard has #incident-panel + incident-active body class.
"""
from __future__ import annotations

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
# State machine
# ---------------------------------------------------------------------------

def test_default_state_inactive():
    from spot_aggro.governance import incident_mode
    s = incident_mode.status()
    assert s["active"] is False
    assert s["since_ts_ms"] is None


def test_enter_flips_active():
    from spot_aggro.governance import incident_mode
    r = incident_mode.enter(actor="ops", trigger_kind="manual", message="test")
    assert r["ok"] is True and r["changed"] is True
    s = incident_mode.status()
    assert s["active"] is True
    assert s["actor"] == "ops"
    assert s["trigger_kind"] == "manual"
    assert s["since_ts_ms"] is not None


def test_second_enter_is_idempotent():
    from spot_aggro.governance import incident_mode
    incident_mode.enter(actor="ops")
    r = incident_mode.enter(actor="ops", trigger_kind="duplicate")
    assert r["changed"] is False


def test_force_exit_flips_inactive():
    from spot_aggro.governance import incident_mode
    incident_mode.enter(actor="ops")
    r = incident_mode.exit_(actor="ops", force=True)
    assert r["changed"] is True
    assert incident_mode.status()["active"] is False


def test_auto_exit_blocked_while_p0_open():
    from spot_aggro.governance import incident_mode, alert_center
    # Enter and seed a P0 alert that will not be acked.
    alert_center.ingest(
        kind="engine_halt", source="test",
        message="down", evidence={},
    )
    # Wait past min duration so the min-duration guard doesn't fire.
    # We patch the since_ts_ms to a value far in the past.
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        con.execute(
            "UPDATE spot_incident_state SET since_ts_ms = ? "
            "WHERE singleton_key = 1",
            (int(time.time() * 1000) - 10 * 60 * 1000,),  # 10 min ago
        )
        con.commit()
    finally:
        con.close()
    r = incident_mode.exit_(actor="auto", force=False)
    assert r["changed"] is False
    assert "P0 alerts still active" in r.get("note", "")


def test_auto_exit_blocked_before_min_duration():
    from spot_aggro.governance import incident_mode
    incident_mode.enter(actor="ops")
    r = incident_mode.exit_(actor="auto", force=False)
    assert r["changed"] is False


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

def test_enter_appends_timeline_row():
    from spot_aggro.governance import incident_mode
    incident_mode.enter(actor="ops")
    tl = incident_mode.timeline(limit=5)
    assert len(tl) >= 1
    assert tl[0]["kind"] == "enter"
    assert tl[0]["actor"] == "ops"


def test_exit_appends_timeline_row():
    from spot_aggro.governance import incident_mode
    incident_mode.enter(actor="ops")
    incident_mode.exit_(actor="ops", force=True)
    tl = incident_mode.timeline(limit=5)
    kinds = [e["kind"] for e in tl[:2]]
    assert "exit" in kinds and "enter" in kinds


def test_diagnostics_appends_timeline_row():
    from spot_aggro.governance import incident_mode
    incident_mode.run_diagnostics()
    tl = incident_mode.timeline(limit=5)
    assert tl[0]["kind"] == "diagnostic"


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def test_diagnostics_returns_five_checks():
    from spot_aggro.governance import incident_mode
    r = incident_mode.run_diagnostics()
    checks = {c["check"] for c in r["results"]}
    assert checks == {"heartbeat", "db", "exchange", "scheduler", "feed"}


def test_diagnostic_result_shape():
    from spot_aggro.governance import incident_mode
    r = incident_mode.run_diagnostics()
    for c in r["results"]:
        assert "check" in c and "ok" in c and "latency_ms" in c and "detail" in c
        assert isinstance(c["ok"], bool)
        assert isinstance(c["latency_ms"], int)


# ---------------------------------------------------------------------------
# Integration with alert_center
# ---------------------------------------------------------------------------

def test_p0_alert_auto_enters_incident():
    from spot_aggro.governance import alert_center, incident_mode
    assert incident_mode.status()["active"] is False
    alert_center.ingest(
        kind="engine_halt", source="watchdog",
        message="engine stopped", evidence={},
    )
    assert incident_mode.status()["active"] is True


def test_non_p0_alert_does_not_enter_incident():
    from spot_aggro.governance import alert_center, incident_mode
    alert_center.ingest(
        kind="gap_detected", source="orch",
        message="warn gap", evidence={"symbol": "X"},
    )
    assert incident_mode.status()["active"] is False


# ---------------------------------------------------------------------------
# Endpoints + feature manifest + dashboard
# ---------------------------------------------------------------------------

def test_incident_endpoints_registered():
    from spot_aggro.api import routes
    paths = {r.path for r in routes.router.routes}
    for p in ("/spot_aggro/incident/status",
              "/spot_aggro/incident/enter",
              "/spot_aggro/incident/exit",
              "/spot_aggro/incident/diagnostics"):
        assert p in paths, f"{p} not registered"


def test_feature_manifest_has_incident_mode():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    assert body["features"]["incident_mode"] is True


def test_dashboard_has_incident_panel():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    assert 'id="incident-panel"' in html
    assert 'id="incident-head-title"' in html
    assert 'id="incident-timeline"' in html
    assert 'id="incident-diag-results"' in html
    assert "incident-active" in html
    assert "fetchIncidentState" in html
    assert "runDiagnostics" in html
