"""Unit tests for the SQLite trade/decision logger."""

import os
import tempfile

from core.persistence import TradeLogger
from strategies.decision_engine import DecisionInputs, DecisionOutputs


def _fake_decision_pair():
    inp = DecisionInputs(
        NewsSent=0.2, SocialSent=0.1, FlowSent=0.3,
        Freshness=0.9, Coverage=0.9,
        Samples=100.0, Regime=0.6, ShrinkN=50.0, BaseWR=0.5,
        D5=0.5, D15=0.4, D60=0.3, D240=0.2,
        DecisionPct=75.0, ConfidencePct=70.0,
        EventRiskScore=0.2, DriftScore=0.15,
        EntryPx=100.0, StopPx=98.5, TargetPx=103.0,
        FeeR=0.02, SlipR=0.02,
    )
    out = DecisionOutputs(
        RawSent=0.2, SentQuality=0.9, SentimentPct=60.0,
        RawTrend=0.6, AdaptiveTrendPct=70.0,
        MTFConflict=0.1, MTFConfirmPct=65.0,
        CommercialPct=70.0, RiskPenalty=0.0, ConfPlusPct=72.0,
        PWinPct=58.0, RRTrue=2.0, CostR=0.04, EV_R=0.12, EdgePct=58.0,
        BaseScore=68.0, HardPenalty=0.0, ScoreTotal=68.0,
        TerminalAction="EXECUTE",
    )
    return inp, out


def test_logger_creates_schema():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "test.db")
        logger = TradeLogger(path)
        cursor = logger.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        assert "decisions" in tables
        assert "trades" in tables
        logger.close()


def test_logger_creates_nested_parent_dir():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "nested", "dir", "test.db")
        logger = TradeLogger(path)
        assert os.path.exists(path)
        logger.close()


def test_logger_logs_decision():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "test.db")
        logger = TradeLogger(path)
        inp, out = _fake_decision_pair()
        row_id = logger.log_decision("BTC/USDT", inp, out)
        assert row_id == 1

        cursor = logger.conn.execute(
            "SELECT symbol, action, score_total, pwin_pct FROM decisions"
        )
        row = cursor.fetchone()
        assert row[0] == "BTC/USDT"
        assert row[1] == "EXECUTE"
        assert row[2] == 68.0
        assert row[3] == 58.0
        logger.close()


def test_logger_logs_trade_open_and_close():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "test.db")
        logger = TradeLogger(path)

        entry_ts_ms = 1_700_000_000_000
        trade_id = logger.log_trade_open(
            "BTC/USDT", 1, 0.5, entry_ts_ms, 100.0, 98.5, 103.0
        )
        assert trade_id == 1

        cursor = logger.conn.execute(
            "SELECT status FROM trades WHERE id = ?", (trade_id,)
        )
        assert cursor.fetchone()[0] == "open"

        closed = {
            "symbol": "BTC/USDT",
            "direction": 1,
            "size": 0.5,
            "entry_ts_ms": entry_ts_ms,
            "entry_px": 100.0,
            "stop_px": 98.5,
            "target_px": 103.0,
            "exit_ts_ms": entry_ts_ms + 60_000,
            "exit_px": 103.0,
            "exit_reason": "target",
            "pnl_r": 2.0,
            "pnl_quote": 1.5,
            "balance_after": 10_001.5,
        }
        logger.log_trade_close(closed)

        cursor = logger.conn.execute(
            "SELECT status, pnl_r, exit_reason, balance_after FROM trades WHERE id = ?",
            (trade_id,),
        )
        row = cursor.fetchone()
        assert row == ("closed", 2.0, "target", 10_001.5)
        logger.close()


def test_logger_multiple_open_trades():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "test.db")
        logger = TradeLogger(path)

        logger.log_trade_open("BTC/USDT", 1, 0.5, 1, 100.0, 99.0, 102.0)
        logger.log_trade_open("ETH/USDT", -1, 1.0, 2, 200.0, 202.0, 196.0)

        cursor = logger.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'open'"
        )
        assert cursor.fetchone()[0] == 2
        logger.close()
