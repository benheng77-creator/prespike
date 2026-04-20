"""Phase 11n-9-hh — Layer 3 Resilience & Compliance: hash-chained
immutable trade ledger.

Every committed trade (entry or exit) appends one row here. Each row
carries the SHA-256 of the previous row's canonical payload, forming
a tamper-evident chain:

    row[i].prev_hash == row[i-1].row_hash

A post-hoc auditor can detect any insertion, deletion, or mutation by
re-running the chain. If the head doesn't match, the chain is broken.
Genesis row (id=1) uses the literal string "GENESIS" as prev_hash.

This is ADDITIVE alongside the existing `trade_log` table. We keep
trade_log as the canonical operational view (indexed, queried by
dashboard/forensic), and write a parallel entry to `spot_immutable_ledger`
as the audit-grade record. MAS-grade audit export reads from here.

Schema
------
spot_immutable_ledger
  row_id         PK auto-increment
  ts_ms          timestamp
  kind           'entry' | 'exit' | 'reject' | 'skip' | 'event'
  symbol         TEXT
  tier           TEXT
  notional_usd   REAL
  pnl_usd        REAL
  fee_usd        REAL
  correlation_id TEXT
  payload_json   TEXT        canonical JSON (sorted keys, separators)
  prev_hash      TEXT NOT NULL    SHA-256 hex of prior row
  row_hash       TEXT NOT NULL    SHA-256 hex of THIS row's canonical body

Public API
----------
append(kind, **fields) -> int       append one row; returns row_id.
verify_chain() -> VerdictDict       scan entire chain, return 'ok'|'broken'
                                    plus the row where divergence started.
head_hash() -> str                  latest row_hash (or 'GENESIS' if empty).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

_DB_LOCK = threading.Lock()

GENESIS_HASH = "GENESIS"


def _db_path() -> str:
    return (
        os.environ.get("TRADE_DB_PATH")
        or os.environ.get("CLAW_DB_PATH")
        or "trades.db"
    )


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(), isolation_level=None, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _init_schema() -> None:
    with _DB_LOCK:
        con = _connect()
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_immutable_ledger("
                " row_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " kind TEXT NOT NULL,"
                " symbol TEXT,"
                " tier TEXT,"
                " notional_usd REAL,"
                " pnl_usd REAL,"
                " fee_usd REAL,"
                " correlation_id TEXT,"
                " payload_json TEXT NOT NULL,"
                " prev_hash TEXT NOT NULL,"
                " row_hash TEXT NOT NULL"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_il_ts "
                "ON spot_immutable_ledger(ts_ms DESC)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_il_corr "
                "ON spot_immutable_ledger(correlation_id)"
            )
        finally:
            con.close()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=str)


def _hash_body(
    row_id: int, ts_ms: int, kind: str, symbol: str | None,
    tier: str | None, notional_usd: float | None,
    pnl_usd: float | None, fee_usd: float | None,
    correlation_id: str | None, payload_json: str, prev_hash: str,
) -> str:
    body = _canonical_json({
        "row_id": row_id,
        "ts_ms": ts_ms, "kind": kind, "symbol": symbol,
        "tier": tier, "notional_usd": notional_usd,
        "pnl_usd": pnl_usd, "fee_usd": fee_usd,
        "correlation_id": correlation_id,
        "payload_json": payload_json,
        "prev_hash": prev_hash,
    })
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def head_hash() -> str:
    """Return the latest row_hash (or GENESIS if empty)."""
    try:
        _init_schema()
        with _DB_LOCK:
            con = _connect()
            try:
                r = con.execute(
                    "SELECT row_hash FROM spot_immutable_ledger"
                    " ORDER BY row_id DESC LIMIT 1"
                ).fetchone()
            finally:
                con.close()
        return r["row_hash"] if r else GENESIS_HASH
    except Exception:
        return GENESIS_HASH


def append(
    kind: str, *,
    symbol: str | None = None, tier: str | None = None,
    notional_usd: float | None = None, pnl_usd: float | None = None,
    fee_usd: float | None = None, correlation_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    """Append one row to the chain. Returns row_id. Fail-open: any
    exception logs and returns 0 (caller must treat as best-effort)."""
    try:
        _init_schema()
        ts = int(time.time() * 1000)
        payload_json = _canonical_json(payload or {})
        prev = head_hash()
        with _DB_LOCK:
            con = _connect()
            try:
                # Two-step insert: first insert with placeholder hash,
                # read back the auto-incremented row_id, compute the
                # definitive hash, then update. This guarantees row_id
                # is part of the hash body so deletion of any row will
                # be detected by the chain verifier.
                cur = con.execute(
                    "INSERT INTO spot_immutable_ledger("
                    " ts_ms, kind, symbol, tier, notional_usd,"
                    " pnl_usd, fee_usd, correlation_id, payload_json,"
                    " prev_hash, row_hash"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, kind, symbol, tier, notional_usd,
                     pnl_usd, fee_usd, correlation_id, payload_json,
                     prev, "pending"),
                )
                rid = int(cur.lastrowid or 0)
                h = _hash_body(
                    rid, ts, kind, symbol, tier, notional_usd,
                    pnl_usd, fee_usd, correlation_id, payload_json, prev,
                )
                con.execute(
                    "UPDATE spot_immutable_ledger SET row_hash = ?"
                    " WHERE row_id = ?", (h, rid),
                )
                return rid
            finally:
                con.close()
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

@dataclass
class ChainVerdict:
    ok: bool
    total_rows: int
    last_verified_row: int
    first_broken_row: int | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_chain() -> ChainVerdict:
    """Walk the chain from row 1. Every row must satisfy:
      prev_hash == previous_row.row_hash
      row_hash  == _hash_body(row fields + prev_hash)
    """
    try:
        _init_schema()
        with _DB_LOCK:
            con = _connect()
            try:
                rows = con.execute(
                    "SELECT * FROM spot_immutable_ledger"
                    " ORDER BY row_id ASC"
                ).fetchall()
            finally:
                con.close()
        if not rows:
            return ChainVerdict(
                ok=True, total_rows=0, last_verified_row=0,
                reason="empty ledger",
            )
        expected_prev = GENESIS_HASH
        last_ok = 0
        for r in rows:
            rid = int(r["row_id"])
            if r["prev_hash"] != expected_prev:
                return ChainVerdict(
                    ok=False, total_rows=len(rows),
                    last_verified_row=last_ok,
                    first_broken_row=rid,
                    reason=(
                        f"prev_hash mismatch at row {rid}: "
                        f"got {r['prev_hash'][:12]}… "
                        f"expected {expected_prev[:12]}…"
                    ),
                )
            h = _hash_body(
                rid, int(r["ts_ms"]), r["kind"], r["symbol"],
                r["tier"], r["notional_usd"], r["pnl_usd"],
                r["fee_usd"], r["correlation_id"],
                r["payload_json"], r["prev_hash"],
            )
            if h != r["row_hash"]:
                return ChainVerdict(
                    ok=False, total_rows=len(rows),
                    last_verified_row=last_ok,
                    first_broken_row=rid,
                    reason=(
                        f"row_hash recompute mismatch at row {rid}"
                    ),
                )
            expected_prev = r["row_hash"]
            last_ok = rid
        return ChainVerdict(
            ok=True, total_rows=len(rows),
            last_verified_row=last_ok,
            reason=f"chain intact ({len(rows)} rows)",
        )
    except Exception as e:
        return ChainVerdict(
            ok=False, total_rows=0, last_verified_row=0,
            reason=f"verify error: {str(e)[:120]}",
        )


# ---------------------------------------------------------------------------
# AML export (compliance-grade dump)
# ---------------------------------------------------------------------------

def export_range(
    start_ts_ms: int | None = None,
    end_ts_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Return every ledger row in [start_ts_ms, end_ts_ms]. Used by the
    MAS-grade audit exporter. Read-only; never mutates."""
    try:
        _init_schema()
        clauses = []
        args: list[Any] = []
        if start_ts_ms is not None:
            clauses.append("ts_ms >= ?"); args.append(int(start_ts_ms))
        if end_ts_ms is not None:
            clauses.append("ts_ms <= ?"); args.append(int(end_ts_ms))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with _DB_LOCK:
            con = _connect()
            try:
                rows = con.execute(
                    f"SELECT * FROM spot_immutable_ledger{where}"
                    f" ORDER BY row_id ASC", tuple(args),
                ).fetchall()
            finally:
                con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []
