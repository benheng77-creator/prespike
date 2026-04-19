"""
APEX-Ω persistence — SQLite store for open pairs, peak equity, kill state, costs.

Every mutation is written synchronously so a crash leaves the DB consistent.
Boot reconstructs full state via `load_on_boot()`.

Schema is isolated from legacy claw tables by apex_* prefix; shares the same
SQLite file for audit continuity.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterator, Optional


def _db_path() -> str:
    return (
        os.environ.get("CLAW_DB_PATH")
        or os.environ.get("TRADE_DB_PATH")
        or "trades.db"
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS apex_open_pairs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL UNIQUE,
    module          TEXT NOT NULL,            -- M1 funding | M2 statarb | M3 tri | M4 liq
    side_perp       TEXT NOT NULL,            -- buy | sell
    side_spot       TEXT NOT NULL,            -- buy | sell
    notional_usd    REAL NOT NULL,
    entry_funding   REAL,
    entry_ts_ms     INTEGER NOT NULL,
    updated_ts_ms   INTEGER NOT NULL,
    consensus       REAL NOT NULL,
    conflict        REAL NOT NULL,
    metadata_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_apex_open_pairs_module ON apex_open_pairs(module);

CREATE TABLE IF NOT EXISTS apex_equity_marks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    equity_usd      REAL NOT NULL,
    peak_usd        REAL NOT NULL,
    drawdown_pct    REAL NOT NULL,
    positions_open  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_apex_eq_ts ON apex_equity_marks(ts_ms DESC);

CREATE TABLE IF NOT EXISTS apex_kill_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    drawdown_pct    REAL,
    equity_usd      REAL,
    peak_usd        REAL,
    unlocked_ts_ms  INTEGER,
    unlocked_by     TEXT,
    unlock_reason   TEXT
);

CREATE TABLE IF NOT EXISTS apex_trade_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    module          TEXT NOT NULL,
    action          TEXT NOT NULL,            -- enter | exit | reject | rebalance
    side            TEXT,
    notional_usd    REAL,
    avg_px          REAL,
    fee_usd         REAL,
    pnl_usd         REAL,
    correlation_id  TEXT,
    payload_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_apex_trade_ts ON apex_trade_log(ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_apex_trade_sym ON apex_trade_log(symbol, ts_ms DESC);

CREATE TABLE IF NOT EXISTS apex_llm_cost (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    symbol          TEXT,
    role            TEXT,
    provider        TEXT,
    model           TEXT,
    cost_usd        REAL,
    latency_ms      INTEGER,
    ok              INTEGER,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_apex_llm_ts ON apex_llm_cost(ts_ms DESC);

CREATE TABLE IF NOT EXISTS apex_consensus_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    consensus_score REAL NOT NULL,
    conflict_score  REAL NOT NULL,
    vetoed          INTEGER NOT NULL,
    members_called  INTEGER,
    kl_stop_at      INTEGER,
    payload_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_apex_consensus_ts ON apex_consensus_log(ts_ms DESC);
"""


_lock = threading.Lock()
_initialized = False


_wal_enabled = False


def _connect() -> sqlite3.Connection:
    """Return a new SQLite connection. First call sets WAL mode so concurrent
    readers (API, dashboard) don't block while the engine writes."""
    global _wal_enabled
    con = sqlite3.connect(_db_path(), timeout=10.0, isolation_level="DEFERRED")
    con.row_factory = sqlite3.Row
    if not _wal_enabled:
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA busy_timeout=10000")
            _wal_enabled = True
        except Exception:
            pass
    else:
        con.execute("PRAGMA busy_timeout=10000")
    return con


def init_schema() -> None:
    global _initialized
    with _lock:
        if _initialized:
            return
        con = _connect()
        try:
            con.executescript(_SCHEMA)
            con.commit()
        finally:
            con.close()
        _initialized = True


# ---------------------------------------------------------------------------
# OpenPair CRUD
# ---------------------------------------------------------------------------

@dataclass
class OpenPair:
    symbol: str
    module: str
    side_perp: str
    side_spot: str
    notional_usd: float
    entry_funding: float
    entry_ts_ms: int
    updated_ts_ms: int
    consensus: float
    conflict: float
    metadata: dict[str, Any] = field(default_factory=dict)


def upsert_pair(p: OpenPair) -> None:
    init_schema()
    con = _connect()
    try:
        con.execute(
            """
            INSERT INTO apex_open_pairs
                (symbol, module, side_perp, side_spot, notional_usd,
                 entry_funding, entry_ts_ms, updated_ts_ms,
                 consensus, conflict, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                module=excluded.module,
                side_perp=excluded.side_perp,
                side_spot=excluded.side_spot,
                notional_usd=excluded.notional_usd,
                entry_funding=excluded.entry_funding,
                updated_ts_ms=excluded.updated_ts_ms,
                consensus=excluded.consensus,
                conflict=excluded.conflict,
                metadata_json=excluded.metadata_json
            """,
            (
                p.symbol, p.module, p.side_perp, p.side_spot, p.notional_usd,
                p.entry_funding, p.entry_ts_ms, p.updated_ts_ms,
                p.consensus, p.conflict, json.dumps(p.metadata, default=str),
            ),
        )
        con.commit()
    finally:
        con.close()


def delete_pair(symbol: str) -> None:
    init_schema()
    con = _connect()
    try:
        con.execute("DELETE FROM apex_open_pairs WHERE symbol = ?", (symbol,))
        con.commit()
    finally:
        con.close()


