"""
Spot-local persistence for the L3 calibration table.

Owns its own SQLite table `spot_aggro_calibration_table` using the existing
shared DB connection (same file as the rest of the spot state). Schema is
defined and migrated here — no edit to `shared/persistence/state.py` is
required, keeping that shared file stable across engines.

Rows:
    (tier, symbol, regime, score_decile)  — composite primary key
    n_trades, wins, losses, mean_pnl_pct, sum_pnl_pct, last_trade_ts_ms,
    status (VALID/INVALID/INSUFFICIENT_DATA/NON_MONOTONIC),
    updated_ts_ms

The `status` column is computed at build time by `calibration_engine.py`.
Readers should trust the stored status; writers are the sole source of truth.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from shared.persistence import state as persist

log = logging.getLogger("spot_aggro.gate.l3.store")


_CALIBRATION_SCHEMA = """
-- SPOT AGGRO L3 calibration buckets. Nightly job rebuilds this from the
-- last 30 days of closed trades in apex_trade_log.
CREATE TABLE IF NOT EXISTS spot_aggro_calibration_table (
    tier            TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    regime          TEXT    NOT NULL,
    score_decile    INTEGER NOT NULL,       -- 0..9
    n_trades        INTEGER NOT NULL,
    wins            INTEGER NOT NULL,
    losses          INTEGER NOT NULL,
    mean_pnl_pct    REAL    NOT NULL,
    sum_pnl_pct     REAL    NOT NULL,
    mean_pnl_usd    REAL    NOT NULL,
    last_trade_ts_ms INTEGER,
    status          TEXT    NOT NULL,       -- VALID|INVALID|INSUFFICIENT_DATA|NON_MONOTONIC
    updated_ts_ms   INTEGER NOT NULL,
    PRIMARY KEY (tier, symbol, regime, score_decile)
);
CREATE INDEX IF NOT EXISTS idx_cal_status
    ON spot_aggro_calibration_table(status);
CREATE INDEX IF NOT EXISTS idx_cal_tier_regime
    ON spot_aggro_calibration_table(tier, regime);
"""


STATUS_VALID             = "VALID"
STATUS_INVALID           = "INVALID"
STATUS_INSUFFICIENT      = "INSUFFICIENT_DATA"
STATUS_NON_MONOTONIC     = "NON_MONOTONIC"
ALL_STATUSES = (STATUS_VALID, STATUS_INVALID, STATUS_INSUFFICIENT, STATUS_NON_MONOTONIC)


@dataclass(frozen=True)
class BucketRow:
    tier: str
    symbol: str
    regime: str
    score_decile: int
    n_trades: int
    wins: int
    losses: int
    mean_pnl_pct: float
    sum_pnl_pct: float
    mean_pnl_usd: float
    last_trade_ts_ms: Optional[int]
    status: str
    updated_ts_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "symbol": self.symbol,
            "regime": self.regime,
            "score_decile": self.score_decile,
            "n_trades": self.n_trades,
            "wins": self.wins,
            "losses": self.losses,
            "mean_pnl_pct": round(self.mean_pnl_pct, 6),
            "sum_pnl_pct": round(self.sum_pnl_pct, 6),
            "mean_pnl_usd": round(self.mean_pnl_usd, 6),
            "last_trade_ts_ms": self.last_trade_ts_ms,
            "status": self.status,
            "updated_ts_ms": self.updated_ts_ms,
        }


def init_schema() -> None:
    """Create table/indexes if missing. Safe to call repeatedly."""
    # Ensure the shared DB is initialised first (WAL pragmas + parent tables),
    # then add our table on top.
    persist.init_schema()
    con = persist._connect()
    try:
        con.executescript(_CALIBRATION_SCHEMA)
        con.commit()
    finally:
        con.close()


def _row_from_sqlite(r: sqlite3.Row) -> BucketRow:
    return BucketRow(
        tier=r["tier"],
        symbol=r["symbol"],
        regime=r["regime"],
        score_decile=int(r["score_decile"]),
        n_trades=int(r["n_trades"]),
        wins=int(r["wins"]),
        losses=int(r["losses"]),
        mean_pnl_pct=float(r["mean_pnl_pct"]),
        sum_pnl_pct=float(r["sum_pnl_pct"]),
        mean_pnl_usd=float(r["mean_pnl_usd"]),
        last_trade_ts_ms=int(r["last_trade_ts_ms"]) if r["last_trade_ts_ms"] is not None else None,
        status=str(r["status"]),
        updated_ts_ms=int(r["updated_ts_ms"]),
    )


def replace_buckets(rows: Iterable[BucketRow]) -> int:
    """Atomically replace the full table contents with `rows`.

    Use when the nightly calibrator has produced a new snapshot; we don't
    incrementally update to avoid half-states. Returns rows written.
    """
    init_schema()
    rows = list(rows)
    now_ms = int(time.time() * 1000)
    con = persist._connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM spot_aggro_calibration_table")
        con.executemany(
            """
            INSERT INTO spot_aggro_calibration_table
                (tier, symbol, regime, score_decile, n_trades, wins, losses,
                 mean_pnl_pct, sum_pnl_pct, mean_pnl_usd, last_trade_ts_ms,
                 status, updated_ts_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.tier, r.symbol, r.regime, r.score_decile,
                    r.n_trades, r.wins, r.losses,
                    r.mean_pnl_pct, r.sum_pnl_pct, r.mean_pnl_usd,
                    r.last_trade_ts_ms,
                    r.status, r.updated_ts_ms or now_ms,
                )
                for r in rows
            ],
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return len(rows)


def lookup(
    tier: str, symbol: str, regime: str, score_decile: int,
) -> Optional[BucketRow]:
    init_schema()
    con = persist._connect()
    try:
        row = con.execute(
            """
            SELECT * FROM spot_aggro_calibration_table
            WHERE tier=? AND symbol=? AND regime=? AND score_decile=?
            """,
            (tier, symbol, regime, int(score_decile)),
        ).fetchone()
    finally:
        con.close()
    return _row_from_sqlite(row) if row else None


def list_buckets(
    *,
    tier: Optional[str] = None,
    symbol: Optional[str] = None,
    regime: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 5000,
) -> list[BucketRow]:
    """Filter helper — analytics surfaces should use this, never SQL directly.
    All tiers (including Tier C) are returned regardless of status."""
    init_schema()
    where: list[str] = []
    args: list[Any] = []
    if tier is not None:
        where.append("tier=?")
        args.append(tier)
    if symbol is not None:
        where.append("symbol=?")
        args.append(symbol)
    if regime is not None:
        where.append("regime=?")
        args.append(regime)
    if status is not None:
        where.append("status=?")
        args.append(status)
    sql = "SELECT * FROM spot_aggro_calibration_table"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY tier, symbol, regime, score_decile LIMIT ?"
    args.append(int(limit))

    con = persist._connect()
    try:
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    return [_row_from_sqlite(r) for r in rows]


def clear_all() -> None:
    """Testing + full rebuild helper."""
    init_schema()
    con = persist._connect()
    try:
        con.execute("DELETE FROM spot_aggro_calibration_table")
        con.commit()
    finally:
        con.close()
