"""
Claw reconciler tests — drift detection, startup recovery, no bot mutation.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from claw import execution_tracker as tracker  # noqa: E402
from claw import reconciler  # noqa: E402
from claw.db import init_claw_schema  # noqa: E402
from claw.incidents import list_incidents  # noqa: E402
from claw.ingest import record_bot_decision  # noqa: E402


@pytest.fixture()
def tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "claw_recon.db")
        monkeypatch.setenv("CLAW_DB_PATH", path)
        init_claw_schema(path)
        yield path


# ---------------------------------------------------------------------------
# Pure resolver
# ---------------------------------------------------------------------------

def test_reconcile_row_filled_on_exchange():
    row = {"id": 1, "state": "submitted"}
    res = reconciler.reconcile_row(row, {"state": "filled", "filled_qty": 0.01, "avg_fill_px": 50_000})
    assert res.action == "filled"


def test_reconcile_row_canceled_on_exchange():
    row = {"id": 1, "state": "submitted"}
    res = reconciler.reconcile_row(row, {"state": "canceled"})
    assert res.action == "canceled"


def test_reconcile_row_rejected_on_exchange():
    row = {"id": 1, "state": "submitted"}
    res = reconciler.reconcile_row(row, {"state": "rejected", "reason": "RISK_LIMIT"})
    assert res.action == "rejected"
    assert "RISK_LIMIT" in res.note


def test_reconcile_row_still_open_no_op():
    row = {"id": 1, "state": "submitted"}
    res = reconciler.reconcile_row(row, {"state": "open"})
    assert res.action == "no_op"


def test_reconcile_row_missing_raises_incident_classification():
    row = {"id": 1, "state": "submitted"}
    res = reconciler.reconcile_row(row, None)
    assert res.action == "incident"
    assert "missing" in res.note.lower()


# ---------------------------------------------------------------------------
# Applied resolution + startup recovery
# ---------------------------------------------------------------------------

def test_reconcile_open_executions_applies_fills(tmp_db):
    tracker.begin_execution(strategy_id="s", symbol="BTCUSDT",
                            side="BUY", requested_qty=0.01, requested_px=50_000.0)
    tracker.begin_execution(strategy_id="s", symbol="ETHUSDT",
                            side="SELL", requested_qty=0.1, requested_px=3000.0)

    def fetch(row):
        if row["symbol"] == "BTCUSDT":
            return {"state": "filled", "filled_qty": 0.01, "avg_fill_px": 50_050.0}
        return {"state": "open"}

    summary = reconciler.reconcile_open_executions(fetch)
    assert summary.reconciled == 1
    assert summary.still_open == 1
    rows = tracker.list_executions(state="reconciled")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"


def test_reconciler_opens_incident_on_missing(tmp_db):
    tracker.begin_execution(strategy_id="s", symbol="BTCUSDT",
                            side="BUY", requested_qty=0.01)
    summary = reconciler.reconcile_open_executions(lambda _row: None)
    assert summary.incidents == 1
    assert len(list_incidents(unresolved_only=True)) == 1


def test_reconciler_never_touches_bot_truth(tmp_db):
    # Record a bot decision; reconcile; ensure bot_decisions_immutable is unchanged.
    bot = record_bot_decision(
        strategy_id="decision_engine",
        payload={"PWinPct": 60.0, "ScoreTotal": 80.0},
        ingest_source="unit-test",
    )
    tracker.begin_execution(strategy_id="s", symbol="BTCUSDT",
                            side="BUY", requested_qty=0.01, bot_decision_id=bot["id"])
    reconciler.reconcile_open_executions(
        lambda _r: {"state": "filled", "filled_qty": 0.01, "avg_fill_px": 50_000.0}
    )
    con = sqlite3.connect(tmp_db)
    try:
        row = con.execute(
            "SELECT payload_sha256 FROM bot_decisions_immutable WHERE id = ?",
            (bot["id"],),
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    assert row[0] == bot["payload_sha256"]


def test_startup_recover_returns_summary(tmp_db):
    tracker.begin_execution(strategy_id="s", symbol="BTCUSDT",
                            side="BUY", requested_qty=0.01)
    summary = reconciler.startup_recover(
        lambda _r: {"state": "filled", "filled_qty": 0.01, "avg_fill_px": 50_000.0}
    )
    assert summary.reconciled == 1
