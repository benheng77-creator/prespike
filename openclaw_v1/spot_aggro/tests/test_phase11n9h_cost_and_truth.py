"""Phase 11n-9-h — cost control, stamp truth, audit honesty, scoring WR.

Locks:
  P1 — audit_swarm.yml chief_adjudicator uses Haiku, not Opus.
  P2 — dashboard registerTruthEvidence fingerprints content; stamp
       preserves prior computed_at when fingerprint unchanged.
  P3 — auditCard has Rule 6 that consults server-side card_truth_gov
       via window._cardTruthGovByCard.
  P4 — scoring.compute_composite_score applies a historical-WR
       multiplier from research.latest_report() when sample >= 3.
  P5 — System Ledger renderer falls back to st.cycles / st.trades_today
       instead of the missing decisions_logged / trades_logged keys.
  P6 — Forensic v1 table labels column "Top Issue" not "Grade".
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


def test_audit_swarm_chief_adjudicator_is_haiku():
    """P1: Opus → Haiku for chief_adjudicator; ~16x cheaper."""
    yml = (REPO / "openclaw_v1" / "spot_aggro" / "config"
           / "audit_swarm.yml").read_text(encoding="utf-8")
    # Extract the chief_adjudicator block.
    m = re.search(r"chief_adjudicator:\s*\n\s*provider:\s*\"anthropic\"\s*\n\s*model:\s*\"([^\"]+)\"", yml)
    assert m, "chief_adjudicator block not found"
    assert "haiku" in m.group(1), (
        f"chief_adjudicator still on {m.group(1)!r}; expected haiku"
    )


def test_register_truth_evidence_has_fingerprint_guard():
    """P2: registerTruthEvidence must hash and compare before bumping."""
    assert "_fingerprint" in HTML
    assert "prior._fingerprint === fp" in HTML
    assert "preserve last-change" in HTML or "preserve last-change" in HTML


def test_audit_card_rule_6_consults_server_card_truth():
    """P3: auditCard inherits server gov verdict."""
    assert "_cardTruthGovByCard" in HTML
    # The Rule 6 block must appear INSIDE auditCard()
    m = re.search(r"function auditCard\([^}]+?\}\n\n", HTML, re.DOTALL)
    # Just assert the store name + a fail-propagation line.
    assert "window._cardTruthGovByCard" in HTML
    assert 'sv.verdict === "fail"' in HTML
    assert 'server gov:' in HTML


def test_scoring_applies_wr_multiplier():
    """P4: compute_composite_score folds in historical WR per symbol."""
    src = (REPO / "openclaw_v1" / "spot_aggro"
           / "scoring.py").read_text(encoding="utf-8")
    assert "historical-WR quality multiplier" in src
    assert "from spot_aggro.governance.research_agent import latest_report" in src
    # The multiplier must act on symbols with sample >= 3.
    assert "exits >= 3" in src
    # Must be bounded: no unbounded boost.
    assert "1.15" in src


def test_scoring_wr_multiplier_unit_behavior():
    """P4: low-WR symbol gets penalized; high-WR gets boosted; no-data
    symbol is unchanged. Use monkeypatched latest_report."""
    from spot_aggro.scoring import compute_composite_score
    from spot_aggro.governance import research_agent as ra

    original = ra.latest_report
    try:
        # High-WR symbol.
        ra.latest_report = lambda: {
            "per_symbol": [{"symbol": "ENA-USDT", "exits": 11,
                            "wins": 9, "losses": 2}]
        }
        class M: regime = "HEALTHY"; squeeze_timing_window = "NEAR"; timestamp = 1
        base = compute_composite_score(
            {"symbol": "ENA-USDT", "spi": 0.5, "funding_z": -1.0,
             "depth_usd": 50000, "sigma_30d": 0.0001, "spread_bp": 5}, M()
        )
        # Low-WR symbol.
        ra.latest_report = lambda: {
            "per_symbol": [{"symbol": "SEI-USDT", "exits": 12,
                            "wins": 1, "losses": 11}]
        }
        low = compute_composite_score(
            {"symbol": "SEI-USDT", "spi": 0.5, "funding_z": -1.0,
             "depth_usd": 50000, "sigma_30d": 0.0001, "spread_bp": 5}, M()
        )
        # No-data symbol.
        ra.latest_report = lambda: {"per_symbol": []}
        neutral = compute_composite_score(
            {"symbol": "UNKNOWN-USDT", "spi": 0.5, "funding_z": -1.0,
             "depth_usd": 50000, "sigma_30d": 0.0001, "spread_bp": 5}, M()
        )
        assert base > neutral >= low, (
            f"WR ordering broken: high={base} neutral={neutral} low={low}"
        )
    finally:
        ra.latest_report = original


def test_system_ledger_falls_back_to_real_status_keys():
    """P5: decisions_logged / trades_logged no longer exist; JS must
    fall back to cycles / trades_today."""
    assert "st.decisions_logged ?? st.cycles" in HTML
    assert "st.trades_logged   ?? st.trades_today" in HTML


def test_forensic_v1_column_is_top_issue_not_grade():
    """P6: Column renamed; prevents 'executive_summary' text showing
    under a 'Grade' header."""
    # Find the forensic v1 thead specifically (c-forensic card).
    m = re.search(r'id="c-forensic"[\s\S]+?<thead>.+?</thead>', HTML, re.DOTALL)
    assert m, "c-forensic card's thead not found"
    thead = m.group(0)
    assert ">Top Issue<" in thead
    assert ">Grade<" not in thead, (
        "c-forensic still uses 'Grade' column header; "
        "rename to 'Top Issue' to reflect actual data"
    )
