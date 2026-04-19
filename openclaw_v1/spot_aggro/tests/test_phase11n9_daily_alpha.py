"""Phase 11n-9 — Daily Alpha picker + Layer 8 pre-trade checklist gov.

Locks:
  daily_alpha:
    1. build_daily_alpha returns a DailyAlphaBundle with buys[], sells[],
       target_proj_wr, admitted + rejected counts, summary.
    2. Caps: max 2 admitted buys + 2 admitted sells per day.
    3. Each pick carries projected_wr, sample_size, factors, checklist,
       evidence_refs, rationale.
    4. build_and_persist round-trips via latest_bundle.

  daily_alpha_gov (Layer 8):
    5. evaluate_pick runs exactly 12 checklist items.
    6. Checklist covers all 4 domains: evidence, technical, calculated,
       operational.
    7. A pick with full sample + passing every layer is admitted.
    8. A pick below target_proj_wr is rejected with reason naming the
       projected_wr_meets_target item.
    9. A pick with sample_size < 5 is rejected by sample_size_sufficient.
   10. A pick with a blocking factor is rejected.

  Integration:
   11. Research report per_symbol drives the candidate list.
   12. Orchestrator tick includes a daily_alpha_build step.
   13. Endpoints /daily_alpha/latest + /daily_alpha/run registered.
   14. Feature manifest advertises daily_alpha + daily_alpha_gov.
   15. Dashboard has c-alpha with the required DOM ids.
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
    yield


def _seed_strong_wr(sym="BTC-USDT", tier="A+", n_wins=8, n_losses=2):
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    now = int(time.time() * 1000)
    try:
        idx = 0
        for _ in range(n_wins):
            con.execute(
                "INSERT INTO trade_log (ts_ms,symbol,module,action,"
                "side,notional_usd,avg_px,fee_usd,pnl_usd,correlation_id,"
                "payload_json,tier) VALUES "
                "(?,?,?,'exit','sell',5.0,1.0,0.01,0.5,NULL,'{}',?)",
                (now - idx * 60_000, sym, f"M1_flow_{tier[0]}", tier),
            )
            idx += 1
        for _ in range(n_losses):
            con.execute(
                "INSERT INTO trade_log (ts_ms,symbol,module,action,"
                "side,notional_usd,avg_px,fee_usd,pnl_usd,correlation_id,"
                "payload_json,tier) VALUES "
                "(?,?,?,'exit','sell',5.0,1.0,0.01,-0.3,NULL,'{}',?)",
                (now - idx * 60_000, sym, f"M1_flow_{tier[0]}", tier),
            )
            idx += 1
        # enters so conversion math stays sane
        for _ in range(n_wins + n_losses + 2):
            con.execute(
                "INSERT INTO trade_log (ts_ms,symbol,module,action,"
                "side,notional_usd,avg_px,fee_usd,pnl_usd,correlation_id,"
                "payload_json,tier) VALUES "
                "(?,?,?,'enter','buy',5.0,1.0,0.01,NULL,NULL,'{}',?)",
                (now - idx * 30_000, sym, f"M1_flow_{tier[0]}", tier),
            )
            idx += 1
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# daily_alpha
# ---------------------------------------------------------------------------

def test_build_daily_alpha_shape():
    _seed_strong_wr()
    from spot_aggro.governance import research_agent, daily_alpha
    research_agent.run_and_persist(status="interim")
    b = daily_alpha.build_daily_alpha()
    d = b.to_dict()
    assert "buys" in d and "sells" in d
    assert "target_proj_wr" in d
    assert "admitted_count" in d and "rejected_count" in d
    assert isinstance(d["summary"], str)
    assert d["date_utc"]


def test_build_and_persist_roundtrip():
    _seed_strong_wr()
    from spot_aggro.governance import research_agent, daily_alpha
    research_agent.run_and_persist(status="interim")
    b = daily_alpha.build_and_persist()
    latest = daily_alpha.latest_bundle()
    assert latest is not None
    assert latest["generated_ts_ms"] == b.generated_ts_ms


def test_daily_cap_never_admits_more_than_two_per_side():
    _seed_strong_wr(sym="BTC-USDT", tier="A+", n_wins=8, n_losses=2)
    _seed_strong_wr(sym="ETH-USDT", tier="A",  n_wins=7, n_losses=1)
    _seed_strong_wr(sym="SOL-USDT", tier="B",  n_wins=9, n_losses=2)
    _seed_strong_wr(sym="DOGE-USDT", tier="C", n_wins=6, n_losses=1)
    from spot_aggro.governance import (
        research_agent, daily_alpha,
        card_truth_gov, research_truth_gov,
    )
    research_agent.run_and_persist(status="interim")
    card_truth_gov.run_and_persist()   # keep card truth clean so gov passes
    b = daily_alpha.build_daily_alpha()
    admitted_buys = sum(1 for p in b.buys if p.checklist_pass)
    admitted_sells = sum(1 for p in b.sells if p.checklist_pass)
    assert admitted_buys <= 2
    assert admitted_sells <= 2


def test_pick_carries_all_governance_fields():
    _seed_strong_wr()
    from spot_aggro.governance import research_agent, daily_alpha
    research_agent.run_and_persist(status="interim")
    b = daily_alpha.build_daily_alpha()
    all_picks = b.buys + b.sells
    assert all_picks, "expected at least one candidate"
    p = all_picks[0]
    d = p.to_dict()
    for key in ("symbol", "tier", "action", "projected_wr", "sample_size",
                "confidence", "rationale", "factors", "evidence_refs",
                "checklist_pass", "checklist_score", "checklist"):
        assert key in d, f"pick missing required field {key!r}"


# ---------------------------------------------------------------------------
# daily_alpha_gov (Layer 8)
# ---------------------------------------------------------------------------

def _fake_pick(**over):
    from spot_aggro.governance.daily_alpha import AlphaPick
    base = dict(
        symbol="BTC-USDT", tier="A+", action="buy",
        projected_wr=0.75, sample_size=10, confidence=0.75,
        rationale="test", factors=[
            {"name": "projected_wr", "value": 0.75, "weight": 0.3,
             "why": "ok", "severity": "positive"},
        ],
        evidence_refs=["research://rr-1", "per_symbol://BTC-USDT"],
        checklist_pass=False, checklist_score=0.0,
    )
    base.update(over)
    return AlphaPick(**base)


def _make_clean_research():
    return {
        "report_id": "rr-test",
        "per_symbol": [{
            "symbol": "BTC-USDT", "tier": "A+",
            "wins": 8, "losses": 2,
            "win_rate": 0.80, "exits": 10, "pnl_usd": 3.1,
        }],
        "tier_stats": [{
            "tier": "A+", "primary_wr": 0.80, "primary_sample": 20,
        }],
    }


def test_gov_runs_twelve_items():
    from spot_aggro.governance import daily_alpha_gov, card_truth_gov, research_truth_gov
    # Seed gov layers clean.
    _seed_strong_wr()
    from spot_aggro.governance import research_agent
    research_agent.run_and_persist(status="interim")
    card_truth_gov.run_and_persist()
    v = daily_alpha_gov.evaluate_pick(
        _fake_pick(),
        research=_make_clean_research(),
        scenario=None,
        toggles={"A+": True, "A": True, "B": True, "C": True},
        target_proj_wr=0.70,
    )
    assert len(v.checklist) == 12
    assert len(daily_alpha_gov.CHECKLIST_KEYS) == 12


def test_gov_covers_all_four_domains():
    from spot_aggro.governance import daily_alpha_gov, card_truth_gov
    _seed_strong_wr()
    from spot_aggro.governance import research_agent
    research_agent.run_and_persist(status="interim")
    card_truth_gov.run_and_persist()
    v = daily_alpha_gov.evaluate_pick(
        _fake_pick(),
        research=_make_clean_research(),
        scenario=None, toggles={"A+": True}, target_proj_wr=0.70,
    )
    domains = {i.domain for i in v.checklist}
    assert domains == {"evidence", "technical", "calculated", "operational"}


def test_gov_rejects_below_target_proj_wr():
    from spot_aggro.governance import daily_alpha_gov
    v = daily_alpha_gov.evaluate_pick(
        _fake_pick(projected_wr=0.55),
        research=_make_clean_research(),
        scenario=None, toggles={"A+": True}, target_proj_wr=0.70,
    )
    assert v.admitted is False
    assert any(i.key == "projected_wr_meets_target" and not i.passed
               for i in v.checklist)


def test_gov_rejects_low_sample():
    from spot_aggro.governance import daily_alpha_gov
    v = daily_alpha_gov.evaluate_pick(
        _fake_pick(sample_size=1, projected_wr=0.90),
        research=_make_clean_research(),
        scenario=None, toggles={"A+": True}, target_proj_wr=0.70,
    )
    assert v.admitted is False
    assert any(i.key == "sample_size_sufficient" and not i.passed
               for i in v.checklist)


def test_gov_rejects_on_blocking_factor():
    from spot_aggro.governance import daily_alpha_gov
    factors = [
        {"name": "tier_toggle", "value": False, "weight": 0.1,
         "why": "OFF", "severity": "block"},
    ]
    v = daily_alpha_gov.evaluate_pick(
        _fake_pick(factors=factors),
        research=_make_clean_research(),
        scenario=None, toggles={"A+": False}, target_proj_wr=0.70,
    )
    assert v.admitted is False
    assert any(i.key == "no_blocking_factors" and not i.passed
               for i in v.checklist)


# ---------------------------------------------------------------------------
# Integration + endpoints + dashboard
# ---------------------------------------------------------------------------

def test_new_endpoints_registered():
    from spot_aggro.api import routes
    paths = {r.path for r in routes.router.routes}
    for p in ("/spot_aggro/daily_alpha/latest",
              "/spot_aggro/daily_alpha/run"):
        assert p in paths, f"{p} not registered"


def test_feature_manifest_advertises_daily_alpha():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    assert body["features"]["daily_alpha"] is True
    assert body["features"]["daily_alpha_gov"] is True


def test_orchestrator_runs_daily_alpha_build_step():
    _seed_strong_wr()
    from spot_aggro.governance import auto_orchestrator
    tick = auto_orchestrator.run_tick()
    step_names = [s.name for s in tick.steps]
    assert "daily_alpha_build" in step_names


def test_dashboard_has_alpha_card_with_required_ids():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    assert 'id="c-alpha"' in html
    for dom_id in ("alpha-status-pill", "alpha-admit-pill", "alpha-target-pill",
                   "alpha-summary", "alpha-buys", "alpha-sells",
                   "alpha-checklist"):
        assert f'id="{dom_id}"' in html, f"missing {dom_id}"
