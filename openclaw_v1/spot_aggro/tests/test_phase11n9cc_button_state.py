"""Phase 11n-9-cc — action-button state machine regression locks.

Validates the dashboard HTML carries:
  - Stable ids for all 6 action buttons (btn-start, btn-stop,
    btn-resume, btn-pause, btn-halt, btn-refresh).
  - Each button has the `.action-btn` class + data-kind.
  - _applyActionButtonStates() defined + called from refresh().
  - State table covers 5 states (UNKNOWN/STOPPED/RUNNING/PAUSED/HALTED).
  - Capture-phase click guard blocks .action-disabled clicks.
  - CSS classes .action-allowed / .action-suggested / .action-disabled
    + keyframes action-pulse all present.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


def test_all_six_action_buttons_have_stable_ids():
    for bid in ("btn-start", "btn-stop", "btn-resume",
                "btn-pause", "btn-halt", "btn-refresh"):
        assert f'id="{bid}"' in HTML, f"missing id={bid}"


def test_buttons_carry_action_btn_class_and_data_kind():
    for kind in ("start", "stop", "resume", "pause", "halt", "refresh"):
        assert f'data-kind="{kind}"' in HTML, f"missing data-kind={kind}"
    # action-btn class applied to all six
    assert HTML.count("action-btn") >= 6


def test_state_machine_function_defined():
    assert "function _applyActionButtonStates(sa, kill)" in HTML


def test_state_machine_invoked_from_refresh():
    assert "_applyActionButtonStates(sa, kill)" in HTML


def test_state_table_covers_all_five_states():
    for state in ("UNKNOWN", "STOPPED", "RUNNING", "PAUSED", "HALTED"):
        assert f"{state}:" in HTML, f"state {state} missing from table"


def test_capture_phase_click_guard_present():
    assert 'btn.classList.contains("action-disabled")' in HTML
    assert 'e.preventDefault();' in HTML
    assert 'e.stopImmediatePropagation();' in HTML


def test_css_classes_defined():
    for cls in (".action-btn", ".action-allowed",
                ".action-suggested", ".action-disabled",
                "@keyframes action-pulse"):
        assert cls in HTML, f"CSS {cls} missing"


def test_posture_hint_span_present():
    assert 'id="engine-posture-hint"' in HTML
    assert 'id="eph-state"' in HTML
    assert 'id="eph-next"' in HTML


def test_build_tag_bumped():
    assert 'content="phase-11n-9-cc-2026-04-20"' in HTML


def test_feature_flag_advertised():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    assert body["features"]["action_button_state_machine"] is True
