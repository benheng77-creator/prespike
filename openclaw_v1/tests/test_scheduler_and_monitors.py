import asyncio
import time
from pathlib import Path

import pytest

from audit import AuditLedger
from monitors.balance_monitor import BalanceMonitor
from monitors.data_freshness import DataFreshnessMonitor
from monitors.heartbeat import HeartbeatMonitor
from monitors.pnl_consistency import PnLConsistencyMonitor
from monitors.txn_monitor import TxnMonitor
from scheduler import Scheduler


@pytest.fixture()
def ledger(tmp_path):
    return AuditLedger(db_path=str(tmp_path / "a.db"), jsonl_path=str(tmp_path / "a.jsonl"))


# ---------- monitors ----------

def test_heartbeat_ok_after_tick(tmp_path, ledger):
    path = tmp_path / "hb"
    m = HeartbeatMonitor(heartbeat_path=str(path), max_age_s=60, ledger=ledger)
    m.tick()
    r = asyncio.run(m())
    assert r.ok is True
    assert r.detail["age_s"] <= 60


def test_heartbeat_stale(tmp_path, ledger):
    path = tmp_path / "hb"
    path.write_text(str(int((time.time() - 120) * 1000)))
    r = asyncio.run(HeartbeatMonitor(heartbeat_path=str(path), max_age_s=30, ledger=ledger)())
    assert r.ok is False
    assert r.detail["age_s"] >= 30


def test_data_freshness_ok_and_stale(ledger):
    fresh = DataFreshnessMonitor(last_ts_ms_getter=lambda: int(time.time() * 1000),
                                 max_age_s=30, ledger=ledger)
    r = asyncio.run(fresh())
    assert r.ok is True

    stale = DataFreshnessMonitor(last_ts_ms_getter=lambda: int((time.time() - 120) * 1000),
                                 max_age_s=30, ledger=ledger)
    r = asyncio.run(stale())
    assert r.ok is False


def test_balance_monitor_within_tolerance(ledger):
    m = BalanceMonitor(expected_balance_fn=lambda: 1000.0,
                       observed_balance_fn=lambda: 1004.0,
                       tolerance_frac=0.01, ledger=ledger)
    r = asyncio.run(m())
    assert r.ok is True


def test_balance_monitor_drift(ledger):
    m = BalanceMonitor(expected_balance_fn=lambda: 1000.0,
                       observed_balance_fn=lambda: 950.0,
                       tolerance_frac=0.01, ledger=ledger)
    r = asyncio.run(m())
    assert r.ok is False


def test_txn_monitor_flags_stuck(ledger):
    cid = ledger.record(kind="intent", symbol="BTC/USDT")
    # no finalized row: should flag
    m = TxnMonitor(ledger=ledger)
    r = asyncio.run(m())
    assert r.ok is False
    assert cid in r.detail["stuck_correlation_ids"]


def test_txn_monitor_clean_when_finalized(ledger):
    from audit.correlation import with_correlation
    with with_correlation() as cid:
        ledger.record(kind="intent")
        ledger.record(kind="finalized")
    m = TxnMonitor(ledger=ledger)
    r = asyncio.run(m())
    assert r.ok is True


class _FakePortfolioAdapter:
    def __init__(self, bal, start, closed):
        self._snap = {"balance": bal, "starting_balance": start,
                      "has_open_position": False, "closed_trades_count": len(closed)}
        self._closed = closed

    def snapshot(self): return dict(self._snap)
    def recent_closed(self, limit=1000): return list(self._closed)


def test_pnl_consistency_pass(ledger):
    pfa = _FakePortfolioAdapter(1003.0, 1000.0, [{"pnl": 2.0}, {"pnl": 1.0}])
    r = asyncio.run(PnLConsistencyMonitor(portfolio_adapter=pfa, ledger=ledger)())
    assert r.ok is True


def test_pnl_consistency_fail(ledger):
    pfa = _FakePortfolioAdapter(1010.0, 1000.0, [{"pnl": 2.0}])
    r = asyncio.run(PnLConsistencyMonitor(portfolio_adapter=pfa, ledger=ledger)())
    assert r.ok is False


# ---------- scheduler ----------

def test_scheduler_registers_and_runs_interval_job(ledger):
    s = Scheduler(ledger=ledger, tick_s=0.01)
    counter = {"n": 0}

    async def handler():
        counter["n"] += 1

    s.register("tick", handler, interval_s=0)  # fire as fast as possible

    async def drive():
        await s.start()
        await asyncio.sleep(0.05)
        await s.stop()

    asyncio.run(drive())
    assert counter["n"] >= 1
    assert s.history(5)[-1]["name"] == "tick"


def test_scheduler_run_now(ledger):
    s = Scheduler(ledger=ledger)
    async def h(): return 42
    s.register("once", h, interval_s=60)
    r = asyncio.run(s.run_now("once"))
    assert r.ok is True
    assert r.result == 42


def test_scheduler_records_to_ledger(ledger):
    s = Scheduler(ledger=ledger)
    async def h(): return None
    s.register("probe", h, interval_s=60)
    asyncio.run(s.run_now("probe"))
    rows = ledger.fetch_recent(10)
    assert any(r["kind"] == "scheduler_run" and r["phase"] == "probe" for r in rows)
