"""Phase 11n-9-ss — Contrarian + Deep Value isolated panel tests.

Covers:
  1. Unit: scope guard correctly classifies variants.
  2. Unit: feature flag gates panel.
  3. Unit: RBAC rejects without role / admin token.
  4. Integration: /dashboard returns only CDV-scoped data.
  5. Smoke: dashboard endpoint loads all 9 sections.
  6. Failover: backend db-missing degrades gracefully.
  7. Governance: freeze requires two-operator header.
  8. Build tag + flags advertised.
  9. Main panel untouched: c-gov-board, c-horse-race etc still present.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import HTTPException


REPO = Path(__file__).resolve().parents[3]
HTML_MAIN = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
HTML_CDV = (REPO / "web" / "strategy" / "contrarian-deepvalue"
            / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def _iso_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    monkeypatch.setenv("FEATURE_CONTRARIAN_DEEPVALUE_PANEL", "1")
    for m in (
        "spot_aggro.governance.strategy_scope_guard",
        "spot_aggro.api.routes_strategy_cdv",
    ):
        mod = importlib.import_module(m)
        importlib.reload(mod)
    # Seed minimal trade_log so daily_report helpers don't crash.
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS trade_log("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL,"
        " module TEXT, action TEXT NOT NULL, tier TEXT,"
        " notional_usd REAL, fee_usd REAL, pnl_usd REAL,"
        " side TEXT, avg_px REAL, correlation_id TEXT,"
        " payload_json TEXT)"
    )
    con.commit(); con.close()
    yield db


# ---------------------------------------------------------------------------
# 1. Scope guard unit
# ---------------------------------------------------------------------------

def test_scope_guard_contrarian_allowed():
    from spot_aggro.governance.strategy_scope_guard import (
        CDV_VARIANTS, require_cdv_scope,
    )
    assert "contrarian" in CDV_VARIANTS
    assert "deep_value" in CDV_VARIANTS
    # no-variant row is allowed (may be an aggregate)
    require_cdv_scope({})
    require_cdv_scope({"variant": "contrarian"})
    require_cdv_scope({"variant": "deep_value"})


def test_scope_guard_rejects_other_variants():
    from spot_aggro.governance.strategy_scope_guard import (
        require_cdv_scope, ScopeViolation,
    )
    for bad in ("control", "momentum", "mean_reversion"):
        with pytest.raises(ScopeViolation):
            require_cdv_scope({"variant": bad})


def test_filter_cdv_rows_drops_other_variants():
    from spot_aggro.governance.strategy_scope_guard import filter_cdv_rows
    rows = [
        {"variant": "contrarian", "x": 1},
        {"variant": "momentum", "x": 2},
        {"variant": "deep_value", "x": 3},
        {"variant": "control", "x": 4},
        {"no_variant_key": True},
    ]
    out = filter_cdv_rows(rows)
    assert len(out) == 3   # contrarian, deep_value, no-variant
    variants = [r.get("variant") for r in out]
    assert set(variants) == {"contrarian", "deep_value", None}


def test_mask_non_cdv_fields_cleans_horse_race_standings():
    from spot_aggro.governance.strategy_scope_guard import mask_non_cdv_fields
    payload = {
        "standings": [
            {"variant": "control", "pnl": 1},
            {"variant": "contrarian", "pnl": 2},
            {"variant": "momentum", "pnl": 3},
            {"variant": "deep_value", "pnl": 4},
        ],
    }
    masked = mask_non_cdv_fields(payload)
    assert len(masked["standings"]) == 2
    vs = {s["variant"] for s in masked["standings"]}
    assert vs == {"contrarian", "deep_value"}


# ---------------------------------------------------------------------------
# 2. Feature flag
# ---------------------------------------------------------------------------

def test_feature_flag_off_blocks_panel(tmp_path, monkeypatch):
    monkeypatch.delenv("FEATURE_CONTRARIAN_DEEPVALUE_PANEL", raising=False)
    import spot_aggro.governance.strategy_scope_guard as g
    importlib.reload(g)
    assert g.feature_enabled() is False


def test_feature_flag_on_enables_panel(monkeypatch):
    monkeypatch.setenv("FEATURE_CONTRARIAN_DEEPVALUE_PANEL", "1")
    import spot_aggro.governance.strategy_scope_guard as g
    importlib.reload(g)
    assert g.feature_enabled() is True


# ---------------------------------------------------------------------------
# 3. RBAC
# ---------------------------------------------------------------------------

def test_rbac_rejects_without_role(_iso_db, monkeypatch):
    monkeypatch.delenv("OPS_ADMIN_TOKEN", raising=False)
    from spot_aggro.api.routes_strategy_cdv import cdv_dashboard
    with pytest.raises(HTTPException) as exc:
        cdv_dashboard(window_min=60, x_cdv_role=None, x_ops_token=None)
    assert exc.value.status_code == 403


def test_rbac_accepts_viewer_role(_iso_db):
    from spot_aggro.api.routes_strategy_cdv import cdv_dashboard
    body = cdv_dashboard(
        window_min=60,
        x_cdv_role="strategy:contrarian_deepvalue_viewer",
        x_ops_token=None,
    )
    assert body["ok"] is True
    assert body["strategy"] == "contrarian_deepvalue"


def test_rbac_accepts_admin_token(_iso_db, monkeypatch):
    monkeypatch.setenv("OPS_ADMIN_TOKEN", "test-admin-secret")
    from spot_aggro.api.routes_strategy_cdv import cdv_dashboard
    body = cdv_dashboard(
        window_min=60, x_cdv_role=None, x_ops_token="test-admin-secret",
    )
    assert body["ok"] is True


# ---------------------------------------------------------------------------
# 4 + 5. Integration + smoke
# ---------------------------------------------------------------------------

def test_dashboard_returns_only_cdv_variants(_iso_db):
    from spot_aggro.api.routes_strategy_cdv import cdv_dashboard
    body = cdv_dashboard(
        window_min=60,
        x_cdv_role="strategy:contrarian_deepvalue_viewer",
        x_ops_token=None,
    )
    assert set(body["variants"]) == {"contrarian", "deep_value"}


def test_dashboard_loads_all_9_sections(_iso_db):
    from spot_aggro.api.routes_strategy_cdv import cdv_dashboard
    body = cdv_dashboard(
        window_min=60,
        x_cdv_role="strategy:contrarian_deepvalue_viewer",
        x_ops_token=None,
    )
    for section in (
        "header", "summary", "contrarian", "deep_value",
        "pipeline", "positions", "execution", "risk", "daily_report",
    ):
        assert section in body, f"missing section: {section}"


def test_variant_view_rejects_non_cdv(_iso_db):
    from spot_aggro.api.routes_strategy_cdv import _build_variant_view
    from spot_aggro.governance.strategy_scope_guard import ScopeViolation
    with pytest.raises(ScopeViolation):
        _build_variant_view("control", 0)
    with pytest.raises(ScopeViolation):
        _build_variant_view("momentum", 0)


# ---------------------------------------------------------------------------
# 6. Failover
# ---------------------------------------------------------------------------

def test_dashboard_degrades_on_db_error(tmp_path, monkeypatch):
    # Point to a non-existent / unwritable db path to force errors.
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "missing_dir" / "x.db"))
    monkeypatch.setenv("FEATURE_CONTRARIAN_DEEPVALUE_PANEL", "1")
    import spot_aggro.api.routes_strategy_cdv as r
    importlib.reload(r)
    body = r.cdv_dashboard(
        window_min=60,
        x_cdv_role="strategy:contrarian_deepvalue_viewer",
        x_ops_token=None,
    )
    assert body["ok"] is True
    assert "contrarian" in body
    assert "deep_value" in body


# ---------------------------------------------------------------------------
# 7. Governance freeze two-operator rule
# ---------------------------------------------------------------------------

def test_freeze_requires_second_operator(_iso_db, monkeypatch):
    monkeypatch.setenv("OPS_ADMIN_TOKEN", "test-admin-secret")
    from spot_aggro.api.routes_strategy_cdv import cdv_freeze_new_entries
    with pytest.raises(HTTPException) as exc:
        cdv_freeze_new_entries(
            reason="test panic",
            x_cdv_role=None,
            x_ops_token="test-admin-secret",
            x_cdv_second_operator=None,
        )
    assert exc.value.status_code == 403
    assert "two-operator" in str(exc.value.detail).lower()


def test_freeze_accepts_with_second_operator(_iso_db, monkeypatch):
    monkeypatch.setenv("OPS_ADMIN_TOKEN", "test-admin-secret")
    from spot_aggro.api.routes_strategy_cdv import cdv_freeze_new_entries
    body = cdv_freeze_new_entries(
        reason="authorized halt",
        x_cdv_role=None,
        x_ops_token="test-admin-secret",
        x_cdv_second_operator="op2-ben-heng",
    )
    assert body["ok"] is True


# ---------------------------------------------------------------------------
# 8. Build tag + flags
# ---------------------------------------------------------------------------

def test_phase_ss_build_tag_and_flags():
    import re
    from spot_aggro.api.routes import spot_aggro_build, SERVER_BUILD
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "ss"), SERVER_BUILD
    feats = spot_aggro_build().get("features") or {}
    assert feats.get("cdv_panel_isolated") is True
    assert feats.get("cdv_panel_rbac_two_operator") is True


# ---------------------------------------------------------------------------
# 9. Existing main panel untouched
# ---------------------------------------------------------------------------

def test_main_panel_preserves_all_existing_cards():
    """Phase-ss must NOT remove or rename any existing cards."""
    must_survive = [
        "c-engine", "c-conn", "c-today", "c-acct",
        "c-horse-race", "c-gov-board", "c-kill-ladder",
        "c-positions", "c-trades", "c-funnel",
        "c-exchange-compare",
        "c-resilience", "c-model-gov",
        "c-alpha", "c-consensus", "c-llm", "c-swarm",
        "c-research", "c-mio",
    ]
    for cid in must_survive:
        assert f'id="{cid}"' in HTML_MAIN, f"card removed from main panel: {cid}"


def test_main_panel_has_cdv_nav_link():
    # Only additive modification allowed: one link to the new panel.
    assert "/strategy/contrarian_deepvalue/" in HTML_MAIN
    assert "STRATEGY · CDV" in HTML_MAIN


def test_cdv_panel_html_present():
    assert "Contrarian + Deep Value" in HTML_CDV
    # All 9 sections must exist in the HTML.
    for section_id in (
        "sec-header", "sec-summary", "sec-contrarian", "sec-deepvalue",
        "sec-pipeline", "sec-positions", "sec-execution",
        "sec-risk", "sec-daily",
    ):
        assert f'id="{section_id}"' in HTML_CDV, f"missing section: {section_id}"


def test_cdv_panel_fetches_scoped_endpoint():
    # Script must call /strategy/contrarian_deepvalue/dashboard
    assert "/strategy/contrarian_deepvalue/dashboard" in HTML_CDV
    # Strategy namespace marker in meta
    assert 'content="contrarian_deepvalue"' in HTML_CDV
