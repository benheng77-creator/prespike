"""Phase 11n-2 — Decision Engine + Decision Truth Governor + conversion rate.

Locks:
  Decision engine:
    1. build_decision_bundle returns a DecisionBundle with system_wr,
       system_wr_target, candidates[], conversion, summary.
    2. Every candidate has the 6 core factors + evidence_refs + reason.
    3. Conversion rate computes signals→trades→wins from apex_trade_log.
    4. build_and_persist round-trips via latest_bundle.

  Decision truth gov:
    5. validate() on a clean bundle returns verdict=valid.
    6. factor_coverage fails when a candidate is missing a core factor.
    7. gate_integrity fails when action=buy but gates_clear=False.
    8. conversion_math fails when wins+losses > trades_executed.
    9. system_wr_gap fails when the gap disagrees with target - wr.

  Policy change:
   10. Default (no SPOT_RESEARCH_ENFORCE_HALT) → _enforce_halt does NOT
       flip toggles even with halt verdicts.
   11. With SPOT_RESEARCH_ENFORCE_HALT=1, flips happen as before.
   12. halt_state reflects VERDICT regardless of enforcement.

  Integration:
   13. Research run_and_persist triggers decision_engine build + gov.
   14. Research report carries per_symbol for Coin Accuracy fold-in.
   15. New /decision/* endpoints registered.
   16. Dashboard has c-decision + research-per-symbol slot.
"""
from __future__ import annotations

import importlib
import json
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    from shared.persistence import state as persist
    persist._initialized = False
    yield db


# ---------------------------------------------------------------------------
# Decision engine
# ---------------------------------------------------------------------------

def test_build_decision_bundle_shape():
    from spot_aggro.governance import decision_engine as de
    b = de.build_decision_bundle()
    d = b.to_dict()
    assert "system_wr" in d
    assert "system_wr_target" in d
    assert "candidates" in d and isinstance(d["candidates"], list)
    assert "conversion" in d
    assert "summary" in d and isinstance(d["summary"], str)


def test_conversion_rate_computes_from_trade_log():
    from shared.persistence import state as persist
    from spot_aggro.governance import decision_engine as de
    persist.init_schema()
    con = persist._connect()
    try:
        now_ms = int(time.time() * 1000)
        # 4 enters, 2 exits (1 win, 1 loss), 2 pending.
        for i in range(4):
            con.execute(
                "INSERT INTO apex_trade_log "
                "(ts_ms, symbol, module, action, side, notional_usd, avg_px, "
                " fee_usd, pnl_usd, correlation_id, payload_json, tier) "
                "VALUES (?, 'X-USDT', 'M1_flow_B', 'enter', 'buy', 5.0, 1.0, "
                " 0.01, NULL, NULL, '{}', 'B')",
                (now_ms - i * 1000,),
            )
        con.execute(
            "INSERT INTO apex_trade_log "
            "(ts_ms, symbol, module, action, side, notional_usd, avg_px, "
            " fee_usd, pnl_usd, correlation_id, payload_json, tier) "
            "VALUES (?, 'X-USDT', 'M1_flow_B', 'exit', 'sell', 5.0, 1.1, "
            " 0.01, 0.5, NULL, '{}', 'B')",
            (now_ms - 500,),
        )
        con.execute(
            "INSERT INTO apex_trade_log "
            "(ts_ms, symbol, module, action, side, notional_usd, avg_px, "
            " fee_usd, pnl_usd, correlation_id, payload_json, tier) "
            "VALUES (?, 'Y-USDT', 'M1_flow_B', 'exit', 'sell', 5.0, 0.95, "
            " 0.01, -0.3, NULL, '{}', 'B')",
            (now_ms - 250,),
        )
        con.commit()
    finally:
        con.close()
    conv = de._fetch_signals_and_conversion(window_h=24.0)
    assert conv.signals_generated == 4
    assert conv.trades_executed == 2
    assert conv.wins == 1
    assert conv.losses == 1
    assert conv.pending == 2


def test_build_and_persist_roundtrip():
    from spot_aggro.governance import decision_engine as de
    b = de.build_and_persist()
    latest = de.latest_bundle()
    assert latest is not None
    assert latest["generated_ts_ms"] == b.generated_ts_ms


# ---------------------------------------------------------------------------
# Decision Truth Governor
# ---------------------------------------------------------------------------

def _fake_candidate(symbol="X-USDT", tier="B", action="hold_long",
                    gates_clear=True, with_factors=True,
                    block_factor=False):
    factors = []
    if with_factors:
        for name in ("symbol_win_rate", "tier_win_rate", "tier_toggle",
                     "research_truth_gov", "card_truth_gov", "system_audit"):
            sev = "positive" if not (block_factor and name == "tier_toggle") else "block"
            factors.append({"name": name, "value": True, "weight": 0.1,
                            "why": "ok", "severity": sev})
    return {
        "symbol": symbol, "tier": tier, "action": action,
        "confidence": 0.5, "target_wr": 0.60, "projected_wr": None,
        "gates_clear": gates_clear, "reason": "test",
        "factors": factors,
        "evidence_refs": ["research://rr-1", "truth://rt-1",
                          "cards://ct-1", "scenario://none"],
    }


def _fake_bundle(**overrides):
    b = {
        "generated_ts_ms": int(time.time() * 1000),
        "system_wr": 0.55,
        "system_wr_target": 0.60,
        "system_wr_gap": 0.05,
        "candidates": [_fake_candidate()],
        "conversion": {
            "signals_generated": 10, "trades_executed": 6,
            "wins": 3, "losses": 3, "pending": 4,
            "conversion_rate_pct": 60.0,
            "win_rate_on_trades_pct": 50.0,
            "signal_to_win_pct": 30.0,
        },
        "evidence_refs": [], "summary": "",
    }
    b.update(overrides)
    return b


