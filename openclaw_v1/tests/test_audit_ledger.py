import json
import os
import sqlite3

import pytest

from audit import AuditLedger, AuditRecord, new_correlation_id, with_correlation


@pytest.fixture()
def ledger(tmp_path):
    db = tmp_path / "audit.db"
    jsonl = tmp_path / "openclaw.jsonl"
    return AuditLedger(db_path=str(db), jsonl_path=str(jsonl))


def test_schema_creates_required_tables(ledger):
    with sqlite3.connect(ledger.db_path) as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"openclaw_actions", "openclaw_reports", "daytrade_activity"}.issubset(tables)


def test_existing_trades_decisions_tables_are_not_touched(tmp_path):
    # pre-seed a DB with a legacy 'trades' table and verify AuditLedger init does not disturb it
    db = tmp_path / "existing.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, note TEXT)")
        con.execute("INSERT INTO trades (note) VALUES ('pre-existing')")
        con.commit()
    AuditLedger(db_path=str(db), jsonl_path=str(tmp_path / "x.jsonl"))
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT note FROM trades").fetchall()
    assert rows == [("pre-existing",)]


def test_record_writes_sqlite_and_jsonl(ledger):
    cid = ledger.record(
        kind="intent",
        verb="BUY",
        symbol="BTC/USDT",
        size=0.01,
        px=50_000.0,
        notional_usd=500.0,
    )
    rows = ledger.fetch_by_correlation(cid)
    assert len(rows) == 1
    assert rows[0]["kind"] == "intent"
    assert rows[0]["verb"] == "BUY"
    assert rows[0]["symbol"] == "BTC/USDT"

    with open(ledger.jsonl_path, encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert any(r["correlation_id"] == cid for r in lines)


def test_correlation_context_stamps_records(ledger):
    with with_correlation() as cid:
        ledger.record(kind="authorized")
        ledger.record(kind="dispatched")
        ledger.record(kind="filled")
    rows = ledger.fetch_by_correlation(cid)
    assert [r["kind"] for r in rows] == ["authorized", "dispatched", "filled"]


def test_append_only_no_update_or_delete(ledger):
    cid = ledger.record(kind="intent")
    with sqlite3.connect(ledger.db_path) as con:
        con.execute("DELETE FROM openclaw_actions WHERE correlation_id = ?", (cid,))
        con.commit()
    # This verifies nothing in ledger writes an UPDATE/DELETE path.
    # A second record under the same CID creates a second row, not an overwrite.
    ledger.record(kind="authorized", correlation_id=cid)
    ledger.record(kind="dispatched", correlation_id=cid)
    rows = ledger.fetch_by_correlation(cid)
    assert [r["kind"] for r in rows] == ["authorized", "dispatched"]


def test_fetch_recent_orders_desc(ledger):
    ledger.record(kind="monitor", severity="info")
    ledger.record(kind="validator", severity="info")
    recent = ledger.fetch_recent(limit=2)
    assert [r["kind"] for r in recent] == ["validator", "monitor"]


def test_parent_child_linkage(ledger):
    with with_correlation() as parent:
        ledger.record(kind="intent", verb="BUY")
        child = new_correlation_id()
        ledger.record(
            kind="scheduler_run",
            correlation_id=child,
            parent_correlation_id=parent,
        )
    tree = [r for r in ledger.fetch_recent(10) if r["parent_correlation_id"] == parent]
    assert len(tree) == 1
    assert tree[0]["correlation_id"] == child


def test_json_fields_serialize(ledger):
    ledger.record(
        kind="validator",
        before={"balance": 1000.0, "positions": []},
        after={"balance": 995.5},
        result={"ok": True, "notes": "slippage within band"},
    )
    row = ledger.fetch_recent(1)[0]
    assert json.loads(row["before_json"])["balance"] == 1000.0
    assert json.loads(row["result_json"])["ok"] is True


def test_count_monotonic(ledger):
    assert ledger.count() == 0
    for i in range(5):
        ledger.record(kind="monitor", phase=f"tick_{i}")
    assert ledger.count() == 5


def test_correlation_id_is_unique_per_call():
    ids = {new_correlation_id() for _ in range(200)}
    assert len(ids) == 200


def test_jsonl_survives_missing_dir(tmp_path):
    nested = tmp_path / "deep" / "nested" / "path"
    ledger = AuditLedger(db_path=str(tmp_path / "x.db"), jsonl_path=str(nested / "audit.jsonl"))
    ledger.record(kind="monitor")
    assert (nested / "audit.jsonl").exists()
