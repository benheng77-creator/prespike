"""
Tests for the SPOT dashboard truth-contract (Phase 9 UI).

These tests parse `web/ops/index.html` as text and assert the truth model
is correctly wired without needing a browser:

  * Page-level trust governor banner is present.
  * 4 lane separators (analysis, execution, accounting, infra) exist.
  * 5 highest-severity priority cards carry the full truth-contract
    data attributes.
  * Every named card has its exact operator-supplied helper line.
  * Governance/execution hard-lock tokens are NOT introduced into the
    dashboard (no capital-lock UI, no forensic_v2 code import).

SPOT AGGRO only. Runs as part of the normal spot test suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_PATH = REPO_ROOT / "web" / "ops" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Page-level trust governor + lanes
# ---------------------------------------------------------------------------

def test_trust_governor_banner_exists(html: str) -> None:
    assert 'id="trust-governor"' in html
    assert 'id="tg-label"' in html
    assert 'id="tg-detail"' in html


def test_trust_governor_has_all_five_states_in_css(html: str) -> None:
    """Banner CSS defines exactly the five states operator specified."""
    required = (
        "#trust-governor.trusted",
        "#trust-governor.degraded",
        "#trust-governor.mixed",
        "#trust-governor.stale",
        "#trust-governor.accounting_mismatch",
    )
    for sel in required:
        assert sel in html, f"missing governor state CSS: {sel}"


def test_truth_governor_js_function_present(html: str) -> None:
    assert "function truthGovernor(" in html
    # And refresh() must call it — bound by try/catch so errors don't break refresh
    assert "truthGovernor({" in html


def test_four_lane_separators_present(html: str) -> None:
    lane_texts = {
        "analysis": "Analysis lane · scoring, regime, calibration, governance",
        "execution": "Execution lane · live engine state and orders",
        "accounting": "Accounting lane · audit totals and immutable ledger",
        "infra": "Infrastructure lane · probes, incidents, worker reliability",
    }
    for cls, text in lane_texts.items():
        assert f'class="lane-sep {cls}"' in html, f"missing lane separator {cls}"
        assert text in html, f"missing lane text for {cls}"


# ---------------------------------------------------------------------------
# Priority cards — full truth-contract attributes
# ---------------------------------------------------------------------------

PRIORITY_CARDS = {
    "c-funnel": {
        "lane": "analysis",
        "window_basis": "6h",
        "scope_id": "spot_aggro:all_tiers",
        "helper_fragment": "Default: 6h · where trades get blocked",
    },
    "c-heatmap": {
        "lane": "analysis",
        "window_basis": "24h",
        "scope_id": "spot_aggro:all_tiers",
        "helper_fragment": "Default: 24h · short-term tier quality",
    },
    "c-runtime": {
        "lane": "execution",
        "window_basis": "live",
        "scope_id": "spot_aggro:engine_process",
        "helper_fragment": "Current state · live backend/runtime facts",
    },
    "c-ledger": {
        "lane": "accounting",
        # Phase 11 final — underlying counters are process-session, not
        # 24h-backfilled. Scope and window basis corrected so card label
        # and actual data bind honestly. Flip to "24h" only when a real
        # 24h-windowed audit endpoint is wired.
        "window_basis": "session-local",
        "scope_id": "spot_aggro:trade_log_session",
        # Phase 11n-9-h: helper rewritten to match real data sources.
        "helper_fragment": "Live · Decisions increments every scoring cycle",
    },
    "c-issues": {
        "lane": "infra",
        "window_basis": "live",
        "scope_id": "spot_aggro:watchdog",
        "helper_fragment": "Current state · root causes and fixes",
    },
    "c-quadrant": {
        "lane": "analysis",
        "window_basis": "live-2h-rolling",
        "scope_id": "spot_aggro:scored_universe",
        "helper_fragment": "Default: Live / 2h rolling · current setup quality",
    },
}


@pytest.mark.parametrize("card_id", list(PRIORITY_CARDS.keys()))
def test_priority_card_has_full_truth_contract(html: str, card_id: str) -> None:
    spec = PRIORITY_CARDS[card_id]
    # Locate the card's opening <div> block and grab a window of text
    # after id="c-..." where the data attributes live
    anchor = f'id="{card_id}"'
    assert anchor in html, f"card {card_id} not found"
    idx = html.index(anchor)
    window = html[idx:idx + 1500]

    # Contract attributes — all six required
    for attr in (
        f'data-lane="{spec["lane"]}"',
        f'data-source-id=',
        f'data-scope-id="{spec["scope_id"]}"',
        f'data-window-basis="{spec["window_basis"]}"',
        'data-refresh-basis=',
        'data-summary-source="visible_data_only"',
        'data-confidence-source=',
    ):
        assert attr in window, f"card {card_id} missing attribute: {attr}"

    # Helper line with exact operator text
    assert spec["helper_fragment"] in window, (
        f"card {card_id} missing helper text: {spec['helper_fragment']}"
    )

    # Truth-meta strip
    assert 'class="truth-meta"' in window, f"card {card_id} missing truth-meta strip"


# ---------------------------------------------------------------------------
# All 23 operator helper strings (exact match)
# ---------------------------------------------------------------------------

# Exact strings supplied by the operator. Order is insertion order of cards.
HELPER_STRINGS = (
    "Default: 30m · short-term regime and top symbols",
    "Live snapshot · ranked now",
    # Phase 11e — Open Positions helper updated to acknowledge the dual
    # content reality (scored positions have live TP/SL, reconciled
    # holdings show safe labels). Old wording treated all positions as
    # if they were live strategy, which misled operators.
    # Phase 11n-9-d — helper updated to acknowledge the sweep mechanism
    # (reconciled positions are now swept into governance every tick).
    "Live · scored positions show live TP/SL · reconciled positions are swept into spot_aggro governance (keep/sell/link) every orchestrator tick",
    "Default: 1h · latest executed and rejected actions",
    # Phase 11n-9-k — c-alerts replaced with Alert Center UI.
    "Every gov gap, pre-trade block, orchestrator warn, and adapter error",
    "Default: Live / 2h rolling · current setup quality",
    "Default: 6h · where trades get blocked",
    "Default: 24h · short-term tier quality",
    "Default: 7d · symbol quality with enough sample",
    "Default: Fast 2m / Standard 10m / Heavy 6h",
    "Current state · live backend/runtime facts",
    "Current schedule · engine timing loops",
    "Current config · active behavior rules",
    "Current state · exchange integration health",
    "Live · Decisions increments every scoring cycle · Trades only when an order ships · Accuracy bound to pre-trade gate pass-rate",
    "Default: 24h · immutable decision trail",
    "Default: 6h main review · generated reports",
    "Default: 2h / 6h / 24h · fast, operational, strategic truth",
    "Live now · probes and incidents",
    "Current state · root causes and fixes",
    "Live now · worker reliability",
    "Manual simulation view",
)


@pytest.mark.parametrize("helper", HELPER_STRINGS)
def test_operator_helper_string_is_present_verbatim(html: str, helper: str) -> None:
    assert helper in html, f"missing verbatim helper string: {helper}"


# ---------------------------------------------------------------------------
# Hard-lock regressions
# ---------------------------------------------------------------------------

def test_dashboard_has_no_capital_blocker_banner(html: str) -> None:
    """No capital-based startup-blocker UI. Capital advisory is OK; anything
    that looks like a capital-lock banner is a regression per
    feedback_no_capital_lock rule."""
    forbidden = (
        "capital_blocked",
        "capital-blocked",
        "capital_lock",
        "capital-lock",
        "MIN_CAPITAL_REQUIRED",
        "refuses to start",
    )
    for token in forbidden:
        assert token not in html, (
            f"dashboard contains forbidden capital-lock token {token!r}"
        )


def test_dashboard_does_not_import_forensic_v2_as_code() -> None:
    """The dashboard talks to forensic_v2 via HTTP endpoints, not Python
    imports. Guard against any accidental Python-import-looking pattern."""
    src = INDEX_PATH.read_text(encoding="utf-8")
    # It's HTML, so only JS fetches matter. Accept string URLs like
    # /spot_aggro/ops/spot_aggro/forensic_v2/list; reject Python-style imports.
    for pat in (
        "import forensic_v2",
        "from forensic_v2",
        "from spot_aggro.forensic_v2",
    ):
        assert pat not in src, (
            f"dashboard contains non-URL forensic_v2 pattern {pat!r}"
        )


def test_three_lanes_always_listed_in_analysis_card_labels(html: str) -> None:
    """Both priority analysis cards carry the analysis lane chip."""
    for card_id in ("c-funnel", "c-heatmap", "c-quadrant"):
        idx = html.index(f'id="{card_id}"')
        window = html[idx:idx + 1500]
        assert '<span class="lane-analysis">analysis</span>' in window, (
            f"card {card_id} missing analysis lane chip"
        )


def test_heatmap_truth_strip_names_all_four_tiers(html: str) -> None:
    """Operator rule: A+/A/B/C always shown in analytics. The heatmap's
    truth strip states this explicitly so operators don't expect a
    disabled-tier card to be hidden."""
    idx = html.index('id="c-heatmap"')
    window = html[idx:idx + 1500]
    assert "A+ · A · B · C" in window, (
        "heatmap truth strip must state 'A+ · A · B · C always shown'"
    )


# ---------------------------------------------------------------------------
# Phase 9b — summaries bound to visible data only
# ---------------------------------------------------------------------------

def _extract_function_body(html: str, name: str) -> str:
    """Return the textual body of `function name(...)` up to its matching
    closing brace. Coarse but sufficient for the token-scan guards below."""
    marker = f"function {name}("
    idx = html.index(marker)
    # Advance to first "{"
    brace = html.index("{", idx)
    depth = 0
    for i in range(brace, len(html)):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[brace:i + 1]
    raise AssertionError(f"could not find end of function {name}")


def test_funnel_summary_binds_to_visible_funnel_payload(html: str) -> None:
    """Flow Diagnosis must read the same /spot_aggro/ops/spot_aggro/funnel payload
    the card body shows. It must NOT pull from the 500-row trade tail or
    the consensus tail (different windows)."""
    body = _extract_function_body(html, "updateFunnelReport")
    assert "_lastFunnel" in body, (
        "updateFunnelReport must read the unified-window funnel payload"
    )
    for forbidden in ("_lastTrades", "_lastCons", "_lastSa?.tier_system",
                      "tier_system?.tiered_universe"):
        assert forbidden not in body, (
            f"updateFunnelReport reaches into {forbidden!r}; violates the "
            f"visible-data-only contract"
        )


def test_heatmap_summary_binds_to_visible_heatmap_rows(html: str) -> None:
    """Tier Performance Summary must parse the already-rendered heatmap
    rows (Lane 1 + Lane 2 + Lane 3), not reach back into the 500-row trade
    tail (different window / scope than what the card shows).

    Phase 11b — summary now reads three tbodies: #tb-heatmap (canonical
    performance), #tb-heatmap-activity (canonical flow), and
    #tb-heatmap-reconciled (reconciled activity).
    """
    body = _extract_function_body(html, "updateHeatmapReport")
    # All three lane tbodies must be read from the DOM.
    assert '#tb-heatmap tr' in body, "updateHeatmapReport must read Lane 1 rows"
    assert '#tb-heatmap-activity tr' in body, "updateHeatmapReport must read Lane 2 rows"
    assert '#tb-heatmap-reconciled tr' in body, "updateHeatmapReport must read Lane 3 rows"
    # Still must not reach into hidden dataset tails.
    for forbidden in ("_lastTrades", "tradeRows", "payload_json"):
        assert forbidden not in body, (
            f"updateHeatmapReport reaches into {forbidden!r}; violates the "
            f"visible-data-only contract"
        )


def test_issues_report_element_and_function_present(html: str) -> None:
    assert 'id="issues-report"' in html
    assert "function updateIssuesReport(" in html


def test_issues_summary_binds_to_visible_issue_rows(html: str) -> None:
    body = _extract_function_body(html, "updateIssuesReport")
    assert 'getElementById("tb-issues")' in body, (
        "updateIssuesReport must read #tb-issues DOM rows"
    )
    # Must not reach into external sources.
    for forbidden in ("_lastTrades", "_lastSa", "fetch(", "await f("):
        assert forbidden not in body, (
            f"updateIssuesReport reaches into {forbidden!r}; violates the "
            f"visible-data-only contract"
        )


def test_refresh_invokes_all_three_priority_summaries(html: str) -> None:
    """Priority summaries must rebind every refresh cycle, not only on
    their slow intervals — otherwise the body and summary can diverge for
    up to 15 minutes."""
    body = _extract_function_body(html, "refresh")
    for fn in ("updateFunnelReport()", "updateHeatmapReport()", "updateIssuesReport()"):
        assert fn in body, f"refresh() must call {fn}"


def test_card_report_foot_class_defined(html: str) -> None:
    """Styling for the new issues-report block lives in .card-report-foot
    (no inline styles)."""
    assert ".card-report-foot{" in html
    assert ".card-report-foot .report-title" in html
    assert ".card-report-foot .report-meta" in html


# ---------------------------------------------------------------------------
# Phase 9c — canonical tier seeding (Tier A/A+ must never disappear)
# ---------------------------------------------------------------------------

def _heatmap_block(html: str) -> str:
    """Return the heatmap JS render block source (not the HTML card markup).
    Both surfaces carry the A3 marker; we want the JS one, identified by
    the `// ──` comment prefix used only in the script region."""
    marker = "// ── A3. Tier Accuracy Heatmap"
    idx = html.index(marker)
    end = html.index("// ── A4. Coin Accuracy Matrix", idx)
    return html[idx:end]


def test_heatmap_seeds_all_four_canonical_tiers(html: str) -> None:
    """The heatmap renderer must pre-seed A+/A/B/C with zero counters so
    missing-data tiers still render. A missing tier is a truth-coherence
    bug per operator lock."""
    block = _heatmap_block(html)
    assert 'CANONICAL_TIERS = ["A+", "A", "B", "C"]' in block, (
        "heatmap must declare the canonical A+/A/B/C tier list"
    )
    assert "for (const t of CANONICAL_TIERS)" in block, (
        "heatmap must seed tierStats from CANONICAL_TIERS before aggregating"
    )


def test_heatmap_renders_in_canonical_order_not_lexicographic(html: str) -> None:
    """Rendering must use the canonical order A+ → A → B → C, not the
    lexicographic order that placed "A" before "A+"."""
    block = _heatmap_block(html)
    assert "CANONICAL_TIERS.map(t => [t, tierStats[t]])" in block, (
        "heatmap must render in canonical CANONICAL_TIERS order"
    )
    # The old lexicographic sort must be gone so it can't accidentally
    # resurface a different order.
    assert "a[0].localeCompare(b[0])" not in block, (
        "heatmap must not re-sort tiers lexicographically"
    )


def test_heatmap_has_no_empty_state_fallback_row(html: str) -> None:
    """With canonical seeding, the 'no trades yet — charts populate…'
    fallback is unreachable and must be removed so the table cannot fall
    back to hiding every tier."""
    block = _heatmap_block(html)
    assert "no trades yet — charts populate after first entries" not in block, (
        "stale 'empty' fallback row must be removed — canonical tiers "
        "always render"
    )


def test_heatmap_renders_no_data_row_for_zero_activity_tier(html: str) -> None:
    """A canonical tier with zero activity must render a 'NO DATA' quality
    pill — not disappear from the table. Phase 11b: Lane 1 also introduces
    'NO CLOSED EXITS' (some activity but no exits closed yet) and
    'LOW SAMPLE' (exits > 0 but < 3) for finer operator signal."""
    block = _heatmap_block(html)
    assert '"NO DATA"' in block, (
        "heatmap must surface a NO DATA row for a fully-empty canonical tier"
    )
    # Lane 1 quality labels — all three must be reachable.
    assert '"NO CLOSED EXITS"' in block, (
        "Lane 1 must mark tiers with activity but no closed exits as NO CLOSED EXITS"
    )
    assert '"LOW SAMPLE"' in block, (
        "Lane 1 must mark under-3-sample tiers as LOW SAMPLE"
    )


def test_heatmap_meta_reports_tiers_shown_and_active(html: str) -> None:
    """The meta line must state `4 canonical tiers shown` (always) alongside
    an active-exits count, so a degraded-sample run still visibly shows all
    tiers. Phase 11b — wording refined to 'with closed exits' for honesty."""
    block = _heatmap_block(html)
    assert "canonical tiers shown" in block
    assert "with closed exits" in block, (
        "meta must distinguish 'shown' (always 4) from 'with closed exits'"
    )


def test_heatmap_routes_reconciled_to_lane3_not_canonical_table(html: str) -> None:
    """Phase 11b — reconciled positions (module starts with M_reconciled)
    are routed to Lane 3 BEFORE the canonical tier check. This replaces the
    old 'off-enum' routing with a stricter, explicit lane separation.
    Canonical tiers never accumulate reconciled activity."""
    block = _heatmap_block(html)
    # Reconciled routing is by module prefix, not by tier value.
    assert 'module.startsWith("M_reconciled")' in block, (
        "reconciled rows must be routed by module prefix, not tier value"
    )
    # Lane 3 accumulator exists.
    assert "reconStats" in block
    # Unknown-provenance lane exists (should stay empty post-migration).
    assert "unknownByModule" in block
    # Top-level tier field is read (post-Phase-11b migration).
    assert "let tier = t.tier;" in block


# ---------------------------------------------------------------------------
# Phase 9d — visible tier execution toggles in the UI
# ---------------------------------------------------------------------------

def test_tier_toggle_card_present(html: str) -> None:
    """The dashboard must carry a visible card for execution-only tier toggles."""
    assert 'id="c-tier-toggles"' in html
    assert "<h3>Tier Execution Toggles</h3>" in html
    assert 'data-lane="execution"' in html
    # Scope must be execution-only (not analysis)
    assert 'data-scope-id="spot_aggro:tier_execution"' in html


def test_tier_toggle_helper_text_exact(html: str) -> None:
    """Exact operator-required phrasing (execution only, analytics unchanged)."""
    assert "Execution only. Analysis still includes this tier." in html


@pytest.mark.parametrize("label", [
    "Trade Tier A+", "Trade Tier A", "Trade Tier B", "Trade Tier C",
])
def test_tier_toggle_labels_exact(html: str, label: str) -> None:
    assert f"<b>{label}</b>" in html


def test_tier_toggle_checkbox_for_each_canonical_tier(html: str) -> None:
    """One checkbox per canonical tier, each wired to `flipTierToggle`."""
    idx = html.index('id="c-tier-toggles"')
    end = html.index("</div>\n\n<!-- 16. Automation Schedule", idx)
    block = html[idx:end]
    for tier in ("A+", "A", "B", "C"):
        assert f'data-tier="{tier}"' in block, f"missing data-tier={tier}"
        assert f"flipTierToggle('{tier}'" in block, (
            f"missing onchange handler for tier {tier}"
        )
    # Four <input type="checkbox"> elements in this block
    assert block.count('type="checkbox"') == 4


def test_tier_toggle_note_states_analytics_unchanged(html: str) -> None:
    """The card must make execution-only semantics unmistakable so the
    operator doesn't mistake toggling for deletion or filtering."""
    idx = html.index('id="c-tier-toggles"')
    end = html.index("</div>\n\n<!-- 16. Automation Schedule", idx)
    block = html[idx:end]
    assert "ORDERS only" in block
    for word in ("Scoring", "ranking", "analytics", "funnel",
                 "heatmaps", "reports", "forensic", "historical"):
        assert word in block, f"tier-toggle note missing '{word}'"


