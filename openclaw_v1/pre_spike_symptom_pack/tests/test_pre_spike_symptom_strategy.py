"""BotXLLM Panel contract conformance tests."""
from __future__ import annotations

import importlib

import pytest


def test_top_level_contract():
    m = importlib.import_module("pre_spike_symptom_pack.strategies.pre_spike_symptom_v1")
    assert isinstance(m.SYMBOLS, list) and len(m.SYMBOLS) >= 1
    assert isinstance(m.TIMEFRAME, str)
    assert isinstance(m.LOOKBACK_BARS, int) and m.LOOKBACK_BARS > 0
    assert isinstance(m.DEFAULTS, dict)
    assert callable(m.reset_state)
    assert callable(m.generate_signal)


def test_recipe_spread_has_required_knobs():
    m = importlib.import_module("pre_spike_symptom_pack.strategies.pre_spike_symptom_v1")
    required = {
        "threshold_tau_offset", "sigma_cap", "adx_cap",
        "spread_cap_bps", "funding_cap_bps_8h",
        "k_stop", "k_target", "max_holding_hours",
        "spot_per_trade_pct",
    }
    assert required <= set(m.RECIPE_SPREAD.keys())


def test_apply_recipe_clips_oob():
    m = importlib.import_module("pre_spike_symptom_pack.strategies.pre_spike_symptom_v1")
    m.apply_recipe({"k_stop": 99.0})
    assert m.DEFAULTS["k_stop"] <= 2.00
    m.apply_recipe({"k_stop": -5.0})
    assert m.DEFAULTS["k_stop"] >= 0.50


def test_generate_signal_short_window():
    m = importlib.import_module("pre_spike_symptom_pack.strategies.pre_spike_symptom_v1")
    m.reset_state()
    assert m.generate_signal([]) is None
    assert m.generate_signal([{"high": 1, "low": 1, "close": 1}] * 50) is None
