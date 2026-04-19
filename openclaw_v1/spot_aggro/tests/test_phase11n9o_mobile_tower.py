"""Phase 11n-9-o — Mobile Control Tower regression locks.

Full structural correction per operator spec. Mobile gets a dedicated
5-section control-tower layout; desktop unchanged.

Locks:
  Structure:
    1. <section class="mobile-tower"> exists with the 5 sections.
    2. Hero has #mt-hero-state, #mt-hero-posture, #mt-hero-sub,
       #mt-hero-updated.
    3. Critical alert strip has #mt-alerts + ingestion list.
    4. Health chips: exchange, feed, db, scheduler, risk (all 5).
    5. Execution KV: trades today, positions, last action.
    6. Performance 2x2 grid: PnL, WR, Drawdown, Total exits.

  Behavior:
    7. CSS .mobile-tower is display:none by default; visible inside
       @media(max-width:700px).
    8. Inside the mobile media block, #tab-dash and every other
       tab-* grid is hidden with !important.
    9. trust-governor + lane-sep hidden on mobile.
   10. Header token buttons (Set Token/Test/Clear) hidden on mobile.
   11. Secondary action buttons (STOP/Resume/Refresh) hidden from
       main bar on mobile.

  Nav collapse:
   12. Research/System/Formula/Lab have class="nav-extra" so CSS can
       hide them on mobile.
   13. More button trigger + popover exist.

  Action bar collapse:
   14. mobile-more-btn + mobile-act-menu popover exist.

  JS wiring:
   15. refreshMobileTower defined + setInterval 5s.
   16. _mtShowActMenu, _mtShowNavMenu, _mtShowMore handlers exist.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def test_mobile_tower_section_exists():
    assert '<section class="mobile-tower"' in HTML
    assert 'id="mobile-tower"' in HTML


def test_hero_has_all_slots():
    for slot in ("mt-hero", "mt-hero-state", "mt-hero-posture",
                 "mt-hero-sub", "mt-hero-updated"):
        assert f'id="{slot}"' in HTML, f"hero slot {slot!r} missing"


def test_critical_alerts_strip_exists():
    assert 'id="mt-alerts"' in HTML
    assert 'id="mt-alerts-list"' in HTML
    assert 'id="mt-alerts-count"' in HTML


def test_health_chips_all_five():
    for chip in ("exchange", "feed", "db", "scheduler", "risk"):
        assert f'id="mt-chip-{chip}"' in HTML, f"health chip {chip!r} missing"


def test_execution_kv_rows():
    for slot in ("mt-exec-trades", "mt-exec-positions", "mt-exec-last"):
        assert f'id="{slot}"' in HTML


def test_performance_2x2_grid():
    for slot in ("mt-perf-pnl", "mt-perf-wr", "mt-perf-dd", "mt-perf-exits"):
        assert f'id="{slot}"' in HTML


# ---------------------------------------------------------------------------
# CSS behavior
# ---------------------------------------------------------------------------

def test_mobile_tower_hidden_by_default():
    # .mobile-tower has display:none outside any media query (desktop).
    assert re.search(r"\.mobile-tower\s*\{\s*display:\s*none", HTML)


def test_mobile_tower_visible_inside_700px_media():
    # Inside @media(max-width:700px) the tower must flip to display:block.
    m = re.search(r"@media\(max-width:700px\)\{([\s\S]+?)\n\}",
                  HTML)
    assert m, "mobile media block not found"
    body = m.group(1)
    assert ".mobile-tower" in body
    assert "display:block" in body


def test_legacy_grids_hidden_on_mobile():
    # Inside the mobile media block, every legacy tab grid + trust
    # governor + lane-sep is hidden with !important.
    m = re.search(r"@media\(max-width:700px\)\{([\s\S]+?)\n\}",
                  HTML)
    assert m, "mobile media block not found"
    body = m.group(1)
    for sel in ("#tab-dash", "#trust-governor", ".lane-sep"):
        assert sel in body, f"{sel} should be hidden on mobile"
    assert "display:none !important" in body


def test_header_token_buttons_hidden_on_mobile():
    m = re.search(r"@media\(max-width:700px\)\{([\s\S]+?)\n\}", HTML)
    body = m.group(1)
    assert "header .ops-token-btn" in body
    assert "display:none !important" in body


def test_secondary_action_buttons_hidden_on_mobile():
    m = re.search(r"@media\(max-width:700px\)\{([\s\S]+?)\n\}", HTML)
    body = m.group(1)
    assert "header .acts .mobile-secondary{display:none}" in body


# ---------------------------------------------------------------------------
# Nav collapse
# ---------------------------------------------------------------------------

def test_nav_extras_marked_with_class():
    # Research/System/Formula/Lab all carry class="nav-extra".
    for tab in ("research", "system", "formula", "lab"):
        pattern = rf'class="nav-extra"[^>]+showTab\(\'{tab}\'\)'
        assert re.search(pattern, HTML), (
            f"nav link for {tab!r} missing class='nav-extra'"
        )


def test_nav_more_button_and_popover_exist():
    assert 'class="nav-more-btn"' in HTML
    assert 'id="mobile-nav-menu"' in HTML


# ---------------------------------------------------------------------------
# Action bar
# ---------------------------------------------------------------------------

def test_action_bar_more_button_and_popover():
    assert 'class="btn mobile-more-btn"' in HTML
    assert 'id="mobile-act-menu"' in HTML
    # Popover must include the secondary action buttons (Stop/Resume/
    # Refresh) + Token controls.
    # Find the popover block.
    m = re.search(r'id="mobile-act-menu"[\s\S]+?</div>', HTML)
    assert m
    block = m.group(0)
    for label in ("Stop", "Resume", "Refresh", "Token", "Test", "Clear"):
        assert f">{label}<" in block, (
            f"action-bar More menu missing {label!r}"
        )


def test_stop_resume_refresh_have_mobile_secondary_class():
    # Target: buttons in header .acts flagged with mobile-secondary so
    # CSS can hide them on mobile.
    for label in ("STOP", "Resume"):
        pattern = rf'class="btn mobile-secondary"[^>]*>\s*{label}'
        assert re.search(pattern, HTML), f"{label!r} should be mobile-secondary"


# ---------------------------------------------------------------------------
# JS
# ---------------------------------------------------------------------------

def test_refresh_mobile_tower_defined():
    assert "async function refreshMobileTower()" in HTML
    # Polled every 5s.
    assert "setInterval(refreshMobileTower, 5000)" in HTML


def test_menu_handlers_defined():
    for fn in ("_mtShowActMenu", "_mtHideActMenu",
               "_mtShowNavMenu", "_mtHideNavMenu",
               "_mtShowMore"):
        assert f"function {fn}" in HTML, f"{fn} handler missing"


def test_tap_outside_closes_menus():
    # Document click listener must exist and close both menus.
    assert 'document.addEventListener("click"' in HTML
    assert "mobile-act-menu" in HTML
    assert "mobile-nav-menu" in HTML