def test_tier_toggle_backend_endpoints_fetched_on_refresh(html: str) -> None:
    body = _extract_function_body(html, "refresh")
    # Phase 9e — the tier-toggle surface is now served from the spot-owned
    # /spot_aggro router, not /spot_aggro/ops/spot_aggro/*.
    assert 'f("/spot_aggro/tier_toggles")' in body, (
        "refresh() must fetch tier_toggles state every cycle from the spot router"
    )
    assert "renderTierToggles(tierToggles)" in body


def test_flipTierToggle_posts_via_postJSON(html: str) -> None:
    """The flip handler must POST to the correct endpoint with the
    admin-token header via postJSON. Phase 9e — spot-owned URL."""
    body = _extract_function_body(html, "flipTierToggle")
    assert 'postJSON("/spot_aggro/tier_toggles"' in body
    assert "tier: tier" in body
    assert "enabled: enabled" in body
    # Must re-render from the authoritative response, not trust the local
    # optimistic state — prevents UI drift if the server rejects.
    assert "renderTierToggles(r)" in body


# ---------------------------------------------------------------------------
# Phase 9f — remaining contradiction reconciliations
# ---------------------------------------------------------------------------

def test_engine_card_has_posture_pill_and_truth_meta(html: str) -> None:
    """Trading Engine Status must carry a POSTURE pill (not just a LIVE
    badge) and a truth-meta strip describing the posture's sources."""
    idx = html.index('id="c-engine"')
    # Card runs until the next top-level card (System Connectivity).
    end = html.index('id="c-conn"', idx)
    block = html[idx:end]
    assert 'id="eng-posture"' in block, "posture pill element missing"
    assert 'id="eng-posture-detail"' in block
    assert 'data-scope-id="spot_aggro:engine_posture"' in block
    assert 'class="truth-meta"' in block
    assert "posture source: status + tier toggles + watchdog" in block


