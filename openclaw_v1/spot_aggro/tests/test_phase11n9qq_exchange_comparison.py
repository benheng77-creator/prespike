"""Phase 11n-9-qq — Crypto.com read-only comparison feed tests.

Validates:
  1. cryptocom_public module exposes fetch_ticker / fetch_depth / symbol conversion.
  2. Symbol translation OKX<->CDC round-trips.
  3. exchange_comparison_feed: persist + latest + per_symbol_gap.
  4. Endpoints respond with ok:true shape.
  5. Build tag + flags.
  6. Dashboard HTML carries comparison card + tab assignment.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import time
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def _iso_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    for m in ("spot_aggro.ops.scheduler.exchange_comparison_feed",):
        mod = importlib.import_module(m)
        importlib.reload(mod)
    yield tmp_path


# 1 — adapter surface
def test_cdc_adapter_public_surface():
    from shared.adapters import cryptocom_public as cdc
    for name in (
        "fetch_ticker_sync", "fetch_depth_sync",
        "fetch_ticker", "fetch_depth",
        "fetch_ticker_and_depth", "is_reachable",
        "_okx_to_cdc_symbol", "_cdc_to_okx_symbol",
        "CdcTicker", "CdcDepthSnapshot",
    ):
        assert hasattr(cdc, name), f"missing: {name}"


# 2 — symbol translation
def test_symbol_translation_roundtrip():
    from shared.adapters.cryptocom_public import (
        _okx_to_cdc_symbol, _cdc_to_okx_symbol,
    )
    assert _okx_to_cdc_symbol("ENA-USDT") == "ENA_USDT"
    assert _cdc_to_okx_symbol("ENA_USDT") == "ENA-USDT"
    assert _cdc_to_okx_symbol(_okx_to_cdc_symbol("BTC-USDT")) == "BTC-USDT"


# 3 — comparison feed persist + read
def test_comparison_feed_persist_and_read(_iso_db):
    import spot_aggro.ops.scheduler.exchange_comparison_feed as xcf
    xcf._init_schema()
    now = int(time.time() * 1000)
    # Fabricate rows for both exchanges.
    rows = [
        xcf.ComparisonRow(
            ts_ms=now, symbol="ENA-USDT", exchange="okx",
            last=0.116, bid=0.1159, ask=0.1161, spread_bp=17.2,
            bid_depth_usd=2200, ask_depth_usd=2300, top_depth_usd=2200,
            ok=True,
        ),
        xcf.ComparisonRow(
            ts_ms=now, symbol="ENA-USDT", exchange="cryptocom",
            last=0.1161, bid=0.116, ask=0.1162, spread_bp=17.2,
            bid_depth_usd=450, ask_depth_usd=480, top_depth_usd=450,
            ok=True,
        ),
        xcf.ComparisonRow(
            ts_ms=now, symbol="BTC-USDT", exchange="okx",
            last=50000, bid=49999, ask=50001, spread_bp=0.4,
            bid_depth_usd=5_000_000, ask_depth_usd=5_000_000,
            top_depth_usd=5_000_000, ok=True,
        ),
        xcf.ComparisonRow(
            ts_ms=now, symbol="BTC-USDT", exchange="cryptocom",
            last=50010, bid=50009, ask=50011, spread_bp=0.4,
            bid_depth_usd=2_000_000, ask_depth_usd=2_000_000,
            top_depth_usd=2_000_000, ok=True,
        ),
    ]
    for r in rows:
        xcf._persist(r)

    latest = xcf.latest_comparison_rows(window_min=5)
    assert len(latest) == 4
    symbols = {r["symbol"] for r in latest}
    assert symbols == {"ENA-USDT", "BTC-USDT"}
    exchanges = {r["exchange"] for r in latest}
    assert exchanges == {"okx", "cryptocom"}


# 4 — per_symbol_gap math
def test_per_symbol_gap_winner_detection(_iso_db):
    import spot_aggro.ops.scheduler.exchange_comparison_feed as xcf
    xcf._init_schema()
    now = int(time.time() * 1000)
    xcf._persist(xcf.ComparisonRow(
        ts_ms=now, symbol="ENA-USDT", exchange="okx",
        last=0.116, bid=0, ask=0, spread_bp=5.0,
        bid_depth_usd=0, ask_depth_usd=0, top_depth_usd=2200, ok=True,
    ))
    xcf._persist(xcf.ComparisonRow(
        ts_ms=now, symbol="ENA-USDT", exchange="cryptocom",
        last=0.1161, bid=0, ask=0, spread_bp=10.0,
        bid_depth_usd=0, ask_depth_usd=0, top_depth_usd=450, ok=True,
    ))
    gap = xcf.per_symbol_gap(window_min=5)
    assert len(gap) == 1
    g = gap[0]
    assert g["symbol"] == "ENA-USDT"
    assert g["depth_winner"] == "okx"         # 2200 > 450
    assert g["spread_winner"] == "okx"        # 5bp < 10bp
    assert g["both_ok"] is True
    # Price drift math: |0.116 - 0.1161| / 0.11605 * 10000 ≈ 8.6 bp
    assert 8 <= g["price_drift_bp"] <= 10


# 5 — endpoint shapes
def test_exchange_comparison_endpoint_shape(_iso_db):
    from spot_aggro.api.routes import spot_aggro_exchange_comparison
    body = spot_aggro_exchange_comparison(window_min=30)
    assert body["ok"] is True
    assert "gap" in body
    assert "raw_rows" in body
    assert "depth_winner_okx" in body
    assert "depth_winner_cryptocom" in body


def test_phase_qq_build_and_flags():
    import re
    from spot_aggro.api.routes import spot_aggro_build, SERVER_BUILD
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "qq"), SERVER_BUILD
    feats = spot_aggro_build().get("features") or {}
    assert feats.get("exchange_comparison_feed") is True
    assert feats.get("cryptocom_readonly_adapter") is True


# 6 — dashboard wiring
def test_dashboard_comparison_card_present():
    assert 'id="c-exchange-compare"' in HTML
    assert "Exchange Comparison" in HTML
    assert "async function _refreshExchangeCompare" in HTML
    assert '"c-exchange-compare"' in HTML  # in _TAB_CARDS.research
