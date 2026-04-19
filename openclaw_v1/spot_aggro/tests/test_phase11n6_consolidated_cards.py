"""Phase 11n-6 — Consolidated RESEARCH + GOV cards.

Single unified RESEARCH card (c-research) holds WR + scenarios +
decision + coin accuracy. Single unified GOV card (c-gov) holds
research-truth + card-truth + loop-novelty + decision-truth verdict
pills with one aggregate rollup pill. Standalone
c-research-gov / c-cards-gov / c-loop-novelty / c-scenarios / c-decision
cards are deleted.

Locks:
  1. c-research + c-gov both exist in the DOM exactly once.
  2. c-research-gov / c-cards-gov / c-loop-novelty / c-scenarios /
     c-decision div-with-id occurrences are GONE (no phantom duplicate).
  3. All legacy DOM ids referenced by fetch/render functions still
     exist somewhere in the HTML (nested inside c-research / c-gov).
  4. Aggregate rollup pill (#gov-aggregate-pill) + helper function
     _updateGovAggregate are present.
  5. No JS stamp call targets a now-dead card id.
  6. CardSpec registry advertises c-gov with critical=True and does NOT
     advertise any of the deprecated governor cards.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


def _count_card_divs(card_id: str) -> int:
    # Count opening <div ... id="<card_id>"> occurrences (cards only).
    return len(re.findall(
        rf'<div[^>]+id="{re.escape(card_id)}"', HTML,
    ))


def test_unified_research_card_exists_once():
    assert _count_card_divs("c-research") == 1


def test_unified_gov_card_exists_once():
    assert _count_card_divs("c-gov") == 1


def test_deprecated_standalone_cards_are_removed():
    for cid in ("c-research-gov", "c-cards-gov", "c-loop-novelty",
                "c-scenarios", "c-decision"):
        assert _count_card_divs(cid) == 0, (
            f"deprecated card {cid!r} still present as top-level card — "
            "it must be folded into c-research or c-gov"
        )


def test_legacy_dom_ids_still_present_nested():
    """Every inner DOM slot the JS renderers write to must still exist
    (nested inside c-research or c-gov)."""
    required_ids = [
        # research slots
        "research-wr-pill", "research-halt-banner", "research-when",
        "research-tier-grid", "research-recs", "research-notes",
        "research-per-symbol", "research-history", "research-meta",
        # scenarios slots
        "scenarios-summary", "scenarios-best", "scenarios-history",
        # decision slots
        "decision-wr-pill", "decision-conv-pill", "decision-gov-pill",
        "decision-summary", "decision-conversion", "decision-candidates",
        "decision-gov-findings",
        # gov verdict pills
        "research-gov-verdict", "research-gov-summary", "research-gov-findings",
        "cards-gov-verdict", "cards-gov-summary", "cards-gov-breakdown",
        "loop-verdict", "loop-summary", "loop-findings",
        # aggregate
        "gov-aggregate-pill",
    ]
    missing = [i for i in required_ids if f'id="{i}"' not in HTML]
    assert not missing, (
        f"renderer DOM slots missing: {missing}. Add them nested inside "
        "c-research or c-gov so existing JS keeps working."
    )


def test_aggregate_rollup_helper_present():
    assert "_updateGovAggregate" in HTML, (
        "_updateGovAggregate() helper missing — aggregate GOV pill cannot update"
    )


def test_no_stamp_targets_dead_card_ids():
    """Every _regEvMinimal call must target a card that still exists."""
    dead = ("c-research-gov", "c-cards-gov", "c-loop-novelty",
            "c-scenarios", "c-decision")
    for cid in dead:
        pattern = rf'_regEvMinimal\(\s*["\']{re.escape(cid)}["\']'
        hits = re.findall(pattern, HTML)
        assert not hits, (
            f"_regEvMinimal still targets dead card {cid!r} ({len(hits)}× )"
        )


def test_card_spec_registry_has_gov_and_no_deprecated_ones():
    from spot_aggro.governance.card_truth_gov import CARD_SPECS
    ids = {spec.card_id for spec in CARD_SPECS}
    assert "c-gov" in ids
    assert "c-research" in ids
    for dead in ("c-research-gov", "c-cards-gov", "c-loop-novelty",
                 "c-scenarios", "c-decision"):
        assert dead not in ids, (
            f"CardSpec registry still advertises dead card {dead!r}"
        )


def test_unified_gov_card_has_four_layer_verdict_pills():
    """The GOV card must expose all four layer verdict pills."""
    # Extract the block between c-gov opening tag and its closing </div>.
    m = re.search(
        r'<div class="[^"]*" id="c-gov"[\s\S]+?</div>\s*</div>',
        HTML,
    )
    assert m, "c-gov card block not found"
    block = m.group(0)
    for pill_id in ("gov-aggregate-pill", "research-gov-verdict",
                    "cards-gov-verdict", "loop-verdict",
                    "decision-gov-pill"):
        assert f'id="{pill_id}"' in block, (
            f"{pill_id!r} not inside c-gov card"
        )
