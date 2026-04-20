"""Opportunity Fabric — Sprint 5 tests: Passive liquidity inference."""
from __future__ import annotations

import importlib
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def _iso_liq(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    monkeypatch.delenv("SPOT_LIQ_INFERENCE_GATE", raising=False)
    import spot_aggro.governance.liquidity_inference as liq
    importlib.reload(liq)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS spot_exchange_comparison("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts_ms INTEGER, symbol TEXT, exchange TEXT,"
        " last REAL, bid REAL, ask REAL, spread_bp REAL,"
        " bid_depth_usd REAL, ask_depth_usd REAL, top_depth_usd REAL,"
        " ok INTEGER)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS spot_live_variant_entries("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " variant TEXT, symbol TEXT, status TEXT,"
        " notional_usd REAL, realized_pnl_usd REAL,"
        " slippage_bp REAL, fill_rate REAL,"
        " opened_ts_ms INTEGER, closed_ts_ms INTEGER)"
    )
    con.commit()
    con.close()
    yield db, liq


def _seed_book(db: Path, symbol: str, spread_bp: float,
               bid_d: float, ask_d: float, top_d: float) -> None:
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO spot_exchange_comparison("
        " ts_ms, symbol, exchange, last, bid, ask, spread_bp,"
        " bid_depth_usd, ask_depth_usd, top_depth_usd, ok)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (int(time.time() * 1000), symbol, "OKX", 100.0, 99.9, 100.1,
         spread_bp, bid_d, ask_d, top_d, 1),
    )
    con.commit()
    con.close()


def _seed_own(db: Path, symbol: str, slip: float, fill: float,
              age_min: float = 1.0) -> None:
    ts = int(time.time() * 1000) - int(age_min * 60_000)
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO spot_live_variant_entries("
        " variant, symbol, status, notional_usd,"
        " slippage_bp, fill_rate, closed_ts_ms)"
        " VALUES(?,?,?,?,?,?,?)",
        ("contrarian", symbol, "closed", 25.0, slip, fill, ts),
    )
    con.commit()
    con.close()


def test_empty_db_returns_high_risk_score(_iso_liq):
    _, liq = _iso_liq
    r = liq.reading_for("NEW-USDT")
    # Thin book + unknown slip + no own fills -> score should be high.
    assert r.liquidity_score >= 0.5
    assert r.top_depth_usd is None


def test_deep_liquid_book_scores_low(_iso_liq):
    db, liq = _iso_liq
    _seed_book(db, "BTC-USDT", spread_bp=3.0, bid_d=50_000, ask_d=50_000, top_d=100_000)
    _seed_own(db, "BTC-USDT", slip=2.0, fill=0.99)
    _seed_own(db, "BTC-USDT", slip=3.0, fill=0.99)
    r = liq.reading_for("BTC-USDT")
    assert r.liquidity_score < 0.25
    assert r.expected_slippage_bp is not None


def test_thin_book_raises_score(_iso_liq):
    db, liq = _iso_liq
    _seed_book(db, "ALT-USDT", spread_bp=25.0, bid_d=200, ask_d=200, top_d=400)
    r = liq.reading_for("ALT-USDT")
    assert r.liquidity_score > 0.5
    assert r.thinness_score > 0.9


def test_default_off_never_blocks(_iso_liq):
    db, liq = _iso_liq
    _seed_book(db, "DEAD-USDT", spread_bp=99.0, bid_d=10, ask_d=10, top_d=20)
    blocked, r = liq.should_block("DEAD-USDT")
    assert blocked is False
    # But the reading still reports dangerous conditions.
    assert r.liquidity_score > 0.7


def test_gate_on_blocks_high_score(_iso_liq, monkeypatch):
    db, liq = _iso_liq
    monkeypatch.setenv("SPOT_LIQ_INFERENCE_GATE", "1")
    # Threshold low enough to trigger on any realistically-bad book.
    monkeypatch.setenv("SPOT_LIQ_ABORT_SCORE", "0.7")
    importlib.reload(liq)
    # Truly dangerous book: paper-thin, wide spread, one-sided + bad own flow.
    _seed_book(db, "DEAD-USDT", spread_bp=99.0, bid_d=10, ask_d=200, top_d=20)
    for _ in range(3):
        _seed_own(db, "DEAD-USDT", slip=40.0, fill=0.50)
    blocked, r = liq.should_block("DEAD-USDT")
    assert blocked is True
    assert r.liquidity_score >= liq.SCORE_ABORT_THRESHOLD


def test_own_fill_slippage_leaks_into_expected(_iso_liq):
    db, liq = _iso_liq
    _seed_book(db, "SOL-USDT", spread_bp=5.0, bid_d=20_000, ask_d=20_000, top_d=40_000)
    # Realized slippage on our own fills is much worse than book would suggest.
    for _ in range(5):
        _seed_own(db, "SOL-USDT", slip=25.0, fill=0.90)
    r = liq.reading_for("SOL-USDT")
    # expected_slippage should reflect the worse realized value.
    assert r.expected_slippage_bp >= 25.0 - 1


def test_book_imbalance_computed_correctly(_iso_liq):
    db, liq = _iso_liq
    _seed_book(db, "ETH-USDT", spread_bp=8.0, bid_d=80_000, ask_d=20_000, top_d=100_000)
    r = liq.reading_for("ETH-USDT")
    assert r.book_imbalance is not None
    assert 0.75 < r.book_imbalance < 0.85    # 80k / 100k = 0.8


def test_n_own_fills_24h_counts_within_window(_iso_liq):
    db, liq = _iso_liq
    _seed_book(db, "X-USDT", spread_bp=10.0, bid_d=5_000, ask_d=5_000, top_d=10_000)
    _seed_own(db, "X-USDT", slip=3.0, fill=0.99, age_min=60)      # inside 24h
    _seed_own(db, "X-USDT", slip=3.0, fill=0.99, age_min=60*48)   # outside
    r = liq.reading_for("X-USDT")
    assert r.n_own_fills_24h == 1


def test_regulatory_acknowledgement_present_in_docstring():
    """Sprint 5 contract — the docstring must still contain the
    regulatory acknowledgement. Prevents accidental removal."""
    import spot_aggro.governance.liquidity_inference as liq
    doc = (liq.__doc__ or "").upper()
    assert "ACTIVE PROBING" in doc
    assert "WILL NOT" in doc or "PASSIVE" in doc
    assert "SPOOFING" in doc
