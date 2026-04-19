"""Phase 11n-9-r — mobile single-overlay state machine regression locks.

Operator screenshots showed two mobile popovers on screen at the same
time — the action More menu (top-right) and the nav More menu (bottom-
right) both visible, both blocking the Performance card. Root cause:
each menu had its own ad-hoc show/hide toggle and the outside-click
handler only closed act/nav, never the in-page mt-more drawer. Three
overlays, no state machine.

Fix: a single `_mtActiveOverlay` variable tracks which overlay is open
('actMenu' | 'navMenu' | 'mtMore' | null). Opening any overlay first
closes whatever is active. A shared backdrop at z:49 blocks taps behind
the menu (z:50) and body scroll is locked while an overlay is open.

Locks:
  State machine:
    1. `_mtActiveOverlay` variable declared.
    2. `_MT_OVERLAY_IDS` map binds 3 overlay kinds to their DOM ids.
    3. `_mtOpenOverlay(kind)` calls `_mtCloseOverlay()` before opening
       (strict single-overlay rule).
    4. `_mtCloseOverlay()` iterates all overlay ids and hides each.
    5. `_mtCloseOverlay()` clears body scroll-lock.
    6. `_mtOpenOverlay()` sets body overflow:hidden.

  Backdrop:
    7. `_mtBackdropEl()` creates a full-screen fixed element with
       background rgba(0,0,0,0.4) at z-index 49.
    8. Backdrop click invokes `_mtCloseOverlay`.

  z-index scale:
    9. Mobile menus use z-index:50 (not the old 200).
   10. Backdrop uses z-index:49 (one less than menu).

  Handlers:
   11. Public `_mtShowActMenu`/`_mtShowNavMenu`/`_mtShowMore` route
       through `_mtOpenOverlay`.
   12. ESC key closes any active overlay.
   13. Tap-outside closes the active overlay.

  Tap targets + spacing:
   14. Mobile menus declare min-height:44px and min-width:44px on
       their items.
   15. Header action buttons have min-height:44px inside the <=700px
       media block.
   16. Body has padding-bottom:72px on mobile so the bottom nav
       never covers content.

  Build tags:
   17. SERVER_BUILD == phase-11n-9-r-2026-04-20.
   18. dashboard-build meta == phase-11n-9-r-2026-04-20.
   19. /build features manifest advertises mobile_single_overlay: True.
"""
from __future__ import annotations

import re
from pathlib import Path


def _phase_key(suffix):
    """Phase ordering: length first, then lexicographic.
    'p' < 'q' < 'z' < 'aa' < 'ab'."""
    return (len(suffix), suffix)


REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def test_overlay_state_var_declared():
    assert "let _mtActiveOverlay = null" in HTML


def test_overlay_ids_map_exists():
    assert "_MT_OVERLAY_IDS" in HTML
    assert '"mobile-act-menu"' in HTML
    assert '"mobile-nav-menu"' in HTML
    assert '"mt-more"' in HTML


def test_open_closes_previous_first():
    # Strict single-overlay rule: _mtOpenOverlay must invoke
    # _mtCloseOverlay at the top of its body.
    m = re.search(
        r"function _mtOpenOverlay\(kind\)\s*\{([\s\S]{0,400})",
        HTML,
    )
    assert m, "_mtOpenOverlay not found"
    body = m.group(1)
    assert "_mtCloseOverlay()" in body, (
        "_mtOpenOverlay must call _mtCloseOverlay first — single-overlay rule"
    )


def test_close_hides_all_three_overlays():
    m = re.search(
        r"function _mtCloseOverlay\(\)\s*\{([\s\S]{0,500})\}",
        HTML,
    )
    assert m
    body = m.group(1)
    # It iterates the id map and hides each.
    assert "_MT_OVERLAY_IDS" in body
    assert 'style.display = "none"' in body


def test_open_and_close_manage_body_scroll_lock():
    # Opening: body.overflow = 'hidden'. Closing: body.overflow = '' (unset).
    assert 'document.body.style.overflow = "hidden"' in HTML
    assert 'document.body.style.overflow = ""' in HTML


# ---------------------------------------------------------------------------
# Backdrop
# ---------------------------------------------------------------------------

def test_backdrop_is_created_on_demand():
    assert "function _mtBackdropEl" in HTML
    assert '"_mt-backdrop"' in HTML
    assert "rgba(0,0,0,0.4)" in HTML


def test_backdrop_z_index_is_one_less_than_menu():
    # Menu at 50, backdrop at 49.
    assert "z-index:49" in HTML