def test_posture_updater_binds_to_visible_signals(html: str) -> None:
    body = _extract_function_body(html, "updateEnginePosture")
    # Inputs must come from the refresh ctx (sa, kill, tierToggles, wd).
    # No hidden datasets.
    for tok in ("ctx.sa", "ctx.kill", "ctx.tierToggles", "ctx.wd"):
        assert tok in body, f"posture updater missing signal {tok}"
    # Must produce one of the explicit posture labels.
    for label in ('"READY"', '"HALTED"', '"DEGRADED"', '"PAUSED"'):
        assert label in body, f"posture label {label} missing"
    # Called from refresh().
    rbody = _extract_function_body(html, "refresh")
    assert "updateEnginePosture({sa, kill, tierToggles, wd})" in rbody


def test_posture_css_states_defined(html: str) -> None:
    for sel in (
        ".posture-pill.ready", ".posture-pill.degraded",
        ".posture-pill.halted", ".posture-pill.paused",
    ):
        assert sel in html, f"missing posture CSS {sel}"


def test_executed_activity_card_has_truth_meta_and_mix_foot(html: str) -> None:
    idx = html.index('id="c-trades"')
    end = html.index('id="c-alerts"', idx)
    block = html[idx:end]
    assert 'data-scope-id="spot_aggro:trade_log_activity"' in block
    assert 'class="truth-meta"' in block
    assert "enter · exit · reject rows" in block
    assert 'id="trades-mix"' in block
    # Heading must name the reject rows so operator is not misled.
    assert "Executed + rejected" in block


