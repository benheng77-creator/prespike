"""Phase 11n-9-ee — three-way strategy-variant shadow horse race.

Validates:
  1. strategy_variants exposes evaluate_{control, contrarian, mean_reversion}
     + evaluate_all + VariantDecision contract.
  2. Contrarian admits only bottom-quartile control scores + liquid coins.
  3. Mean-reversion admits only the 5-filter oversold-bounce pattern.
  4. three_way_shadow records authz rows for all 3 variants on one call.
  5. record_exit mirrors PnL into every variant that admitted.
  6. evaluate() returns ThreeWayVerdict with 3 standings + promotion verdict.
  7. Promotion rule: >=200 exits + Wilson-lower > 0 + margin >0.02 over
     second-best Wilson-upper.
  8. /spot_aggro/build advertises the phase-ee feature flags.
  9. /spot_aggro/gov/three_way_shadow endpoint shape is stable.
 10. Dashboard HTML carries build bump, horse-race card, _refreshHorseRace()
     function + its refresh() wiring.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    # Clear any import-time cache of the db path by re-importing.
    import importlib
    import spot_aggro.governance.three_way_shadow as tw
    importlib.reload(tw)
    import spot_aggro.governance.strategy_variants as sv
    importlib.reload(sv)
    yield db


class _FakeMio:
    timestamp = 0
    regime = "UNKNOWN"
    squeeze_timing_window = "NONE"


# ---------------------------------------------------------------------------
# 1. strategy_variants contract
# ---------------------------------------------------------------------------

def test_strategy_variants_public_surface():
    from spot_aggro.governance import strategy_variants as sv
    for name in (
        "evaluate_control", "evaluate_contrarian",
        "evaluate_mean_reversion", "evaluate_all",
        "VariantDecision", "VARIANT_NAMES",
    ):
        assert hasattr(sv, name), f"strategy_variants missing {name}"
    assert set(sv.VARIANT_NAMES) == {"control", "contrarian", "mean_reversion"}


def test_variant_decision_shape():
    from spot_aggro.governance.strategy_variants import evaluate_all
    decisions = evaluate_all(
        {"symbol": "BTC-USDT", "spi": 0.5, "funding_z": 0.0,
         "depth_usd": 500_000, "spread_bp": 5, "return_24h": 0.0},
        _FakeMio(),
    )
    assert len(decisions) == 3
    for d in decisions:
        assert d.variant in ("control", "contrarian", "mean_reversion")
        assert isinstance(d.passed, bool)
        assert isinstance(d.score, float)
        assert isinstance(d.reason, str)
        assert isinstance(d.evidence, dict)


# ---------------------------------------------------------------------------
# 2. Contrarian passes on bottom-quartile + liquid
# ---------------------------------------------------------------------------

def test_contrarian_rejects_high_score():
    # High composite score (≥0.30) → contrarian should REJECT.
    from spot_aggro.governance.strategy_variants import evaluate_contrarian
    coin = {"symbol": "X-USDT", "spi": 0.9, "funding_z": -2.0,
            "depth_usd": 1_000_000, "spread_bp": 3}
    d = evaluate_contrarian(coin, _FakeMio())
    # This coin will score high (good liquidity + negative funding + high spi)
    # so contrarian should NOT pass.
    assert d.passed is False


def test_contrarian_accepts_low_score_liquid_coin():
    # Low composite + liquid + tight spread → contrarian admits.
    # Force a very low composite: positive funding (penalty), zero spi,
    # zero depth/volatility boost, wide-but-sub-limit spread.
    from spot_aggro.governance.strategy_variants import evaluate_contrarian
    coin = {"symbol": "Y-USDT", "spi": 0.0, "funding_z": 2.0,
            "depth_usd": 250_000, "spread_bp": 12,
            "return_24h": 0.0, "sigma_30d": 0.0}
    d = evaluate_contrarian(coin, _FakeMio())
    # Composite will be ~0.15-0.25 (below 0.30). Coin is liquid + spread
    # under 15bp → contrarian admits.
    assert d.passed is True, f"expected admit, got {d}"
    assert "contrarian ADMIT" in d.reason


def test_contrarian_rejects_illiquid_even_if_low_score():
    from spot_aggro.governance.strategy_variants import evaluate_contrarian
    coin = {"symbol": "Z-USDT", "spi": 0.05, "funding_z": 1.5,
            "depth_usd": 50_000, "spread_bp": 5}  # below $200k liquidity floor
    d = evaluate_contrarian(coin, _FakeMio())
    assert d.passed is False
    assert "illiquid" in d.reason


# ---------------------------------------------------------------------------
# 3. Mean-reversion filters
# ---------------------------------------------------------------------------

def test_mean_reversion_admits_oversold_coin():
    from spot_aggro.governance.strategy_variants import evaluate_mean_reversion
    coin = {
        "funding_z": -1.5,   # deep squeeze
        "return_24h": -0.12,  # -12%
        "depth_usd": 500_000,
        "spread_bp": 8,
    }
    d = evaluate_mean_reversion(coin, _FakeMio())
    assert d.passed is True
    assert "mean-rev ADMIT" in d.reason


def test_mean_reversion_rejects_shallow_drawdown():
    from spot_aggro.governance.strategy_variants import evaluate_mean_reversion
    coin = {
        "funding_z": -1.5,
        "return_24h": -0.02,  # only -2%, not deep enough
        "depth_usd": 500_000,
        "spread_bp": 8,
    }
    d = evaluate_mean_reversion(coin, _FakeMio())
    assert d.passed is False
    assert "deep_drawdown" in d.reason


def test_mean_reversion_respects_rsi_when_provided():
    from spot_aggro.governance.strategy_variants import evaluate_mean_reversion
    base = {
        "funding_z": -1.5, "return_24h": -0.12,
        "depth_usd": 500_000, "spread_bp": 8,
    }
    # RSI 40 (not oversold) → reject.
    coin = dict(base, rsi_14=40)
    assert evaluate_mean_reversion(coin, _FakeMio()).passed is False
    # RSI 20 (oversold) → admit.
    coin = dict(base, rsi_14=20)
    assert evaluate_mean_reversion(coin, _FakeMio()).passed is True


# ---------------------------------------------------------------------------
# 4. three_way_shadow records 3 authz rows per call
# ---------------------------------------------------------------------------

def test_record_authz_writes_three_variant_rows(_isolated_db):
    from spot_aggro.governance.three_way_shadow import record_authz, _connect
    n = record_authz(
        live_authz_id="a-1", symbol="BTC-USDT", side="buy", tier="A",
        coin={"spi": 0.5, "funding_z": -1.0, "depth_usd": 500_000,
              "spread_bp": 5, "return_24h": -0.10},
        mio=_FakeMio(),
    )
    assert n == 3
    con = _connect()
    try:
        rows = con.execute(
            "SELECT variant FROM shadow_variant_authorizations"
            " WHERE live_authz_id = 'a-1'"
        ).fetchall()
    finally:
        con.close()
    variants = {r["variant"] for r in rows}
    assert variants == {"control", "contrarian", "mean_reversion"}


# ---------------------------------------------------------------------------
# 5. record_exit mirrors PnL to admitted variants only
# ---------------------------------------------------------------------------

def test_record_exit_mirrors_only_admitted_variants(_isolated_db):
    from spot_aggro.governance.three_way_shadow import (
        record_authz, record_exit, _connect,
    )
    # Authz: mean-rev admits (deep drawdown + squeeze), control rejects
    # (spi too low to pass 0.70 gate).
    record_authz(
        live_authz_id="a-2", symbol="BTC-USDT", side="buy", tier="A",
        coin={"spi": 0.1, "funding_z": -1.5, "depth_usd": 500_000,
              "spread_bp": 5, "return_24h": -0.12},
        mio=_FakeMio(),
    )
    # Mirror a $10 profit exit.
    n = record_exit(
        correlation_id="a-2", symbol="BTC-USDT", tier="A",
        pnl_usd=10.0, fee_usd=0.5, notional_usd=100.0,
    )
    # Should be exactly the number of variants that admitted.
    con = _connect()
    try:
        admitted = con.execute(
            "SELECT variant FROM shadow_variant_authorizations"
            " WHERE live_authz_id = 'a-2' AND variant_passed = 1"
        ).fetchall()
        exits = con.execute(
            "SELECT variant, net_pnl FROM shadow_variant_exits"
            " WHERE correlation_id = 'a-2'"
        ).fetchall()
    finally:
        con.close()
    assert n == len(admitted)
    assert len(exits) == len(admitted)
    for e in exits:
        assert e["net_pnl"] == pytest.approx(9.5)


# ---------------------------------------------------------------------------
# 6 + 7. evaluate() returns verdict + promotion rule
# ---------------------------------------------------------------------------

def test_evaluate_returns_three_standings(_isolated_db):
    from spot_aggro.governance.three_way_shadow import evaluate
    v = evaluate()
    assert len(v.standings) == 3
    names = {s.variant for s in v.standings}
    assert names == {"control", "contrarian", "mean_reversion"}
    assert v.promotion_verdict in ("insufficient", "racing", "promote")


def test_promotion_rule_insufficient_below_min_exits(_isolated_db):
    from spot_aggro.governance.three_way_shadow import (
        record_authz, record_exit, evaluate,
    )
    # Seed a handful of exits — well below the 200 minimum.
    for i in range(10):
        record_authz(
            live_authz_id=f"seed-{i}", symbol="BTC-USDT",
            side="buy", tier="A",
            coin={"spi": 0.05, "funding_z": -1.5,
                  "depth_usd": 500_000, "spread_bp": 5,
                  "return_24h": -0.12},
            mio=_FakeMio(),
        )
        record_exit(
            correlation_id=f"seed-{i}", symbol="BTC-USDT",
            tier="A", pnl_usd=5.0, notional_usd=100.0,
        )
    v = evaluate()
    # Must NOT promote below MIN_EXITS_FOR_PROMOTION.
    assert v.promotion_verdict == "racing"


# ---------------------------------------------------------------------------
# 8. Feature flags advertised
# ---------------------------------------------------------------------------

def test_build_tag_and_flags_phase_ee():
    from spot_aggro.api.routes import spot_aggro_build, SERVER_BUILD
    assert SERVER_BUILD == "phase-11n-9-ee-2026-04-20"
    body = spot_aggro_build()
    feats = body.get("features") or {}
    assert feats.get("strategy_variants_three_way") is True
    assert feats.get("variant_horse_race") is True


# ---------------------------------------------------------------------------
# 9. Endpoint shape
# ---------------------------------------------------------------------------

def test_three_way_shadow_endpoint_shape():
    from spot_aggro.api.routes import spot_aggro_three_way_shadow
    body = spot_aggro_three_way_shadow()
    assert body["ok"] is True
    assert "state" in body
    state = body["state"]
    assert "standings" in state
    assert "promotion_verdict" in state


# ---------------------------------------------------------------------------
# 10. Dashboard HTML wiring
# ---------------------------------------------------------------------------

def test_build_meta_phase_ee():
    assert 'content="phase-11n-9-ee-2026-04-20"' in HTML


def test_horse_race_card_present():
    assert 'id="c-horse-race"' in HTML
    assert "Strategy Horse Race" in HTML
    assert 'id="horse-race-tbody"' in HTML
    assert 'id="hr-verdict"' in HTML


def test_refresh_horse_race_function_defined():
    assert "async function _refreshHorseRace()" in HTML
    assert "/spot_aggro/gov/three_way_shadow" in HTML


def test_refresh_calls_horse_race():
    assert "_refreshHorseRace();" in HTML
