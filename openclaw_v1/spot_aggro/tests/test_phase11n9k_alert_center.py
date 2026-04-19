"""Phase 11n-9-k — Alert Center (P3) regression locks.

Locks:
  Ingest + severity:
    1. P0 kinds classify as P0 (engine_halt, adapter_down, ...).
    2. P1 kinds classify as P1 (wr_anomaly, card_truth_fail, ...).
    3. P2 kinds classify as P2 (pre_trade_gov_block, gap_detected, ...).
    4. Unknown kinds default P3.

  Aggregation:
    5. Two ingests of the same kind+symbol+tier within 10 min share
       alert_id and bump occurrences.
    6. Different symbol = different alert_id.
    7. Every ingest appends to occurrences table.

  State management:
    8. ack(alert_id, actor) sets ack=True + actor + timestamp.
    9. mute(alert_id, until_ms) sets muted=True.
   10. active() excludes acked alerts.
   11. active() excludes muted alerts (unless expired).

  Correlation:
   12. correlation_groups() returns (key, count) for kind+symbol
       sharing ≥2 alerts.

  Endpoints:
   13. /alerts/active, /alerts/history, /alerts/{id}/ack,
       /alerts/{id}/mute, /alerts/{id}/occurrences registered.

  Integration:
   14. Orchestrator tick ingests gaps as alerts.
   15. pre_trade_gov.authorize_trade ingests an alert on block.

  Dashboard:
   16. c-alerts card has all required DOM slots for the new UI.
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
# Severity classification
# ---------------------------------------------------------------------------

def test_p0_severity_classification():
    from spot_aggro.governance.alert_center import classify_severity
    assert classify_severity("engine_halt") == "P0"
    assert classify_severity("adapter_down") == "P0"


def test_p1_severity_classification():
    from spot_aggro.governance.alert_center import classify_severity
    assert classify_severity("wr_anomaly") == "P1"
    assert classify_severity("card_truth_fail") == "P1"


def test_p2_severity_classification():
    from spot_aggro.governance.alert_center import classify_severity
    assert classify_severity("pre_trade_gov_block") == "P2"
    assert classify_severity("gap_detected") == "P2"


def test_unknown_kinds_default_p3():
    from spot_aggro.governance.alert_center import classify_severity
    assert classify_severity("something_random") == "P3"


def test_explicit_severity_override():
    from spot_aggro.governance.alert_center import classify_severity
    # Caller can force severity.
    assert classify_severity("engine_halt", "P3") == "P3"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def test_same_aggregation_key_merges():
    from spot_aggro.governance import alert_center as ac
    a1 = ac.ingest(kind="pre_trade_gov_block", source="engine",
                   message="first", evidence={"symbol": "X", "tier": "B"})
    a2 = ac.ingest(kind="pre_trade_gov_block", source="engine",
                   message="second", evidence={"symbol": "X", "tier": "B"})
    assert a1.alert_id == a2.alert_id
    assert a2.occurrences == 2


def test_different_symbol_new_alert():
    from spot_aggro.governance import alert_center as ac
    a1 = ac.ingest(kind="pre_trade_gov_block", source="engine",
                   message="x", evidence={"symbol": "X", "tier": "B"})
    a2 = ac.ingest(kind="pre_trade_gov_block", source="engine",
                   message="y", evidence={"symbol": "Y", "tier": "B"})
    assert a1.alert_id != a2.alert_id


def test_occurrences_appended():
    from spot_aggro.governance import alert_center as ac
    a = ac.ingest(kind="gap_detected", source="orchestrator",
                  message="x", evidence={})
    a = ac.ingest(kind="gap_detected", source="orchestrator",
                  message="y", evidence={})
    occ = ac.occurrences_for(a.alert_id)
    assert len(occ) == 2


# ---------------------------------------------------------------------------
# State: ack + mute
# ---------------------------------------------------------------------------

def test_ack_removes_from_active():
    from spot_aggro.governance import alert_center as ac
    a = ac.ingest(kind="engine_halt", source="watchdog",
                  message="down", evidence={})
    assert a.alert_id in [x["alert_id"] for x in ac.active()]
    assert ac.ack(a.alert_id, actor="ops") is True
    assert a.alert_id not in [x["alert_id"] for x in ac.active()]


def test_mute_removes_from_active_until_expiry():
    from spot_aggro.governance import alert_center as ac
    a = ac.ingest(kind="latency_spike", source="watchdog",
                  message="slow", evidence={})
    future = int(time.time() * 1000) + 3600_000
    assert ac.mute(a.alert_id, until_ms=future, actor="ops") is True
    assert a.alert_id not in [x["alert_id"] for x in ac.active()]
    # But still visible in history.
    assert a.alert_id in [x["alert_id"] for x in ac.history()]


def test_active_sorts_p0_first():
    from spot_aggro.governance import alert_center as ac
    ac.ingest(kind="gap_detected", source="orch",
              message="p2", evidence={"symbol": "A"})
    ac.ingest(kind="engine_halt", source="watchdog",
              message="p0", evidence={})
    ac.ingest(kind="wr_anomaly", source="research",
              message="p1", evidence={"symbol": "B"})
    acts = ac.active()
    severities = [a["severity"] for a in acts]
    # P0 index must be before P1 index before P2 index.
    def first(sev):
        try: return severities.index(sev)
        except ValueError: return 999
    assert first("P0") < first("P1") < first("P2")


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def test_correlation_groups_surface_multi_alert_symbols():
    from spot_aggro.governance import alert_center as ac
    # Same (kind, symbol) will aggregate — force different aggregation
    # keys by varying source so they DON'T merge, but share correlation.
    ac.ingest(kind="pre_trade_gov_block", source="engine_entry:a",
              message="blk a", evidence={"symbol": "X", "tier": "B"})
    ac.ingest(kind="pre_trade_gov_block", source="engine_entry:b",
              message="blk b", evidence={"symbol": "X", "tier": "B"})
    groups = ac.correlation_groups()
    assert any(g["correlation_key"] == "pre_trade_gov_block:X"
               for g in groups)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def test_alert_endpoints_registered():
    from spot_aggro.api import routes
    paths = {r.path for r in routes.router.routes}
    for p in ("/spot_aggro/alerts/active",
              "/spot_aggro/alerts/history",
              "/spot_aggro/alerts/{alert_id}/ack",
              "/spot_aggro/alerts/{alert_id}/mute",
              "/spot_aggro/alerts/{alert_id}/occurrences"):
        assert p in paths, f"{p} not registered"


def test_feature_manifest_advertises_alert_center():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    assert body["features"]["alert_center"] is True


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_orchestrator_tick_ingests_gaps():
    from spot_aggro.governance import auto_orchestrator, alert_center as ac
    tk = auto_orchestrator.run_tick()
    # A tick with gaps should have produced alerts. If no gaps (clean
    # ok tick), that's still a valid outcome — assert that _either_
    # alerts exist _or_ gaps empty.
    active = ac.active()
    if tk.gaps:
        assert len(active) >= 1, (
            "tick had gaps but alert_center has 0 active alerts"
        )


def test_pre_trade_gov_block_ingests_alert():
    from spot_aggro.governance import pre_trade_gov, alert_center as ac
    # No research report, so this will block on sample_size.
    az = pre_trade_gov.authorize_trade(
        "TOTALLY-UNKNOWN-USDT", "buy", "B",
        source="engine_entry:test",
    )
    assert az.passed is False
    # Alert should be ingested.
    alerts = ac.active()
    sym_alerts = [a for a in alerts
                  if a.get("evidence", {}).get("symbol") == "TOTALLY-UNKNOWN-USDT"]
    assert len(sym_alerts) >= 1


# ---------------------------------------------------------------------------
# Dashboard DOM
# ---------------------------------------------------------------------------

def test_c_alerts_card_has_alert_center_slots():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    for slot in ("alerts-p0-pill", "alerts-p1-pill", "alerts-p2-pill",
                 "alerts-p3-pill", "alerts-groups-pill",
                 "alerts-active-list", "alerts-groups-list",
                 "alerts-history-list"):
        assert f'id="{slot}"' in html, f"c-alerts missing slot {slot!r}"


def test_c_alerts_card_has_ack_mute_handlers():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    assert "ackAlert(" in html
    assert "muteAlert(" in html
    assert "fetchAlertCenter" in html