def test_trades_mix_binds_to_visible_tb_trades(html: str) -> None:
    body = _extract_function_body(html, "updateTradesMix")
    assert 'getElementById("tb-trades")' in body, (
        "updateTradesMix must read #tb-trades DOM rows"
    )
    for forbidden in ("_lastTrades", "fetch(", "await f("):
        assert forbidden not in body, (
            f"updateTradesMix reaches into {forbidden!r}; "
            f"violates visible-data-only contract"
        )
    # Called from refresh().
    rbody = _extract_function_body(html, "refresh")
    assert "updateTradesMix()" in rbody


def test_runtime_card_scope_distinguishes_server_from_spot_engine(html: str) -> None:
    idx = html.index('id="c-runtime"')
    end = html.index('id="c-tier-toggles"', idx)
    block = html[idx:end]
    assert "/status (server)" in block
    # Phase 10 — spot status now served from spot-owned router.
    assert "/spot_aggro/status (engine)" in block
    assert "server + spot engine process" in block


def test_llm_ensemble_card_has_quorum_foot_bound_to_visible_rows(html: str) -> None:
    idx = html.index('id="c-llm"')
    end = html.index('id="c-sim"', idx)
    block = html[idx:end]
    assert 'data-scope-id="spot_aggro:llm_workers"' in block
    assert 'id="llm-quorum"' in block
    assert "audit swarm is 4-of-4 strict" in block


def test_llm_quorum_readout_binds_to_visible_rows(html: str) -> None:
    body = _extract_function_body(html, "updateLLMQuorumReadout")
    assert 'getElementById("tb-llm")' in body
    for forbidden in ("fetch(", "await f("):
        assert forbidden not in body
    # Quorum logic is 4-of-4 strict — any softening is a regression.
    assert "okCount >= 4" in body
    rbody = _extract_function_body(html, "refresh")
    assert "updateLLMQuorumReadout()" in rbody


def test_forensic_card_separates_v1_from_v2_windows(html: str) -> None:
    """The v1 Forensic Accuracy card must state its window is per-row and
    that its cadence is distinct from Forensic Truth (v2)."""
    idx = html.index('id="c-forensic"')
    end = html.index('id="c-forensic-v2"', idx)
    block = html[idx:end]
    assert 'data-window-basis="per-row-Period-column"' in block
    assert "not same cadence as Forensic Truth (v2)" in block
    assert "window per row" in block  # subtitle clarifies window source


def test_forensic_meta_binds_to_visible_rows(html: str) -> None:
    """Forensic-meta must describe the visible rows' span, not a hardcoded
    label like 'N report(s) generated'."""
    # We search the whole HTML because the meta setter lives in refresh().
    assert "visible span" in html, (
        "forensic-meta must describe the visible rows' span"
    )
    assert "minStart" in html and "maxEnd" in html, (
        "forensic-meta must compute the span from visible row timestamps"
    )


def test_infra_card_distinguishes_unknown_from_clean(html: str) -> None:
    idx = html.index('id="c-infra"')
    end = html.index('id="c-issues"', idx)
    block = html[idx:end]
    assert 'data-scope-id="spot_aggro:infra_probes"' in block
    assert "clean ≠ unknown" in block
    assert 'id="infra-sub-state"' in block
    # Body element must declare the state class container.
    assert 'class="infra-summary-line"' in block


def test_infra_state_updater_uses_wd_from_ctx_and_has_four_states(html: str) -> None:
    body = _extract_function_body(html, "updateInfraState")
    # wd must come from the refresh ctx — no hidden fetch.
    assert "ctx.wd" in body
    for forbidden in ("fetch(", "await f("):
        assert forbidden not in body
    # Each of the four states must be settable. CLEAN vs UNKNOWN is the
    # main locked distinction.
    for label in ("UNKNOWN", "CLEAN", "DEGRADED", "WARN"):
        assert label in body, f"missing infra state label {label}"
    # Called from refresh().
    rbody = _extract_function_body(html, "refresh")
    assert "updateInfraState({wd})" in rbody


def test_infra_summary_css_states_defined(html: str) -> None:
    for sel in (
        ".infra-summary-line.clean",
        ".infra-summary-line.degraded",
        ".infra-summary-line.bad",
        ".infra-summary-line.unknown",
    ):
        assert sel in html, f"missing infra state CSS {sel}"


def test_issues_summary_note_clean_vs_unknown(html: str) -> None:
    """System Issues card's summary must carry the 'clean ≠ unknown' signal
    so an empty table cannot be mistaken for a healthy probe."""
    idx = html.index('id="c-issues"')
    end = html.index('id="c-llm"', idx)
    block = html[idx:end]
    assert "clean ≠ unknown" in block
    assert "awaiting first fetch" in block


# ---------------------------------------------------------------------------
# Phase 9g — Account / Gate / MIO reconciliations
# ---------------------------------------------------------------------------

def test_account_card_declared_as_accounting_lane(html: str) -> None:
    """Account Snapshot mixes accounting (equity/fees) with operational
    counters (open pairs, llm cost). The card must be declared under the
    accounting lane and its body must visibly split the two groups."""
    idx = html.index('id="c-acct"')
    end = html.index('id="c-gates"', idx)
    block = html[idx:end]
    assert 'data-lane="accounting"' in block
    assert 'data-scope-id="spot_aggro:account_state"' in block
    assert 'class="truth-meta"' in block
    # Two labelled groups
    assert block.count('class="acct-group-label"') == 2, (
        "account card must visibly split accounting vs operational groups"
    )
    assert "Accounting · lifetime" in block
    assert "Operational · current + 24h" in block


def test_account_card_states_no_capital_based_blocking(html: str) -> None:
    """Phase 9g guard — the accounting card must explicitly carry the
    no-capital-blocking signal in its truth-meta strip so the operator
    cannot mistake low equity for a blocker."""
    idx = html.index('id="c-acct"')
    end = html.index('id="c-gates"', idx)
    block = html[idx:end]
    assert "no capital-based blocking" in block


def test_gate_card_names_real_l0_to_l6_chain(html: str) -> None:
    """Strategy Gate Status card must reference the real L0..L6 chain,
    not the pre-phase-2 composite-gate shape."""
    idx = html.index('id="c-gates"')
    end = html.index("<!-- ═══ ROW 3", idx)
    block = html[idx:end]
    assert 'data-scope-id="spot_aggro:entry_gate_chain"' in block
    for label in ("L0", "L1", "L2", "L3", "L6"):
        assert label in block, f"gate card missing {label} label"
    assert "L0..L6" in block or "L0 advisory" in block
    assert "tier toggle" in block
    assert 'class="truth-meta"' in block


def test_gate_card_uses_class_styling_not_inline(html: str) -> None:
    """No inline styles for the gate chip row / summary — must use
    .gate-chip-row and .gate-summary-line classes."""
    idx = html.index('id="c-gates"')
    end = html.index("<!-- ═══ ROW 3", idx)
    block = html[idx:end]
    assert 'class="gate-chip-row"' in block
    assert 'class="gate-summary-line"' in block
    # CSS classes defined
    assert ".gate-chip-row{" in html
    assert ".gate-summary-line{" in html


def test_mio_card_has_truth_meta_and_freshness_foot(html: str) -> None:
    idx = html.index('id="c-mio"')
    end = html.index('id="c-universe"', idx)
    block = html[idx:end]
    assert 'data-scope-id="spot_aggro:mio_cycle"' in block
    assert 'data-window-basis="30m cycle"' in block
    assert 'class="truth-meta"' in block
    assert 'id="mio-freshness"' in block
    # Stale-threshold must be visible to the operator in the truth-meta.
    assert "stale if Updated &gt; 35m" in block