def list_open_pairs() -> list[OpenPair]:
    init_schema()
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM apex_open_pairs ORDER BY entry_ts_ms ASC"
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        out.append(OpenPair(
            symbol=r["symbol"], module=r["module"],
            side_perp=r["side_perp"], side_spot=r["side_spot"],
            notional_usd=r["notional_usd"],
            entry_funding=r["entry_funding"] or 0.0,
            entry_ts_ms=r["entry_ts_ms"], updated_ts_ms=r["updated_ts_ms"],
            consensus=r["consensus"], conflict=r["conflict"],
            metadata=json.loads(r["metadata_json"] or "{}"),
        ))
    return out


# ---------------------------------------------------------------------------
# Equity + Kill
# ---------------------------------------------------------------------------

def record_equity(equity_usd: float, peak_usd: float, positions: int) -> None:
    init_schema()
    dd = (peak_usd - equity_usd) / peak_usd if peak_usd > 0 else 0.0
    con = _connect()
    try:
        con.execute(
            "INSERT INTO apex_equity_marks (ts_ms, equity_usd, peak_usd, drawdown_pct, positions_open) VALUES (?, ?, ?, ?, ?)",
            (int(time.time() * 1000), equity_usd, peak_usd, dd, positions),
        )
        con.commit()
    finally:
        con.close()


def latest_peak() -> Optional[float]:
    init_schema()
    con = _connect()
    try:
        r = con.execute(
            "SELECT MAX(peak_usd) AS p FROM apex_equity_marks"
        ).fetchone()
    finally:
        con.close()
    return float(r["p"]) if r and r["p"] is not None else None


def record_kill_event(reason: str, drawdown_pct: float,
                      equity_usd: float, peak_usd: float) -> int:
    init_schema()
    con = _connect()
    try:
        cur = con.execute(
            "INSERT INTO apex_kill_events (ts_ms, reason, drawdown_pct, equity_usd, peak_usd) VALUES (?, ?, ?, ?, ?)",
            (int(time.time() * 1000), reason, drawdown_pct, equity_usd, peak_usd),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def record_kill_unlock(kill_id: int, by: str, reason: str) -> None:
    init_schema()
    con = _connect()
    try:
        con.execute(
            "UPDATE apex_kill_events SET unlocked_ts_ms = ?, unlocked_by = ?, unlock_reason = ? WHERE id = ?",
            (int(time.time() * 1000), by, reason, kill_id),
        )
        con.commit()
    finally:
        con.close()


def latest_unresolved_kill() -> Optional[dict[str, Any]]:
    init_schema()
    con = _connect()
    try:
        r = con.execute(
            "SELECT * FROM apex_kill_events WHERE unlocked_ts_ms IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return dict(r) if r else None


# ---------------------------------------------------------------------------
# Trade + LLM cost + consensus logging
# ---------------------------------------------------------------------------

def log_trade(*, symbol: str, module: str, action: str,
              side: Optional[str] = None, notional_usd: Optional[float] = None,
              avg_px: Optional[float] = None, fee_usd: Optional[float] = None,
              pnl_usd: Optional[float] = None,
              correlation_id: Optional[str] = None,
              payload: Optional[dict[str, Any]] = None) -> None:
    init_schema()
    con = _connect()
    try:
        con.execute(
            """
            INSERT INTO apex_trade_log
                (ts_ms, symbol, module, action, side, notional_usd, avg_px,
                 fee_usd, pnl_usd, correlation_id, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1000), symbol, module, action, side,
                notional_usd, avg_px, fee_usd, pnl_usd, correlation_id,
                json.dumps(payload or {}, default=str),
            ),
        )
        con.commit()
    finally:
        con.close()


def log_llm_cost(*, symbol: Optional[str], role: str, provider: str,
                 model: str, cost_usd: float, latency_ms: int, ok: bool,
                 error: Optional[str] = None) -> None:
    init_schema()
    con = _connect()
    try:
        con.execute(
            "INSERT INTO apex_llm_cost (ts_ms, symbol, role, provider, model, cost_usd, latency_ms, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(time.time() * 1000), symbol, role, provider, model,
             cost_usd, latency_ms, 1 if ok else 0, error),
        )
        con.commit()
    finally:
        con.close()


def log_consensus(*, symbol: str, consensus_score: float, conflict_score: float,
                  vetoed: bool, members_called: int, kl_stop_at: Optional[int],
                  payload: dict[str, Any]) -> None:
    init_schema()
    con = _connect()
    try:
        con.execute(
            "INSERT INTO apex_consensus_log (ts_ms, symbol, consensus_score, conflict_score, vetoed, members_called, kl_stop_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(time.time() * 1000), symbol, consensus_score, conflict_score,
             1 if vetoed else 0, members_called, kl_stop_at,
             json.dumps(payload, default=str)),
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Boot recovery
# ---------------------------------------------------------------------------

@dataclass
class BootState:
    open_pairs: list[OpenPair]
    peak_usd: Optional[float]
    unresolved_kill: Optional[dict[str, Any]]


def load_on_boot() -> BootState:
    """Reconstruct everything the engine needs at startup."""
    init_schema()
    return BootState(
        open_pairs=list_open_pairs(),
        peak_usd=latest_peak(),
        unresolved_kill=latest_unresolved_kill(),
    )


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """For batch writes. Commits on success; rolls back on exception."""
    init_schema()
    con = _connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
