"""Opportunity Fabric — Sprint 3 tests: Execution SLO gate."""
from __future__ import annotations

import importlib
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def _iso_slo(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    monkeypatch.setenv("SPOT_EXEC_SLO_SLIPPAGE_BP", "10")
    monkeypatch.setenv("SPOT_EXEC_SLO_FILL_RATE", "0.95")
    # default-off by default — individual tests flip it.
    monkeypatch.delenv("SPOT_EXEC_SLO_GATE", raising=False)
    import spot_aggro.governance.execution_slo as slo
    importlib.reload(slo)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS spot_live_variant_entries("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " variant TEXT, symbol TEXT, status TEXT,"
        " notional_usd REAL, realized_pnl_usd REAL,"
        " opened_ts_ms INTEGER, closed_ts_ms INTEGER)"
    )
    con.commit()
    con.close()
    slo._ensure_slo_columns()
    yield db, slo


def _seed(db: Path, variant: str, slippage_bp: float, fill_rate: float) -> None:
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO spot_live_variant_entries("
        " variant, status, notional_usd, slippage_bp, fill_rate, closed_ts_ms)"
        " VALUES(?,?,?,?,?,?)",
        (variant, "closed", 25.0, slippage_bp, fill_rate, int(time.time() * 1000)),
    )
    con.commit()
    con.close()


def test_default_off_passes_verdict_through(_iso_slo):
    _, slo = _iso_slo
    assert slo.gate_enabled() is False
    up = slo.upgrade_verdict("promote", "Wilson-lower > 0", "contrarian")
    assert up.upgraded_verdict == "promote"
    assert "SLO gate off" in up.upgraded_reason


def test_gate_on_promotes_full_when_slo_green(_iso_slo, monkeypatch):
    db, slo = _iso_slo
    monkeypatch.setenv("SPOT_EXEC_SLO_GATE", "1")
    importlib.reload(slo)
    # Good execution: 5bp slip, 98% fill.
    for _ in range(10):
        _seed(db, "contrarian", slippage_bp=5.0, fill_rate=0.98)
    up = slo.upgrade_verdict("promote", "Wilson-lower > 0", "contrarian")
    assert up.upgraded_verdict == "promote_full"
    assert "SLO GREEN" in up.upgraded_reason


def test_gate_on_promotes_statistical_when_slippage_too_high(_iso_slo, monkeypatch):
    db, slo = _iso_slo
    monkeypatch.setenv("SPOT_EXEC_SLO_GATE", "1")
    importlib.reload(slo)
    # Bad slippage: 15bp (> 10 SLO).
    for _ in range(10):
        _seed(db, "contrarian", slippage_bp=15.0, fill_rate=0.98)
    up = slo.upgrade_verdict("promote", "Wilson-lower > 0", "contrarian")
    assert up.upgraded_verdict == "promote_statistical"
    assert "SLO FAIL" in up.upgraded_reason


def test_gate_on_promotes_statistical_when_fill_rate_too_low(_iso_slo, monkeypatch):
    db, slo = _iso_slo
    monkeypatch.setenv("SPOT_EXEC_SLO_GATE", "1")
    importlib.reload(slo)
    # Fill rate 92% < 95%.
    for _ in range(10):
        _seed(db, "contrarian", slippage_bp=5.0, fill_rate=0.92)
    up = slo.upgrade_verdict("promote", "Wilson-lower > 0", "contrarian")
    assert up.upgraded_verdict == "promote_statistical"
    assert "fill_rate" in up.upgraded_reason


def test_non_promote_verdicts_never_change(_iso_slo, monkeypatch):
    db, slo = _iso_slo
    monkeypatch.setenv("SPOT_EXEC_SLO_GATE", "1")
    importlib.reload(slo)
    for verdict in ("racing", "insufficient", "permanent_disable", "disabled"):
        up = slo.upgrade_verdict(verdict, "stub reason", "contrarian")
        assert up.upgraded_verdict == verdict


def test_metrics_returns_none_with_no_data(_iso_slo):
    _, slo = _iso_slo
    m = slo.metrics_for("contrarian")
    assert m.n_exits_with_metrics == 0
    assert m.slippage_bp_mean is None
    assert m.slo_all_pass is False


def test_metrics_computes_mean_and_p95(_iso_slo):
    db, slo = _iso_slo
    # 20 rows with known slippages: mean = (1+2+...+20)/20 = 10.5
    for i in range(1, 21):
        _seed(db, "deep_value", slippage_bp=float(i), fill_rate=0.99)
    m = slo.metrics_for("deep_value")
    assert m.n_exits_with_metrics == 20
    assert abs(m.slippage_bp_mean - 10.5) < 0.5
    assert m.slippage_bp_p95 >= 18  # p95 over 1..20 lands around 19-20


def test_metrics_safe_when_column_missing(_iso_slo, monkeypatch, tmp_path):
    """If the parent table lacks slippage/fill columns (fresh install),
    metrics_for() must not raise."""
    fresh_db = tmp_path / "fresh.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(fresh_db))
    import spot_aggro.governance.execution_slo as slo
    importlib.reload(slo)
    # Don't create the parent table at all.
    m = slo.metrics_for("contrarian")
    assert m.n_exits_with_metrics == 0


def test_ensure_columns_is_idempotent(_iso_slo):
    _, slo = _iso_slo
    slo._ensure_slo_columns()
    slo._ensure_slo_columns()
    slo._ensure_slo_columns()
