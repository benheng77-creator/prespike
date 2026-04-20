"""Phase 11n-9-uu — Options 1 + 2 tests.

Option 1: exchange_data_quality.py
Option 2: integration_trigger.py
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import time
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def _iso_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    monkeypatch.setenv("FEATURE_CONTRARIAN_DEEPVALUE_PANEL", "1")
    for m in (
        "spot_aggro.ops.scheduler.exchange_comparison_feed",
        "spot_aggro.governance.exchange_data_quality",
        "spot_aggro.governance.integration_trigger",
        "spot_aggro.governance.strategy_scope_guard",
        "spot_aggro.api.routes_strategy_cdv",
    ):
        mod = importlib.import_module(m)
        importlib.reload(mod)
    import spot_aggro.ops.scheduler.exchange_comparison_feed as xcf
    xcf._init_schema()
    yield db


def _seed_comparison(db, symbol, exchange, ts_ms, last, top_depth,
                     spread_bp=5.0, ok=True):
    import spot_aggro.ops.scheduler.exchange_comparison_feed as xcf
    row = xcf.ComparisonRow(
        ts_ms=ts_ms, symbol=symbol, exchange=exchange,
        last=last, bid=last - 0.001, ask=last + 0.001,
        spread_bp=spread_bp,
        bid_depth_usd=top_depth, ask_depth_usd=top_depth,
        top_depth_usd=top_depth, ok=ok,
    )
    xcf._persist(row)


def _seed_shadow_reject(db, ts_ms, symbol, variant, reason):
    """Seed a shadow_variant_authorizations reject row for trigger test."""
    import spot_aggro.governance.three_way_shadow as tw
    tw._init_schema()
    con = tw._connect()
    try:
        con.execute(
            "INSERT INTO shadow_variant_authorizations("
            " live_authz_id, ts_ms, symbol, side, tier,"
            " variant, variant_score, variant_passed, reason,"
            " evidence_json, model_version"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"test-{ts_ms}-{symbol}", ts_ms, symbol, "buy", "C",
             variant, 0.5, 0, reason, "{}", "test"),
        )
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Option 1 — data quality audit
# ---------------------------------------------------------------------------

def test_data_quality_module_surface():
    from spot_aggro.governance import exchange_data_quality as dq
    for name in (
        "generate_report", "ExchangeSymbolQuality", "SymbolCrossExchange",
        "DataQualityReport", "STALE_BP_THRESHOLD", "SUSTAINED_DRIFT_BP",
    ):
        assert hasattr(dq, name), f"missing: {name}"


def test_uptime_pct_100_when_all_ok(_iso_db):
    from spot_aggro.governance.exchange_data_quality import generate_report
    now = int(time.time() * 1000)
    # 10 ok rows for BTC on OKX in last hour
    for i in range(10):
        _seed_comparison(
            _iso_db, "BTC-USDT", "okx",
            now - i * 60_000, last=50000 + i, top_depth=1_000_000,
        )
    rpt = generate_report(window_min=120)
    okx_btc = next(
        p for p in rpt.per_exchange_symbol
        if p.symbol == "BTC-USDT" and p.exchange == "okx"
    )
    assert okx_btc.uptime_pct == 1.0
    assert okx_btc.n_samples == 10


def test_uptime_pct_reflects_failures(_iso_db):
    from spot_aggro.governance.exchange_data_quality import generate_report
    now = int(time.time() * 1000)
    for i in range(10):
        ok = (i % 2 == 0)  # alternating
        _seed_comparison(
            _iso_db, "BTC-USDT", "cryptocom",
            now - i * 60_000, last=50000, top_depth=1_000_000, ok=ok,
        )
    rpt = generate_report(window_min=120)
    cdc = next(
        p for p in rpt.per_exchange_symbol
        if p.symbol == "BTC-USDT" and p.exchange == "cryptocom"
    )
    assert cdc.uptime_pct == 0.5


def test_sustained_drift_detection(_iso_db):
    """6 consecutive 60s buckets with 50bp drift -> 1 sustained event."""
    from spot_aggro.governance.exchange_data_quality import generate_report
    now = int(time.time() * 1000)
    for i in range(6):
        ts = now - i * 60_000
        _seed_comparison(_iso_db, "ENA-USDT", "okx", ts, last=0.1000,
                         top_depth=5_000)
        _seed_comparison(_iso_db, "ENA-USDT", "cryptocom", ts, last=0.1005,
                         top_depth=100_000)
    rpt = generate_report(window_min=60)
    ce = next(c for c in rpt.cross_exchange if c.symbol == "ENA-USDT")
    assert ce.sustained_drift_events >= 1


def test_verdict_classification(_iso_db):
    from spot_aggro.governance.exchange_data_quality import generate_report
    now = int(time.time() * 1000)
    # Low-activity symbol -> unfit
    for i in range(3):
        ts = now - i * 60_000
        _seed_comparison(_iso_db, "LOW-USDT", "okx", ts, 1.0, 100)
        _seed_comparison(_iso_db, "LOW-USDT", "cryptocom", ts, 1.0, 100)
    rpt = generate_report(window_min=60)
    ce = next(c for c in rpt.cross_exchange if c.symbol == "LOW-USDT")
    assert ce.verdict in ("unfit", "watch")


def test_endpoint_exchange_quality_returns_report(_iso_db):
    from spot_aggro.api.routes_strategy_cdv import cdv_exchange_quality
    body = cdv_exchange_quality(
        window_min=60,
        x_cdv_role="strategy:contrarian_deepvalue_viewer",
        x_ops_token=None,
    )
    assert body["ok"] is True
    assert body["strategy"] == "contrarian_deepvalue"
    assert "report" in body
    assert "per_exchange_symbol" in body["report"]
    assert "cross_exchange" in body["report"]


# ---------------------------------------------------------------------------
# Option 2 — integration trigger
# ---------------------------------------------------------------------------

def test_trigger_module_surface():
    from spot_aggro.governance import integration_trigger as it
    for name in (
        "record_signal", "scan_admit_blocked_signals",
        "scan_sustained_drift_signals",
        "compute_verdict", "latest_signals",
        "KIND_ADMIT_BLOCKED", "KIND_DRIFT_PAYOFF", "KIND_FILL_SLIP_GAP",
        "ACTIONABLE_THRESHOLD", "WATCHING_THRESHOLD",
    ):
        assert hasattr(it, name), f"missing: {name}"


def test_verdict_none_with_no_signals(_iso_db):
    from spot_aggro.governance.integration_trigger import compute_verdict
    v = compute_verdict(window_min=60)
    assert v.cdc_value_signal == "none"
    assert v.n_signals_24h == 0


def test_verdict_watching_at_5_signals(_iso_db):
    from spot_aggro.governance.integration_trigger import (
        record_signal, compute_verdict, KIND_FILL_SLIP_GAP,
    )
    for i in range(6):
        record_signal(
            kind=KIND_FILL_SLIP_GAP, symbol=f"T{i}-USDT",
            gap_bp=25.0, notes="test",
        )
    v = compute_verdict(window_min=60)
    assert v.cdc_value_signal == "watching"
    assert v.n_signals_24h >= 5


def test_verdict_actionable_at_20_signals(_iso_db):
    from spot_aggro.governance.integration_trigger import (
        record_signal, compute_verdict, KIND_FILL_SLIP_GAP,
    )
    for i in range(25):
        record_signal(
            kind=KIND_FILL_SLIP_GAP, symbol=f"T{i % 5}-USDT",
            gap_bp=30.0, notes="test",
        )
    v = compute_verdict(window_min=60)
    assert v.cdc_value_signal == "actionable"
    assert v.n_signals_24h >= 20


def test_scan_admit_blocked_detects_depth_gap(_iso_db):
    """Seed a CDV rejection + exchange comparison row with CDC 100x
    deeper than OKX; scan must record an admit_blocked signal."""
    from spot_aggro.governance.integration_trigger import (
        scan_admit_blocked_signals, latest_signals, KIND_ADMIT_BLOCKED,
    )
    now = int(time.time() * 1000)
    # CDC 100x deeper
    _seed_comparison(_iso_db, "ENA-USDT", "okx", now, last=0.1, top_depth=1_000)
    _seed_comparison(_iso_db, "ENA-USDT", "cryptocom", now, last=0.1,
                     top_depth=150_000)
    # Reject row for ENA-USDT with 'liquid' reason
    _seed_shadow_reject(_iso_db, now, "ENA-USDT", "deep_value",
                        "deep-value reject: failed=liquid")
    n = scan_admit_blocked_signals(window_min=60)
    assert n >= 1
    sigs = latest_signals(limit=5)
    assert any(
        s["kind"] == KIND_ADMIT_BLOCKED and s["symbol"] == "ENA-USDT"
        for s in sigs
    )


def test_scan_admit_blocked_ignores_when_depth_parity(_iso_db):
    """When CDC depth is NOT 10x+ OKX, no signal recorded."""
    from spot_aggro.governance.integration_trigger import (
        scan_admit_blocked_signals, latest_signals,
    )
    now = int(time.time() * 1000)
    _seed_comparison(_iso_db, "OP-USDT", "okx", now, last=0.12, top_depth=50_000)
    _seed_comparison(_iso_db, "OP-USDT", "cryptocom", now, last=0.12, top_depth=30_000)
    _seed_shadow_reject(_iso_db, now, "OP-USDT", "deep_value",
                        "deep-value reject: failed=liquid")
    n = scan_admit_blocked_signals(window_min=60)
    assert n == 0


def test_endpoint_integration_trigger_returns_verdict(_iso_db):
    from spot_aggro.api.routes_strategy_cdv import cdv_integration_trigger
    body = cdv_integration_trigger(
        window_min=60,
        x_cdv_role="strategy:contrarian_deepvalue_viewer",
        x_ops_token=None,
    )
    assert body["ok"] is True
    assert body["strategy"] == "contrarian_deepvalue"
    assert "verdict" in body
    assert body["verdict"]["cdc_value_signal"] in ("none", "watching", "actionable")


def test_cdv_panel_has_exchange_intel_card():
    html = (REPO / "web" / "strategy" / "contrarian-deepvalue"
            / "index.html").read_text(encoding="utf-8")
    assert 'id="sec-xintel"' in html
    assert "Exchange Intelligence" in html
    assert 'id="xi-tbody"' in html
    assert "async function fetchXIntel" in html
    # 60s refresh cadence wired.
    assert "setInterval(fetchXIntel, 60_000)" in html


def test_phase_uu_build_and_flags():
    import re
    from spot_aggro.api.routes import spot_aggro_build, SERVER_BUILD
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "uu"), SERVER_BUILD
    feats = spot_aggro_build().get("features") or {}
    assert feats.get("exchange_data_quality_audit") is True
    assert feats.get("integration_trigger_rule") is True
