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
CREATE TABLE IF NOT EXISTS open_pairs (
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
CREATE INDEX IF NOT EXISTS idx_open_pairs_module ON open_pairs(module);

CREATE TABLE IF NOT EXISTS equity_marks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    equity_usd      REAL NOT NULL,
    peak_usd        REAL NOT NULL,
    drawdown_pct    REAL NOT NULL,
    positions_open  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eq_ts ON equity_marks(ts_ms DESC);

CREATE TABLE IF NOT EXISTS kill_events (
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

CREATE TABLE IF NOT EXISTS trade_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    module          TEXT NOT NULL,
    action          TEXT NOT NULL,            -- enter | exit | reject | skip | rebalance
    side            TEXT,
    notional_usd    REAL,
    avg_px          REAL,
    fee_usd         REAL,
    pnl_usd         REAL,
    correlation_id  TEXT,
    payload_json    TEXT,
    tier            TEXT                       -- Phase 11b final: canonical A+/A/B/C or "?" for reconciled
);
CREATE INDEX IF NOT EXISTS idx_trade_ts ON trade_log(ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_trade_sym ON trade_log(symbol, ts_ms DESC);
-- idx_trade_tier is created inside the migration fn instead, because
-- on pre-Phase-11b DBs the `tier` column doesn't exist until after ALTER.

CREATE TABLE IF NOT EXISTS llm_cost (
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
CREATE INDEX IF NOT EXISTS idx_llm_ts ON llm_cost(ts_ms DESC);

CREATE TABLE IF NOT EXISTS consensus_log (
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
CREATE INDEX IF NOT EXISTS idx_consensus_ts ON consensus_log(ts_ms DESC);

-- SPOT AGGRO per-(symbol, tier, regime) outcome memory. Feeds adaptive
-- universe: soft composite multiplier, cooldown on loss streak, and last-
-- resort suppression. Never used by perp modules.
CREATE TABLE IF NOT EXISTS spot_aggro_coin_memory (
    symbol              TEXT    NOT NULL,
    tier                TEXT    NOT NULL,  -- 'A+', 'A', 'B', 'C'
    regime              TEXT    NOT NULL,  -- MIO regime label at entry
    trades              INTEGER NOT NULL DEFAULT 0,
    wins                INTEGER NOT NULL DEFAULT 0,
    losses              INTEGER NOT NULL DEFAULT 0,
    sum_pnl_usd         REAL    NOT NULL DEFAULT 0.0,
    last_exit_ts        INTEGER NOT NULL DEFAULT 0,
    loss_streak         INTEGER NOT NULL DEFAULT 0,
    suppressed_until_ts INTEGER NOT NULL DEFAULT 0,
    updated_ts          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, tier, regime)
);
CREATE INDEX IF NOT EXISTS idx_spot_coin_memory_symbol
    ON spot_aggro_coin_memory(symbol);

-- SPOT AGGRO 5-LLM forensic-truth runs. One row per report; payload_json
-- holds the full SHARED_INPUT + per-role outputs + L5 adjudication.
CREATE TABLE IF NOT EXISTS spot_forensic_runs (
    report_id              TEXT PRIMARY KEY,
    generated_ts_ms        INTEGER NOT NULL,
    window_start_ts_ms     INTEGER NOT NULL,
    window_end_ts_ms       INTEGER NOT NULL,
    n_trades_in_window     INTEGER NOT NULL,
    overall_verdict        TEXT,
    expectancy_usd         REAL,
    win_rate               REAL,
    profit_factor          REAL,
    friction_ratio         REAL,
    quorum_returned        INTEGER NOT NULL,
    quorum_required        INTEGER NOT NULL,
    degraded_confidence    INTEGER NOT NULL,
    cost_usd_total         REAL    NOT NULL DEFAULT 0,
    latency_ms_total       INTEGER NOT NULL DEFAULT 0,
    evidence_gap_count     INTEGER NOT NULL DEFAULT 0,
    payload_json           TEXT    NOT NULL,
    pdf_path               TEXT
);
CREATE INDEX IF NOT EXISTS idx_forensic_ts
    ON spot_forensic_runs(generated_ts_ms DESC);

-- SPOT AGGRO MIO regime samples. One row per engine heartbeat when
-- regime or confidence changes. Feeds forensic L3 transition analysis.
CREATE TABLE IF NOT EXISTS spot_aggro_regime_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms              INTEGER NOT NULL,
    regime             TEXT    NOT NULL,
    regime_confidence  REAL,
    squeeze_timing     TEXT,
    edge_status        TEXT,
    universe_quality   TEXT
);
CREATE INDEX IF NOT EXISTS idx_spot_regime_ts
    ON spot_aggro_regime_log(ts_ms DESC);
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
            # Phase 11b final — canonical-tier recovery migration.
            # CREATE TABLE IF NOT EXISTS does NOT add new columns to an
            # existing table, so we ALTER + backfill here. Idempotent:
            # the existence check guards against re-runs, and the backfill
            # only touches rows where tier is NULL.
            _migrate_trade_log_tier_column(con)
            con.commit()
        finally:
            con.close()
        _initialized = True


def _migrate_trade_log_tier_column(con) -> None:
    """Add `tier` column to trade_log on pre-existing DBs and backfill
    it from the best available evidence. Canonical tier is recovered from:
      1. payload_json.tier if present and non-empty (authoritative)
      2. module pattern (M1_squeeze_* → A, M1_flow_B → B, M1_scalp_C → C,
         M1_squeeze_A+ → A+, M3_blitz → A+)
      3. "?" literal for reconciled modules (M_reconciled, M_reconciled_lowconf)
         — honest sentinel, NOT invented
    Never fabricates a tier: rows that cannot be resolved stay NULL, and
    the heatmap lane for "unknown-provenance" surfaces them explicitly.
    """
    # Detect whether tier column already exists.
    cols = [r[1] for r in con.execute("PRAGMA table_info(trade_log)").fetchall()]
    if "tier" not in cols:
        con.execute("ALTER TABLE trade_log ADD COLUMN tier TEXT")
    # Index creation is idempotent and runs on BOTH fresh and legacy DBs
    # (fresh DBs defined the column in _SCHEMA but not the index).
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_trade_tier "
        "ON trade_log(tier, action, ts_ms DESC)"
    )
    # Backfill. Each branch is a pure-SQL expression so it runs in one pass
    # per case without pulling rows into Python.
    # 1. payload_json.tier when present and non-empty.
    con.execute(
        "UPDATE trade_log SET tier = json_extract(payload_json, '$.tier') "
        "WHERE tier IS NULL "
        "AND payload_json IS NOT NULL "
        "AND json_extract(payload_json, '$.tier') IS NOT NULL "
        "AND json_extract(payload_json, '$.tier') != ''"
    )
    # 2. Module-pattern recovery (ordered most-specific first).
    module_map = (
        ("M1_squeeze_Aplus", "A+"),  # defensive — no current module uses this
        ("M3_blitz",         "A+"),
        ("M1_squeeze_A",     "A"),
        ("M1_squeeze",       "A"),    # bare (legacy)
        ("M1_flow_B",        "B"),
        ("M1_flow",          "B"),
        ("M1_scalp_C",       "C"),
        ("M1_scalp",         "C"),
    )
    for mod_prefix, tier in module_map:
        con.execute(
            "UPDATE trade_log SET tier = ? "
            "WHERE tier IS NULL AND module LIKE ?",
            (tier, mod_prefix + "%"),
        )
    # 3. Reconciled sentinel.
    con.execute(
        "UPDATE trade_log SET tier = '?' "
        "WHERE tier IS NULL AND module LIKE 'M_reconciled%'"
    )
    # Anything still NULL has unknown provenance; heatmap surfaces it as
    # UNKNOWN, not silently dropped.


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
            INSERT INTO open_pairs
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
        con.execute("DELETE FROM open_pairs WHERE symbol = ?", (symbol,))
        con.commit()
    finally:
        con.close()


def list_open_pairs() -> list[OpenPair]:
    init_schema()
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM open_pairs ORDER BY entry_ts_ms ASC"
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
            "INSERT INTO equity_marks (ts_ms, equity_usd, peak_usd, drawdown_pct, positions_open) VALUES (?, ?, ?, ?, ?)",
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
            "SELECT MAX(peak_usd) AS p FROM equity_marks"
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
            "INSERT INTO kill_events (ts_ms, reason, drawdown_pct, equity_usd, peak_usd) VALUES (?, ?, ?, ?, ?)",
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
            "UPDATE kill_events SET unlocked_ts_ms = ?, unlocked_by = ?, unlock_reason = ? WHERE id = ?",
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
            "SELECT * FROM kill_events WHERE unlocked_ts_ms IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return dict(r) if r else None


# ---------------------------------------------------------------------------
# SPOT AGGRO regime timeline (for forensic L3)
# ---------------------------------------------------------------------------

def log_regime_sample(
    *, regime: str, regime_confidence: Optional[float] = None,
    squeeze_timing: Optional[str] = None, edge_status: Optional[str] = None,
    universe_quality: Optional[str] = None,
) -> None:
    """Append a regime sample. Engine calls this on every MIO cycle.

    Cheap (single INSERT); indexed by ts_ms for forensic windowed queries.
    Never raises — telemetry must not block the trading loop.
    """
    try:
        init_schema()
        con = _connect()
        try:
            con.execute(
                "INSERT INTO spot_aggro_regime_log "
                "(ts_ms, regime, regime_confidence, squeeze_timing, edge_status, universe_quality) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (int(time.time() * 1000), str(regime or "UNKNOWN"),
                 regime_confidence, squeeze_timing, edge_status, universe_quality),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass  # telemetry failure is non-fatal


def fetch_regime_timeline(
    start_ts_ms: int, end_ts_ms: int, limit: int = 5000,
) -> list[dict[str, Any]]:
    """Return regime samples in window, ascending by ts_ms."""
    init_schema()
    con = _connect()
    try:
        rows = con.execute(
            "SELECT ts_ms, regime, regime_confidence, squeeze_timing, "
            "       edge_status, universe_quality "
            "FROM spot_aggro_regime_log "
            "WHERE ts_ms >= ? AND ts_ms < ? "
            "ORDER BY ts_ms ASC LIMIT ?",
            (int(start_ts_ms), int(end_ts_ms), int(limit)),
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Trade + LLM cost + consensus logging
# ---------------------------------------------------------------------------

def log_trade(*, symbol: str, module: str, action: str,
              side: Optional[str] = None, notional_usd: Optional[float] = None,
              avg_px: Optional[float] = None, fee_usd: Optional[float] = None,
              pnl_usd: Optional[float] = None,
              correlation_id: Optional[str] = None,
              payload: Optional[dict[str, Any]] = None,
              tier: Optional[str] = None) -> None:
    """Insert one trade-log row.

    Phase 11b final — `tier` is now a first-class column. Callers MUST pass
    the canonical tier (A+/A/B/C) for scored activity, or "?" for reconciled
    positions. Omitting it is tolerated for backward-compatibility: we fall
    back to payload["tier"] → module pattern → "?" only for reconciled → NULL.
    The backfill migration uses the same hierarchy for historical rows.
    """
    init_schema()
    # Resolve tier with the same hierarchy as the historical backfill, so
    # new rows are categorized consistently with migrated old rows.
    resolved_tier = tier
    if resolved_tier is None and payload is not None:
        t = payload.get("tier")
        if t:
            resolved_tier = str(t)
    if resolved_tier is None and module:
        # Match the most specific prefix first.
        for prefix, derived in (
            ("M3_blitz",        "A+"),
            ("M1_squeeze_A",    "A"),
            ("M1_squeeze",      "A"),
            ("M1_flow_B",       "B"),
            ("M1_flow",         "B"),
            ("M1_scalp_C",      "C"),
            ("M1_scalp",        "C"),
            ("M_reconciled",    "?"),
        ):
            if module.startswith(prefix):
                resolved_tier = derived
                break
    # If still None, leave NULL; the heatmap's UNKNOWN lane will surface it.

    con = _connect()
    try:
        con.execute(
            """
            INSERT INTO trade_log
                (ts_ms, symbol, module, action, side, notional_usd, avg_px,
                 fee_usd, pnl_usd, correlation_id, payload_json, tier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1000), symbol, module, action, side,
                notional_usd, avg_px, fee_usd, pnl_usd, correlation_id,
                json.dumps(payload or {}, default=str),
                resolved_tier,
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
            "INSERT INTO llm_cost (ts_ms, symbol, role, provider, model, cost_usd, latency_ms, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            "INSERT INTO consensus_log (ts_ms, symbol, consensus_score, conflict_score, vetoed, members_called, kl_stop_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
