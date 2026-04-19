"""
Claw execution tracker — idempotent submission + full lifecycle audit.

Claw NEVER decides what to trade. It only records what was attempted, how
the exchange responded, and cross-references that back to the immutable bot
decision row. Given the same (strategy_id, idempotency_key) a retry becomes
a no-op — the previous row is returned instead.

Lifecycle:
    queued -> submitted -> (ack_ws | ack_rest) -> partial* -> filled
                                                           -> rejected
                                                           -> canceled
    reconciled is applied by claw.reconciler once exchange state agrees.

Typical use:

    exec_row = begin_execution(
        strategy_id="decision_engine",
        bot_decision_id=ingest_result["id"],
        symbol="BTCUSDT", side="BUY", requested_qty=0.01,
        requested_px=50_000.0, exchange="binance",
    )
    # ... call real executor ...
    mark_submitted(exec_row["id"], exchange_order_id="abc123")
    record_partial(exec_row["id"], filled_qty=0.003, avg_fill_px=50_010.0)
    mark_filled(exec_row["id"], filled_qty=0.01, avg_fill_px=50_012.0)
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from typing import Any, Iterable, Mapping, Optional

from .db import claw_db_path, connect, init_claw_schema


log = logging.getLogger("claw.execution_tracker")


STATES = (
    "queued", "submitted", "ack_ws", "ack_rest",
    "partial", "filled", "rejected", "canceled", "reconciled",
)
TERMINAL_STATES = {"filled", "rejected", "canceled"}


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------

def build_idempotency_key(
    *,
    strategy_id: str,
    symbol: str,
    side: str,
    requested_qty: float,
    minute_bucket_ts: Optional[int] = None,
    bot_decision_id: Optional[int] = None,
    salt: str = "",
) -> str:
    """Deterministic dedupe key.

    Same (strategy, symbol, side, qty rounded to 6dp, same minute bucket,
    same bot decision) → same key → single execution row.
    """
    if minute_bucket_ts is None:
        minute_bucket_ts = int(time.time() // 60)
    parts = (
        strategy_id, symbol, side.upper(),
        f"{float(requested_qty):.6f}",
        str(minute_bucket_ts),
        str(bot_decision_id) if bot_decision_id is not None else "",
        salt,
    )
    blob = "|".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Lifecycle writers
# ---------------------------------------------------------------------------

def begin_execution(
    *,
    strategy_id: str,
    symbol: str,
    side: str,
    requested_qty: float,
    bot_decision_id: Optional[int] = None,
    correlation_id: Optional[str] = None,
    requested_px: Optional[float] = None,
    exchange: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Queue a new execution. Idempotent on (strategy_id, idempotency_key).

    Returns {id, state, deduplicated}. If an existing row matches the key,
    the existing row is returned with deduplicated=True — no new row is
    inserted and no event is emitted.
    """
    if side.upper() not in ("BUY", "SELL", "CLOSE", "REDUCE"):
        raise ValueError(f"invalid side: {side}")
    key = idempotency_key or build_idempotency_key(
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        requested_qty=requested_qty,
        bot_decision_id=bot_decision_id,
    )
    now = _now_ms()
    path = init_claw_schema(db_path)
    con = connect(path)
    try:
        try:
            cur = con.execute(
                """
                INSERT INTO claw_executions
                    (created_ts_ms, updated_ts_ms, bot_decision_id,
                     correlation_id, idempotency_key, strategy_id, symbol,
                     side, requested_qty, state, requested_px, exchange,
                     metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    now, now, bot_decision_id, correlation_id, key,
                    strategy_id, symbol, side.upper(), float(requested_qty),
                    float(requested_px) if requested_px is not None else None,
                    exchange,
                    json.dumps(dict(metadata or {}), default=str),
                ),
            )
            con.commit()
            row_id = cur.lastrowid
            _emit_event(con, row_id, "queued", {
                "strategy_id": strategy_id, "symbol": symbol, "side": side,
                "requested_qty": requested_qty,
            })
            con.commit()
            return {"id": row_id, "state": "queued", "deduplicated": False,
                    "idempotency_key": key}
        except sqlite3.IntegrityError:
            row = con.execute(
                "SELECT id, state FROM claw_executions "
                "WHERE strategy_id = ? AND idempotency_key = ?",
                (strategy_id, key),
            ).fetchone()
            return {"id": row["id"], "state": row["state"],
                    "deduplicated": True, "idempotency_key": key}
    finally:
        con.close()


def mark_submitted(
    execution_id: int,
    *,
    exchange_order_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Mark an execution as submitted to the exchange."""
    _update(
        execution_id,
        updates={
            "state": "submitted",
            "exchange_order_id": exchange_order_id,
            "submitted_ts_ms": _now_ms(),
        },
        from_states=("queued",),
        event="submitted",
        event_payload={"exchange_order_id": exchange_order_id},
        db_path=db_path,
    )


def mark_ack(
    execution_id: int,
    *,
    channel: str,  # "ws" | "rest"
    db_path: Optional[str] = None,
) -> None:
    """Record an acknowledgement from the exchange."""
    if channel not in ("ws", "rest"):
        raise ValueError("channel must be 'ws' or 'rest'")
    field = "ws_ack_ts_ms" if channel == "ws" else "rest_ack_ts_ms"
    _update(
        execution_id,
        updates={
            "state": f"ack_{channel}",
            field: _now_ms(),
        },
        from_states=("submitted", "ack_ws", "ack_rest", "partial"),
        event=f"ack_{channel}",
        event_payload={},
        db_path=db_path,
    )


def record_partial(
    execution_id: int,
    *,
    filled_qty: float,
    avg_fill_px: float,
    db_path: Optional[str] = None,
) -> None:
    """Accumulate a partial fill. Never overwrites — only advances filled_qty."""
    now = _now_ms()
    con = connect(db_path)
    try:
        row = con.execute(
            "SELECT id, state, filled_qty, avg_fill_px, requested_qty, requested_px "
            "FROM claw_executions WHERE id = ?",
            (execution_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"execution {execution_id} not found")
        if row["state"] in TERMINAL_STATES:
            log.info("record_partial ignored: execution %s in terminal state %s",
                     execution_id, row["state"])
            return
        new_filled = max(float(row["filled_qty"] or 0.0), float(filled_qty))
        # Rolling VWAP: if we have prior fill, blend; else use new.
        if row["filled_qty"] and row["avg_fill_px"]:
            prev_notional = float(row["filled_qty"]) * float(row["avg_fill_px"])
            delta_qty = max(0.0, new_filled - float(row["filled_qty"]))
            new_notional = prev_notional + (delta_qty * float(avg_fill_px))
            new_vwap = new_notional / new_filled if new_filled > 0 else float(avg_fill_px)
        else:
            new_vwap = float(avg_fill_px)
        slippage_bps = None
        if row["requested_px"]:
            slippage_bps = (new_vwap - float(row["requested_px"])) / float(row["requested_px"]) * 1e4
        con.execute(
            "UPDATE claw_executions "
            "SET state = 'partial', filled_qty = ?, avg_fill_px = ?, "
            "    slippage_bps = ?, updated_ts_ms = ? "
            "WHERE id = ?",
            (new_filled, new_vwap, slippage_bps, now, execution_id),
        )
        _emit_event(con, execution_id, "partial", {
            "filled_qty": new_filled,
            "avg_fill_px": new_vwap,
        })
        con.commit()
    finally:
        con.close()


def mark_filled(
    execution_id: int,
    *,
    filled_qty: float,
    avg_fill_px: float,
    db_path: Optional[str] = None,
) -> None:
    """Terminal state — order fully filled."""
    now = _now_ms()
    con = connect(db_path)
    try:
        row = con.execute(
            "SELECT requested_px FROM claw_executions WHERE id = ?",
            (execution_id,),
        ).fetchone()
        slippage_bps = None
        if row and row["requested_px"]:
            slippage_bps = (float(avg_fill_px) - float(row["requested_px"])) / float(row["requested_px"]) * 1e4
        con.execute(
            "UPDATE claw_executions "
            "SET state = 'filled', filled_qty = ?, avg_fill_px = ?, "
            "    slippage_bps = ?, final_ts_ms = ?, updated_ts_ms = ? "
            "WHERE id = ?",
            (float(filled_qty), float(avg_fill_px), slippage_bps, now, now, execution_id),
        )
        _emit_event(con, execution_id, "filled", {
            "filled_qty": filled_qty,
            "avg_fill_px": avg_fill_px,
        })
        con.commit()
    finally:
        con.close()


def mark_rejected(
    execution_id: int,
    *,
    reason: str,
    db_path: Optional[str] = None,
) -> None:
    """Terminal state — exchange rejected the order."""
    _update(
        execution_id,
        updates={
            "state": "rejected",
            "rejected_reason": reason,
            "final_ts_ms": _now_ms(),
        },
        from_states=("queued", "submitted", "ack_ws", "ack_rest", "partial"),
        event="rejected",
        event_payload={"reason": reason},
        db_path=db_path,
    )


def mark_canceled(
    execution_id: int,
    *,
    reason: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Terminal state — order canceled before completion."""
    _update(
        execution_id,
        updates={
            "state": "canceled",
            "rejected_reason": reason,
            "final_ts_ms": _now_ms(),
        },
        from_states=("queued", "submitted", "ack_ws", "ack_rest", "partial"),
        event="canceled",
        event_payload={"reason": reason},
        db_path=db_path,
    )


def record_retry(
    execution_id: int,
    *,
    reason: str,
    db_path: Optional[str] = None,
) -> None:
    """Bump retry counter + store last error. Never changes filled_qty or state mid-retry."""
    con = connect(db_path)
    try:
        con.execute(
            "UPDATE claw_executions "
            "SET retries = retries + 1, last_error = ?, updated_ts_ms = ? "
            "WHERE id = ?",
            (reason, _now_ms(), execution_id),
        )
        _emit_event(con, execution_id, "retry", {"reason": reason})
        con.commit()
    finally:
        con.close()


def mark_reconciled(
    execution_id: int,
    *,
    detail: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Tag an execution row as verified against the exchange."""
    con = connect(db_path)
    try:
        con.execute(
            "UPDATE claw_executions SET state = 'reconciled', updated_ts_ms = ? WHERE id = ?",
            (_now_ms(), execution_id),
        )
        _emit_event(con, execution_id, "reconciled", {"detail": detail})
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def get_execution(execution_id: int, *, db_path: Optional[str] = None) -> dict[str, Any]:
    con = connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM claw_executions WHERE id = ?", (execution_id,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise LookupError(f"execution {execution_id} not found")
    return dict(row)


def list_executions(
    *,
    state: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = 50,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    clauses, args = [], []
    if state:
        clauses.append("state = ?"); args.append(state)
    if symbol:
        clauses.append("symbol = ?"); args.append(symbol)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    con = connect(db_path)
    try:
        rows = con.execute(
            f"SELECT * FROM claw_executions{where} ORDER BY id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def list_events(execution_id: int, *, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    con = connect(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM claw_execution_events WHERE execution_id = ? "
            "ORDER BY id ASC",
            (execution_id,),
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def list_open_executions(*, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    """Non-terminal rows — useful for startup recovery."""
    open_states: Iterable[str] = [s for s in STATES if s not in TERMINAL_STATES and s != "reconciled"]
    placeholders = ",".join(["?"] * len(list(open_states)))
    open_states = [s for s in STATES if s not in TERMINAL_STATES and s != "reconciled"]
    con = connect(db_path)
    try:
        rows = con.execute(
            f"SELECT * FROM claw_executions WHERE state IN ({placeholders}) "
            f"ORDER BY id ASC",
            tuple(open_states),
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _update(
    execution_id: int,
    *,
    updates: Mapping[str, Any],
    from_states: Iterable[str],
    event: str,
    event_payload: Mapping[str, Any],
    db_path: Optional[str] = None,
) -> None:
    cols = [f"{k} = ?" for k in updates.keys()]
    cols.append("updated_ts_ms = ?")
    args = list(updates.values()) + [_now_ms(), execution_id]
    state_filter = ",".join(["?"] * len(tuple(from_states)))
    from_tuple = tuple(from_states)
    con = connect(db_path)
    try:
        row = con.execute(
            "SELECT id, state FROM claw_executions WHERE id = ?",
            (execution_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"execution {execution_id} not found")
        if row["state"] not in from_tuple and updates.get("state") != row["state"]:
            log.warning(
                "claw.execution_tracker: illegal transition %s -> %s on id=%s",
                row["state"], updates.get("state"), execution_id,
            )
        con.execute(
            f"UPDATE claw_executions SET {', '.join(cols)} WHERE id = ?",
            args,
        )
        _emit_event(con, execution_id, event, dict(event_payload))
        con.commit()
    finally:
        con.close()


def _emit_event(con: sqlite3.Connection, execution_id: int,
                kind: str, payload: Mapping[str, Any]) -> None:
    con.execute(
        "INSERT INTO claw_execution_events (execution_id, ts_ms, kind, payload_json) "
        "VALUES (?, ?, ?, ?)",
        (execution_id, _now_ms(), kind,
         json.dumps(dict(payload), default=str)),
    )


def _now_ms() -> int:
    return int(time.time() * 1000)
