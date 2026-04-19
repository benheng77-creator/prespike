"""
Claw watchdog tests — probes, auto-heal, infra-only guarantees, commentary tagging.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from claw import commentary, watchdog  # noqa: E402
from claw.db import init_claw_schema  # noqa: E402
from claw.incidents import list_incidents  # noqa: E402


@pytest.fixture()
def tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "claw_watchdog.db")
        monkeypatch.setenv("CLAW_DB_PATH", path)
        init_claw_schema(path)
        yield path


# ---------------------------------------------------------------------------
# Built-in probes
# ---------------------------------------------------------------------------

def test_probe_db_succeeds_on_fresh_db(tmp_db):
    res = watchdog.probe_db()
    assert res.probe == "db"
    assert res.ok is True
    assert res.latency_ms >= 0.0


def test_probe_clock_forward_movement():
    res = watchdog.probe_clock()
    assert res.probe == "clock"
    assert res.ok is True


def test_probe_clock_detects_large_skew():
    res = watchdog.probe_clock(ntp_skew_ms=120_000.0, threshold_ms=30_000.0)
    assert res.ok is False
    assert "skew_ms" in res.detail


def test_ws_heartbeat_probe_returns_fail_when_silent():
    probe = watchdog.make_ws_heartbeat_probe(lambda: None)
    res = probe()
    assert res.probe == "ws"
    assert res.ok is False
    assert "no_heartbeat" in res.detail


def test_ws_heartbeat_probe_ok_when_recent():
    probe = watchdog.make_ws_heartbeat_probe(lambda: 2.0, max_age_s=10.0)
    res = probe()
    assert res.ok is True


def test_exchange_session_probe_enforces_freshness():
    probe = watchdog.make_exchange_session_probe(lambda: 45.0, max_age_s=30.0)
    res = probe()
    assert res.ok is False


# ---------------------------------------------------------------------------
# Runner + auto-heal
# ---------------------------------------------------------------------------

def test_run_probes_records_health_and_incidents(tmp_db):
    def failing_probe():
        return watchdog.ProbeResult(probe="ws", ok=False, latency_ms=1.0,
                                    detail="stub")
    report = watchdog.run_probes([watchdog.probe_db, failing_probe])
    assert any(p.probe == "db" and p.ok for p in report.probes)
    assert any(p.probe == "ws" and not p.ok for p in report.probes)
    assert report.incidents_opened == 1
    rows = list_incidents(unresolved_only=True)
    assert len(rows) == 1
    assert rows[0]["component"] == "ws"


def test_auto_heal_is_called_only_on_failure(tmp_db):
    calls = []
    def heal(r):
        calls.append(r.probe)
        return "reconnected"
    def failing_probe():
        return watchdog.ProbeResult(probe="ws", ok=False, latency_ms=1.0,
                                    detail="dropped")
    def ok_probe():
        return watchdog.ProbeResult(probe="db", ok=True, latency_ms=1.0)
    report = watchdog.run_probes(
        [ok_probe, failing_probe],
        auto_heal={"ws": heal, "db": heal},
    )
    assert calls == ["ws"]
    ws = [p for p in report.probes if p.probe == "ws"][0]
    assert ws.auto_action == "reconnected"


def test_list_recent_probes_returns_rows(tmp_db):
    watchdog.run_probes([watchdog.probe_db, watchdog.probe_clock])
    rows = watchdog.list_recent_probes()
    kinds = {r["probe"] for r in rows}
    assert {"db", "clock"}.issubset(kinds)


# ---------------------------------------------------------------------------
# Non-interference guarantees
# ---------------------------------------------------------------------------

def test_watchdog_module_does_not_import_strategy_code():
    mod = importlib.import_module("claw.watchdog")
    src = (open(mod.__file__, "r", encoding="utf-8").read() if mod.__file__ else "")
    for forbidden in (
        "from strategies", "import strategies",
        "from binary15m", "import binary15m",
        "from binary15", "import binary15",
        "from apex_v2", "import apex_v2",
    ):
        assert forbidden not in src, f"claw.watchdog must not reference {forbidden!r}"


def test_commentary_always_tags_non_authoritative(monkeypatch):
    # Stub the underlying adapter so we do not hit the network.
    def fake_annotate(payload):
        return {"available": True, "reason": "ok",
                "fields": {"narrative": "sample"}, "model": "stub"}
    monkeypatch.setenv("GEMINI_API_KEY", "stub")
    import backtest_plus.gemini_adapter as ga
    monkeypatch.setattr(ga, "annotate_run", fake_annotate)
    monkeypatch.setattr(ga, "synthesize_scenario", fake_annotate)
    monkeypatch.setattr(ga, "suggest_blend", fake_annotate)
    monkeypatch.setattr(ga, "detect_anomalies", fake_annotate)
    monkeypatch.setattr(ga, "full_report", fake_annotate)

    for fn in (
        lambda: commentary.annotate_run({"summary": {}}),
        lambda: commentary.synthesize_scenario("x"),
        lambda: commentary.suggest_blend("x"),
        lambda: commentary.detect_anomalies({}),
        lambda: commentary.full_report({}),
    ):
        out = fn()
        assert out["source"] == "claw.commentary"
        assert out["authoritative"] is False
        assert out["nic_version"] == "CLAW-NIC-v1"


def test_commentary_even_when_adapter_fails_tags_non_authoritative(monkeypatch):
    # If underlying adapter returns an error fallback, commentary must still tag.
    def fake_fail(_payload):
        return {"available": False, "reason": "no_api_key", "fields": {}}
    import backtest_plus.gemini_adapter as ga
    monkeypatch.setattr(ga, "annotate_run", fake_fail)
    out = commentary.annotate_run({"summary": {}})
    assert out["available"] is False
    assert out["authoritative"] is False
    assert out["source"] == "claw.commentary"