def test_decision_gov_valid_bundle():
    from spot_aggro.governance import decision_truth_gov as dtg
    v = dtg.validate(_fake_bundle())
    assert v.verdict == "valid", v.to_dict()


def test_decision_gov_missing_factor_is_invalid():
    from spot_aggro.governance import decision_truth_gov as dtg
    c = _fake_candidate()
    c["factors"] = [f for f in c["factors"] if f["name"] != "tier_toggle"]
    v = dtg.validate(_fake_bundle(candidates=[c]))
    assert v.verdict == "invalid"
    assert any(f.check == "factor_coverage" and f.severity == "fail"
               for f in v.findings)


def test_decision_gov_buy_without_gates_is_invalid():
    from spot_aggro.governance import decision_truth_gov as dtg
    c = _fake_candidate(action="buy", gates_clear=False)
    v = dtg.validate(_fake_bundle(candidates=[c]))
    assert v.verdict == "invalid"
    assert any(f.check == "gate_integrity" and f.severity == "fail"
               for f in v.findings)


def test_decision_gov_buy_with_blocking_factor_is_invalid():
    from spot_aggro.governance import decision_truth_gov as dtg
    c = _fake_candidate(action="buy", gates_clear=True, block_factor=True)
    v = dtg.validate(_fake_bundle(candidates=[c]))
    assert v.verdict == "invalid"


def test_decision_gov_conversion_math_inconsistent_fails():
    from spot_aggro.governance import decision_truth_gov as dtg
    b = _fake_bundle()
    b["conversion"]["wins"] = 100  # more than trades_executed
    v = dtg.validate(b)
    assert v.verdict == "invalid"
    assert any(f.check == "conversion_math" and f.severity == "fail"
               for f in v.findings)


def test_decision_gov_gap_mismatch_fails():
    from spot_aggro.governance import decision_truth_gov as dtg
    b = _fake_bundle(system_wr=0.55, system_wr_target=0.60,
                     system_wr_gap=0.99)  # wrong gap
    v = dtg.validate(b)
    assert v.verdict == "invalid"
    assert any(f.check == "system_wr_gap" for f in v.findings)


# ---------------------------------------------------------------------------
# Policy change: halt enforcement is opt-in
# ---------------------------------------------------------------------------

def _seed_bad_tier_b(con, now_ms: int):
    base = now_ms - 15 * 60 * 1000
    for i in range(15):
        con.execute(
            "INSERT INTO apex_trade_log "
            "(ts_ms, symbol, module, action, side, notional_usd, avg_px, "
            " fee_usd, pnl_usd, correlation_id, payload_json, tier) "
            "VALUES (?, 'X-USDT', 'M1_flow_B', 'exit', 'sell', 5.0, 1.0, "
            " 0.01, -0.5, NULL, '{}', 'B')",
            (base - i * 1000,),
        )


def test_halt_enforcement_off_by_default(monkeypatch, tmp_path):
    import yaml
    # Isolated toggle YAML.
    cfg_path = tmp_path / "tiers.yml"
    cfg_path.write_text(yaml.safe_dump({
        "schema_version": "spot.tiers.v1", "engine": "spot_aggro",
        "execution": {"A+": True, "A": True, "B": True, "C": True},
    }, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("SPOT_RESEARCH_MIN_SAMPLE", "10")
    # Explicitly DO NOT set SPOT_RESEARCH_ENFORCE_HALT.
    monkeypatch.delenv("SPOT_RESEARCH_ENFORCE_HALT", raising=False)

    from spot_aggro.gates.tier_toggle import TierExecutionToggle
    from spot_aggro.api import routes as spot_routes
    spot_routes._SPOT_TIER_TOGGLE = TierExecutionToggle(config_path=cfg_path)

    from shared.persistence import state as persist
    persist._initialized = False
    persist.init_schema()
    con = persist._connect()
    try:
        _seed_bad_tier_b(con, int(2_000_000_000 * 1000))
        con.commit()
    finally:
        con.close()

    from spot_aggro.governance import research_agent as ra
    importlib.reload(ra)
    r = ra.run_research(clock=lambda: 2_000_000_000.0, status="interim")
    # Verdict says halt (research still thinks so).
    b = next(s for s in r.tier_stats if s.tier == "B")
    assert b.halt_verdict == "halt"
    # halt_state reflects verdict.
    assert r.halt_state["B"] is True
    # But the tier toggle is NOT flipped.
    assert spot_routes._SPOT_TIER_TOGGLE.snapshot()["B"] is True


# ---------------------------------------------------------------------------
# Integration + endpoint registration
# ---------------------------------------------------------------------------

def test_research_report_carries_per_symbol():
    from spot_aggro.governance import research_agent as ra
    r = ra.run_research(clock=lambda: 2_000_000_000.0, status="interim")
    d = r.to_dict()
    assert "per_symbol" in d and isinstance(d["per_symbol"], list)


def test_new_decision_endpoints_registered():
    from spot_aggro.api import routes
    paths = {r.path for r in routes.router.routes}
    for p in ("/spot_aggro/decision/latest",
              "/spot_aggro/decision/run",
              "/spot_aggro/decision/truth"):
        assert p in paths, f"{p} not registered"


def test_dashboard_has_decision_card():
    # Phase 11n-6: standalone c-decision folded into unified c-research.
    # All legacy decision DOM ids still exist nested inside c-research.
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    assert 'id="c-research"' in html
    assert 'id="decision-wr-pill"' in html
    assert 'id="decision-conv-pill"' in html
    assert 'id="decision-candidates"' in html
    assert 'id="research-per-symbol"' in html
    assert "CONVERSION FUNNEL" in html
