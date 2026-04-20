"""Phase 11n-9-tt — System Activity card on CDV panel.

Surface for the operator: every background daemon's last-run
timestamp, next-run ETA, observation, health status. No more
waiting-for-nothing.
"""
from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
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
    yield db


def test_system_activity_endpoint_returns_7_components(_iso_db):
    from spot_aggro.api.routes_strategy_cdv import cdv_system_activity
    body = cdv_system_activity(
        x_cdv_role="strategy:contrarian_deepvalue_viewer",
        x_ops_token=None,
    )
    assert body["ok"] is True
    assert body["strategy"] == "contrarian_deepvalue"
    assert body["n_total"] == 7
    names = [c["name"] for c in body["components"]]
    # All 6 documented daemons + shadow_scorer (implied by engine).
    expected = {
        "engine_heartbeat", "shadow_scorer", "exchange_comparison",
        "formula_review", "daily_report", "kill_ladder", "heartbeat_writer",
    }
    assert set(names) == expected


def test_system_activity_feature_flag_gated(tmp_path, monkeypatch):
    monkeypatch.delenv("FEATURE_CONTRARIAN_DEEPVALUE_PANEL", raising=False)
    import spot_aggro.governance.strategy_scope_guard as g
    import spot_aggro.api.routes_strategy_cdv as r
    importlib.reload(g); importlib.reload(r)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        r.cdv_system_activity(
            x_cdv_role="strategy:contrarian_deepvalue_viewer",
            x_ops_token=None,
        )
    assert exc.value.status_code == 404


def test_system_activity_requires_rbac(_iso_db, monkeypatch):
    monkeypatch.delenv("OPS_ADMIN_TOKEN", raising=False)
    from spot_aggro.api.routes_strategy_cdv import cdv_system_activity
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        cdv_system_activity(x_cdv_role=None, x_ops_token=None)
    assert exc.value.status_code == 403


def test_system_activity_component_shape(_iso_db):
    from spot_aggro.api.routes_strategy_cdv import cdv_system_activity
    body = cdv_system_activity(
        x_cdv_role="strategy:contrarian_deepvalue_viewer",
        x_ops_token=None,
    )
    for c in body["components"]:
        # Every component must carry these keys so the UI renders.
        for k in ("name", "label", "cadence_s", "status",
                  "observation", "ok"):
            assert k in c, f"component {c.get('name')} missing: {k}"


def test_overall_status_computed(_iso_db):
    from spot_aggro.api.routes_strategy_cdv import cdv_system_activity
    body = cdv_system_activity(
        x_cdv_role="strategy:contrarian_deepvalue_viewer",
        x_ops_token=None,
    )
    assert body["overall_status"] in ("all_green", "degraded")
    assert 0 <= body["n_healthy"] <= body["n_total"]


def test_cdv_panel_has_system_activity_card():
    assert 'id="sec-activity"' in HTML_CDV
    assert "System Activity" in HTML_CDV
    assert 'id="act-tbody"' in HTML_CDV
    assert "async function fetchActivity" in HTML_CDV
    # 15s refresh interval so operator sees daemons tick.
    assert "setInterval(fetchActivity, 15_000)" in HTML_CDV


def test_phase_tt_build_and_flag():
    import re
    from spot_aggro.api.routes import spot_aggro_build, SERVER_BUILD
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "tt"), SERVER_BUILD
    feats = spot_aggro_build().get("features") or {}
    assert feats.get("cdv_system_activity_card") is True


def test_exchange_comparison_daemon_visible_in_activity(_iso_db):
    """Seed a comparison row; activity card must reflect fresh feed."""
    import spot_aggro.ops.scheduler.exchange_comparison_feed as xcf
    xcf._init_schema()
    import time as _t
    row = xcf.ComparisonRow(
        ts_ms=int(_t.time() * 1000),
        symbol="BTC-USDT", exchange="cryptocom",
        last=50000, bid=49999, ask=50001, spread_bp=0.4,
        bid_depth_usd=1_000_000, ask_depth_usd=1_000_000,
        top_depth_usd=1_000_000, ok=True,
    )
    xcf._persist(row)
    from spot_aggro.api.routes_strategy_cdv import cdv_system_activity
    body = cdv_system_activity(
        x_cdv_role="strategy:contrarian_deepvalue_viewer",
        x_ops_token=None,
    )
    ex = next(c for c in body["components"] if c["name"] == "exchange_comparison")
    assert ex["ok"] is True
    assert ex["last_age_s"] is not None
    assert ex["last_age_s"] < 10
