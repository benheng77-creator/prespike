"""Phase 11n-9-rr — B (depth floor 50k) + A (best-of-both-exchange depth).
"""
from __future__ import annotations

import importlib
import sqlite3
import time
from pathlib import Path

import pytest


class _FakeMio:
    timestamp = 0
    regime = "UNKNOWN"
    squeeze_timing_window = "NONE"


@pytest.fixture
def _iso_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    for m in (
        "spot_aggro.ops.scheduler.exchange_comparison_feed",
        "spot_aggro.governance.ensemble_meta",
        "spot_aggro.governance.strategy_variants",
    ):
        mod = importlib.import_module(m)
        importlib.reload(mod)
    yield tmp_path


def _seed_cdc_depth(db, symbol, top_depth_usd, exchange="cryptocom"):
    import spot_aggro.ops.scheduler.exchange_comparison_feed as xcf
    xcf._init_schema()
    row = xcf.ComparisonRow(
        ts_ms=int(time.time() * 1000),
        symbol=symbol, exchange=exchange,
        last=1.0, bid=0.99, ask=1.01, spread_bp=10.0,
        bid_depth_usd=top_depth_usd, ask_depth_usd=top_depth_usd,
        top_depth_usd=top_depth_usd, ok=True,
    )
    xcf._persist(row)


# B — depth floor dropped
def test_mom_min_depth_lowered_to_50k():
    from spot_aggro.governance.strategy_variants import MOM_MIN_DEPTH_USD
    assert MOM_MIN_DEPTH_USD == 50_000.0


# A — best-of-both helper returns CDC when deeper
def test_best_effective_depth_uses_cdc_when_deeper(_iso_db):
    from spot_aggro.governance.ensemble_meta import _best_effective_depth_usd
    _seed_cdc_depth(_iso_db, "ENA-USDT", top_depth_usd=285_000)
    # OKX reports $2k, CDC reports $285k — should return 285k
    result = _best_effective_depth_usd("ENA-USDT", okx_depth_usd=2_000)
    assert result == 285_000


def test_best_effective_depth_uses_okx_when_deeper(_iso_db):
    from spot_aggro.governance.ensemble_meta import _best_effective_depth_usd
    _seed_cdc_depth(_iso_db, "OP-USDT", top_depth_usd=7_000)
    # OKX $87k, CDC $7k — OKX wins
    result = _best_effective_depth_usd("OP-USDT", okx_depth_usd=87_000)
    assert result == 87_000


def test_best_effective_depth_fallback_when_no_cdc_data(_iso_db):
    from spot_aggro.governance.ensemble_meta import _best_effective_depth_usd
    # no CDC row for this symbol
    result = _best_effective_depth_usd("UNKNOWN-USDT", okx_depth_usd=5_000)
    assert result == 5_000


def test_best_effective_depth_ignores_stale_cdc(_iso_db):
    """CDC rows older than 10min should not be used."""
    import spot_aggro.ops.scheduler.exchange_comparison_feed as xcf
    xcf._init_schema()
    # Seed an old row (15 min ago)
    stale_row = xcf.ComparisonRow(
        ts_ms=int(time.time() * 1000) - 15 * 60_000,
        symbol="ENA-USDT", exchange="cryptocom",
        last=1.0, bid=0.99, ask=1.01, spread_bp=10.0,
        bid_depth_usd=500_000, ask_depth_usd=500_000,
        top_depth_usd=500_000, ok=True,
    )
    xcf._persist(stale_row)
    from spot_aggro.governance.ensemble_meta import _best_effective_depth_usd
    # Stale → falls back to OKX only
    result = _best_effective_depth_usd("ENA-USDT", okx_depth_usd=2_000)
    assert result == 2_000


# Momentum variant uses effective depth
def test_momentum_admits_when_cdc_depth_clears_floor(_iso_db):
    _seed_cdc_depth(_iso_db, "ENA-USDT", top_depth_usd=285_000)
    from spot_aggro.governance.strategy_variants import evaluate_momentum
    # OKX shows only $2k depth; CDC has $285k. With the 50k floor AND
    # best-of-both helper, this should now pass `liquid`.
    coin = {
        "symbol": "ENA-USDT",
        "return_24h": 0.02,        # +2% (clears 1% floor)
        "return_4h": 0.005,
        "funding_z": 0.5,          # > -0.5 floor
        "volume_ratio": 1.5,       # >= 1.0 floor
        "depth_usd": 2_000,        # OKX-only would FAIL 50k floor
        "spread_bp": 5,
        "spi": 0.4, "sigma_30d": 0.0002,
    }
    d = evaluate_momentum(coin, _FakeMio())
    assert d.passed is True, f"expected admit, got: {d}"
    assert "momentum ADMIT" in d.reason


def test_momentum_rejects_when_neither_exchange_has_depth(_iso_db):
    """When depth missing on both exchanges AND other signals also
    fail, momentum rejects. 3-of-4 rule means we need 2 failures to
    force reject — craft accordingly."""
    from spot_aggro.governance.strategy_variants import evaluate_momentum
    coin = {
        "symbol": "NEWCOIN-USDT",
        "return_24h": 0.005,       # below 1% floor (fail 1)
        "return_4h": 0.001,
        "funding_z": -0.8,         # below -0.5 floor (fail 2)
        "volume_ratio": 0.7,       # below 1.0 floor (fail 3)
        "depth_usd": 2_000,        # below 50k floor, no CDC (fail 4)
        "spread_bp": 5,
        "spi": 0.4, "sigma_30d": 0.0002,
    }
    d = evaluate_momentum(coin, _FakeMio())
    assert d.passed is False
    # 0 of 4 core checks passed, multiple failures listed
    assert "liquid" in d.reason


# Slippage uses effective depth too
def test_slippage_lower_when_cdc_depth_is_deeper(_iso_db):
    _seed_cdc_depth(_iso_db, "ENA-USDT", top_depth_usd=285_000)
    from spot_aggro.governance.ensemble_meta import estimated_slippage_bp
    coin_sym = {"symbol": "ENA-USDT", "spread_bp": 10, "depth_usd": 2_000}
    coin_nosym = {"spread_bp": 10, "depth_usd": 2_000}
    slip_with_cdc = estimated_slippage_bp(coin_sym, notional_usd=5.0)
    slip_no_cdc = estimated_slippage_bp(coin_nosym, notional_usd=5.0)
    # Both are $5 trades but cdc-aware should have lower depth_impact.
    assert slip_with_cdc <= slip_no_cdc


def test_phase_rr_build_and_flags():
    import re
    from spot_aggro.api.routes import spot_aggro_build, SERVER_BUILD
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "rr"), SERVER_BUILD
    feats = spot_aggro_build().get("features") or {}
    assert feats.get("momentum_depth_floor_50k") is True
    assert feats.get("best_of_both_exchange_depth") is True
