"""Phase 11n-9-ii — live variant gate (contrarian + deep_value live).

Validates:
  1. strategy_variants exposes evaluate_deep_value + added to VARIANT_NAMES.
  2. deep_value admits only when WR>=55% AND 7d in [-30%,-3%] AND liquid.
  3. live_variant_gate inactive when SPOT_LIVE_VARIANTS empty.
  4. live_variant_gate rejects when exposure cap breached.
  5. live_variant_gate rejects when session DD exceeds cap.
  6. live_variant_gate rejects when kill_ladder at L2+.
  7. live_variant_gate admits when enabled variant passes all caps.
  8. Build tag + flags + endpoint shape.
"""
from __future__ import annotations

import importlib
import pytest


class _FakeMio:
    timestamp = 0
    regime = "UNKNOWN"
    squeeze_timing_window = "NONE"


@pytest.fixture
def _iso_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    for m in (
        "spot_aggro.governance.strategy_variants",
        "spot_aggro.governance.live_variant_gate",
        "spot_aggro.governance.kill_ladder",
    ):
        mod = importlib.import_module(m)
        importlib.reload(mod)
    yield tmp_path


# 1. Deep value added to VARIANT_NAMES
def test_deep_value_in_variant_names():
    from spot_aggro.governance.strategy_variants import VARIANT_NAMES
    assert "deep_value" in VARIANT_NAMES
    assert len(VARIANT_NAMES) == 4


# 2a. Rejects when WR data missing
def test_deep_value_rejects_no_wr_data():
    from spot_aggro.governance.strategy_variants import evaluate_deep_value
    coin = {"symbol": "NEW-USDT", "funding_z": -1.0, "ret_7d": -0.10,
            "depth_usd": 1_000_000, "spread_bp": 5}
    d = evaluate_deep_value(coin, _FakeMio())
    assert d.passed is False
    assert "wr_proven" in d.reason


# 2b. Rejects when 7d return outside window
def test_deep_value_rejects_shallow_drawdown(monkeypatch):
    from spot_aggro.governance import strategy_variants as sv
    monkeypatch.setattr(sv, "_historical_wr",
                        lambda sym: (0.60, 10))
    coin = {"symbol": "X-USDT", "funding_z": -1.0,
            "ret_7d": -0.01,  # only -1%, too shallow
            "depth_usd": 1_000_000, "spread_bp": 5}
    d = sv.evaluate_deep_value(coin, _FakeMio())
    assert d.passed is False
    assert "oversold_but_alive" in d.reason


# 2c. Admits when all filters pass
def test_deep_value_admits_when_filters_pass(monkeypatch):
    from spot_aggro.governance import strategy_variants as sv
    monkeypatch.setattr(sv, "_historical_wr",
                        lambda sym: (0.65, 20))
    coin = {"symbol": "OK-USDT", "funding_z": -0.5,
            "ret_7d": -0.10, "depth_usd": 1_000_000, "spread_bp": 5}
    d = sv.evaluate_deep_value(coin, _FakeMio())
    assert d.passed is True
    assert "deep-value ADMIT" in d.reason


# 3. Gate inactive when env unset
def test_live_variant_gate_inactive_by_default(monkeypatch, _iso_db):
    monkeypatch.delenv("SPOT_LIVE_VARIANTS", raising=False)
    from spot_aggro.governance import live_variant_gate as lvg
    assert lvg.live_variants_active() is False
    v = lvg.evaluate({}, _FakeMio(), candidate_size_usd=1.0)
    assert v.ok is False
    assert "not_enabled" in v.reason


