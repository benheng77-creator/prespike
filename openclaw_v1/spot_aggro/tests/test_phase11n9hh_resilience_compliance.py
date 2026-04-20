"""Phase 11n-9-hh — Layer 3 Resilience & Compliance regression.

Validates:
  1. immutable_ledger exposes append / verify_chain / head_hash /
     export_range with fail-open semantics.
  2. append() writes a row with SHA-256 row_hash and correct prev_hash
     linkage to the prior row. Genesis row uses "GENESIS" as prev.
  3. verify_chain returns ok for an untouched chain.
  4. Mutating a row (e.g. changing pnl_usd after the fact) causes
     verify_chain to report broken with first_broken_row == that id.
  5. resilience.canary_health returns 5 probes with per-check status.
  6. recovery_playbook returns ok=True + counts (even on empty DB).
  7. export_aml_audit returns chain_verdict + ledger_rows.
  8. /gov/canary_health endpoint shape.
  9. /gov/immutable_ledger/verify endpoint shape.
 10. /gov/aml_audit_export requires admin token.
 11. Build tag + flags.
 12. Dashboard HTML has c-resilience card + _refreshResilience +
     chain-verify button + AML export button.
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
    import spot_aggro.governance.immutable_ledger as il
    import spot_aggro.governance.resilience as res
    importlib.reload(il); importlib.reload(res)
    yield db


# ---------------------------------------------------------------------------
# 1 + 2. Ledger contract + linkage
# ---------------------------------------------------------------------------

def test_immutable_ledger_public_surface():
    import spot_aggro.governance.immutable_ledger as il
    for name in (
        "append", "verify_chain", "head_hash", "export_range",
        "ChainVerdict", "GENESIS_HASH",
    ):
        assert hasattr(il, name), f"immutable_ledger missing {name}"


def test_append_links_to_prev_row(_iso_db):
    from spot_aggro.governance.immutable_ledger import (
        append, head_hash, GENESIS_HASH, _connect,
    )
    assert head_hash() == GENESIS_HASH
    a = append("entry", symbol="X-USDT", notional_usd=10.0,
               correlation_id="c1")
    h1 = head_hash()
    b = append("exit", symbol="X-USDT", pnl_usd=0.5,
               correlation_id="c1")
    con = _connect()
    try:
        rows = con.execute(
            "SELECT row_id, prev_hash, row_hash"
            " FROM spot_immutable_ledger ORDER BY row_id ASC"
        ).fetchall()
    finally:
        con.close()
    assert rows[0]["prev_hash"] == GENESIS_HASH
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]
    assert rows[0]["row_hash"] == h1


# ---------------------------------------------------------------------------
# 3 + 4. Verify chain on untouched + tampered ledgers
# ---------------------------------------------------------------------------

def test_verify_chain_ok_on_untouched(_iso_db):
    from spot_aggro.governance.immutable_ledger import (
        append, verify_chain,
    )
    for i in range(5):
        append("entry", symbol=f"X{i}-USDT", notional_usd=10.0,
               correlation_id=f"c-{i}")
    v = verify_chain()
    assert v.ok is True
    assert v.total_rows == 5
    assert v.first_broken_row is None


def test_verify_chain_detects_tampering(_iso_db):
    from spot_aggro.governance.immutable_ledger import (
        append, verify_chain, _connect,
    )
    for i in range(3):
        append("entry", symbol=f"X{i}-USDT", notional_usd=10.0,
               correlation_id=f"c-{i}")
    # Tamper row 2's pnl_usd post-hoc.
    con = _connect()
    try:
        con.execute(
            "UPDATE spot_immutable_ledger SET pnl_usd = 999.99"
            " WHERE row_id = 2"
        )
    finally:
        con.close()
    v = verify_chain()
    assert v.ok is False
    assert v.first_broken_row == 2
    assert "row_hash recompute mismatch" in v.reason


# ---------------------------------------------------------------------------
# 5. Canary
# ---------------------------------------------------------------------------

def test_canary_returns_five_probes(_iso_db):
    from spot_aggro.governance.resilience import canary_health
    body = canary_health()
    # 5 probes: db, kill_ladder, heartbeat, freeze, model_registry.
    assert len(body["checks"]) == 5
    names = {c["name"] for c in body["checks"]}
    assert names == {
        "db_reachable", "kill_ladder_L0", "heartbeat_fresh",
        "freeze_off", "model_registry_populated",
    }


# ---------------------------------------------------------------------------
# 6. Recovery playbook
# ---------------------------------------------------------------------------

def test_recovery_playbook_shape(_iso_db):
    from spot_aggro.governance.resilience import recovery_playbook
    # Need at least the shadow tables to exist; import three_way_shadow
    # and init.
    import spot_aggro.governance.three_way_shadow as tw
    tw._init_schema()
    body = recovery_playbook(window_min=60)
    assert body["ok"] is True
    assert "n_authz_last_window" in body
    assert "n_exits_last_window" in body
    assert "n_trades_last_window" in body


# ---------------------------------------------------------------------------
# 7. AML export
# ---------------------------------------------------------------------------

def test_aml_export_shape(_iso_db):
    from spot_aggro.governance.resilience import export_aml_audit
    import spot_aggro.governance.three_way_shadow as tw
    tw._init_schema()
    bundle = export_aml_audit()
    assert bundle["ok"] is True
    assert "chain_verdict" in bundle
    assert "ledger_rows" in bundle
    assert "canary_snapshot" in bundle


# ---------------------------------------------------------------------------
# 8 + 9. Endpoints
# ---------------------------------------------------------------------------

def test_canary_endpoint_shape(_iso_db):
    from spot_aggro.api.routes import spot_aggro_canary_health
    body = spot_aggro_canary_health()
    assert body["ok"] is True
    assert "checks" in body


def test_ledger_verify_endpoint_shape(_iso_db):
    from spot_aggro.api.routes import spot_aggro_ledger_verify
    body = spot_aggro_ledger_verify()
    assert body["ok"] is True
    assert "verdict" in body
    assert "head" in body


# ---------------------------------------------------------------------------
# 10. AML export requires admin
# ---------------------------------------------------------------------------

def test_aml_export_admin_gate(_iso_db, monkeypatch):
    from fastapi import HTTPException
    from spot_aggro.api.routes import spot_aggro_aml_audit_export
    monkeypatch.setenv("OPS_ADMIN_TOKEN", "admin-secret")
    with pytest.raises(HTTPException) as exc:
        spot_aggro_aml_audit_export(
            start_ts_ms=None, end_ts_ms=None, x_ops_token=None,
        )
    assert exc.value.status_code in (401, 403)


# ---------------------------------------------------------------------------
# 11. Build tag + flags
# ---------------------------------------------------------------------------

def test_phase_hh_build_and_flags():
    import re
    from spot_aggro.api.routes import spot_aggro_build, SERVER_BUILD
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "hh"), SERVER_BUILD
    feats = (spot_aggro_build().get("features") or {})
    for flag in (
        "immutable_ledger_hashchain", "canary_health_check",
        "degraded_mode_auto_fallback", "recovery_playbook",
        "aml_audit_export", "human_in_loop_L3_L4",
    ):
        assert feats.get(flag) is True, f"missing flag: {flag}"


# ---------------------------------------------------------------------------
# 12. Dashboard
# ---------------------------------------------------------------------------

def test_dashboard_hh():
    import re
    m = re.search(r'content="phase-11n-9-([a-z]+)-2026-04-20"', HTML)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "hh")
    assert 'id="c-resilience"' in HTML
    assert "Resilience & Compliance" in HTML or "Resilience &amp; Compliance" in HTML
    assert "async function _refreshResilience" in HTML
    assert 'id="rs-verify-btn"' in HTML
    assert 'id="rs-export-btn"' in HTML
    assert '"c-resilience"' in HTML   # registered in _TAB_CARDS
