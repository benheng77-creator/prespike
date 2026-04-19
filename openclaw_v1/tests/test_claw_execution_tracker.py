"""
Claw execution tracker tests — idempotency, partials, retries, state machine.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from claw import execution_tracker as tracker  # noqa: E402
from claw.db import init_claw_schema  # noqa: E402
from claw.ingest import record_bot_decision  # noqa: E402


@pytest.fixture()
def tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "claw_exec.db")
        monkeypatch.setenv("CLAW_DB_PATH", path)
        init_claw_schema(path)
        yield path


def _record_bot(tmp_db):
    return record_bot_decision(
        strategy_id="decision_engine",
        payload={"PWinPct": 60.0, "ScoreTotal": 80.0, "symbol": "BTCUSDT"},
        ingest_source="unit-test",
        symbol="BTCUSDT",
    )


def test_begin_execution_creates_queued_row(tmp_db):
    bot = _record_bot(tmp_db)
    res = tracker.begin_execution(
        strategy_id="decision_engine",
        bot_decision_id=bot["id"],
        symbol="BTCUSDT", side="BUY",
        requested_qty=0.01, requested_px=50_000.0,
        exchange="binance",
    )
    assert res["state"] == "queued"
    assert res["deduplicated"] is False
    row = tracker.get_execution(res["id"])
    assert row["state"] == "queued"
    assert row["bot_decision_id"] == bot["id"]


def test_begin_execution_is_idempotent(tmp_db):
    bot = _record_bot(tmp_db)
    key = tracker.build_idempotency_key(
        strategy_id="decision_engine", symbol="BTCUSDT",
        side="BUY", requested_qty=0.01, minute_bucket_ts=123,
        bot_decision_id=bot["id"],
    )
    a = tracker.begin_execution(
        strategy_id="decision_engine", bot_decision_id=bot["id"],
        symbol="BTCUSDT", side="BUY", requested_qty=0.01,
        idempotency_key=key,
    )
    b = tracker.begin_execution(
        strategy_id="decision_engine", bot_decision_id=bot["id"],
        symbol="BTCUSDT", side="BUY", requested_qty=0.01,
        idempotency_key=key,
    )
    assert a["id"] == b["id"]
    assert b["deduplicated"] is True


def test_full_lifecycle_state_transitions(tmp_db):
    res = tracker.begin_execution(
        strategy_id="decision_engine", bot_decision_id=None,
        symbol="BTCUSDT", side="BUY",
        requested_qty=0.01, requested_px=50_000.0,
    )
    tracker.mark_submitted(res["id"], exchange_order_id="xid-1")
    tracker.mark_ack(res["id"], channel="ws")
    tracker.record_partial(res["id"], filled_qty=0.003, avg_fill_px=50_010.0)
    tracker.record_partial(res["id"], filled_qty=0.007, avg_fill_px=50_020.0)
    tracker.mark_filled(res["id"], filled_qty=0.01, avg_fill_px=50_015.0)

    row = tracker.get_execution(res["id"])
    assert row["state"] == "filled"
    assert row["filled_qty"] == pytest.approx(0.01)
    assert row["slippage_bps"] is not None

    events = [e["kind"] for e in tracker.list_events(res["id"])]
    assert events[0] == "queued"
    assert "submitted" in events
    assert "ack_ws" in events
    assert events.count("partial") == 2
    assert events[-1] == "filled"


def test_partials_never_overwrite(tmp_db):
    res = tracker.begin_execution(
        strategy_id="decision_engine", bot_decision_id=None,
        symbol="BTCUSDT", side="BUY", requested_qty=0.01,
    )
    tracker.mark_submitted(res["id"])
    tracker.record_partial(res["id"], filled_qty=0.005, avg_fill_px=50_000.0)
    # "late" event claims less qty than we already saw — ignored (max wins)
    tracker.record_partial(res["id"], filled_qty=0.003, avg_fill_px=49_000.0)
    row = tracker.get_execution(res["id"])
    assert row["filled_qty"] == pytest.approx(0.005)


def test_rejected_is_terminal(tmp_db):
    res = tracker.begin_execution(
        strategy_id="decision_engine", bot_decision_id=None,
        symbol="BTCUSDT", side="BUY", requested_qty=0.01,
    )
    tracker.mark_rejected(res["id"], reason="INSUFFICIENT_FUNDS")
    row = tracker.get_execution(res["id"])
    assert row["state"] == "rejected"
    assert row["rejected_reason"] == "INSUFFICIENT_FUNDS"


def test_retries_increment_without_changing_state(tmp_db):
    res = tracker.begin_execution(
        strategy_id="decision_engine", bot_decision_id=None,
        symbol="BTCUSDT", side="BUY", requested_qty=0.01,
    )
    tracker.mark_submitted(res["id"])
    tracker.record_retry(res["id"], reason="RATE_LIMIT")
    tracker.record_retry(res["id"], reason="NETWORK")
    row = tracker.get_execution(res["id"])
    assert row["state"] == "submitted"
    assert row["retries"] == 2


def test_list_open_excludes_terminal(tmp_db):
    a = tracker.begin_execution(strategy_id="s", symbol="BTCUSDT",
                                side="BUY", requested_qty=0.01)
    b = tracker.begin_execution(strategy_id="s", symbol="ETHUSDT",
                                side="SELL", requested_qty=0.1)
    tracker.mark_submitted(b["id"])
    tracker.mark_filled(b["id"], filled_qty=0.1, avg_fill_px=3000.0)
    open_rows = tracker.list_open_executions()
    ids = {r["id"] for r in open_rows}
    assert a["id"] in ids
    assert b["id"] not in ids


def test_idempotency_key_minute_bucket_distinct(tmp_db):
    # Different minute buckets → different keys → different rows
    k1 = tracker.build_idempotency_key(
        strategy_id="s", symbol="BTCUSDT", side="BUY",
        requested_qty=0.01, minute_bucket_ts=100,
    )
    k2 = tracker.build_idempotency_key(
        strategy_id="s", symbol="BTCUSDT", side="BUY",
        requested_qty=0.01, minute_bucket_ts=200,
    )
    assert k1 != k2
