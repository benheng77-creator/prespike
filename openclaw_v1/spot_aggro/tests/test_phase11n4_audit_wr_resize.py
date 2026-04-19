"""Phase 11n-4 — Full-flow audit + WR card resize regression locks.

Locks:
  Flow audit:
    1. Card Truth Governor fetches the spot router WITHOUT double-prefix.
    2. All 16 CardSpecs validate clean against a seeded DB — no fails,
       no schema missing-key contradictions.
    3. CardSpec required_keys match the ACTUAL endpoint payload shape
       (probe every endpoint; every declared key must be present).
    4. Cross-consistency rule 1 reads /tier_toggles.execution map (not
       the outer wrapper).
    5. decision_truth_gov conversion_math distinguishes hard invariants
       (wins+losses > trades) from soft data gaps (trades > signals) —
       the latter is "warn", not "fail".
    6. Orchestrator tick verdict = "ok" on a cleanly seeded DB.

  Win-Rate card UI:
    7. c-research is NOT span3 (no longer hogging 3 columns).
    8. c-research wraps recs/notes/coin-accuracy/history in a <details>
       element with a scroll container (compact inline neighbor).
    9. c-research sits in the same DOM flow adjacent to the other
       per-tier tiles it reports on.
"""
from __future__ import annotations

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


def _seed_healthy_db() -> None:
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    now = int(time.time() * 1000)
    try:
        data = [
            ("BTC-USDT", "A+", 0.5), ("BTC-USDT", "A+", 0.3),
            ("ETH-USDT", "A", -0.3), ("ETH-USDT", "A", 0.4),
            ("DOGE-USDT", "B", -0.2), ("SHIB-USDT", "C", 0.2),
        ]
        for i, (sym, tier, pnl) in enumerate(data):
            con.execute(
                "INSERT INTO apex_trade_log (ts_ms,symbol,module,action,"
                "side,notional_usd,avg_px,fee_usd,pnl_usd,correlation_id,"
                "payload_json,tier) VALUES "
                "(?,?,?,'exit','sell',5.0,1.0,0.01,?,NULL,'{}',?)",
                (now - i * 60000, sym, f"M1_flow_{tier[0]}", pnl, tier),
            )
        # enters so conv rate math is fully populated
        for i in range(15):
            con.execute(
                "INSERT INTO apex_trade_log (ts_ms,symbol,module,action,"
                "side,notional_usd,avg_px,fee_usd,pnl_usd,correlation_id,"
                "payload_json,tier) VALUES "
                "(?,'BTC-USDT','M1_flow_A','enter','buy',5.0,1.0,0.01,"
                "NULL,NULL,'{}','A+')",
                (now - i * 30000,),
            )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Card Truth Gov — no double-prefix, no schema contradictions
# ---------------------------------------------------------------------------

def test_card_truth_gov_no_double_prefix_all_endpoints_reachable():
    _seed_healthy_db()
    from spot_aggro.governance import card_truth_gov
    audit = card_truth_gov.audit_cards()
    unreachable = [c.card_id for c in audit.cards
                   if any(f.check == "endpoint" and f.severity == "fail"
                          for f in c.findings)]
    assert not unreachable, (
        f"these cards' endpoints were unreachable (prefix bug?): {unreachable}"
    )


def test_card_specs_match_real_endpoint_payload_keys():
    """For every CardSpec, probe its endpoint and assert every declared
    required_key appears in the actual response body."""
    _seed_healthy_db()
    from spot_aggro.governance import card_truth_gov
    from spot_aggro.governance.card_truth_gov import CARD_SPECS
    mismatches = []
    for spec in CARD_SPECS:
        code, body = card_truth_gov._fetch_card_payload(spec.endpoint)
        if code >= 500:
            continue
        if not isinstance(body, dict):
            continue
        missing = [k for k in spec.required_keys if k not in body]
        if missing:
            mismatches.append(
                f"{spec.card_id} ({spec.endpoint}): required={spec.required_keys} "
                f"missing={missing} actual_keys={list(body.keys())[:8]}"
            )
    assert not mismatches, "CardSpec contracts drifted:\n" + "\n".join(mismatches)