def test_mio_freshness_binds_to_visible_age(html: str) -> None:
    body = _extract_function_body(html, "updateMIOFreshness")
    assert 'getElementById("mio-age")' in body, (
        "updateMIOFreshness must read the visible #mio-age element"
    )
    # No hidden fetches, no backdoor dataset.
    for forbidden in ("fetch(", "await f(", "_lastSa", "_lastTrades"):
        assert forbidden not in body, (
            f"updateMIOFreshness reaches into {forbidden!r}"
        )
    # Four explicit verdict labels.
    for label in ('"FRESH"', '"AGING"', '"STALE"', '"UNKNOWN"'):
        assert label in body, f"missing MIO freshness label {label}"
    # Wired into refresh().
    rbody = _extract_function_body(html, "refresh")
    assert "updateMIOFreshness()" in rbody


def test_mio_card_has_no_inline_top6_style(html: str) -> None:
    """The old Market Intelligence card used inline styles for the top-6
    line. Must move to .mio-top6 class per the no-inline-style rule."""
    idx = html.index('id="c-mio"')
    end = html.index('id="c-universe"', idx)
    block = html[idx:end]
    assert 'class="mio-top6"' in block
    assert ".mio-top6{" in html


# ---------------------------------------------------------------------------
# Phase 9h — Connectivity / Performance / Universe / Positions
# ---------------------------------------------------------------------------

def test_connectivity_card_has_truth_meta_and_summary(html: str) -> None:
    idx = html.index('id="c-conn"')
    end = html.index('id="c-today"', idx)
    block = html[idx:end]
    assert 'data-lane="infra"' in block
    assert 'data-scope-id="spot_aggro:connectivity"' in block
    assert 'class="truth-meta"' in block
    assert 'id="conn-summary"' in block
    # Explicit UNKNOWN-vs-healthy signal — a grey dot must not be silently
    # collapsed into 'healthy'.
    assert "any grey dot = UNKNOWN, not healthy" in block


def test_connectivity_summary_binds_to_visible_pills(html: str) -> None:
    body = _extract_function_body(html, "updateConnectivitySummary")
    assert 'getElementById("conn-pills")' in body
    # Must look at visible dot classes, not any hidden dataset.
    assert '.dot' in body
    for forbidden in ("fetch(", "await f(", "_lastSa"):
        assert forbidden not in body, (
            f"updateConnectivitySummary reaches into {forbidden!r}"
        )
    for label in ('"HEALTHY"', '"WARN"', '"DEGRADED"', '"UNKNOWN"'):
        assert label in body, f"missing connectivity label {label}"
    rbody = _extract_function_body(html, "refresh")
    assert "updateConnectivitySummary()" in rbody


def test_performance_card_is_accounting_lane_and_lifetime(html: str) -> None:
    idx = html.index('id="c-today"')
    end = html.index("<!-- ═══ ROW 2", idx)
    block = html[idx:end]
    assert 'data-lane="accounting"' in block
    assert 'data-scope-id="spot_aggro:lifetime_pnl"' in block
    assert "lifetime (NOT today, NOT session)" in block, (
        "Performance card must explicitly disambiguate lifetime vs today"
    )
    assert "no capital-based blocking" in block
    assert 'id="perf-summary"' in block


def test_performance_summary_binds_to_visible_tiles(html: str) -> None:
    body = _extract_function_body(html, "updatePerformanceSummary")
    # Pulls each tile's textContent — not the backend, not a cached dict.
    for tile_id in ("tp-pnl", "tp-wr", "tp-exp", "tp-avg-win", "tp-trades"):
        assert f'"{tile_id}"' in body, (
            f"updatePerformanceSummary must read visible tile {tile_id}"
        )
    for forbidden in ("fetch(", "await f(", "_lastSa", "_lastTrades"):
        assert forbidden not in body
    rbody = _extract_function_body(html, "refresh")
    assert "updatePerformanceSummary()" in rbody


def test_universe_card_has_truth_meta_and_tier_summary(html: str) -> None:
    idx = html.index('id="c-universe"')
    end = html.index('id="c-positions"', idx)
    block = html[idx:end]
    assert 'data-scope-id="spot_aggro:scored_universe"' in block
    assert 'class="truth-meta"' in block
    assert "no tier filtering" in block
    assert 'id="universe-summary"' in block
    assert "all tiers A+/A/B/C visible when scored" in block


def test_universe_summary_counts_all_four_tiers(html: str) -> None:
    body = _extract_function_body(html, "updateUniverseSummary")
    assert 'getElementById("tb-uni")' in body
    # Must count every canonical tier, never hide one.
    assert '"A+": 0' in body
    assert '"A": 0' in body
    assert '"B": 0' in body
    assert '"C": 0' in body
    for forbidden in ("fetch(", "await f(", "_lastSa"):
        assert forbidden not in body
    rbody = _extract_function_body(html, "refresh")
    assert "updateUniverseSummary()" in rbody


def test_positions_card_distinguishes_zero_from_unknown(html: str) -> None:
    idx = html.index('id="c-positions"')
    end = html.index('id="c-trades"', idx)
    block = html[idx:end]
    assert 'data-scope-id="spot_aggro:open_positions"' in block
    assert "empty ≠ unknown" in block
    assert "empty table = zero positions (authoritative)" in block
    assert "awaiting first fetch" in block


def test_positions_summary_emits_authoritative_zero_or_unknown(html: str) -> None:
    body = _extract_function_body(html, "updatePositionsSummary")
    assert 'getElementById("tb-pos")' in body
    # Authoritative zero path vs unknown path must both exist.
    assert "ZERO" in body
    assert "state unknown, not zero" in body
    for forbidden in ("fetch(", "await f(", "_lastTrades"):
        assert forbidden not in body
    rbody = _extract_function_body(html, "refresh")
    assert "updatePositionsSummary({sa})" in rbody


# ---------------------------------------------------------------------------
# Phase 9i — remaining deferred cards get truth-meta
# ---------------------------------------------------------------------------

def _card_block(html: str, start_id: str, end_needle: str) -> str:
    idx = html.index(f'id="{start_id}"')
    end = html.index(end_needle, idx)
    return html[idx:end]


def test_consensus_card_has_truth_meta_and_separates_from_audit_swarm(html: str) -> None:
    block = _card_block(html, "c-consensus", "<!-- Phase 9 — lane separator: ANALYSIS")
    assert 'data-scope-id="spot_aggro:consensus_decisions"' in block
    assert 'class="truth-meta"' in block
    # Must clearly separate consensus analysis from audit-swarm gate to
    # avoid the pre-Phase-7 "3-of-5 passes" confusion.
    assert "consensus is ANALYSIS" in block
    assert "audit swarm (4-of-4)" in block


def test_alerts_card_has_truth_meta_and_empty_is_authoritative(html: str) -> None:
    # Phase 11n-9-k: c-alerts upgraded to full Alert Center. Old legacy
    # "awaiting first fetch" table replaced with severity pills +
    # active list. Contract assertions refreshed to the new structure.
    block = _card_block(html, "c-alerts", "<!-- ═══ ANALYTICS: 4 Executive Charts")
    assert 'data-scope-id="spot_aggro:alert_center"' in block
    assert 'class="truth-meta"' in block
    # Severity pills present (P0/P1/P2/P3).
    for sev in ("P0", "P1", "P2", "P3"):
        assert f'alerts-{sev.lower()}-pill' in block
    # Correlation + ack + mute surfaces.
    assert 'alerts-groups-pill' in block
    assert 'alerts-active-list' in block


def test_matrix_card_has_truth_meta(html: str) -> None:
    block = _card_block(html, "c-matrix", 'id="c-swarm"')
    assert 'data-scope-id="spot_aggro:per_symbol"' in block
    assert 'data-window-basis="7d"' in block
    assert 'class="truth-meta"' in block
    # Old inline-styled report foot must be a class-based foot now.
    assert 'class="card-report-foot"' in block
    assert "bound to visible coin rows" in block


