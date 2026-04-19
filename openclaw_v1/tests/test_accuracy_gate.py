"""Tests for the accuracy gate — rolling WR floor and proving period."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from core.accuracy_gate import AccuracyGate, AccuracyGateConfig


def _seed_trades(db_path: str, symbol: str, outcomes: list[float]) -> None:
    """Write a trades table and insert closed trades with given pnl_r values."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trades ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "symbol TEXT, direction INTEGER, size REAL, "
        "entry_ts_ms INTEGER, entry_px REAL, "
        "stop_px REAL, target_px REAL, "
        "exit_ts_ms INTEGER, exit_px REAL, exit_reason TEXT, "
        "pnl_r REAL, pnl_quote REAL, balance_after REAL, status TEXT)"
    )
    for r in outcomes:
        conn.execute(
            "INSERT INTO trades(symbol, status, pnl_r, direction, size, entry_ts_ms, entry_px) "
            "VALUES(?, 'closed', ?, 1, 1.0, 0, 0)",
            (symbol, r),
        )
    conn.commit()
    conn.close()


def test_gate_disabled_always_allows(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_trades(db, "BTC/USDT", [-1] * 30)  # 0% WR
    cfg = AccuracyGateConfig(enabled=False, db_path=db)
    gate = AccuracyGate(cfg)
    decision = gate.evaluate("BTC/USDT")
    assert decision.allow is True
    assert "disabled" in decision.reason


def test_below_floor_vetoes_paper(tmp_path):
    db = str(tmp_path / "t.db")
    # 30 trades, 10 wins = 33% WR — below 75% floor
    outcomes = [2.0] * 10 + [-1.0] * 20
    _seed_trades(db, "BTC/USDT", outcomes)
    cfg = AccuracyGateConfig(
        enabled=True,
        window_size=30,
        floor_pct=0.75,
        min_trades_before_floor=10,
        proving_wins=1,
        db_path=db,
    )
    gate = AccuracyGate(cfg)
    decision = gate.evaluate("BTC/USDT", mode="paper")
    assert decision.allow is False
    assert "floor" in decision.reason
    assert decision.stats.rolling_wr < 0.75


def test_above_floor_allows(tmp_path):
    db = str(tmp_path / "t.db")
    # 30 trades, 24 wins = 80% WR — passes 75% floor
    outcomes = [2.0] * 24 + [-1.0] * 6
    _seed_trades(db, "BTC/USDT", outcomes)
    cfg = AccuracyGateConfig(
        enabled=True,
        window_size=30,
        floor_pct=0.75,
        min_trades_before_floor=10,
        proving_wins=5,
        db_path=db,
    )
    gate = AccuracyGate(cfg)
    decision = gate.evaluate("BTC/USDT", mode="live")
    assert decision.allow is True
    assert decision.stats.rolling_wr >= 0.75
    assert decision.stats.proving_satisfied is True


def test_insufficient_samples_passes_through(tmp_path):
    db = str(tmp_path / "t.db")
    # Only 5 trades; under min_trades_before_floor → no veto regardless of WR
    outcomes = [-1.0] * 5
    _seed_trades(db, "NEW/USDT", outcomes)
    cfg = AccuracyGateConfig(
        enabled=True,
        window_size=30,
        floor_pct=0.75,
        min_trades_before_floor=10,
        proving_wins=1,
        db_path=db,
    )
    gate = AccuracyGate(cfg)
    decision = gate.evaluate("NEW/USDT", mode="paper")
    assert decision.allow is True


def test_live_requires_proving_wins(tmp_path):
    db = str(tmp_path / "t.db")
    # 3 wins, enough for paper if floor passed, but proving_wins=5
    outcomes = [2.0, 2.0, 2.0]
    _seed_trades(db, "ETH/USDT", outcomes)
    cfg = AccuracyGateConfig(
        enabled=True,
        window_size=30,
        floor_pct=0.75,
        min_trades_before_floor=10,
        proving_wins=5,
        db_path=db,
    )
    gate = AccuracyGate(cfg)
    paper_decision = gate.evaluate("ETH/USDT", mode="paper")
    live_decision = gate.evaluate("ETH/USDT", mode="live")
    # Paper passes because window_trades<min_trades_before_floor
    assert paper_decision.allow is True
    # Live is vetoed because total_wins<proving_wins
    assert live_decision.allow is False
    assert "proving" in live_decision.reason.lower() or "lifetime" in live_decision.reason.lower()


def test_missing_db_is_handled_gracefully(tmp_path):
    cfg = AccuracyGateConfig(
        enabled=True,
        db_path=str(tmp_path / "does_not_exist.db"),
    )
    gate = AccuracyGate(cfg)
    decision = gate.evaluate("BTC/USDT", mode="paper")
    # With no DB, we have 0 trades; the gate should still allow (below min_trades_before_floor)
    assert decision.allow is True


def test_cooldown_latches_after_floor_failure(tmp_path):
    db = str(tmp_path / "t.db")
    outcomes = [2.0] * 5 + [-1.0] * 15  # 25% WR over 20 trades
    _seed_trades(db, "BTC/USDT", outcomes)
    cfg = AccuracyGateConfig(
        enabled=True,
        window_size=20,
        floor_pct=0.75,
        min_trades_before_floor=10,
        proving_wins=1,
        db_path=db,
    )
    gate = AccuracyGate(cfg)

    first = gate.evaluate("BTC/USDT", mode="paper")
    assert first.allow is False

    # Second call immediately after should hit the cooldown branch
    second = gate.evaluate("BTC/USDT", mode="paper")
    assert second.allow is False
    assert "cooldown" in second.reason.lower() or "floor" in second.reason.lower()
