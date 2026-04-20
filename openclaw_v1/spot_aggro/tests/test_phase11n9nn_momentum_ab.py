"""Phase 11n-9-nn — momentum variant + structured A/B + WhatsApp ping.

Validates:
  1. momentum in VARIANT_NAMES; evaluate_all returns 5 decisions.
  2. evaluate_momentum admits on +3% 24h / fz>0 / vol>=1.5 / liquid / spread.
  3. evaluate_momentum rejects on shallow 24h return or negative funding.
  4. live_variant_gate exposes _per_variant_cap_usd + record_variant_entry
     + record_variant_exit + _variant_exposure_usd.
  5. Per-variant cap blocks admit when variant exposure + candidate > cap.
  6. Model registry bootstrap registers momentum version.
  7. dev_ping module exposes ping_task_complete.
  8. Build tag + flags advertised.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]


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
        "spot_aggro.governance.model_registry",
    ):
        mod = importlib.import_module(m)
        importlib.reload(mod)
    yield tmp_path


# 1 — momentum in VARIANT_NAMES + evaluate_all returns 5
def test_momentum_registered():
    from spot_aggro.governance.strategy_variants import (
        VARIANT_NAMES, evaluate_all,
    )
    assert "momentum" in VARIANT_NAMES
    assert len(VARIANT_NAMES) == 5
    ds = evaluate_all({"symbol": "X", "spi": 0.3, "funding_z": 0,
                       "depth_usd": 100_000, "spread_bp": 5,
                       "return_24h": 0.0}, _FakeMio())
    assert len(ds) == 5
    assert any(d.variant == "momentum" for d in ds)


# 2 — momentum admit on confirming signals
def test_momentum_admits_on_all_signals(_iso_db):
    from spot_aggro.governance.strategy_variants import evaluate_momentum
    coin = {
        "return_24h": 0.05,        # +5%
        "funding_z": 1.5,          # long-side squeeze
        "volume_ratio": 2.0,
        "depth_usd": 1_000_000,
        "spread_bp": 5,
        "spi": 0.5, "sigma_30d": 0.0002,
    }
    d = evaluate_momentum(coin, _FakeMio())
    assert d.passed is True, f"expected admit, got: {d}"
    assert "momentum ADMIT" in d.reason


# 3 — momentum rejects shallow 24h
def test_momentum_rejects_shallow_move():
    from spot_aggro.governance.strategy_variants import evaluate_momentum
    coin = {
        "return_24h": 0.01,        # +1% — below 3% floor
        "funding_z": 1.0,
        "volume_ratio": 2.0,
        "depth_usd": 1_000_000,
        "spread_bp": 5,
    }
    d = evaluate_momentum(coin, _FakeMio())
    assert d.passed is False
    assert "upside_confirmed" in d.reason


def test_momentum_rejects_negative_funding():
    from spot_aggro.governance.strategy_variants import evaluate_momentum
    coin = {
        "return_24h": 0.05,
        "funding_z": -0.5,         # wrong side
        "volume_ratio": 2.0,
        "depth_usd": 1_000_000,
        "spread_bp": 5,
    }
    d = evaluate_momentum(coin, _FakeMio())
    assert d.passed is False
    assert "longside_funding" in d.reason


# 4 — live_variant_gate surface
def test_live_variant_gate_ab_surface():
    from spot_aggro.governance import live_variant_gate as lvg
    for name in (
        "_per_variant_cap_usd", "_variant_exposure_usd",
        "record_variant_entry", "record_variant_exit",
    ):
        assert hasattr(lvg, name), f"live_variant_gate missing {name}"


# 5 — per-variant cap blocks admit
def test_per_variant_cap_blocks(_iso_db, monkeypatch):
    monkeypatch.setenv("SPOT_LIVE_VARIANTS", "momentum,deep_value")
    monkeypatch.setenv("SPOT_LIVE_MAX_EXPOSURE_USD", "50")
    monkeypatch.setenv("SPOT_LIVE_PER_VARIANT_CAP_USD", "25")
    from spot_aggro.governance import live_variant_gate as lvg
    monkeypatch.setattr(lvg, "_current_exposure_usd", lambda: 20.0)
    monkeypatch.setattr(lvg, "_live_session_pnl_usd", lambda: 0.0)
    monkeypatch.setattr(lvg, "_kill_ladder_blocks", lambda: False)
    monkeypatch.setattr(lvg, "_variant_exposure_usd",
                        lambda v: 22.0 if v == "momentum" else 0.0)
    # Coin that momentum would admit but would push variant over $25 cap
    coin = {
        "return_24h": 0.05, "funding_z": 1.5, "volume_ratio": 2.0,
        "depth_usd": 1_000_000, "spread_bp": 5,
        "spi": 0.5, "sigma_30d": 0.0002,
    }
    v = lvg.evaluate(coin, _FakeMio(), candidate_size_usd=5.0)
    assert v.ok is False
    assert "per_variant_cap_breach" in v.reason


def test_per_variant_cap_allows_when_under(_iso_db, monkeypatch):
    monkeypatch.setenv("SPOT_LIVE_VARIANTS", "momentum,deep_value")
    monkeypatch.setenv("SPOT_LIVE_PER_VARIANT_CAP_USD", "25")
    from spot_aggro.governance import live_variant_gate as lvg
    monkeypatch.setattr(lvg, "_current_exposure_usd", lambda: 10.0)
    monkeypatch.setattr(lvg, "_live_session_pnl_usd", lambda: 0.0)
    monkeypatch.setattr(lvg, "_kill_ladder_blocks", lambda: False)
    monkeypatch.setattr(lvg, "_variant_exposure_usd",
                        lambda v: 10.0 if v == "momentum" else 0.0)
    # Use stronger momentum signal so score clears meta-gate 0.60 floor.
    # Phase-oo meta-gate requires variant_score >= 0.60 + regime_weight >= 0.50
    # + net_expectancy > 10bp. Big 24h return + high funding_z + high volume.
    coin = {
        "return_24h": 0.12, "funding_z": 2.5, "volume_ratio": 3.0,
        "depth_usd": 2_000_000, "spread_bp": 3,
        "spi": 0.5, "sigma_30d": 0.00010,  # calm regime for stable weight
    }
    v = lvg.evaluate(coin, _FakeMio(), candidate_size_usd=5.0)
    assert v.ok is True, f"expected admit, got: {v.reason[:200]}"
    assert v.admitting_variant == "momentum"


# 6 — model registry knows momentum
def test_model_registry_momentum(_iso_db):
    from spot_aggro.governance.model_registry import (
        bootstrap_self_register, current_version,
    )
    recs = bootstrap_self_register()
    ids = {r.model_id for r in recs}
    assert "momentum" in ids
    assert current_version("momentum").startswith("v")


# 7 — dev_ping surface
def test_dev_ping_surface():
    from spot_aggro.ops.notifications import dev_ping
    assert hasattr(dev_ping, "ping_task_complete")
    assert hasattr(dev_ping, "_configured")
    # Not configured in test env.
    r = dev_ping.ping_task_complete("test", "test-body")
    assert r.status in ("not_configured", "delivered",
                        "http_error:URLError",
                        "rejected_by_callmebot:http200")


# 8 — build tag + flags
def test_phase_nn_build_and_flags():
    import re
    from spot_aggro.api.routes import spot_aggro_build, SERVER_BUILD
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "nn"), SERVER_BUILD
    feats = spot_aggro_build().get("features") or {}
    for flag in (
        "momentum_variant", "structured_ab_per_variant_cap",
        "dev_ping_whatsapp",
    ):
        assert feats.get(flag) is True, f"missing: {flag}"
