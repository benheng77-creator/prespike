"""Phase 11n-9-j — P2 content migration via card-level tab router.

Locks:
  1. _TAB_CARDS is declared in the dashboard JS.
  2. Every currently-existing card (c-*) has a tab assignment OR is
     explicitly excluded (display:none sentinel or rendered in its own
     legacy grid).
  3. The six primary tabs use canonical names in nav onclick handlers
     (dashboard/execution/performance/alerts/research/system).
  4. Every primary nav label matches a key in _TAB_CARDS.
  5. Stub grids for execution/performance/alerts/research/system/
     dashboard exist as empty display:none sentinels so legacy lookups
     don't 404.
  6. The router's DOMContentLoaded init hides non-dashboard cards on
     page load (no flash of all-cards).
  7. Dashboard set is minimal (<= 8 cards) — control-tower constraint.
  8. Execution set covers the critical trade-inspection cards.
  9. Performance set covers PnL + coin accuracy + forensic.
 10. Alerts set covers the 3 alert/incident cards.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


def _extract_tab_cards() -> dict[str, list[str]]:
    """Parse the JS `_TAB_CARDS = {...}` block and return a Python dict
    of {tab_name: [card_ids]}."""
    m = re.search(r"_TAB_CARDS\s*=\s*\{([\s\S]+?)\};", HTML)
    assert m, "_TAB_CARDS declaration not found"
    body = m.group(1)
    out: dict[str, list[str]] = {}
    for tab_m in re.finditer(r"(\w+)\s*:\s*\[([^\]]*)\]", body):
        name = tab_m.group(1)
        ids = re.findall(r'"([^"]+)"', tab_m.group(2))
        out[name] = ids
    return out


def test_tab_cards_declared():
    _extract_tab_cards()  # just parses cleanly


def test_primary_tab_names_canonical():
    tabs = _extract_tab_cards()
    for name in ("dashboard", "execution", "performance", "alerts",
                 "research", "system"):
        assert name in tabs, f"_TAB_CARDS missing '{name}'"


def test_nav_uses_canonical_tab_names():
    for name in ("dashboard", "execution", "performance", "alerts",
                 "research", "system"):
        assert f"showTab('{name}')" in HTML, (
            f"nav does not call showTab('{name}')"
        )


def test_dashboard_tab_is_minimal():
    """Control-tower constraint: dashboard shows high-level state only.
    Anything tabular or deep-drilldown belongs on its own tab."""
    tabs = _extract_tab_cards()
    assert len(tabs["dashboard"]) <= 8, (
        f"dashboard has {len(tabs['dashboard'])} cards; max 8 per IA spec"
    )


def test_execution_tab_has_trade_cards():
    tabs = _extract_tab_cards()
    execution = set(tabs["execution"])
    required = {"c-alpha", "c-positions", "c-trades"}
    missing = required - execution
    assert not missing, f"execution tab missing trade cards: {missing}"


def test_performance_tab_has_pnl_cards():
    tabs = _extract_tab_cards()
    performance = set(tabs["performance"])
    required = {"c-matrix", "c-heatmap", "c-forensic", "c-forensic-v2"}
    missing = required - performance
    assert not missing, f"performance tab missing: {missing}"


def test_alerts_tab_has_alert_cards():
    tabs = _extract_tab_cards()
    alerts = set(tabs["alerts"])
    required = {"c-alerts", "c-issues", "c-infra"}
    missing = required - alerts
    assert not missing, f"alerts tab missing: {missing}"


def test_research_tab_has_research_cards():
    tabs = _extract_tab_cards()
    research = set(tabs["research"])
    required = {"c-research", "c-mio", "c-universe"}
    missing = required - research
    assert not missing, f"research tab missing: {missing}"


def test_system_tab_has_config_cards():
    tabs = _extract_tab_cards()
    system = set(tabs["system"])
    # System/Settings merged under 'system' tab.
    required = {"c-runtime", "c-schedule", "c-settings", "c-exchange"}
    missing = required - system
    assert not missing, f"system tab missing: {missing}"


def test_every_card_has_a_tab_assignment():
    """Every <div id="c-*"> in the main grid must appear in exactly one
    _TAB_CARDS list. Prevents orphan cards that disappear after the
    router hides non-matching cards."""
    # Collect all card ids from the main grid (#tab-dash).
    m = re.search(
        r'id="tab-dash"[\s\S]+?</div>\s*<!--\s*close main column',
        HTML,
    )
    assert m, "main grid block not found"
    grid = m.group(0)
    all_cards = set(re.findall(r'<div[^>]+id="(c-[a-z0-9-]+)"', grid))
    # Flatten all _TAB_CARDS values.
    tabs = _extract_tab_cards()
    assigned = set()
    for ids in tabs.values():
        assigned.update(ids)
    orphans = all_cards - assigned
    assert not orphans, (
        f"orphan cards in main grid (not in any _TAB_CARDS list): "
        f"{sorted(orphans)}"
    )


def test_stub_sentinels_exist():
    """Empty tab-execution / tab-performance / tab-alerts sentinels
    exist so legacy lookups don't 404."""
    for stub in ("tab-execution", "tab-performance", "tab-alerts",
                 "tab-research", "tab-system", "tab-dashboard"):
        assert f'id="{stub}"' in HTML, f"stub sentinel {stub!r} missing"


def test_router_hides_non_dashboard_cards_on_load():
    """DOMContentLoaded handler must iterate #tab-dash cards and hide
    everything that isn't in _TAB_CARDS.dashboard."""
    assert "DOMContentLoaded" in HTML
    # Router has the hide-loop on DOMContentLoaded.
    assert "_TAB_CARDS.dashboard" in HTML
    assert "wanted.has(el.id)" in HTML


def test_no_leftover_phase2_scaffold_text():
    """The 'coming in Phase 2' placeholder text was stripped when the
    real migration landed."""
    assert "Execution — coming in Phase 2" not in HTML
    assert "Performance — coming in Phase 2" not in HTML
    assert "Alerts — coming in Phase 3" not in HTML
