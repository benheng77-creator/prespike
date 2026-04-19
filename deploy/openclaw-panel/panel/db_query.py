"""
SQLite read helper for the OpenClaw Control Panel.

The panel server spawns this script with one argument: a query name from the
QUERIES map. The script connects to <tradingRoot>/logs/openclaw.db, runs the
query, and prints a JSON document on stdout. The DB is opened read-only and
in URI mode so concurrent writes from main.py are safe.

Usage:
    python db_query.py <db_path> <query_name>

Output: JSON on stdout. Errors print to stderr and the script exits non-zero.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone


QUERIES = {
    "open_trades": (
        "SELECT id, symbol, direction, size, entry_ts_ms, entry_px, stop_px, "
        "target_px, status FROM trades WHERE status = 'open' ORDER BY id DESC"
    ),
    "recent_closed_trades": (
        "SELECT id, symbol, direction, size, entry_ts_ms, exit_ts_ms, "
        "entry_px, exit_px, exit_reason, pnl_r, pnl_quote, balance_after, "
        "status FROM trades WHERE status <> 'open' ORDER BY id DESC LIMIT 20"
    ),
    "pnl_summary": (
        "SELECT COUNT(*) AS closed_count, "
        "COALESCE(SUM(pnl_r), 0.0) AS total_r, "
        "COALESCE(AVG(pnl_r), 0.0) AS avg_r, "
        "COALESCE(SUM(pnl_quote), 0.0) AS total_quote, "
        "COALESCE(SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END), 0) AS wins, "
        "COALESCE(SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END), 0) AS losses "
        "FROM trades WHERE status <> 'open'"
    ),
    "latest_decision": (
        "SELECT id, created_at, symbol, action, score_total, pwin_pct, ev_r, "
        "rr_true, entry_px, stop_px, target_px FROM decisions "
        "ORDER BY id DESC LIMIT 1"
    ),
    "recent_decisions": (
        "SELECT id, created_at, symbol, action, score_total, pwin_pct, ev_r "
        "FROM decisions ORDER BY id DESC LIMIT 15"
    ),
    "decision_count": "SELECT COUNT(*) AS n FROM decisions",
    "trade_count": "SELECT COUNT(*) AS n FROM trades",
    "balance_latest": (
        "SELECT balance_after FROM trades WHERE balance_after IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ),
    "today_trades": (
        "SELECT id, symbol, direction, size, entry_ts_ms, exit_ts_ms, "
        "entry_px, exit_px, exit_reason, pnl_r, pnl_quote, balance_after, "
        "status FROM trades "
        "WHERE date(COALESCE(exit_ts_ms, entry_ts_ms) / 1000, 'unixepoch', 'localtime') "
        "= date('now', 'localtime') "
        "ORDER BY id DESC"
    ),
    "today_pnl": (
        "SELECT "
        "  COUNT(*) AS closed_count, "
        "  COALESCE(SUM(pnl_r), 0.0) AS total_r, "
        "  COALESCE(AVG(pnl_r), 0.0) AS avg_r, "
        "  COALESCE(SUM(pnl_quote), 0.0) AS total_quote, "
        "  COALESCE(SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END), 0) AS wins, "
        "  COALESCE(SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END), 0) AS losses, "
        "  COALESCE(MAX(pnl_r), 0.0) AS best_r, "
        "  COALESCE(MIN(pnl_r), 0.0) AS worst_r "
        "FROM trades "
        "WHERE status <> 'open' AND exit_ts_ms IS NOT NULL "
        "  AND date(exit_ts_ms / 1000, 'unixepoch', 'localtime') "
        "  = date('now', 'localtime')"
    ),
    "daily_pnl_history": (
        "SELECT "
        "  date(exit_ts_ms / 1000, 'unixepoch', 'localtime') AS day, "
        "  COUNT(*) AS count, "
        "  COALESCE(SUM(pnl_r), 0.0) AS total_r, "
        "  COALESCE(SUM(pnl_quote), 0.0) AS total_quote, "
        "  COALESCE(SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END), 0) AS wins, "
        "  COALESCE(SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END), 0) AS losses "
        "FROM trades "
        "WHERE status <> 'open' AND exit_ts_ms IS NOT NULL "
        "GROUP BY day "
        "ORDER BY day DESC "
        "LIMIT 14"
    ),
    "today_decisions_breakdown": (
        "SELECT action, COUNT(*) AS n FROM decisions "
        "WHERE date(created_at / 1000, 'unixepoch', 'localtime') "
        "  = date('now', 'localtime') "
        "GROUP BY action ORDER BY n DESC"
    ),
}


def rows_to_dicts(cursor, rows):
    cols = [d[0] for d in cursor.description] if cursor.description else []
    return [dict(zip(cols, row)) for row in rows]


def gate_snapshot(db_path: str, symbol: str, window: int) -> dict:
    """Return rolling window + lifetime counts for a single symbol."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    try:
        totals = conn.execute(
            "SELECT COUNT(*) n, "
            "COALESCE(SUM(CASE WHEN pnl_r>0 THEN 1 ELSE 0 END),0) w "
            "FROM trades WHERE symbol=? AND status<>'open' AND pnl_r IS NOT NULL",
            (symbol,),
        ).fetchone()
        rows = conn.execute(
            "SELECT pnl_r FROM trades "
            "WHERE symbol=? AND status<>'open' AND pnl_r IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (symbol, window),
        ).fetchall()
    finally:
        conn.close()

    total_trades = int(totals[0] or 0)
    total_wins = int(totals[1] or 0)
    window_trades = len(rows)
    window_wins = sum(1 for (r,) in rows if r and r > 0)
    window_losses = window_trades - window_wins
    return {
        "symbol": symbol,
        "window_trades": window_trades,
        "window_wins": window_wins,
        "window_losses": window_losses,
        "total_trades_closed": total_trades,
        "total_wins": total_wins,
    }


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: db_query.py <db_path> <query_name> [args...]"}))
        sys.exit(2)

    db_path = sys.argv[1]
    query_name = sys.argv[2]

    if query_name == "gate_snapshot":
        if len(sys.argv) < 5:
            print(json.dumps({"error": "usage: db_query.py <db> gate_snapshot <symbol> <window>"}))
            sys.exit(2)
        symbol = sys.argv[3]
        try:
            window = int(sys.argv[4])
        except ValueError:
            print(json.dumps({"error": f"window must be int: {sys.argv[4]}"}))
            sys.exit(2)
        if not os.path.exists(db_path):
            print(json.dumps({"error": f"db not found: {db_path}"}))
            sys.exit(2)
        try:
            print(json.dumps(gate_snapshot(db_path, symbol, window)))
        except sqlite3.Error as exc:
            print(json.dumps({"error": f"query: {exc}"}))
            sys.exit(2)
        return

    if query_name not in QUERIES:
        print(json.dumps({"error": f"unknown query: {query_name}"}))
        sys.exit(2)

    if not os.path.exists(db_path):
        print(json.dumps({"error": f"db not found: {db_path}"}))
        sys.exit(2)

    uri = f"file:{db_path}?mode=ro&immutable=0"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        print(json.dumps({"error": f"connect: {exc}"}))
        sys.exit(2)

    try:
        cursor = conn.execute(QUERIES[query_name])
        rows = cursor.fetchall()
        data = rows_to_dicts(cursor, rows)
        print(
            json.dumps(
                {
                    "query": query_name,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "rows": data,
                }
            )
        )
    except sqlite3.Error as exc:
        print(json.dumps({"error": f"query: {exc}"}))
        sys.exit(2)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