def test_swarm_card_has_truth_meta_and_distinguishes_audit_swarm(html: str) -> None:
    block = _card_block(html, "c-swarm", "<!-- ═══ ROW 8")
    assert 'data-scope-id="spot_aggro:research_swarm"' in block
    assert 'class="truth-meta"' in block
    # Must declare itself NOT the audit-swarm (execution gate).
    assert "distinct from audit-swarm (4-of-4 execution gate)" in block
    # Inline styles must be replaced by classes (may be combined with
    # other class tokens, e.g. class="sg g4 sw-stat-row").
    assert "sw-stat-row" in block
    assert "sw-watchlist" in block


def test_schedule_card_has_truth_meta_and_non_live_refresh(html: str) -> None:
    block = _card_block(html, "c-schedule", 'id="c-settings"')
    assert 'data-scope-id="spot_aggro:schedule"' in block
    assert 'data-refresh-basis="page-load"' in block, (
        "schedule card must declare it is not live"
    )
    assert 'class="truth-meta"' in block
    assert "liveness: see Runtime Control Panel" in block


def test_settings_card_has_truth_meta_and_no_capital_block(html: str) -> None:
    block = _card_block(html, "c-settings", 'lane-sep accounting')
    assert 'data-scope-id="spot_aggro:active_settings"' in block
    assert 'class="truth-meta"' in block
    assert "no capital-based blocking" in block
    # Inline-styled modules row must be class-based now.
    assert 'class="set-modules-row"' in block


def test_exchange_card_has_truth_meta_and_configured_vs_connected(html: str) -> None:
    block = _card_block(html, "c-exchange", 'id="c-ledger"')
    assert 'data-scope-id="spot_aggro:exchange_integration"' in block
    assert 'data-refresh-basis="page-load"' in block
    assert 'class="truth-meta"' in block
    # The card must make the configured-vs-connected distinction visible.
    assert "configured ≠ connected" in block


def test_botledger_card_has_truth_meta(html: str) -> None:
    block = _card_block(html, "c-botledger", '<!-- ═══ Forensic Reports')
    assert 'data-scope-id="spot_aggro:bot_decisions"' in block
    assert 'class="truth-meta"' in block
    assert "card hidden until first row" in block


def test_sim_card_has_truth_meta_and_non_live_markers(html: str) -> None:
    block = _card_block(html, "c-sim", "</div><!-- /tab-dash")
    assert 'data-scope-id="spot_aggro:simulation"' in block
    assert 'class="truth-meta"' in block
    # Must be impossible to mistake the Capital input for live equity.
    assert "NOT connected to live trading" in block
    assert "capital field is hypothetical, not live equity" in block
    assert "Capital (hypothetical)" in block
    # Inline styles must be replaced with classes (sim-run-btn is
    # combined with the existing .btn utility class).
    assert 'class="sim-grid"' in block
    assert 'class="sim-input"' in block
    assert "sim-run-btn" in block


def test_all_deferred_cards_now_carry_truth_meta_attributes(html: str) -> None:
    """Every card that previously lacked truth-meta now carries all five
    required data-* attributes. This is the acceptance test for the
    Phase 9 series."""
    card_ids = (
        "c-conn", "c-today", "c-acct", "c-gates", "c-consensus",
        "c-mio", "c-universe", "c-positions", "c-trades", "c-alerts",
        "c-quadrant", "c-funnel", "c-heatmap", "c-matrix", "c-swarm",
        "c-engine", "c-runtime", "c-schedule", "c-settings", "c-exchange",
        "c-ledger", "c-botledger", "c-forensic", "c-llm", "c-sim",
        "c-tier-toggles", "c-issues", "c-infra",
    )
    required_attrs = (
        "data-lane", "data-source-id", "data-scope-id",
        "data-window-basis", "data-refresh-basis",
        "data-summary-source", "data-confidence-source",
    )
    missing: list[tuple[str, str]] = []
    for cid in card_ids:
        anchor = f'id="{cid}"'
        if anchor not in html:
            continue  # card may be absent (feature-flagged); don't false-fail
        idx = html.index(anchor)
        # The attributes live in the opening <div class="c" id="..."> tag
        # block — scan the next 1000 chars which always covers the opening.
        window = html[idx:idx + 1500]
        for attr in required_attrs:
            if attr not in window:
                missing.append((cid, attr))
    assert not missing, f"cards missing truth-contract attributes: {missing}"


# ---------------------------------------------------------------------------
# Phase 9j — Account Snapshot rebind + governor PAUSED recognition
# ---------------------------------------------------------------------------

def test_account_snapshot_reads_equity_from_pnl_not_status(html: str) -> None:
    """Phase 9j: equity/peak/drawdown must come from /spot_aggro/ops/pnl. The old
    sa.capital_usd / sa.peak_equity read from /spot_aggro/ops/spot_aggro/status is
    the bug that tripped the ACCOUNTING MISMATCH banner when the engine
    was stopped."""
    body = _extract_function_body(html, "refresh")
    # The Account Snapshot block must read pnl.equity_usd, pnl.peak_usd,
    # pnl.drawdown_pct, pnl.open_pairs. Use substring checks that are tied
    # to the new source.
    assert "pnl.equity_usd" in body, (
        "Account Snapshot must read pnl.equity_usd, not sa.capital_usd"
    )
    assert "pnl.peak_usd" in body
    assert "pnl.drawdown_pct" in body
    assert "pnl.open_pairs" in body
    # Old broken reads must be gone from the Account Snapshot block. Scan
    # ONLY the stretch of refresh() that follows the Account Snapshot
    # comment so unrelated uses elsewhere (if any) don't false-fail.
    anchor = "// ── 4. Account Snapshot ──"
    start = body.index(anchor)
    end = body.index("// ── 5.", start)
    acct_block = body[start:end]
    for old in ("sa.capital_usd", "sa.peak_equity", "sa.current_equity"):
        assert old not in acct_block, (
            f"Account Snapshot still reads old broken source {old!r}"
        )


def test_account_snapshot_truth_meta_names_pnl_as_equity_source(html: str) -> None:
    """Truth-meta strip must name /spot_aggro/ops/pnl as the authoritative equity
    source so the operator can see the mapping.

    Phase 11f — /spot_aggro/ops/pnl is now explicitly labeled 'shared ops infra' so
    the operator cannot misread it as apex-engine ownership. The
    (authoritative) marker is retained and prefixed with the ownership
    clarifier.
    """
    idx = html.index('id="c-acct"')
    end = html.index('id="c-gates"', idx)
    block = html[idx:end]
    # Phase 10 — spot stats endpoint relocated to spot-owned router.
    assert 'data-source-id="/spot_aggro/ops/pnl + /spot_aggro/stats + /spot_aggro/ops/llm/cost"' in block
    assert "equity source: /spot_aggro/ops/pnl (shared ops infra · authoritative)" in block


def test_governor_recognizes_running_false_as_paused(html: str) -> None:
    body = _extract_function_body(html, "truthGovernor")
    # Must explicitly detect running:false.
    assert "sa.running === false" in body, (
        "governor must detect sa.running === false"
    )
    # Must set the paused state, not trusted/accounting_mismatch.
    assert 'state = "paused"' in body


def test_governor_paused_state_suppresses_accounting_mismatch(html: str) -> None:
    """When PAUSED, the accounting mismatch check must be skipped so a
    sparse status endpoint doesn't trigger a false ACCOUNTING MISMATCH."""
    body = _extract_function_body(html, "truthGovernor")
    # The accounting-mismatch block must be guarded by a paused check.
    assert 'if (state !== "paused")' in body, (
        "accounting-mismatch detection must be guarded by the paused state"
    )


