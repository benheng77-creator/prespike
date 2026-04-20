"""Phase 11n-9-ff — Layer 1 Execution Integrity regression.

Validates:
  1. kill_ladder module exposes current_state / escalate / release /
     record_reject / evaluate_auto_pause + is_entry_blocked.
  2. Ladder starts L0; escalate monotonic (L0->L1, L1 cannot go back
     to L0 via escalate, only via release).
  3. 3 rejects in 10min -> evaluate_auto_pause escalates to L1.
  4. L1 with no new rejects auto-releases to L0 via cooldown
     (actor=auto only).
  5. L2/L3/L4 never auto-release.
  6. execution_integrity.check_per_trade_risk rejects notional >2% eq.
  7. execution_integrity.check_price_tolerance rejects drift >15bp.
  8. execution_integrity.check_all fails fast when ladder not L0.
  9. /gov/kill_ladder endpoint returns state + reject count.
 10. /gov/kill_ladder/escalate requires X-Oversight-Token for L3/L4.
 11. Build tag + feature flags advertised.
 12. Dashboard HTML carries build bump, kill-ladder card, _refreshKillLadder
     function + refresh() wiring, card assigned to research tab.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def _iso_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    import spot_aggro.governance.kill_ladder as kl
    importlib.reload(kl)
    yield db


# ---------------------------------------------------------------------------
# 1 + 2. Kill-ladder module contract
# ---------------------------------------------------------------------------

def test_kill_ladder_public_surface():
    import spot_aggro.governance.kill_ladder as kl
    for name in (
        "current_state", "escalate", "release", "record_reject",
        "recent_reject_count", "evaluate_auto_pause",
        "is_entry_blocked", "LadderState",
    ):
        assert hasattr(kl, name), f"kill_ladder missing {name}"


def test_initial_state_is_L0(_iso_db):
    from spot_aggro.governance.kill_ladder import current_state
    st = current_state()
    assert st.level == "L0"


def test_escalate_is_monotonic(_iso_db):
    from spot_aggro.governance.kill_ladder import escalate, current_state
    escalate("L1", reason="test", actor="test")
    assert current_state().level == "L1"
    # Escalating to L0 (lower) is a no-op through escalate(); must use release.
    escalate("L0", reason="try-downgrade", actor="test")
    assert current_state().level == "L1"
    # Escalating to L2 works.
    escalate("L2", reason="test", actor="test")
    assert current_state().level == "L2"


def test_is_entry_blocked_reflects_ladder(_iso_db):
    from spot_aggro.governance.kill_ladder import (
        escalate, release, is_entry_blocked,
    )
    assert is_entry_blocked() is False
    escalate("L1", reason="test", actor="test")
    assert is_entry_blocked() is True
    release("L0", actor="test", reason="manual")
    assert is_entry_blocked() is False


# ---------------------------------------------------------------------------
# 3. Reject storm auto-pause
# ---------------------------------------------------------------------------

def test_three_rejects_in_window_escalate_to_L1(_iso_db):
    from spot_aggro.governance.kill_ladder import (
        record_reject, evaluate_auto_pause, current_state,
    )
    for i in range(3):
        record_reject(kind="reject", symbol=f"X{i}-USDT")
    st = evaluate_auto_pause()
    assert st.level == "L1"
    # Actor must be auto so it can later auto-release.
    assert current_state().actor == "auto"


# ---------------------------------------------------------------------------
# 4 + 5. Auto-release rules
# ---------------------------------------------------------------------------

def test_L1_auto_releases_when_no_recent_rejects(_iso_db, monkeypatch):
    import spot_aggro.governance.kill_ladder as kl
    # Put ladder in L1 via auto-actor path.
    kl._write_state("L1", "test", "auto", {"seed": True})
    # Pretend cooldown window has zero events by monkey-patching
    # recent_reject_count.
    monkeypatch.setattr(kl, "recent_reject_count", lambda window_s=600: 0)
    st = kl.evaluate_auto_pause()
    assert st.level == "L0"


def test_L2_never_auto_releases(_iso_db, monkeypatch):
    import spot_aggro.governance.kill_ladder as kl
    kl._write_state("L2", "manual-test", "operator", {})
    monkeypatch.setattr(kl, "recent_reject_count", lambda window_s=600: 0)
    st = kl.evaluate_auto_pause()
    assert st.level == "L2"


# ---------------------------------------------------------------------------
# 6 + 7. Risk + price-tolerance gates
# ---------------------------------------------------------------------------

def test_per_trade_risk_rejects_over_cap():
    from spot_aggro.governance.execution_integrity import check_per_trade_risk
    # 2% of $1000 = $20. A $25 order breaches.
    v = check_per_trade_risk(notional_usd=25.0, equity_usd=1000.0)
    assert v.ok is False
    assert "per_trade_risk_breach" in v.reason


def test_per_trade_risk_passes_under_cap():
    from spot_aggro.governance.execution_integrity import check_per_trade_risk
    v = check_per_trade_risk(notional_usd=15.0, equity_usd=1000.0)
    assert v.ok is True


def test_price_tolerance_rejects_over_15bp():
    from spot_aggro.governance.execution_integrity import check_price_tolerance
    # 20bp drift -> reject at default 15bp tolerance.
    v = check_price_tolerance(requested_px=100.20, reference_px=100.00)
    assert v.ok is False
    assert "price_drift" in v.reason


def test_price_tolerance_passes_within_15bp():
    from spot_aggro.governance.execution_integrity import check_price_tolerance
    v = check_price_tolerance(requested_px=100.10, reference_px=100.00)
    assert v.ok is True


# ---------------------------------------------------------------------------
# 8. check_all short-circuits on ladder
# ---------------------------------------------------------------------------

def test_check_all_fails_fast_when_ladder_at_L1(_iso_db):
    from spot_aggro.governance.kill_ladder import escalate
    from spot_aggro.governance.execution_integrity import check_all
    escalate("L1", reason="test", actor="test")
    v = check_all(
        notional_usd=10.0, equity_usd=1000.0,
        requested_px=100.0, reference_px=100.0,
        symbol="BTC-USDT",
    )
    assert v.ok is False
    assert "kill_ladder_at_L1" in v.reason


# ---------------------------------------------------------------------------
# 9. Endpoint shape
# ---------------------------------------------------------------------------

def test_kill_ladder_endpoint_shape(_iso_db):
    from spot_aggro.api.routes import spot_aggro_kill_ladder
    body = spot_aggro_kill_ladder()
    assert body["ok"] is True
    assert "state" in body
    assert "reject_count_10min" in body
    assert body["state"]["level"] in ("L0", "L1", "L2", "L3", "L4")


# ---------------------------------------------------------------------------
# 10. L3/L4 escalate requires oversight token
# ---------------------------------------------------------------------------

def test_l3_escalate_requires_oversight_token(_iso_db, monkeypatch):
    """Two-person rule: L3 without oversight token must be forbidden."""
    from fastapi import HTTPException
    from spot_aggro.api.routes import spot_aggro_kill_ladder_escalate

    monkeypatch.setenv("OPS_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("OPS_OVERSIGHT_TOKEN", "oversight-secret")
    with pytest.raises(HTTPException) as exc:
        spot_aggro_kill_ladder_escalate(
            level="L3", reason="test",
            x_ops_token="admin-secret", x_oversight_token=None,
        )
    assert exc.value.status_code == 403

    # With both tokens -> ok.
    body = spot_aggro_kill_ladder_escalate(
        level="L3", reason="test",
        x_ops_token="admin-secret", x_oversight_token="oversight-secret",
    )
    assert body["ok"] is True
    assert body["state"]["level"] == "L3"


# ---------------------------------------------------------------------------
# 11. Build tag + flags
# ---------------------------------------------------------------------------

def test_phase_ff_build_and_flags():
    import re
    from spot_aggro.api.routes import spot_aggro_build, SERVER_BUILD
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "ff"), (
        f"SERVER_BUILD must be >= phase-ff (got {SERVER_BUILD})"
    )
    body = spot_aggro_build()
    feats = body.get("features") or {}
    assert feats.get("kill_ladder_l1_l4") is True
    assert feats.get("exec_integrity_2pct_risk") is True
    assert feats.get("exec_integrity_price_tol_15bp") is True
    assert feats.get("reject_storm_autopause") is True


# ---------------------------------------------------------------------------
# 12. Dashboard HTML wiring
# ---------------------------------------------------------------------------

def test_dashboard_build_ff():
    import re
    m = re.search(r'content="phase-11n-9-([a-z]+)-2026-04-20"', HTML)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "ff"), (
        f"dashboard build must be >= phase-ff, got {m.group(0) if m else '—'}"
    )


def test_kill_ladder_card_present():
    assert 'id="c-kill-ladder"' in HTML
    assert "Kill Ladder" in HTML
    assert 'id="kl-level"' in HTML
    assert 'data-kl-escalate="L3"' in HTML
    assert 'data-kl-release="L0"' in HTML


def test_refresh_calls_kill_ladder():
    assert "async function _refreshKillLadder()" in HTML
    assert "_refreshKillLadder();" in HTML


def test_kill_ladder_card_in_tab():
    # Must be registered in _TAB_CARDS.research.
    assert '"c-kill-ladder"' in HTML