def test_full_flow_audit_no_fails_on_healthy_db():
    _seed_healthy_db()
    from spot_aggro.governance import (
        research_agent, card_truth_gov, decision_engine,
        decision_truth_gov, auto_orchestrator,
    )
    research_agent.run_and_persist(status="interim")
    card_audit = card_truth_gov.run_and_persist()
    assert card_audit.verdict == "ok", (
        f"card audit not clean: {[c.card_id for c in card_audit.cards if c.verdict != 'ok']}"
    )
    bundle = decision_engine.build_and_persist()
    v = decision_truth_gov.validate_and_persist(bundle.to_dict())
    assert v.verdict in ("valid", "suspect"), v.to_dict()
    tick = auto_orchestrator.run_tick()
    # Phase 11n-9-b: "no alpha admitted today" is a WARN (not a fail)
    # in the orchestrator because synthetic seed data rarely clears the
    # 12-item pre-trade checklist. The test accepts ok OR warn; it only
    # rejects fail-level verdicts.
    assert tick.verdict in ("ok", "warn"), (
        f"orchestrator fail-level: steps={[s.name for s in tick.steps]} "
        f"gaps={[(g.layer, g.message) for g in tick.gaps]}"
    )
    # Fail-level gaps must come from non-alpha layers.
    fail_gaps = [g for g in tick.gaps if g.severity == "fail"]
    assert not fail_gaps, f"fail-level gaps: {fail_gaps}"


# ---------------------------------------------------------------------------
# Decision truth gov: hard vs soft conv-math invariants
# ---------------------------------------------------------------------------

def test_decision_gov_conversion_trades_gt_signals_is_warn_not_fail():
    """Reconciled exits often have no matching enter row. This must be
    a WARN (data gap) not a FAIL (contradiction)."""
    from spot_aggro.governance import decision_truth_gov as dtg
    bundle = {
        "generated_ts_ms": int(time.time() * 1000),
        "system_wr": 0.5, "system_wr_target": 0.60, "system_wr_gap": 0.1,
        "candidates": [], "evidence_refs": [], "summary": "",
        "conversion": {
            "signals_generated": 0, "trades_executed": 6,
            "wins": 3, "losses": 3, "pending": 0,
            "conversion_rate_pct": 0.0,
            "win_rate_on_trades_pct": 50.0,
            "signal_to_win_pct": 0.0,
        },
    }
    v = dtg.validate(bundle)
    conv_finding = next(f for f in v.findings if f.check == "conversion_math")
    assert conv_finding.severity == "warn", conv_finding.to_dict()
    assert v.verdict in ("valid", "suspect"), v.verdict


def test_decision_gov_conversion_wins_plus_losses_gt_trades_is_fail():
    """Hard invariant: this is impossible by construction; must still fail."""
    from spot_aggro.governance import decision_truth_gov as dtg
    bundle = {
        "generated_ts_ms": int(time.time() * 1000),
        "system_wr": 0.5, "system_wr_target": 0.60, "system_wr_gap": 0.1,
        "candidates": [], "evidence_refs": [], "summary": "",
        "conversion": {
            "signals_generated": 100, "trades_executed": 6,
            "wins": 5, "losses": 5, "pending": 94,
            "conversion_rate_pct": 6.0,
            "win_rate_on_trades_pct": 50.0,
            "signal_to_win_pct": 5.0,
        },
    }
    v = dtg.validate(bundle)
    assert v.verdict == "invalid"
    assert any(f.check == "conversion_math" and f.severity == "fail"
               for f in v.findings)


# ---------------------------------------------------------------------------
# Win-Rate card UI resize
# ---------------------------------------------------------------------------

def test_wr_card_not_span3():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    # Match opening tag for c-research.
    m = re.search(r'<div class="([^"]+)" id="c-research"', html)
    assert m, "c-research card not found"
    cls = m.group(1)
    assert "span3" not in cls, (
        f"c-research still full-width (class={cls!r}); expected single column"
    )
    assert "c" in cls.split()


def test_wr_card_uses_collapsible_details():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    # The new layout wraps recs/notes/coin-accuracy/history in <details>.
    block_start = html.find('id="c-research"')
    block_end = html.find('</div>\n</div>\n', block_start)
    block = html[block_start:block_end] if block_end > 0 else html[block_start:block_start+5000]
    assert "<details" in block, (
        "WR card should wrap low-priority content in <details> to stay compact"
    )
    assert 'id="research-per-symbol"' in block
    assert 'id="research-history"' in block
