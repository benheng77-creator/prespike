"""Phase 11n-9-gg — Retrain queue. Logs model-retrain jobs triggered
by contradiction-freeze events or explicit operator action.

This is a job REGISTRY, not an executor. The real retrain step is
operator-initiated (a commit + deploy). The registry exists so every
freeze event has a corresponding auditable retrain ticket.

Schema
------
spot_retrain_queue
  - job_id          INTEGER PK
  - created_ts_ms   when the ticket was opened
  - trigger_reason  'contradiction_freeze_T3' | 'operator' | 'scheduled'
  - target_model    model_id to retrain (control / contrarian / etc.)
  - status          'pending' | 'in_progress' | 'resolved' | 'cancelled'
  - resolved_ts_ms  nullable
  - resolution      nullable free-form (e.g. 'shipped v2.2 in commit abc')

Public API
----------
open_ticket(target_model, reason, payload=None) -> int
resolve_ticket(job_id, resolution) -> bool
cancel_ticket(job_id, reason) -> bool
list_pending() -> list[RetrainTicket]
on_contradiction_freeze(freeze_level, reason) -> int | None
    Auto-called from the freeze escalation ladder. Opens a ticket for
    every variant on T3 (no-op at T0..T2). Returns ticket id or None.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

_DB_LOCK = threading.Lock()


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
                "CREATE TABLE IF NOT EXISTS spot_retrain_queue("
                " job_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " created_ts_ms INTEGER NOT NULL,"
                " trigger_reason TEXT NOT NULL,"
                " target_model TEXT NOT NULL,"
                " status TEXT NOT NULL DEFAULT 'pending',"
                " resolved_ts_ms INTEGER,"
                " resolution TEXT,"
                " payload_json TEXT"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_rq_status "
                "ON spot_retrain_queue(status, created_ts_ms DESC)"
            )
        finally:
            con.close()


@dataclass
class RetrainTicket:
    job_id: int
    created_ts_ms: int
    trigger_reason: str
    target_model: str
    status: str
    resolved_ts_ms: int | None = None
    resolution: str | None = None
    payload: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def open_ticket(
    target_model: str, reason: str,
    payload: dict[str, Any] | None = None,
) -> int:
    """Insert a pending retrain ticket. Returns job_id.
    If a pending ticket for the same (target_model, reason) already
    exists, returns its job_id without creating a duplicate."""
    try:
        _init_schema()
        now = int(time.time() * 1000)
        with _DB_LOCK:
            con = _connect()
            try:
                existing = con.execute(
                    "SELECT job_id FROM spot_retrain_queue"
                    " WHERE target_model = ? AND trigger_reason = ?"
                    " AND status = 'pending'",
                    (target_model, reason),
                ).fetchone()
                if existing:
                    return int(existing["job_id"])
                cur = con.execute(
                    "INSERT INTO spot_retrain_queue("
                    " created_ts_ms, trigger_reason, target_model,"
                    " status, payload_json"
                    ") VALUES(?,?,?, 'pending', ?)",
                    (now, reason, target_model,
                     json.dumps(payload or {})),
                )
                return int(cur.lastrowid or 0)
            finally:
                con.close()
    except Exception:
        return 0


def resolve_ticket(job_id: int, resolution: str) -> bool:
    try:
        _init_schema()
        now = int(time.time() * 1000)
        with _DB_LOCK:
            con = _connect()
            try:
                con.execute(
                    "UPDATE spot_retrain_queue"
                    " SET status = 'resolved', resolved_ts_ms = ?,"
                    " resolution = ? WHERE job_id = ?",
                    (now, resolution, job_id),
                )
                return True
            finally:
                con.close()
    except Exception:
        return False


def cancel_ticket(job_id: int, reason: str) -> bool:
    try:
        _init_schema()
        now = int(time.time() * 1000)
        with _DB_LOCK:
            con = _connect()
            try:
                con.execute(
                    "UPDATE spot_retrain_queue"
                    " SET status = 'cancelled', resolved_ts_ms = ?,"
                    " resolution = ? WHERE job_id = ?",
                    (now, f"cancelled: {reason}", job_id),
                )
                return True
            finally:
                con.close()
    except Exception:
        return False


def list_pending() -> list[RetrainTicket]:
    try:
        _init_schema()
        with _DB_LOCK:
            con = _connect()
            try:
                rows = con.execute(
                    "SELECT * FROM spot_retrain_queue"
                    " WHERE status = 'pending'"
                    " ORDER BY created_ts_ms DESC"
                ).fetchall()
            finally:
                con.close()
        return [
            RetrainTicket(
                job_id=int(r["job_id"]),
                created_ts_ms=int(r["created_ts_ms"]),
                trigger_reason=r["trigger_reason"],
                target_model=r["target_model"],
                status=r["status"],
                resolved_ts_ms=(int(r["resolved_ts_ms"])
                                if r["resolved_ts_ms"] is not None else None),
                resolution=r["resolution"],
                payload=json.loads(r["payload_json"] or "{}"),
            ) for r in rows
        ]
    except Exception:
        return []


def all_tickets(limit: int = 50) -> list[RetrainTicket]:
    try:
        _init_schema()
        with _DB_LOCK:
            con = _connect()
            try:
                rows = con.execute(
                    "SELECT * FROM spot_retrain_queue"
                    " ORDER BY created_ts_ms DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            finally:
                con.close()
        return [
            RetrainTicket(
                job_id=int(r["job_id"]),
                created_ts_ms=int(r["created_ts_ms"]),
                trigger_reason=r["trigger_reason"],
                target_model=r["target_model"],
                status=r["status"],
                resolved_ts_ms=(int(r["resolved_ts_ms"])
                                if r["resolved_ts_ms"] is not None else None),
                resolution=r["resolution"],
                payload=json.loads(r["payload_json"] or "{}"),
            ) for r in rows
        ]
    except Exception:
        return []


def on_contradiction_freeze(freeze_level: str, reason: str) -> int | None:
    """Auto-open retrain tickets when the freeze escalation ladder
    reaches T3 or beyond. Returns the first job_id opened, or None."""
    try:
        if freeze_level not in ("T3", "T4", "T5", "T6"):
            return None
        from spot_aggro.governance.strategy_variants import VARIANT_NAMES
        first: int | None = None
        for variant in VARIANT_NAMES:
            jid = open_ticket(
                target_model=variant,
                reason=f"contradiction_freeze_{freeze_level}",
                payload={"freeze_reason": reason},
            )
            if first is None and jid:
                first = jid
        return first
    except Exception:
        return None
