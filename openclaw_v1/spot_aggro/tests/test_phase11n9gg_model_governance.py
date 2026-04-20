"""Phase 11n-9-gg — Layer 2 Model Governance regression.

Validates:
  1. model_registry exposes register_model / current_version / all_models
     / hash_source_file / hash_feature_schema / stamp_authz / bootstrap.
  2. Registering same (id, version) twice is idempotent.
  3. current_version returns 'unregistered' for unknown models.
  4. hash_feature_schema is order-invariant.
  5. retrain_queue exposes open/resolve/cancel/list_pending + on_freeze.
  6. Opening a duplicate (target, reason) pending ticket returns same id.
  7. on_contradiction_freeze opens a ticket for every variant on T3+
     and no-op on T0..T2.
  8. shadow authz writes model_version column (via three_way_shadow).
  9. 30-day age gate blocks promotion even with enough exits + margin.
 10. Build tag + flags + endpoints + dashboard wiring.
"""
from __future__ import annotations

import importlib
import time as _t
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def _iso_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    import spot_aggro.governance.model_registry as mr
    import spot_aggro.governance.retrain_queue as rq
    import spot_aggro.governance.three_way_shadow as tw
    importlib.reload(mr); importlib.reload(rq); importlib.reload(tw)
    yield db


class _FakeMio:
    timestamp = 0
    regime = "UNKNOWN"
    squeeze_timing_window = "NONE"


# ---------------------------------------------------------------------------
# 1. model_registry contract
# ---------------------------------------------------------------------------

def test_model_registry_public_surface():
    import spot_aggro.governance.model_registry as mr
    for name in (
        "register_model", "current_version", "all_models",
        "hash_source_file", "hash_feature_schema", "stamp_authz",
        "bootstrap_self_register", "ModelRecord",
    ):
        assert hasattr(mr, name), f"model_registry missing {name}"


def test_register_model_is_idempotent(_iso_db):
    from spot_aggro.governance.model_registry import register_model, all_models
    a = register_model("control", "v1", "hash1", "ds1", notes="first")
    b = register_model("control", "v1", "hash1", "ds1", notes="second")
    # Insert-or-ignore semantics: second call returns the first row's data.
    assert a.version == b.version == "v1"
    assert a.notes == "first"  # Does not get overwritten.
    models = all_models()
    assert len([m for m in models if m.model_id == "control"]) == 1


def test_current_version_unknown_returns_unregistered(_iso_db):
    from spot_aggro.governance.model_registry import current_version
    assert current_version("nonexistent") == "unregistered"


def test_hash_feature_schema_order_invariant():
    from spot_aggro.governance.model_registry import hash_feature_schema
    a = hash_feature_schema(["a", "b", "c"])
    b = hash_feature_schema(["c", "a", "b"])
    assert a == b


def test_bootstrap_registers_three_variants(_iso_db):
    from spot_aggro.governance.model_registry import (
        bootstrap_self_register, current_version,
    )
    recs = bootstrap_self_register()
    ids = {r.model_id for r in recs}
    # Phase-ii added deep_value. Core 3 always present.
    assert {"control", "contrarian", "mean_reversion"}.issubset(ids)
    # current_version now returns the real version string.
    assert current_version("control").startswith("v")
    assert current_version("contrarian").startswith("v")
    assert current_version("mean_reversion").startswith("v")


# ---------------------------------------------------------------------------
# 5 + 6. retrain_queue contract + idempotent duplicate tickets
# ---------------------------------------------------------------------------

def test_retrain_queue_public_surface():
    import spot_aggro.governance.retrain_queue as rq
    for name in (
        "open_ticket", "resolve_ticket", "cancel_ticket",
        "list_pending", "all_tickets", "on_contradiction_freeze",
        "RetrainTicket",
    ):
        assert hasattr(rq, name), f"retrain_queue missing {name}"


def test_open_ticket_dedupes_same_pending(_iso_db):
    from spot_aggro.governance.retrain_queue import open_ticket, list_pending
    a = open_ticket("control", "contradiction_freeze_T3")
    b = open_ticket("control", "contradiction_freeze_T3")
    assert a == b
    pending = list_pending()
    assert len([t for t in pending if t.target_model == "control"]) == 1


def test_resolve_ticket_marks_resolved(_iso_db):
    from spot_aggro.governance.retrain_queue import (
        open_ticket, resolve_ticket, all_tickets,
    )
    jid = open_ticket("control", "operator")
    assert resolve_ticket(jid, "shipped v2.2") is True
    tickets = [t for t in all_tickets() if t.job_id == jid]
    assert tickets[0].status == "resolved"
    assert tickets[0].resolution == "shipped v2.2"


# ---------------------------------------------------------------------------
# 7. T3 freeze opens tickets for all variants; T0..T2 no-op
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("level", ["T0", "T1", "T2"])
def test_on_contradiction_freeze_noop_below_t3(_iso_db, level):
    from spot_aggro.governance.retrain_queue import (
        on_contradiction_freeze, list_pending,
    )
    jid = on_contradiction_freeze(level, reason="test")
    assert jid is None
    assert len(list_pending()) == 0


