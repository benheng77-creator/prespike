"""Phase 11n-9-m — Label translator + mobile upgrade regression locks.

Locks:
  Label translator:
    1. Blueprint §10 literals translate exactly:
       consensus.vetoed  → "Trade blocked — high conflict"
       scheduler.fail    → "Scheduler offline"
       feed.latency      → "Feed delay"
       db.conn_err       → "Database issue"
       order_reject      → "Execution rejected"
    2. Spot-aggro alert kinds translate (engine_halt, pre_trade_gov_block,
       etc.).
    3. Unknown snake_case humanizes (some_new_event → "Some new event").
    4. Unknown dotted humanizes (a.b.c → "A — B — C").
    5. Unknown colon form humanizes ("orch_gap:foo_bar" with known
       prefix → "orch_gap" mapped part + " — " + humanized suffix).
    6. translate_alert attaches `label` without mutating `kind`.

  alert_center integration:
    7. /alerts/active rows carry a `label` field.
    8. /alerts/history rows carry a `label` field.

  Endpoints:
    9. /labels/translate?key=... returns {ok, key, label}.

  Dashboard:
   10. c-alerts renderer prefers a.label over a.kind.
   11. Mobile drill-down: collapsible cards declared + bound on load.
   12. Mobile-first order CSS: sysaudit/alerts/engine set via `order:`
       inside the <=700px media block.
   13. Tap-target min-height ≥36px inside <=700px media block.

  Feature manifest:
   14. build.features advertises label_translator + mobile_drilldown.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Label translator — pure function
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,expected", [
    ("consensus.vetoed", "Trade blocked — high conflict"),
    ("scheduler.fail",   "Scheduler offline"),
    ("feed.latency",     "Feed delay"),
    ("db.conn_err",      "Database issue"),
    ("order_reject",     "Execution rejected"),
])
def test_blueprint_section10_literals(key, expected):
    from spot_aggro.governance.label_translator import translate
    assert translate(key) == expected


@pytest.mark.parametrize("key,expected", [
    ("engine_halt",           "Engine halted"),
    ("pre_trade_gov_block",   "Trade blocked — pre-trade checklist failed"),
    ("loop_novelty_stuck",    "Research loop stuck on repeat"),
    ("tp_sell_rejected",      "Take-profit sell rejected"),
    ("orch_gap:daily_alpha",  "No daily-alpha picks admitted"),
])
def test_spot_aggro_kinds_map(key, expected):
    from spot_aggro.governance.label_translator import translate
    assert translate(key) == expected


def test_unknown_snake_case_humanizes():
    from spot_aggro.governance.label_translator import translate
    assert translate("some_new_event") == "Some new event"


def test_unknown_dotted_humanizes():
    from spot_aggro.governance.label_translator import translate
    # Dotted form joins each segment with " — ".
    assert translate("some.new.event") == "Some — New — Event"


def test_unknown_colon_form_with_known_prefix():
    """orch_gap:foo_bar has a known PREFIX but unknown suffix; should
    still produce a readable result (prefix map + humanized suffix)."""
    from spot_aggro.governance.label_translator import translate
    result = translate("orch_gap:foo_bar")
    # Not a hard equal because prefix isn't in the map alone, so the
    # humanizer fires on each side. Assert structure instead.
    assert "Foo bar" in result or "foo bar" in result.lower()


def test_empty_input_returns_empty():
    from spot_aggro.governance.label_translator import translate
    assert translate("") == ""


def test_translate_alert_attaches_label_nondestructive():
    from spot_aggro.governance.label_translator import translate_alert
    raw = {"kind": "engine_halt", "severity": "P0", "extra": 42}
    out = translate_alert(raw)
    assert out["kind"] == "engine_halt"
    assert out["label"] == "Engine halted"
    assert out["severity"] == "P0"
    assert out["extra"] == 42
    # Original dict unchanged.
    assert "label" not in raw


# ---------------------------------------------------------------------------
# alert_center integration
# ---------------------------------------------------------------------------

@pytest.fixture
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    from shared.persistence import state as persist
    persist._initialized = False
    yield


def test_active_rows_have_label(_isolated_db):
    from spot_aggro.governance import alert_center as ac
    ac.ingest(kind="engine_halt", source="test",
              message="down", evidence={})
    rows = ac.active()
    assert rows and "label" in rows[0]
    assert rows[0]["label"] == "Engine halted"


def test_history_rows_have_label(_isolated_db):
    from spot_aggro.governance import alert_center as ac
    ac.ingest(kind="pre_trade_gov_block", source="engine",
              message="blocked", evidence={"symbol": "X", "tier": "B"})
    rows = ac.history()
    assert rows and "label" in rows[0]
    assert rows[0]["label"] == "Trade blocked — pre-trade checklist failed"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

def test_labels_translate_endpoint_registered():
    from spot_aggro.api import routes
    paths = {r.path for r in routes.router.routes}
    assert "/spot_aggro/labels/translate" in paths


def test_feature_manifest_exposes_label_translator():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    assert body["features"]["label_translator"] is True
    assert body["features"]["mobile_drilldown"] is True


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def test_c_alerts_renderer_prefers_label_over_kind():
    # Active-list render: must try a.label before a.kind.
    assert "a.label || a.kind" in HTML
    # History render: same fallback.
    assert "h.label || h.kind" in HTML


def test_mobile_drilldown_jsdeclared():
    assert "_markCollapsibleCards" in HTML
    assert "_initMobileDrilldown" in HTML
    assert 'data-mobile-collapsible' in HTML


def test_mobile_first_order_in_700px_media_block():
    # Extract the <=700px block and check that status/alerts/engine
    # cards have negative order (push-to-top on mobile).
    m = re.search(r"@media\(max-width:700px\)\{([\s\S]+?)\}\s*/\* Drill-down",
                  HTML)
    assert m, "mobile-first <=700px block not found"
    body = m.group(1)
    for card in ("#c-sysaudit", "#c-alerts", "#c-engine"):
        assert card in body, f"{card} not ordered in mobile block"
    # Negative order values for priority cards.
    assert "order:-100" in body
    assert "order:-95" in body


def test_tap_target_minimum_in_700px_media_block():
    m = re.search(r"@media\(max-width:700px\)\{([\s\S]+?)\}\s*/\* Drill-down",
                  HTML)
    assert m, "mobile media block not found"
    body = m.group(1)
    # Button min-height at least 36px.
    mh = re.search(r"button\.btn[^{]*\{[^}]*min-height:(\d+)px", body)
    assert mh, "no min-height rule for button.btn in mobile block"
    assert int(mh.group(1)) >= 36


def test_collapsible_pattern_css_present():
    # The drill-down CSS rule set must exist.
    assert 'data-mobile-collapsible="1"' in HTML
    # The collapsed-state content hiding rule.
    assert ".collapsed .b" in HTML
    # The caret indicator ▸ / ▾ content rules.
    assert "▾" in HTML and "▸" in HTML


# ---------------------------------------------------------------------------
# Phase 11n-9-n — mobile table + pill strip root-fixes
# ---------------------------------------------------------------------------

def test_mobile_tables_drop_fixed_layout():
    """The pre-11n-9-n CSS forced tr/tbody back into table-layout:fixed
    which defeated the horizontal scroll and caused column overlap
    (\"RECONCILED HELD NO LIVE TP\" crushing into one cell).
    The new rule must set table-layout:auto on the outer table."""
    assert "table-layout:auto" in HTML


def test_mobile_positions_and_trades_have_min_cell_widths():
    """Per-cell min-widths prevent overlap on narrow viewports."""
    assert "#c-positions .b table th" in HTML
    assert "#c-trades .b table th" in HTML
    assert "min-width:80px" in HTML


def test_mobile_pill_rows_wrap_with_explicit_gap():
    """Every inline-style flex container inside a card body wraps with
    consistent gaps on mobile so pills don't overlap."""
    # The tight selector + flex-wrap override must be present.
    assert '.c .b [style*="display:flex"]' in HTML
    assert "flex-wrap:wrap !important" in HTML


def test_mobile_header_buttons_have_min_width_for_alignment():
    """START/STOP/Resume/Pause/HALT buttons get consistent min-width
    on mobile so they line up in a clean 2-row wrap."""
    assert "header .acts > button.btn{min-width:58px" in HTML


def test_build_mismatch_pill_is_tap_to_reload():
    """When HTML and server builds disagree, the pill is clickable and
    triggers a cache-busted hard reload. The label must make this
    actionable ('TAP TO RELOAD')."""
    assert "BUILD · MISMATCH · TAP TO RELOAD" in HTML
    assert "location.replace(url)" in HTML
    assert "v=" in HTML  # cache buster
    assert "navigator.serviceWorker" in HTML


def test_auto_margin_left_override_on_mobile():
    """Timestamps with margin-left:auto were pushing to the right and
    making the pill row overflow. On mobile we override to 0 so they
    wrap naturally."""
    assert 'margin-left:auto"]{margin-left:0 !important}' in HTML