def test_governor_has_paused_label_and_css(html: str) -> None:
    """PAUSED must render a distinct label and have its own banner CSS."""
    body = _extract_function_body(html, "truthGovernor")
    assert 'paused: "PAUSED' in body, "governor label map must include paused"
    # CSS class for the paused banner
    assert "#trust-governor.paused{" in html


# ---------------------------------------------------------------------------
# Phase 9k — stopped-engine truth (UNKNOWN ≠ BAD ≠ FAILURE)
# ---------------------------------------------------------------------------

def _connectivity_block(html: str) -> str:
    """Return the JS block that renders the connectivity pills inside refresh()."""
    marker = "// ── 2. System Connectivity ──"
    idx = html.index(marker)
    end = html.index("// ── 3.", idx)
    return html[idx:end]


def test_connectivity_renderer_treats_stopped_engine_as_unknown(html: str) -> None:
    """When the spot engine is stopped, exchange/feed/scheduler/overall
    must render as UNKNOWN ('x'), not BAD ('r'). DB stays driven by /status."""
    block = _connectivity_block(html)
    assert "engineUnknown" in block, (
        "connectivity block must declare an engineUnknown signal"
    )
    # All three required conditions: missing sa, sa.running===false, and
    # cycles undefined.
    for cond in ("!sa", "sa.running === false", "sa.cycles === undefined"):
        assert cond in block, (
            f"connectivity engineUnknown must check {cond!r}"
        )
    # The renderer must emit "x" (grey/unknown) not "r" when ok is null.
    # The expression `ok === true ? "g" : ok === false ? "r" : "x"` gives
    # null/undefined → "x", false → "r", true → "g".
    assert 'ok === true ? "g" : ok === false ? "r" : "x"' in block, (
        "connectivity pill renderer must explicitly map null → 'x' (unknown)"
    )
    # DB stays driven by /status, not by the engine state.
    assert "st && st.db_present" in block, (
        "DB pill must remain driven by /status, not engine state"
    )


def test_funnel_partial_state_when_engine_stopped(html: str) -> None:
    """Funnel must surface PARTIAL · engine stopped instead of inventing a
    drop-off when scored=0 but downstream rows exist (historical DB)."""
    body = _extract_function_body(html, "updateFunnelReport")
    # Reads the cached engine state — no hidden fetch.
    assert "window._lastSa" in body
    assert "engineStopped" in body
    # The PARTIAL annotation must be present and its trigger must combine
    # engineStopped, scored===0, AND non-zero downstream activity.
    assert "PARTIAL · engine stopped" in body
    assert "engineStopped && scored === 0" in body
    assert "(consFire + entered + rejected + exited) > 0" in body
    # Drop-off cannot be computed truthfully — the partial branch must
    # explicitly say so instead of returning a fake biggest-drop.
    assert "Scored is unknown, not zero" in body
    # No hidden fetches.
    for forbidden in ("fetch(", "await f("):
        assert forbidden not in body


def test_llm_quorum_recognises_sleep_as_idle_not_failure(html: str) -> None:
    """All-sleep workers must produce 'all workers idle (engine stopped)',
    not the red 'quorum NOT reachable' message."""
    body = _extract_function_body(html, "updateLLMQuorumReadout")
    # Sleep is its own bucket.
    assert "let okCount = 0, degraded = 0, bad = 0, sleeping = 0" in body or \
           ("sleeping = 0" in body and "let okCount" in body), (
        "quorum readout must declare a sleeping counter"
    )
    assert "/\\bsleep\\b|\\bidle\\b/" in body, (
        "quorum readout must classify rows whose status text contains 'sleep' or 'idle' as sleeping"
    )
    # All-idle distinct verdict.
    assert "allIdle" in body
    assert "all workers idle (engine stopped)" in body
    # Must NOT claim NOT reachable when all workers are sleeping.
    # Confirm the conditional ordering: allIdle is checked BEFORE
    # the "NOT reachable" branch.
    assert body.index("allIdle") < body.index("NOT reachable"), (
        "all-idle branch must be evaluated before the NOT-reachable branch"
    )


def test_phase9l_infra_state_reads_findings_before_rows(html: str) -> None:
    """Phase 9l — /spot_aggro/ops/watchdog returns {findings, queue_unprocessed}.
    The summary MUST read wd.findings (the actual payload shape), not
    just wd.rows, otherwise a populated table renders a CLEAN summary."""
    body = _extract_function_body(html, "updateInfraState")
    # New normalization chain: findings → rows → bare array.
    assert "Array.isArray(wd.findings)" in body, (
        "updateInfraState must normalize wd.findings first (actual shape)"
    )
    # The chain must still tolerate wd.rows and bare arrays for forward-compat.
    assert "Array.isArray(wd.rows)" in body
    assert "Array.isArray(wd)" in body


# ---------------------------------------------------------------------------
# Phase 10 FINAL — Runtime truth-system coverage (Gap A + B + C + D)
# ---------------------------------------------------------------------------

def test_phase10final_truth_validator_present(html: str) -> None:
    """Gap A — runtime validator function must exist, maintain
    window._truthIssues + window._truthEvidence, and be wired into
    refresh() and a setInterval loop."""
    # Validator + register helper + core state objects
    for needle in (
        "function registerTruthEvidence(",
        "function truthValidator(",
        "window._truthEvidence",
        "window._truthIssues",
        "function _truthClose(",
    ):
        assert needle in html, f"missing truth-system symbol: {needle}"
    # Wired into refresh() before the governor.
    rbody = _extract_function_body(html, "refresh")
    assert "truthValidator();" in rbody, "refresh() must call truthValidator()"
    # And there must be a setInterval driving stale detection.
    assert "setInterval(truthValidator" in html, (
        "truthValidator must run on an interval independent of refresh()"
    )


def test_phase10final_validator_detects_mismatch_and_stale(html: str) -> None:
    """Gap A+B — truthValidator must emit STALE and MISMATCH finding
    kinds and attach .card-stale / .card-mismatch classes to offending
    cards."""
    vbody = _extract_function_body(html, "truthValidator")
    # STALE ttl check
    assert "stale_after_ms" in vbody
    assert '"STALE"' in vbody
    assert "card-stale" in vbody
    # MISMATCH arithmetic check
    assert '"MISMATCH"' in vbody
    assert "card-mismatch" in vbody
    assert "_truthClose(" in vbody, (
        "mismatch path must use numeric-closeness helper, not string equality"
    )


def test_phase10final_governor_consumes_truth_issues(html: str) -> None:
    """Gap A+B — truthGovernor must read window._truthIssues and turn
    MISMATCH into accounting_mismatch, STALE into the stale state."""
    gbody = _extract_function_body(html, "truthGovernor")
    assert "window._truthIssues" in gbody
    assert 'i.kind === "MISMATCH"' in gbody
    assert 'i.kind === "STALE"' in gbody
    assert 'state = "accounting_mismatch"' in gbody
    assert 'state = "stale"' in gbody


def test_phase10final_every_governed_summary_registers_evidence(html: str) -> None:
    """Gap A — every governed summary updater must call
    registerTruthEvidence so the validator has body/summary pairs to
    reconcile."""
    expected_updaters = (
        "updateFunnelReport",
        "updateHeatmapReport",
        "updateTradesMix",
        "updateLLMQuorumReadout",
        "updateConnectivitySummary",
        "updatePerformanceSummary",
        "updateUniverseSummary",
        "updatePositionsSummary",
        "updateReconciliationCard",
        "updateInfraState",
        "updateQuadReport",
    )
    for fn in expected_updaters:
        body = _extract_function_body(html, fn)
        assert "registerTruthEvidence(" in body, (
            f"{fn} must call registerTruthEvidence() for Gap A coverage"
        )


