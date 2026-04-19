"""
Claw ingest boundary — the single entry-point for bot outputs.

Every bot decision that reaches Claw must pass through ``record_bot_decision``.
The boundary:

  * freezes the payload (MappingProxyType)
  * computes the canonical hash (BOT_FIELDS_FROZEN subset)
  * records the row into ``bot_decisions_immutable`` (append-only)
  * asserts the payload hash did not change mid-flight
  * returns a small result dict — id, hash, ingest_ts — never mutates input

Idempotent: if a payload with the same (strategy_id, payload_sha256) has
already been recorded, the existing row is returned.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Mapping, Optional

from .contract import (
    CLAW_NIC_VERSION,
    BotPayloadMutation,
    assert_unchanged,
    bot_payload_hash,
    canonical_payload,
    freeze,
)
from .db import claw_db_path, connect, init_claw_schema


log = logging.getLogger("claw.ingest")


def record_bot_decision(
    *,
    strategy_id: str,
    payload: Mapping[str, Any],
    ingest_source: str,
    symbol: Optional[str] = None,
    cycle_id: Optional[str] = None,
    signature: Optional[str] = None,
    correlation_id: Optional[str] = None,
    db_path: Optional[str] = None,
    ts_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Record a bot decision verbatim. Returns {id, payload_sha256, ts_ms, deduplicated}.

    The caller's ``payload`` is not modified. A frozen view is built, hashed,
    and inserted. If a row with the same (strategy_id, payload_sha256) already
    exists the existing row is returned with ``deduplicated=True``.
    """
    if not strategy_id:
        raise ValueError("strategy_id is required")
    if not ingest_source:
        raise ValueError("ingest_source is required")
    if payload is None:
        raise ValueError("payload is required")

    # Freeze BEFORE anything else touches the payload.
    frozen = freeze(payload)
    payload_sha256 = bot_payload_hash(frozen)
    # Canonical subset (used for tamper-evident JSON blob).
    canon = canonical_payload(frozen)
    # Full JSON blob for disk — use canonical form so two equivalent payloads
    # serialise identically (stable hash inputs, stable disk rows).
    payload_json = json.dumps(
        {
            "frozen": canon,
            "extras": _non_frozen_snapshot(frozen),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    ts = int(ts_ms if ts_ms is not None else time.time() * 1000)

    path = init_claw_schema(db_path)
    con = connect(path)
    try:
        try:
            cur = con.execute(
                """
                INSERT INTO bot_decisions_immutable
                    (ts_ms, strategy_id, cycle_id, symbol,
                     payload_json, payload_sha256, signature,
                     ingest_source, correlation_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts, strategy_id,
                    str(cycle_id) if cycle_id is not None else None,
                    symbol,
                    payload_json, payload_sha256, signature,
                    ingest_source, correlation_id,
                ),
            )
            con.commit()
            row_id = cur.lastrowid
            deduplicated = False
        except sqlite3.IntegrityError:
            # UNIQUE(strategy_id, payload_sha256) hit — already recorded.
            row = con.execute(
                "SELECT id, ts_ms FROM bot_decisions_immutable "
                "WHERE strategy_id = ? AND payload_sha256 = ?",
                (strategy_id, payload_sha256),
            ).fetchone()
            row_id = row["id"] if row else None
            ts = row["ts_ms"] if row else ts
            deduplicated = True
    finally:
        con.close()

    # Contract guard: verify the caller's payload wasn't mutated mid-flight.
    try:
        assert_unchanged(frozen, payload, where="claw.ingest.record_bot_decision")
    except BotPayloadMutation:
        log.exception("%s contract violation at ingest boundary", CLAW_NIC_VERSION)
        raise

    return {
        "id": row_id,
        "payload_sha256": payload_sha256,
        "ts_ms": ts,
        "strategy_id": strategy_id,
        "deduplicated": deduplicated,
        "nic_version": CLAW_NIC_VERSION,
    }


def fetch_bot_decision(row_id: int, *, db_path: Optional[str] = None) -> dict[str, Any]:
    """Return a recorded bot decision by primary key. Raises LookupError."""
    con = connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM bot_decisions_immutable WHERE id = ?",
            (row_id,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise LookupError(f"bot_decisions_immutable id={row_id} not found")
    return dict(row)


def list_bot_decisions(
    *,
    strategy_id: Optional[str] = None,
    limit: int = 50,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return the most recent rows. Read-only."""
    limit = max(1, min(int(limit), 1000))
    con = connect(db_path)
    try:
        if strategy_id:
            rows = con.execute(
                "SELECT id, ts_ms, strategy_id, cycle_id, symbol, payload_sha256, "
                "ingest_source, correlation_id "
                "FROM bot_decisions_immutable "
                "WHERE strategy_id = ? ORDER BY id DESC LIMIT ?",
                (strategy_id, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT id, ts_ms, strategy_id, cycle_id, symbol, payload_sha256, "
                "ingest_source, correlation_id "
                "FROM bot_decisions_immutable ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def _non_frozen_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the non-frozen portion of a payload (everything outside BOT_FIELDS_FROZEN).

    We still keep this on disk so nothing is lost — but the hash only covers
    the frozen subset, so extras can never silently affect the contract.
    """
    from .contract import BOT_FIELDS_FROZEN
    frozen = set(BOT_FIELDS_FROZEN)
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k not in frozen:
            try:
                json.dumps(v, default=str)
                out[k] = v
            except Exception:
                out[k] = str(v)
    return out
