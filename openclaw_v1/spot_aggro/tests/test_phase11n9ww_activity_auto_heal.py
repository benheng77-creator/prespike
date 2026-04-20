"""Phase 11n-9-ww — activity auto-heal governor tests.

Covers:
  - _severity_of classifies components into green/yellow/red correctly.
  - evaluate() records an event per unhealthy component.
  - Cooldown skip: second evaluate() within COOLDOWN_S records skipped_cooldown.
  - Strike cap: after STRIKE_CAP consecutive non-heal outcomes, struck_out.
  - Healer registry has entries for every real component.
  - Safety: engine_heartbeat + kill_ladder are observe-only (did_heal=False).
  - recent_events() returns rows newest-first.
  - SERVER_BUILD = phase-ww + feature flag flipped.
"""
from __future__ import annotations

import importlib
import sqlite3
import time
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def _iso_heal_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    monkeypatch.setenv("SPOT_AUTO_HEAL_COOLDOWN_S", "300")
    monkeypatch.setenv("SPOT_AUTO_HEAL_STRIKE_CAP", "3")
    import spot_aggro.governance.activity_auto_heal as ah
    importlib.reload(ah)
    ah._init_schema()
    yield db, ah


def test_severity_classification(_iso_heal_db):
    _, ah = _iso_heal_db
    assert ah._severity_of({"ok": True, "status": "running"}) == "green"
    assert ah._severity_of({"ok": False, "status": "stale"}) == "yellow"
    assert ah._severity_of({"ok": False, "status": "error"}) == "red"
    assert ah._severity_of({"ok": False, "status": "halted"}) == "red"
    assert ah._severity_of({"ok": False, "status": "idle"}) == "red"
    assert ah._severity_of({"ok": False, "status": "pending"}) == "yellow"
    assert ah._severity_of({"ok": False, "status": "escalated"}) == "yellow"


def test_healer_registry_covers_core_components(_iso_heal_db):
    _, ah = _iso_heal_db
    for name in [
        "formula_review", "daily_report", "heartbeat_writer",
        "exchange_comparison", "shadow_scorer", "engine_heartbeat",
        "kill_ladder",
    ]:
        assert name in ah.HEALERS, f"missing healer for {name}"


def test_engine_heartbeat_is_observe_only(_iso_heal_db):
    _, ah = _iso_heal_db
    label, did_heal, note = ah._heal_engine_heartbeat()
    assert did_heal is False
    assert "observe-only" in label or "operator" in note


def test_kill_ladder_is_observe_only(_iso_heal_db):
    _, ah = _iso_heal_db
    label, did_heal, note = ah._heal_kill_ladder()
    assert did_heal is False
    assert "operator" in note


def test_evaluate_records_event_for_yellow_component(_iso_heal_db, monkeypatch):
    db, ah = _iso_heal_db

    def _fake_components():
        return [{
            "name": "formula_review",
            "label": "Governance: Formula Review",
            "status": "stale", "ok": False,
            "observation": "last tick 7h ago",
        }]
    monkeypatch.setattr(ah, "_fetch_components", _fake_components)

    rpt = ah.evaluate()
    assert rpt["n_yellow"] == 1
    assert rpt["n_red"] == 0
    assert len(rpt["results"]) == 1
    r = rpt["results"][0]
    assert r["component"] == "formula_review"
    # Outcome is healed OR failed depending on whether run() succeeds; either
    # way the row is recorded.
    con = sqlite3.connect(str(db))
    n = con.execute(
        "SELECT COUNT(*) FROM spot_activity_heal_events"
        " WHERE component='formula_review'"
    ).fetchone()[0]
    con.close()
    assert n == 1


def test_cooldown_skips_second_attempt(_iso_heal_db, monkeypatch):
    db, ah = _iso_heal_db
    monkeypatch.setattr(ah, "_fetch_components", lambda: [{
        "name": "formula_review", "status": "stale", "ok": False,
        "observation": "stale",
    }])

    ah.evaluate()                              # first attempt
    rpt2 = ah.evaluate()                       # second, still in cooldown
    outcomes = [r["outcome"] for r in rpt2["results"]]
    assert "skipped_cooldown" in outcomes


def test_strike_cap_fires_struck_out(_iso_heal_db, monkeypatch):
    db, ah = _iso_heal_db
    # Make cooldown effectively zero and force healer to always fail.
    monkeypatch.setenv("SPOT_AUTO_HEAL_COOLDOWN_S", "0")
    importlib.reload(ah)
    ah._init_schema()
    monkeypatch.setattr(ah, "_fetch_components", lambda: [{
        "name": "formula_review", "status": "stale", "ok": False,
        "observation": "stale",
    }])
    monkeypatch.setattr(
        ah, "_heal_formula_review",
        lambda: ("formula_review.run()", False, "forced failure"),
    )
    ah.HEALERS["formula_review"] = ah._heal_formula_review

    # Run STRIKE_CAP attempts — all should fail, populating strikes.
    for _ in range(ah.STRIKE_CAP):
        ah.evaluate()
    # Next pass should struck_out.
    rpt = ah.evaluate()
    outcomes = [r["outcome"] for r in rpt["results"]]
    assert "struck_out" in outcomes


def test_recent_events_newest_first(_iso_heal_db, monkeypatch):
    _, ah = _iso_heal_db
    # Hand-insert three events.
    con = sqlite3.connect(str(_iso_heal_db[0]))
    base = int(time.time() * 1000)
    for i, kind in enumerate(("healed", "failed", "healed")):
        con.execute(
            "INSERT INTO spot_activity_heal_events("
            " ts_ms, component, severity, action, outcome, rationale, context_json"
            ") VALUES(?,?,?,?,?,?,?)",
            (base + i * 100, "formula_review", "yellow",
             "test", kind, f"event {i}", "{}"),
        )
    con.commit()
    con.close()
    rows = ah.recent_events(limit=10)
    assert len(rows) == 3
    # Newest first.
    assert rows[0]["ts_ms"] >= rows[-1]["ts_ms"]


def test_server_build_phase_ww():
    from spot_aggro.api import routes as r
    importlib.reload(r)
    assert "ww" in r.SERVER_BUILD, r.SERVER_BUILD


def test_activity_auto_heal_flag_present():
    from spot_aggro.api import routes as r
    src = Path(r.__file__).read_text(encoding="utf-8")
    assert '"activity_auto_heal"' in src, "feature flag missing"


def test_dashboard_meta_phase_ww():
    for html in (
        REPO / "web" / "ops" / "index.html",
        REPO / "web" / "strategy" / "contrarian-deepvalue" / "index.html",
    ):
        txt = html.read_text(encoding="utf-8", errors="replace")
        assert "phase-11n-9-ww-2026-04-20" in txt, f"{html} meta not bumped"
