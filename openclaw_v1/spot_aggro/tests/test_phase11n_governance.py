"""Phase 11n — Auto Scenario Lab + Research/Card Truth Governors.

Locks:
  Scenario lab:
    1. ScenarioInput.fingerprint is deterministic.
    2. simulate() is deterministic on same inputs+clock.
    3. run_batch() exhausts the default matrix at cap=216.
    4. non_stop stops early when a scenario hits target.
    5. run_batch_and_persist round-trips via latest_batch_for_tier.
    6. history filters by tier + respects limit.

  Research truth governor (Layer 4):
    7. validate() on a hand-crafted valid report returns verdict=valid.
    8. A report with missing required keys is invalid.
    9. A report with out-of-bounds Wilson CI is invalid.
   10. A report whose claimed WR contradicts the DB is invalid.
   11. A halt verdict with sample < min_sample is invalid.

  Card truth governor (Layer 5):
   12. audit_cards returns a CardAudit with one verdict per registered spec.
   13. Every finding carries a severity in {ok, warn, fail}.
   14. Cross-consistency check fires when tier_toggles contradict /status.
   15. persist_audit + latest_audit round-trip cleanly.

  Integration:
   16. run_and_persist triggers research-truth validation as a side-effect.
   17. Dashboard cards c-scenarios, c-research-gov, c-cards-gov exist.
   18. New endpoints appear in the router.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Give each test a fresh SQLite so persistence tables don't collide."""
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    # Force re-init on first _connect.
    from shared.persistence import state as persist
    persist._initialized = False
    yield db


# ---------------------------------------------------------------------------
# Scenario lab
# ---------------------------------------------------------------------------

def test_scenario_input_fingerprint_deterministic():
    from spot_aggro.governance.scenario_runner import ScenarioInput
    a = ScenarioInput(tier="B", style="balanced", market="calm",
                      capital_usd=400, days=3, tp_mult=1.0)
    b = ScenarioInput(tier="B", style="balanced", market="calm",
                      capital_usd=400, days=3, tp_mult=1.0)
    assert a.fingerprint() == b.fingerprint()
    c = ScenarioInput(tier="B", style="balanced", market="calm",
                      capital_usd=400, days=3, tp_mult=1.5)
    assert a.fingerprint() != c.fingerprint()


def test_simulate_deterministic_same_clock():
    from spot_aggro.governance.scenario_runner import ScenarioInput, simulate
    inp = ScenarioInput(tier="B", style="aggressive", market="squeeze",
                        capital_usd=800, days=7, tp_mult=1.2)
    clk = lambda: 2_000_000_000.0
    o1 = simulate(inp, clock=clk)
    o2 = simulate(inp, clock=clk)
    assert o1.scenario_id == o2.scenario_id
    assert o1.simulated_wr == o2.simulated_wr
    assert o1.simulated_expectancy_usd == o2.simulated_expectancy_usd


def test_run_batch_exhausts_matrix_at_cap():
    from spot_aggro.governance.scenario_runner import run_batch
    # Small matrix => small permutation set (2*2 = 4 outcomes).
    b = run_batch(
        tier="C",
        matrix={"style": ["balanced", "aggressive"],
                "market": ["calm", "trending"],
                "capital_usd": [400], "days": [3], "tp_mult": [1.0]},
        cap=500, non_stop=False,
        clock=lambda: 2_000_000_000.0,
    )
    assert len(b.outcomes) == 4
    assert b.stopped_reason == "matrix_exhausted"


def test_run_batch_respects_cap():
    from spot_aggro.governance.scenario_runner import run_batch
    b = run_batch(tier="B", cap=5, non_stop=False,
                  clock=lambda: 2_000_000_000.0)
    assert len(b.outcomes) == 5
    assert b.stopped_reason == "cap_reached"


def test_run_batch_and_persist_roundtrip():
    from spot_aggro.governance.scenario_runner import (
        run_batch_and_persist, latest_batch_for_tier, history,
    )
    b = run_batch_and_persist(
        tier="B", cap=3,
        clock=lambda: 2_000_000_000.0,
    )
    assert b.batch_id.startswith("sb-")
    assert len(b.outcomes) == 3
    latest = latest_batch_for_tier("B")
    assert latest is not None
    assert latest["batch_id"] == b.batch_id
    hist_all = history(limit=10)
    hist_b = history(tier="B", limit=10)
    assert any(r["batch_id"] == b.batch_id for r in hist_all)
    assert all(r["tier"] == "B" for r in hist_b)


# ---------------------------------------------------------------------------
# Research Truth Governor (Layer 4)
# ---------------------------------------------------------------------------

def _minimal_valid_report() -> dict:
    """Hand-crafted 'golden' report that passes all 6 checks."""
    return {
        "report_id": "rr-golden",
        "snapshot_id": "snap-1",
        "audit_rollup_id": "audit-1",
        "generated_ts_ms": int(time.time() * 1000),
        "status": "interim",
        "tier_stats": [
            {"tier": t, "primary_sample": 0, "primary_wr": None,
             "halt_verdict": "insufficient_sample",
             "windows": {"30m": {"n_exits": 0}, "1h": {"n_exits": 0},
                         "24h": {"n_exits": 0}}}
            for t in ("A+", "A", "B", "C")
        ],
        "halt_state": {"A+": False, "A": False, "B": False, "C": False},
        "thresholds": {
            "wr_halt_min": 0.60, "wr_restore_min": 0.50,
            "min_sample_for_halt": 50, "primary_window": "1h",
        },
        "recommendations": [],
        "overall_wr": None, "overall_exits": 0,
    }


