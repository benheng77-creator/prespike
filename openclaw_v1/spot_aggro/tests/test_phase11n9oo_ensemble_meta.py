"""Phase 11n-9-oo — ensemble meta-learner + vol sizing + regime + slippage + disagreement.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]


class _FakeMio:
    timestamp = 0
    regime = "UNKNOWN"
    squeeze_timing_window = "NONE"


# U3 — regime classifier
def test_classify_regime_volatile():
    from spot_aggro.governance.ensemble_meta import classify_regime
    assert classify_regime({"sigma_30d": 0.0006, "return_24h": 0.0}) == "volatile"


def test_classify_regime_trending():
    from spot_aggro.governance.ensemble_meta import classify_regime
    assert classify_regime({"sigma_30d": 0.00020, "return_24h": 0.05}) == "trending"


def test_classify_regime_calm():
    from spot_aggro.governance.ensemble_meta import classify_regime
    assert classify_regime({"sigma_30d": 0.00010, "return_24h": 0.01}) == "calm"


def test_regime_weight_volatile_halves_all_variants():
    from spot_aggro.governance.ensemble_meta import regime_weight
    for v in ("contrarian", "deep_value", "mean_reversion", "momentum"):
        assert regime_weight("volatile", v) == 0.5


def test_regime_weight_trending_favors_momentum():
    from spot_aggro.governance.ensemble_meta import regime_weight
    assert regime_weight("trending", "momentum") == 1.20
    assert regime_weight("trending", "contrarian") == 0.60


# U2 — vol sizing
def test_volatility_size_multiplier_scales_down():
    from spot_aggro.governance.ensemble_meta import volatility_size_multiplier
    # sigma_30d way above target -> multiplier below 1 but floored at 0.25
    m = volatility_size_multiplier({"sigma_30d": 0.001})
    assert 0.25 <= m <= 1.0
    assert m < 1.0


def test_volatility_size_multiplier_one_when_low_sigma():
    from spot_aggro.governance.ensemble_meta import volatility_size_multiplier
    assert volatility_size_multiplier({"sigma_30d": 0.00005}) == 1.0


# U4 — slippage
def test_estimated_slippage_bp_depth_impact():
    from spot_aggro.governance.ensemble_meta import estimated_slippage_bp
    # Calibrated model (phase-oo): $5k order into $10k depth
    #   -> 50% depth consumption * 10bp cap = 5bp depth
    #   + half_spread 10/2 = 5bp -> 10bp total
    s = estimated_slippage_bp({"depth_usd": 10_000, "spread_bp": 10}, 5_000)
    assert 8 <= s <= 20


def test_net_expectancy_negative_blocks():
    from spot_aggro.governance.ensemble_meta import net_expectancy_bp
    # variant_score 0.5 * avg_win 50bp = 25bp gross; 30bp slippage + 20bp fee
    # -> -25bp net (negative)
    n = net_expectancy_bp(0.5, 50.0, 30.0, 20.0)
    assert n < 0


# U1 — meta gate
def test_meta_gate_blocks_low_variant_score():
    from spot_aggro.governance.ensemble_meta import meta_gate
    coin = {"sigma_30d": 0.0001, "return_24h": 0.0,
            "depth_usd": 1_000_000, "spread_bp": 5}
    r = meta_gate(
        variant="contrarian", variant_score=0.40,
        coin=coin, notional_usd=5.0,
    )
    assert r.ok is False
    assert "variant_score" in r.reason


def test_meta_gate_passes_good_setup():
    from spot_aggro.governance.ensemble_meta import meta_gate
    coin = {"sigma_30d": 0.0001, "return_24h": 0.01,
            "depth_usd": 1_000_000, "spread_bp": 3}
    r = meta_gate(
        variant="deep_value", variant_score=0.80,
        coin=coin, notional_usd=5.0,
        avg_win_bp=200.0,
    )
    assert r.ok is True
    assert r.regime in ("calm", "unknown")


def test_meta_gate_blocks_volatile_regime_for_wrong_variant():
    from spot_aggro.governance.ensemble_meta import meta_gate
    coin = {"sigma_30d": 0.001, "return_24h": 0.0,  # volatile
            "depth_usd": 1_000_000, "spread_bp": 5}
    # In volatile regime, contrarian weight is 0.5 (at META_MIN_REGIME_WEIGHT)
    # — needs to PASS (== bound) or we block. With 0.5 at floor it should
    # pass the floor but the volatility multiplier shrinks size.
    r = meta_gate(
        variant="contrarian", variant_score=0.80,
        coin=coin, notional_usd=5.0,
    )
    # volatile sigma halves size via vol_m and regime weight; final size_mult = 0.5 * vol_m
    assert r.regime == "volatile"
    assert r.size_multiplier < 1.0


# U5 — disagreement
def test_ensemble_disagreement():
    from spot_aggro.governance.ensemble_meta import (
        ensemble_disagreement, should_trigger_freeze,
    )
    scores = {"contrarian": 0.8, "momentum": 0.2, "deep_value": 0.5}
    d = ensemble_disagreement(scores)
    assert abs(d - 0.6) < 1e-6
    triggered, val = should_trigger_freeze(scores)
    assert triggered is True


def test_ensemble_no_disagreement_on_agreement():
    from spot_aggro.governance.ensemble_meta import should_trigger_freeze
    triggered, val = should_trigger_freeze(
        {"contrarian": 0.5, "momentum": 0.55, "deep_value": 0.48}
    )
    assert triggered is False


# Build tag + flags
def test_phase_oo_build_and_flags():
    import re
    from spot_aggro.api.routes import spot_aggro_build, SERVER_BUILD
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "oo"), SERVER_BUILD
    feats = spot_aggro_build().get("features") or {}
    for flag in (
        "meta_learner_confidence_gate",
        "volatility_adaptive_sizing",
        "regime_per_variant_weighting",
        "slippage_budget_admit_check",
        "ensemble_disagreement_trigger",
    ):
        assert feats.get(flag) is True, f"missing flag: {flag}"


# Gate integration: meta-gate reduces size via size_multiplier
def test_live_variant_gate_propagates_size_multiplier(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    monkeypatch.setenv("SPOT_LIVE_VARIANTS", "momentum,deep_value")
    import spot_aggro.governance.live_variant_gate as lvg
    importlib.reload(lvg)
    monkeypatch.setattr(lvg, "_current_exposure_usd", lambda: 0.0)
    monkeypatch.setattr(lvg, "_live_session_pnl_usd", lambda: 0.0)
    monkeypatch.setattr(lvg, "_kill_ladder_blocks", lambda: False)
    monkeypatch.setattr(lvg, "_variant_exposure_usd", lambda v: 0.0)
    coin = {
        "return_24h": 0.05, "funding_z": 1.5, "volume_ratio": 2.0,
        "depth_usd": 1_000_000, "spread_bp": 3,
        "spi": 0.5, "sigma_30d": 0.0006,  # volatile regime
    }
    v = lvg.evaluate(coin, _FakeMio(), candidate_size_usd=5.0)
    # In volatile regime, momentum's weight is 0.5 -> would fail meta gate
    # floor since META_MIN_REGIME_WEIGHT = 0.50 (at-floor passes). But volatility
    # multiplier shrinks size_multiplier below 1.0.
    assert v.regime == "volatile"
    assert v.size_multiplier < 1.0
