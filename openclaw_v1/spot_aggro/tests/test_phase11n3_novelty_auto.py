"""Phase 11n-3 — Loop Novelty + Auto Orchestrator tests.

Locks:
  Novelty (scenario_runner):
    1. Consecutive batches for the same tier have DIFFERENT
       batch_signature, axis_order, seed_salt.
    2. Scenario fingerprints differ across passes even when inputs match,
       because salt flows through fingerprint.
    3. pass_index increments monotonically.
    4. Explicit salt/axis/shift overrides win over auto-picked values.
    5. Schema migration is idempotent (run twice, no crash).

  Loop Novelty Governor (Layer 7):
    6. validate_batch on a fresh first-batch returns verdict=novel.
    7. Duplicate signature across back-to-back batches → verdict=stuck.
    8. Repeated axis_order back-to-back → verdict=stuck.
    9. Repeated seed_salt within lookback → verdict=stuck.
   10. persist_verdict + latest_verdict + history round-trip.

  Auto Orchestrator:
   11. run_tick produces an OrchestratorTick with >=1 step.
   12. Persisted tick retrievable via latest_tick + history.
   13. start() is idempotent: two calls, single worker thread.
   14. stop() brings is_running() back to False.
   15. Gap surfaces when card_truth audits a fresh empty system.

  Endpoints + dashboard:
   16. New endpoints registered: /loop/novelty, /auto/status,
       /auto/start, /auto/stop, /auto/tick.
   17. Dashboard has c-auto + c-loop-novelty.
   18. Feature manifest advertises loop_novelty_gov + auto_orchestrator.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    from shared.persistence import state as persist
    persist._initialized = False
    # Make orchestrator background loop cheap/idle in tests.
    monkeypatch.setenv("SPOT_AUTO_INTERVAL_S", "3600")
    yield


# ---------------------------------------------------------------------------
# Scenario novelty
# ---------------------------------------------------------------------------

def test_consecutive_batches_are_novel():
    from spot_aggro.governance import scenario_runner as sr
    b1 = sr.run_batch_and_persist(tier="B", cap=4)
    b2 = sr.run_batch_and_persist(tier="B", cap=4)
    b3 = sr.run_batch_and_persist(tier="B", cap=4)
    sigs = {b1.batch_signature, b2.batch_signature, b3.batch_signature}
    assert len(sigs) == 3, f"signatures not all unique: {sigs}"
    orders = {b1.axis_order, b2.axis_order, b3.axis_order}
    assert len(orders) == 3, f"axis orders not all unique: {orders}"
    salts = {b1.seed_salt, b2.seed_salt, b3.seed_salt}
    assert len(salts) == 3, f"seed salts not unique: {salts}"


def test_pass_index_monotonic():
    from spot_aggro.governance import scenario_runner as sr
    b1 = sr.run_batch_and_persist(tier="C", cap=4)
    b2 = sr.run_batch_and_persist(tier="C", cap=4)
    b3 = sr.run_batch_and_persist(tier="C", cap=4)
    assert b1.pass_index == 0
    assert b2.pass_index == 1
    assert b3.pass_index == 2


def test_scenario_fingerprint_differs_across_passes():
    from spot_aggro.governance import scenario_runner as sr
    b1 = sr.run_batch_and_persist(tier="A", cap=4)
    b2 = sr.run_batch_and_persist(tier="A", cap=4)
    ids1 = [o.scenario_id for o in b1.outcomes]
    ids2 = [o.scenario_id for o in b2.outcomes]
    # Different salt → no id may repeat across passes.
    assert set(ids1).isdisjoint(set(ids2)), (
        f"scenario_ids repeat across passes: {set(ids1) & set(ids2)}"
    )


def test_explicit_overrides_win_over_auto_picked():
    from spot_aggro.governance import scenario_runner as sr
    b = sr.run_batch_and_persist(
        tier="B", cap=4,
        axis_order=("tp_mult", "style", "market", "capital_usd", "days"),
        seed_salt="fixed-salt-xyz",
        value_shift=0,
    )
    assert b.seed_salt == "fixed-salt-xyz"
    assert b.axis_order[0] == "tp_mult"


def test_scenario_runner_init_schema_idempotent():
    from spot_aggro.governance import scenario_runner as sr
    sr._init_schema()
    sr._init_schema()      # second call must not raise
    sr._init_schema()      # third call must not raise
    assert True


# ---------------------------------------------------------------------------
# Loop Novelty Governor
# ---------------------------------------------------------------------------

def test_novelty_gov_first_batch_is_novel():
    from spot_aggro.governance import scenario_runner as sr
    from spot_aggro.governance import loop_novelty_gov as lng
    b = sr.run_batch_and_persist(tier="B", cap=4)
    v = lng.validate_batch(b.to_dict())
    assert v.verdict == "novel", v.to_dict()


def test_novelty_gov_flags_duplicate_signature_as_stuck():
    """If somehow two batches share a signature, the governor must say
    stuck. We simulate by constructing a batch dict with a repeated
    signature after a real batch has been persisted."""
    from spot_aggro.governance import scenario_runner as sr
    from spot_aggro.governance import loop_novelty_gov as lng
    b1 = sr.run_batch_and_persist(tier="B", cap=4)
    fake = dict(b1.to_dict())
    fake["batch_id"] = "sb-duplicate-test"
    # Exact same signature, axis_order, salt → every check should fire.
    v = lng.validate_batch(fake)
    assert v.verdict == "stuck", v.to_dict()
    assert any(f.check == "signature_unique" and f.severity == "fail"
               for f in v.findings)


def test_novelty_gov_persist_latest_history_roundtrip():
    from spot_aggro.governance import scenario_runner as sr
    from spot_aggro.governance import loop_novelty_gov as lng
    b = sr.run_batch_and_persist(tier="C", cap=4)
    # run_batch_and_persist already calls validate_and_persist; so history
    # should already contain a verdict.
    latest = lng.latest_verdict("C")
    assert latest is not None
    assert latest["batch_id"] == b.batch_id
    hist = lng.history("C", limit=10)
    assert any(r["batch_id"] == b.batch_id for r in hist)


# ---------------------------------------------------------------------------
# Auto Orchestrator
# ---------------------------------------------------------------------------

def test_run_tick_produces_steps_and_persists():
    from spot_aggro.governance import auto_orchestrator as ao
    tick = ao.run_tick()
    assert len(tick.steps) >= 1
    assert tick.verdict in ("ok", "warn", "fail")
    latest = ao.latest_tick()
    assert latest is not None
    assert latest["tick_id"] == tick.tick_id


def test_orchestrator_start_idempotent():
    from spot_aggro.governance import auto_orchestrator as ao
    try:
        s1 = ao.start(interval_s=3600)
        s2 = ao.start(interval_s=3600)
        assert ao.is_running() is True
        assert s2.get("already_running") is True
    finally:
        ao.stop()
    assert ao.is_running() is False


def test_orchestrator_stop_is_noop_when_not_running():
    from spot_aggro.governance import auto_orchestrator as ao
    ao.stop()   # belt-and-suspenders — previous test should leave it stopped
    s = ao.stop()
    assert s.get("already_stopped") is True


def test_orchestrator_history_order():
    from spot_aggro.governance import auto_orchestrator as ao
    ao.run_tick()
    time.sleep(0.01)
    ao.run_tick()
    hist = ao.history(limit=5)
    assert len(hist) >= 2
    # Newest first.
    assert hist[0]["started_ts_ms"] >= hist[1]["started_ts_ms"]


# ---------------------------------------------------------------------------
# Endpoints + dashboard
# ---------------------------------------------------------------------------

def test_new_endpoints_registered():
    from spot_aggro.api import routes
    paths = {r.path for r in routes.router.routes}
    for p in ("/spot_aggro/loop/novelty",
              "/spot_aggro/auto/status",
              "/spot_aggro/auto/start",
              "/spot_aggro/auto/stop",
              "/spot_aggro/auto/tick"):
        assert p in paths, f"{p} not registered"


def test_dashboard_has_auto_and_loop_cards():
    # Phase 11n-6: c-loop-novelty folded into unified c-gov (its verdict
    # pill + findings list still exist nested there). c-auto stays as
    # its own orchestrator-control card.
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    assert 'id="c-auto"' in html
    assert 'id="c-gov"' in html
    assert 'id="loop-verdict"' in html
    assert 'id="loop-findings"' in html
    assert "Auto Orchestrator" in html


def test_feature_manifest_advertises_new_layers():
    # The router has prefix="/spot_aggro" baked in; call the function
    # directly rather than re-mounting under another prefix.
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    assert body["features"]["loop_novelty_gov"] is True
    assert body["features"]["auto_orchestrator"] is True