# 4. Exposure cap breach
def test_live_variant_gate_rejects_exposure_breach(monkeypatch, _iso_db):
    monkeypatch.setenv("SPOT_LIVE_VARIANTS", "contrarian")
    monkeypatch.setenv("SPOT_LIVE_MAX_EXPOSURE_USD", "50")
    from spot_aggro.governance import live_variant_gate as lvg
    monkeypatch.setattr(lvg, "_current_exposure_usd", lambda: 45.0)
    monkeypatch.setattr(lvg, "_live_session_pnl_usd", lambda: 0.0)
    monkeypatch.setattr(lvg, "_kill_ladder_blocks", lambda: False)
    v = lvg.evaluate({"symbol": "X", "spi": 0.1, "funding_z": 0,
                      "depth_usd": 1000, "spread_bp": 5, "return_24h": 0,
                      "sigma_30d": 0, "ret_7d": 0},
                     _FakeMio(), candidate_size_usd=10.0)
    assert v.ok is False
    assert "exposure_cap_breach" in v.reason


# 5. DD kill
def test_live_variant_gate_rejects_dd_kill(monkeypatch, _iso_db):
    monkeypatch.setenv("SPOT_LIVE_VARIANTS", "contrarian")
    monkeypatch.setenv("SPOT_LIVE_MAX_DD_USD", "10")
    from spot_aggro.governance import live_variant_gate as lvg
    monkeypatch.setattr(lvg, "_current_exposure_usd", lambda: 0.0)
    monkeypatch.setattr(lvg, "_live_session_pnl_usd", lambda: -11.0)
    monkeypatch.setattr(lvg, "_kill_ladder_blocks", lambda: False)
    v = lvg.evaluate({"symbol": "X"}, _FakeMio(), candidate_size_usd=1.0)
    assert v.ok is False
    assert "live_dd_kill" in v.reason


# 6. Kill-ladder L2 blocks
def test_live_variant_gate_rejects_kill_ladder(monkeypatch, _iso_db):
    monkeypatch.setenv("SPOT_LIVE_VARIANTS", "contrarian")
    from spot_aggro.governance import live_variant_gate as lvg
    monkeypatch.setattr(lvg, "_kill_ladder_blocks", lambda: True)
    v = lvg.evaluate({"symbol": "X"}, _FakeMio(), candidate_size_usd=1.0)
    assert v.ok is False
    assert "kill_ladder" in v.reason


# 7. Admit when contrarian passes
def test_live_variant_gate_admits_on_contrarian_pass(monkeypatch, _iso_db):
    monkeypatch.setenv("SPOT_LIVE_VARIANTS", "contrarian")
    monkeypatch.setenv("SPOT_LIVE_MAX_EXPOSURE_USD", "50")
    from spot_aggro.governance import live_variant_gate as lvg
    monkeypatch.setattr(lvg, "_current_exposure_usd", lambda: 0.0)
    monkeypatch.setattr(lvg, "_live_session_pnl_usd", lambda: 0.0)
    monkeypatch.setattr(lvg, "_kill_ladder_blocks", lambda: False)
    # Coin that contrarian admits: low composite + liquid + tight spread.
    coin = {"symbol": "Y-USDT", "spi": 0.0, "funding_z": 2.0,
            "depth_usd": 250_000, "spread_bp": 12,
            "return_24h": 0.0, "sigma_30d": 0.0}
    v = lvg.evaluate(coin, _FakeMio(), candidate_size_usd=1.0)
    assert v.ok is True
    assert v.admitting_variant == "contrarian"


# 8. Build tag + endpoint
def test_phase_ii_build_and_flags():
    import re
    from spot_aggro.api.routes import spot_aggro_build, SERVER_BUILD
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "ii")
    feats = spot_aggro_build().get("features") or {}
    for flag in (
        "live_variant_gate", "deep_value_variant",
        "live_exposure_cap_50usd", "live_dd_kill_10usd",
    ):
        assert feats.get(flag) is True, f"missing {flag}"


def test_endpoint_shape(monkeypatch, _iso_db):
    monkeypatch.setenv("SPOT_LIVE_VARIANTS", "contrarian,deep_value")
    from spot_aggro.api.routes import spot_aggro_live_variant_gate
    body = spot_aggro_live_variant_gate()
    assert body["ok"] is True
    assert body["active"] is True
    assert set(body["enabled_variants"]) == {"contrarian", "deep_value"}
    assert body["max_exposure_usd"] == 50.0
    assert body["max_dd_usd"] == 10.0
