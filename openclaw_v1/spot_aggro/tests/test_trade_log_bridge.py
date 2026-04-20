"""Opportunity Fabric — Bridge tests."""
from __future__ import annotations

import importlib
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def _iso_bridge(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    import spot_aggro.governance.trade_log_bridge as br
    importlib.reload(br)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE trade_log("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts_ms INTEGER, symbol TEXT, module TEXT,"
        " action TEXT, side TEXT, notional_usd REAL,"
        " avg_px REAL, fee_usd REAL, pnl_usd REAL,"
        " correlation_id TEXT, payload_json TEXT,"
        " tier TEXT, net_pnl REAL, slippage_usd REAL)"
    )
    con.execute(
        "CREATE TABLE spot_live_variant_entries("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts_ms INTEGER, variant TEXT, symbol TEXT,"
        " notional_usd REAL, authz_id TEXT,"
        " status TEXT DEFAULT 'open',"
        " closed_ts_ms INTEGER, realized_pnl_usd REAL)"
    )
    con.commit()
    con.close()
    br._init_schema()
    yield db, br


def _seed(db: Path, ts_ms: int, symbol: str, action: str, side: str | None,
          notional: float | None, pnl: float | None,
          tier: str = "C", regime: str = "UNKNOWN") -> int:
    payload = f'{{"tier": "{tier}", "entry_regime": "{regime}"}}'
    con = sqlite3.connect(str(db))
    cur = con.execute(
        "INSERT INTO trade_log("
        " ts_ms, symbol, module, action, side,"
        " notional_usd, pnl_usd, correlation_id, payload_json, tier)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ts_ms, symbol, "M1_scalp_C", action, side, notional, pnl,
         f"corr-{ts_ms}", payload, tier),
    )
    tid = cur.lastrowid
    con.commit()
    con.close()
    return tid


def test_variant_inference():
    import spot_aggro.governance.trade_log_bridge as br
    assert br._infer_variant({"tier": "C", "entry_regime": "DEAD"}, "M1") == "contrarian"
    assert br._infer_variant({"tier": "C", "entry_regime": "UNKNOWN"}, "M1") == "contrarian"
    assert br._infer_variant({"tier": "C", "entry_regime": "BREAKOUT_BULL"}, "M1") == "momentum"
    assert br._infer_variant({"tier": "B", "entry_regime": "DEAD"}, "M1") == "momentum"
    assert br._infer_variant({"tier": "A+", "entry_regime": "DEAD"}, "M1") == "momentum"
    assert br._infer_variant({}, "unknown") == "legacy_scalp"


def test_bridge_mirrors_enter_and_exit(_iso_bridge):
    db, br = _iso_bridge
    now = int(time.time() * 1000)
    _seed(db, now, "BTC-USDT", "enter", "buy", 5.0, None)
    _seed(db, now + 10_000, "BTC-USDT", "exit", "sell", 5.0, 0.25)
    r = br.run_once()
    assert r.scanned == 2
    assert r.inserted == 1
    assert r.closed == 1
    # Verify DB state.
    con = sqlite3.connect(str(db))
    row = con.execute(
        "SELECT variant, status, realized_pnl_usd"
        " FROM spot_live_variant_entries WHERE symbol = 'BTC-USDT'"
    ).fetchone()
    con.close()
    assert row[0] == "contrarian"
    assert row[1] == "closed"
    assert abs(row[2] - 0.25) < 0.001


def test_bridge_is_idempotent(_iso_bridge):
    db, br = _iso_bridge
    now = int(time.time() * 1000)
    _seed(db, now, "ETH-USDT", "enter", "buy", 5.0, None)
    r1 = br.run_once()
    r2 = br.run_once()
    assert r1.inserted == 1
    # Second call should see 0 new rows (cursor advanced).
    assert r2.scanned == 0


def test_bridge_preserves_cursor_across_runs(_iso_bridge):
    db, br = _iso_bridge
    now = int(time.time() * 1000)
    _seed(db, now, "X1-USDT", "enter", "buy", 5.0, None)
    br.run_once()
    # Add a new trade AFTER first run.
    _seed(db, now + 60_000, "X2-USDT", "enter", "buy", 5.0, None)
    r = br.run_once()
    assert r.scanned == 1
    assert r.inserted == 1


def test_backfill_all_resets_and_rebuilds(_iso_bridge):
    db, br = _iso_bridge
    now = int(time.time() * 1000)
    for i in range(5):
        _seed(db, now + i * 1000, f"SYM{i}-USDT", "enter", "buy", 5.0, None)
    br.run_once()
    con = sqlite3.connect(str(db))
    before = con.execute(
        "SELECT COUNT(*) FROM spot_live_variant_entries"
    ).fetchone()[0]
    con.close()
    assert before == 5
    # Full backfill wipes mirrored rows, re-inserts all.
    br.backfill_all()
    con = sqlite3.connect(str(db))
    after = con.execute(
        "SELECT COUNT(*) FROM spot_live_variant_entries"
    ).fetchone()[0]
    con.close()
    assert after == 5


def test_exit_without_open_is_skipped(_iso_bridge):
    db, br = _iso_bridge
    now = int(time.time() * 1000)
    _seed(db, now, "ORPHAN-USDT", "exit", "sell", 5.0, 0.10)
    r = br.run_once()
    assert r.closed == 0
    assert r.skipped >= 1


def test_contrarian_vs_momentum_tagging(_iso_bridge):
    db, br = _iso_bridge
    now = int(time.time() * 1000)
    _seed(db, now, "A-USDT", "enter", "buy", 5.0, None,
          tier="C", regime="DEAD")          # -> contrarian
    _seed(db, now + 1000, "B-USDT", "enter", "buy", 5.0, None,
          tier="B", regime="DEAD")          # -> momentum
    _seed(db, now + 2000, "C-USDT", "enter", "buy", 5.0, None,
          tier="C", regime="BREAKOUT_BULL") # -> momentum
    br.run_once()
    con = sqlite3.connect(str(db))
    rows = {
        r[0]: r[1] for r in con.execute(
            "SELECT symbol, variant FROM spot_live_variant_entries"
        )
    }
    con.close()
    assert rows["A-USDT"] == "contrarian"
    assert rows["B-USDT"] == "momentum"
    assert rows["C-USDT"] == "momentum"


def test_cursor_survives_restart(_iso_bridge):
    db, br = _iso_bridge
    now = int(time.time() * 1000)
    _seed(db, now, "X-USDT", "enter", "buy", 5.0, None)
    br.run_once()
    assert br._cursor_ts_ms() >= now
    # Reload module — cursor must still be there.
    importlib.reload(br)
    assert br._cursor_ts_ms() >= now


def test_bridge_survives_missing_trade_log(_iso_bridge, tmp_path, monkeypatch):
    fresh = tmp_path / "fresh.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(fresh))
    import spot_aggro.governance.trade_log_bridge as br
    importlib.reload(br)
    # No trade_log table at all — run_once must not raise.
    r = br.run_once()
    assert r.scanned == 0