def test_backdrop_click_closes_overlay():
    # The backdrop el must bind click -> _mtCloseOverlay.
    assert re.search(
        r'addEventListener\("click",\s*_mtCloseOverlay\)', HTML
    ), "backdrop must close overlay on click"


# ---------------------------------------------------------------------------
# z-index scale
# ---------------------------------------------------------------------------

def test_mobile_act_menu_uses_z50():
    # The rule lives inside the <=700px media block. Catch both 'z-index:50'
    # as a literal declaration.
    assert ".mobile-act-menu{position:fixed;top:56px;right:16px;z-index:50;" in HTML


def test_mobile_nav_menu_uses_z50():
    assert ".mobile-nav-menu{position:fixed;bottom:72px;right:16px;z-index:50;" in HTML


def test_old_z200_is_gone():
    # The pre-phase-r z-index:200 is replaced — neither overlay should
    # still declare it.
    assert "z-index:200" not in HTML


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def test_show_handlers_route_through_open_overlay():
    # Phase 11n-9-w: handlers toggle symmetrically — open on first tap,
    # close on second. The open path calls _mtOpenOverlay; the close
    # path calls _mtCloseOverlay. Both must be present per handler.
    for name, kind in [
        ("_mtShowActMenu", "actMenu"),
        ("_mtShowNavMenu", "navMenu"),
    ]:
        pattern = (rf'function {name}\(\)\s*\{{[\s\S]{{0,400}}?'
                   rf'_mtOpenOverlay\("{kind}"\)')
        assert re.search(pattern, HTML), (
            f"{name} must route through _mtOpenOverlay('{kind}')"
        )


def test_esc_key_closes_overlay():
    assert re.search(
        r'addEventListener\("keydown"[\s\S]{1,200}Escape[\s\S]{1,120}_mtCloseOverlay',
        HTML,
    ), "Esc key must close active overlay"


def test_tap_outside_closes_overlay():
    # The tap-outside handler is a 'click' listener that inspects
    # _mtActiveOverlay and calls _mtCloseOverlay.
    assert re.search(
        r'document\.addEventListener\("click"[\s\S]{1,600}_mtCloseOverlay',
        HTML,
    ), "tap-outside handler must call _mtCloseOverlay"


# ---------------------------------------------------------------------------
# Tap targets + spacing
# ---------------------------------------------------------------------------

def test_menu_items_have_44px_tap_target():
    # Every overlay menu item must carry min-height:44px AND min-width:44px.
    # Check inside the mobile media block for .mobile-act-menu .btn.
    assert re.search(
        r"\.mobile-act-menu \.btn\{[^}]*min-height:44px[^}]*min-width:44px",
        HTML,
    ), ".mobile-act-menu .btn must be 44x44 minimum"
    assert re.search(
        r"\.mobile-nav-menu a\{[^}]*min-height:44px[^}]*min-width:44px",
        HTML,
    ), ".mobile-nav-menu a must be 44x44 minimum"


def test_header_action_buttons_have_44px_floor_on_mobile():
    # Inside the <=700px block, header .acts > button.btn must declare
    # min-height:44px.
    m = re.search(r"@media\(max-width:700px\)\{([\s\S]+?)\n\}", HTML)
    assert m
    body = m.group(1)
    assert re.search(
        r"header \.acts > button\.btn\{[^}]*min-height:44px",
        body,
    ), "header action buttons must be 44px min on mobile"


def test_body_has_bottom_padding_on_mobile():
    m = re.search(r"@media\(max-width:700px\)\{([\s\S]+?)\n\}", HTML)
    assert m
    body = m.group(1)
    assert "body{padding-bottom:72px" in body, (
        "body must reserve 72px at bottom so bottom nav never covers content"
    )


# ---------------------------------------------------------------------------
# Build tags
# ---------------------------------------------------------------------------

def test_server_build_is_at_least_phase_r():
    from spot_aggro.api.routes import SERVER_BUILD
    import re
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and _phase_key(m.group(1)) >= _phase_key("r"), f"SERVER_BUILD must be >= phase-r (got {SERVER_BUILD})"


def test_dashboard_build_meta_is_at_least_phase_r():
    import re
    m = re.search(r'content="phase-11n-9-([a-z]+)-2026-04-20"', HTML)
    assert m and _phase_key(m.group(1)) >= _phase_key("r"), (
        f"build tag must be >= phase-r (got {m.group(0) if m else '—'})"
    )


def test_feature_manifest_advertises_single_overlay():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    assert body["features"]["mobile_single_overlay"] is True
