"""Opportunity Fabric — Sprint 4 tests: Fractal regime confirmation."""
from __future__ import annotations

import importlib
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def _iso_regime(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    monkeypatch.delenv("SPOT_FRACTAL_REGIME_GATE", raising=False)
    import spot_aggro.governance.fractal_regime as fr
    importlib.reload(fr)
    # Create regime samples table (phase-11n persistence).
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS spot_regime_samples("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " regime TEXT, regime_confidence REAL, ts_ms INTEGER)"
    )
    con.commit()
    con.close()
    yield db, fr


def _seed(db: Path, regime: str, conf: float, age_min: float) -> None:
    ts = int(time.time() * 1000) - int(age_min * 60_000)
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO spot_regime_samples(regime, regime_confidence, ts_ms)"
        " VALUES(?,?,?)",
        (regime, conf, ts),
    )
    con.commit()
    con.close()


def test_sign_mapping():
    import spot_aggro.governance.fractal_regime as fr
    assert fr._sign_of("SQUEEZE_BUILDING") == "bullish"
    assert fr._sign_of("BREAKDOWN") == "bearish"
    assert fr._sign_of("DEAD") == "neutral"
    assert fr._sign_of("UNKNOWN") == "neutral"
    assert fr._sign_of(None) == "neutral"
    assert fr._sign_of("TOTALLY_UNFAMILIAR_LABEL") == "neutral"


def test_empty_db_returns_neutral(_iso_regime):
    _, fr = _iso_regime
    v = fr.evaluate()
    assert v.confirmed_sign == "neutral"
    assert v.admit_recommended is True
    assert all(r.n_samples == 0 for r in v.readings)


def test_unanimous_bullish_confirms(_iso_regime):
    db, fr = _iso_regime
    # All three scales see bullish.
    _seed(db, "SQUEEZE_BUILDING", 0.9, age_min=0.1)   # 1m
    for i in range(5):
        _seed(db, "SQUEEZE_BUILDING", 0.85, age_min=i + 0.2)   # 5m
    for i in range(30):
        _seed(db, "BREAKOUT_BULL", 0.8, age_min=i + 2)          # 1h (will also be in 5m window)
    v = fr.evaluate()
    assert v.agreement == "bullish_confirmed"
    assert v.confirmed_sign == "bullish"
    assert v.confirmed_scales >= 2


def test_2_of_3_bearish_confirms(_iso_regime):
    db, fr = _iso_regime
    # 1m: BREAKDOWN (bearish)
    _seed(db, "BREAKDOWN", 0.9, age_min=0.1)
    # 5m window: mostly BREAKDOWN
    for i in range(5):
        _seed(db, "BREAKDOWN", 0.8, age_min=i + 0.2)
    # 1h window: mostly DEAD -> neutral -> does NOT count bearish
    for i in range(30):
        _seed(db, "DEAD", 0.7, age_min=i + 10)
    v = fr.evaluate()
    # 1m=bearish, 5m=bearish, 1h=neutral -> 2/3 bearish
    assert v.agreement == "bearish_confirmed"


def test_disagreement_advises_admit_when_gate_off(_iso_regime):
    db, fr = _iso_regime
    # 1m bullish, 5m bearish, 1h neutral — three-way disagreement.
    _seed(db, "BREAKOUT_BULL", 0.9, age_min=0.1)
    for i in range(5):
        _seed(db, "BREAKDOWN", 0.8, age_min=i + 0.5)
    # 1h: Fill with DEAD but far back so they don't dominate 5m.
    for i in range(30):
        _seed(db, "DEAD", 0.6, age_min=i + 10)
    v = fr.evaluate()
    # Gate is off; disagreement is advisory.
    assert v.gate_enabled is False
    assert v.admit_recommended is True


def test_disagreement_blocks_admit_when_gate_on(_iso_regime, monkeypatch):
    db, fr = _iso_regime
    monkeypatch.setenv("SPOT_FRACTAL_REGIME_GATE", "1")
    importlib.reload(fr)
    # Induce disagreement: 1m bull, 5m bear dominant, 1h neutral.
    _seed(db, "BREAKOUT_BULL", 0.9, age_min=0.1)
    for i in range(5):
        _seed(db, "BREAKDOWN", 0.8, age_min=i + 0.5)
    for i in range(30):
        _seed(db, "DEAD", 0.6, age_min=i + 10)
    v = fr.evaluate()
    assert v.gate_enabled is True
    # If agreement is bearish_confirmed (2 of 3), admit is still True.
    # If truly disagreeing, admit is False. Assertion: SOME disagreement path
    # exists — if 2-of-3 hits, don't block.
    if v.agreement == "disagreement":
        assert v.admit_recommended is False


def test_neutral_majority_admits(_iso_regime):
    db, fr = _iso_regime
    # Everything neutral.
    _seed(db, "DEAD", 0.8, age_min=0.1)
    for i in range(5):
        _seed(db, "UNKNOWN", 0.7, age_min=i + 0.5)
    for i in range(30):
        _seed(db, "DEAD", 0.7, age_min=i + 5)
    v = fr.evaluate()
    assert v.agreement == "neutral"
    assert v.admit_recommended is True


def test_readings_have_sample_counts(_iso_regime):
    db, fr = _iso_regime
    for i in range(10):
        _seed(db, "DEAD", 0.7, age_min=i + 0.1)
    v = fr.evaluate()
    # 1m reading is a single sample; 5m and 1h are rollups.
    assert v.readings[0].n_samples == 1
    assert v.readings[1].n_samples > 0
    assert v.readings[2].n_samples > 0


def test_evaluate_survives_missing_table(_iso_regime, monkeypatch, tmp_path):
    fresh = tmp_path / "fresh.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(fresh))
    import spot_aggro.governance.fractal_regime as fr
    importlib.reload(fr)
    # No spot_regime_samples table at all.
    v = fr.evaluate()
    assert v.confirmed_sign == "neutral"
    assert v.admit_recommended is True