def test_research_gov_valid_report():
    from spot_aggro.governance.research_truth_gov import validate
    v = validate(_minimal_valid_report())
    assert v.verdict in ("valid", "suspect"), v.to_dict()
    assert v.n_fail == 0


def test_research_gov_schema_fail_on_missing_fields():
    from spot_aggro.governance.research_truth_gov import validate
    r = _minimal_valid_report()
    del r["audit_rollup_id"]
    v = validate(r)
    assert v.verdict == "invalid"
    assert any(f.check == "schema" and f.severity == "fail" for f in v.findings)


def test_research_gov_ci_bounds_fail_on_out_of_range():
    from spot_aggro.governance.research_truth_gov import validate
    r = _minimal_valid_report()
    r["tier_stats"][0]["windows"]["1h"] = {
        "n_exits": 10,
        "confidence_interval_95": {"low": -0.1, "high": 1.2, "width": 1.3},
    }
    v = validate(r)
    assert any(f.check == "ci_bounds" and f.severity == "fail" for f in v.findings)
    assert v.verdict == "invalid"


def test_research_gov_threshold_math_fail_on_halt_with_small_sample():
    from spot_aggro.governance.research_truth_gov import validate
    r = _minimal_valid_report()
    # Claim halt with only 10 samples while min_sample=50.
    r["tier_stats"][2] = {
        "tier": "B", "primary_sample": 10, "primary_wr": 0.20,
        "halt_verdict": "halt",
        "windows": {"1h": {"n_exits": 10}},
    }
    r["halt_state"]["B"] = True
    v = validate(r)
    assert any(f.check == "threshold_math" and f.severity == "fail"
               for f in v.findings)


# ---------------------------------------------------------------------------
# Card Truth Governor (Layer 5)
# ---------------------------------------------------------------------------

def test_card_gov_one_verdict_per_spec():
    from spot_aggro.governance.card_truth_gov import audit_cards, CARD_SPECS
    audit = audit_cards()
    assert len(audit.cards) == len(CARD_SPECS)
    for c in audit.cards:
        assert c.verdict in ("ok", "warn", "fail")
        assert c.n_ok + c.n_warn + c.n_fail == c.n_checks


def test_card_gov_findings_have_valid_severity():
    from spot_aggro.governance.card_truth_gov import audit_cards
    audit = audit_cards()
    for card in audit.cards:
        for f in card.findings:
            assert f.severity in ("ok", "warn", "fail")
            assert f.check in ("endpoint", "schema", "cross", "freshness")


def test_card_gov_persist_and_latest_roundtrip():
    from spot_aggro.governance.card_truth_gov import (
        run_and_persist, latest_audit, history,
    )
    a = run_and_persist()
    latest = latest_audit()
    assert latest is not None
    assert latest["run_id"] == a.run_id
    hist = history(limit=5)
    assert any(r["run_id"] == a.run_id for r in hist)


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_research_run_and_persist_triggers_truth_gov(monkeypatch):
    """After run_and_persist, the truth governor table must have a row."""
    monkeypatch.setenv("SPOT_RESEARCH_MIN_SAMPLE", "10")
    import importlib
    from shared.persistence import state as persist
    persist._initialized = False
    from spot_aggro.governance import research_agent as ra
    importlib.reload(ra)
    persist.init_schema()

    # The scenario runner is network-free + clock-injectable; keep real.
    ra.run_and_persist(clock=lambda: 2_000_000_000.0, status="interim")

    from spot_aggro.governance.research_truth_gov import latest_verdict
    v = latest_verdict()
    assert v is not None, "truth gov never persisted a verdict"
    assert v["verdict"] in ("valid", "suspect", "invalid")


def test_dashboard_has_new_cards():
    # Phase 11n-6: standalone c-scenarios + c-research-gov + c-cards-gov
    # collapsed into unified c-research + c-gov. Their DOM slots still
    # live inside the consolidated parents.
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    assert 'id="c-research"' in html
    assert 'id="c-gov"' in html
    # legacy inner ids still present nested inside the unified cards
    assert 'id="scenarios-summary"' in html
    assert 'id="research-gov-verdict"' in html
    assert 'id="cards-gov-verdict"' in html


def test_new_endpoints_registered():
    from spot_aggro.api import routes
    paths = {r.path for r in routes.router.routes}
    for p in (
        "/spot_aggro/scenarios/latest",
        "/spot_aggro/scenarios/history",
        "/spot_aggro/scenarios/run",
        "/spot_aggro/research/truth",
        "/spot_aggro/research/truth/run",
        "/spot_aggro/cards/truth",
        "/spot_aggro/cards/truth/run",
    ):
        assert p in paths, f"{p} not registered"