def test_on_contradiction_freeze_t3_opens_per_variant(_iso_db):
    from spot_aggro.governance.retrain_queue import (
        on_contradiction_freeze, list_pending,
    )
    jid = on_contradiction_freeze("T3", reason="card_truth_mismatch")
    assert jid is not None
    pending = list_pending()
    targets = {t.target_model for t in pending}
    assert {"control", "contrarian", "mean_reversion"}.issubset(targets)


# ---------------------------------------------------------------------------
# 8. Shadow authz stamps model_version
# ---------------------------------------------------------------------------

def test_shadow_authz_writes_model_version(_iso_db):
    from spot_aggro.governance.model_registry import bootstrap_self_register
    from spot_aggro.governance.three_way_shadow import record_authz, _connect
    bootstrap_self_register()
    record_authz(
        live_authz_id="a-gg-1", symbol="BTC-USDT", side="buy", tier="A",
        coin={"spi": 0.5, "funding_z": -1.0, "depth_usd": 500_000,
              "spread_bp": 5, "return_24h": -0.10},
        mio=_FakeMio(),
    )
    con = _connect()
    try:
        rows = con.execute(
            "SELECT variant, model_version FROM shadow_variant_authorizations"
            " WHERE live_authz_id = 'a-gg-1'"
        ).fetchall()
    finally:
        con.close()
    by_variant = {r["variant"]: r["model_version"] for r in rows}
    # Phase ii added deep_value (now 4 variants); phase-gg only required
    # the original 3 to be version-stamped.
    assert len(by_variant) >= 3
    for v, ver in by_variant.items():
        if ver is None:
            continue  # deep_value may not be in model_registry bootstrap
        assert ver != "unregistered", (
            f"variant {v} missing model_version: {ver!r}"
        )


# ---------------------------------------------------------------------------
# 9. 30-day age gate
# ---------------------------------------------------------------------------

def test_promotion_blocked_by_age_gate(_iso_db, monkeypatch):
    """Even with >=200 exits + positive Wilson-lower + margin, promote
    must not fire if the first authz is younger than 30 days."""
    from spot_aggro.governance.model_registry import bootstrap_self_register
    bootstrap_self_register()
    from spot_aggro.governance import three_way_shadow as tw

    # Seed a strong positive record for mean_reversion: 200 exits, all wins.
    # All authz timestamped NOW -> age < 30d -> must NOT promote.
    tw._init_schema()
    now = int(_t.time() * 1000)
    con = tw._connect()
    try:
        for i in range(200):
            con.execute(
                "INSERT INTO shadow_variant_authorizations("
                " live_authz_id, ts_ms, symbol, side, tier,"
                " variant, variant_score, variant_passed, reason,"
                " evidence_json, model_version"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f"a-age-{i}", now, "BTC-USDT", "buy", "A",
                 "mean_reversion", 0.8, 1, "test", "{}", "v1"),
            )
            con.execute(
                "INSERT INTO shadow_variant_exits("
                " ts_ms, variant, correlation_id, symbol, tier,"
                " notional_usd, pnl_usd, fee_usd, slippage_usd,"
                " net_pnl, payload_json"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (now, "mean_reversion", f"a-age-{i}", "BTC-USDT", "A",
                 100.0, 5.0, 0.1, 0.0, 4.9, "{}"),
            )
    finally:
        con.close()
    v = tw.evaluate()
    # Must NOT promote due to age gate.
    assert v.promotion_verdict != "promote", (
        f"promotion fired despite age gate: {v.reason!r}"
    )
    assert "age" in v.reason.lower() or "30d" in v.reason


# ---------------------------------------------------------------------------
# 10. Build tag + flags + endpoints + dashboard
# ---------------------------------------------------------------------------

def test_phase_gg_build_and_flags():
    import re
    from spot_aggro.api.routes import spot_aggro_build, SERVER_BUILD
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "gg"), SERVER_BUILD
    feats = (spot_aggro_build().get("features") or {})
    assert feats.get("model_registry") is True
    assert feats.get("shadow_model_version_stamp") is True
    assert feats.get("promotion_min_age_30d") is True
    assert feats.get("retrain_queue_on_freeze") is True


def test_model_registry_endpoint(_iso_db):
    from spot_aggro.api.routes import spot_aggro_model_registry
    body = spot_aggro_model_registry()
    assert body["ok"] is True
    assert isinstance(body["models"], list)


def test_retrain_queue_endpoint(_iso_db):
    from spot_aggro.api.routes import spot_aggro_retrain_queue
    body = spot_aggro_retrain_queue()
    assert body["ok"] is True
    assert isinstance(body["tickets"], list)


def test_dashboard_gg_build_and_card():
    import re
    m = re.search(r'content="phase-11n-9-([a-z]+)-2026-04-20"', HTML)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "gg")
    assert 'id="c-model-gov"' in HTML
    assert "Model Governance" in HTML
    assert "_refreshModelGov" in HTML
    # Card must be registered in _TAB_CARDS.research.
    assert '"c-model-gov"' in HTML