def test_phase10final_stale_css_classes_defined(html: str) -> None:
    """Gap B — stale and mismatch visual markers must be declared."""
    for sel in (".c.card-stale", ".c.card-mismatch"):
        assert sel in html, f"missing Phase 10 final CSS selector: {sel}"


def test_phase10final_executive_read_is_evidence_derived(html: str) -> None:
    """Gap C — Executive Read must be rebuilt as pure evidence-derivation.
    Old narrative phrasing must be gone; new evidence-list render must
    be present."""
    body = _extract_function_body(html, "updateQuadReport")
    # New evidence-list container class is rendered
    assert "exec-evidence-list" in body
    # Must read from governor state, funnel payload, visible rows, and
    # reconciliation — i.e. all 5 evidence sources.
    for src in (
        "trust-governor",
        "_lastFunnel",
        "tb-trades",
        "tb-uni",
        "reconciliation",
    ):
        assert src in body, f"Executive Read missing evidence source {src!r}"
    # No prescriptive action-RENDER from the old narrative version.
    # (Ignore words appearing only in comments — grep the code portion by
    # stripping /* */ and // lines.)
    import re as _re
    code_only = body
    code_only = _re.sub(r"/\*[\s\S]*?\*/", "", code_only)
    code_only = "\n".join(
        line for line in code_only.splitlines()
        if not line.lstrip().startswith("//")
    )
    forbidden_old = (
        "Best zone:",
        "Tool: swarm sizing",
        "Counter: tighten",
        "Action: prioritize",
    )
    for tok in forbidden_old:
        assert tok not in code_only, (
            f"Executive Read still renders pre-Gap-C narrative phrase {tok!r}"
        )
    # The new output must be tied to the "evidence-derived · visible-data-only"
    # header.
    assert "evidence-derived" in body
    # And must register evidence for the validator.
    assert "registerTruthEvidence(" in body


def test_phase10final_system_issues_ingests_truth_findings(html: str) -> None:
    """Gap D — System Issues must merge watchdog rows with truth-coherence
    findings; 'no issues' text must only appear when BOTH are empty."""
    body = _extract_function_body(html, "updateIssuesReport")
    # Reads the truth-issue queue
    assert "window._truthIssues" in body
    # Classifies mismatch + stale kinds explicitly
    assert "MISMATCH" in body or "mismatch" in body
    assert "STALE" in body or "stale" in body
    # Empty state only fires when both are empty — wording locks this.
    assert "watchdog clean · truth coherent" in body
    # Truth-source rows are rendered into the body, not just summarized.
    assert "summary/body mismatch" in body
    assert "stale card" in body


def test_phase10final_executive_read_has_no_free_written_action(html: str) -> None:
    """Gap C (harder guard) — Executive Read must NOT invent a
    'prioritize X / monitor Y' action. Evidence is sufficient; the
    operator reads WRI or Forensic Governor for prescriptions."""
    body = _extract_function_body(html, "updateQuadReport")
    # No action: prioritize ... / monitor ... lines.
    assert ", monitor " not in body, (
        "Executive Read must not render a 'prioritize / monitor' action line"
    )
    # No 'Best zone' aspirational phrasing.
    assert "Best zone" not in body


def test_phase10_dashboard_uses_zero_apex_spot_aggro_urls(html: str) -> None:
    """Phase 10 — dashboard must reference the new spot-owned URLs only.
    Any leftover /spot_aggro/ops/spot_aggro/* string is a contamination regression."""
    assert "/spot_aggro/ops/spot_aggro/" not in html, (
        "dashboard still contains /spot_aggro/ops/spot_aggro/* references — "
        "Phase 10 relocation incomplete"
    )
    # Spot URLs are present (sanity check that the rewrite landed).
    for path in (
        "/spot_aggro/status",
        "/spot_aggro/funnel",
        "/spot_aggro/stats",
        "/spot_aggro/swarm",
        "/spot_aggro/forensic_v2/list",
        "/spot_aggro/tier_toggles",
    ):
        assert path in html, f"dashboard missing relocated spot URL {path!r}"


def test_phase10_dashboard_keeps_shared_apex_endpoints(html: str) -> None:
    """Per Q1 = A, the apex-neutral shared-ops endpoints stay at /spot_aggro/ops/*.
    The dashboard must still call them. This guards against an over-zealous
    future rewrite that strips /spot_aggro/ops/* entirely."""
    for shared in (
        "/spot_aggro/ops/pnl",
        "/spot_aggro/ops/governor",
        "/spot_aggro/ops/watchdog",
        "/spot_aggro/ops/llm/cost",
        "/spot_aggro/ops/llm/health",
        "/spot_aggro/ops/notifications",
        "/spot_aggro/ops/trades",
        "/spot_aggro/ops/consensus",
        "/spot_aggro/ops/kill",
    ):
        assert shared in html, (
            f"dashboard no longer references shared-ops endpoint {shared!r}"
        )


def test_governor_receives_kill_in_ctx(html: str) -> None:
    """Phase 9j also closes a small latent bug: the governor reads ctx.kill
    but Phase 9's call-site did not include kill. Ensure the ctx object
    carries kill now."""
    body = _extract_function_body(html, "refresh")
    assert "truthGovernor({ sa, st, pnl, gov, funnel, trades, wd, kill })" in body, (
        "truthGovernor invocation must include kill in the ctx"
    )


def test_tier_toggle_card_no_longer_references_apex_url(html: str) -> None:
    """Phase 9e regression: the tier-toggle card (truth-meta strip and
    JS fetches) must carry the spot-owned URL, not the /spot_aggro/ops/* variant."""
    idx = html.index('id="c-tier-toggles"')
    end = html.index("</div>\n\n<!-- 16. Automation Schedule", idx)
    block = html[idx:end]
    assert "/spot_aggro/ops/spot_aggro/tier_toggles" not in block, (
        "tier-toggle card still references /spot_aggro/ops/spot_aggro/tier_toggles"
    )
    assert "/spot_aggro/tier_toggles" in block, (
        "tier-toggle card must surface the spot-owned URL in its truth-meta"
    )


def test_postJSON_helper_present_and_sends_admin_header(html: str) -> None:
    body = _extract_function_body(html, "postJSON")
    # Phase 11c — the header now reads from the live getter, not a
    # page-load-snapshotted constant. This is the root fix for the
    # "invalid admin token" operator bug.
    assert '"X-Ops-Token": getOpsToken()' in body, (
        "postJSON must read the admin token LIVE (via getOpsToken()) "
        "so the dashboard picks up late-set tokens without a reload"
    )
    assert '"Content-Type": "application/json"' in body
    assert "JSON.stringify" in body


def test_tier_toggle_has_no_analytics_side_effect_markers(html: str) -> None:
    """Guard: nothing in the flip handler should touch analytics surfaces
    (funnel body, heatmap table, matrix). If a future change wires an
    analytics write into the toggle path, this test trips."""
    body = _extract_function_body(html, "flipTierToggle")
    forbidden = (
        "#tb-funnel", "funnel-body", "tb-heatmap", "tb-matrix",
        "heatmap-report", "funnel-report", "matrix-report",
        "coin_memory", "forensic_v2",
    )
    for tok in forbidden:
        assert tok not in body, (
            f"flipTierToggle touches analytics surface {tok!r}; "
            f"execution-only rule violated"
        )


def test_tier_toggle_css_classes_defined(html: str) -> None:
    """No inline styles — the card uses dedicated classes."""
    for sel in (
        ".tier-toggle-grid{",
        ".tier-toggle-row{",
        ".tier-toggle-state.on{",
        ".tier-toggle-state.off{",
        ".tier-toggle-note{",
        ".tier-toggle-status.ok{",
        ".tier-toggle-status.err{",
    ):
        assert sel in html, f"missing CSS selector {sel}"
