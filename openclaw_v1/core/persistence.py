"""
SQLite trade/decision logger.

Two tables:
  decisions — one row per evaluate() call with verdict + key features +
              live-only signals (book imbalance, funding score, OI delta)
  trades    — one row per opened position, updated on close

Uses stdlib sqlite3 — no external dependency. Synchronous; safe for the
per-bar decision cadence. Forward-only column migrations on startup so
databases created by earlier versions pick up new columns cleanly.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

from strategies.decision_engine import DecisionInputs, DecisionOutputs

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER DEFAULT (strftime('%s','now') * 1000),
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    score_total REAL,
    pwin_pct REAL,
    ev_r REAL,
    rr_true REAL,
    entry_px REAL,
    stop_px REAL,
    target_px REAL,
    d5 REAL, d15 REAL, d60 REAL, d240 REAL,
    drift REAL,
    event_risk REAL,
    news_sent REAL,
    social_sent REAL,
    flow_sent REAL,
    book_imbalance REAL,
    funding_score REAL,
    oi_delta REAL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction INTEGER NOT NULL,
    size REAL NOT NULL,
    entry_ts_ms INTEGER NOT NULL,
    entry_px REAL NOT NULL,
    stop_px REAL NOT NULL,
    target_px REAL NOT NULL,
    exit_ts_ms INTEGER,
    exit_px REAL,
    exit_reason TEXT,
    pnl_r REAL,
    pnl_quote REAL,
    balance_after REAL,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_entry_ts ON trades(entry_ts_ms);
"""

# Columns added to existing `decisions` tables via ALTER TABLE if missing.
DECISION_MIGRATIONS = [
    ("book_imbalance", "REAL"),
    ("funding_score", "REAL"),
    ("oi_delta", "REAL"),
]


def _ensure_columns(conn: sqlite3.Connection, table: str, columns) -> None:
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    for name, sql_type in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


class TradeLogger:
    def __init__(self, db_path: str):
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        _ensure_columns(self.conn, "decisions", DECISION_MIGRATIONS)
        self.conn.commit()

    def log_decision(
        self,
        symbol: str,
        inp: DecisionInputs,
        out: DecisionOutputs,
        extras: Optional[dict] = None,
    ) -> int:
        extras = extras or {}
        cur = self.conn.execute(
            """INSERT INTO decisions (
                symbol, action, score_total, pwin_pct, ev_r, rr_true,
                entry_px, stop_px, target_px,
                d5, d15, d60, d240, drift, event_risk,
                news_sent, social_sent, flow_sent,
                book_imbalance, funding_score, oi_delta
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                symbol, out.TerminalAction, out.ScoreTotal, out.PWinPct,
                out.EV_R, out.RRTrue,
                inp.EntryPx, inp.StopPx, inp.TargetPx,
                inp.D5, inp.D15, inp.D60, inp.D240,
                inp.DriftScore, inp.EventRiskScore,
                inp.NewsSent, inp.SocialSent, inp.FlowSent,
                extras.get("book_imbalance"),
                extras.get("funding_score"),
                extras.get("oi_delta"),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def log_trade_open(
        self,
        symbol: str,
        direction: int,
        size: float,
        entry_ts_ms: int,
        entry_px: float,
        stop_px: float,
        target_px: float,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO trades (
                symbol, direction, size, entry_ts_ms, entry_px, stop_px, target_px, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open')""",
            (symbol, direction, size, entry_ts_ms, entry_px, stop_px, target_px),
        )
        self.conn.commit()
        return cur.lastrowid

    def log_trade_close(self, closed: dict) -> None:
        self.conn.execute(
            """UPDATE trades SET
                exit_ts_ms = ?, exit_px = ?, exit_reason = ?,
                pnl_r = ?, pnl_quote = ?, balance_after = ?, status = 'closed'
            WHERE symbol = ? AND entry_ts_ms = ? AND status = 'open'""",
            (
                closed["exit_ts_ms"], closed["exit_px"], closed["exit_reason"],
                closed["pnl_r"], closed["pnl_quote"], closed["balance_after"],
                closed["symbol"], closed["entry_ts_ms"],
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
