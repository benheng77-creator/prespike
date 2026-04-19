"""
Claw reconciler — cross-check local execution rows against exchange truth.

The reconciler is *read-only with respect to the bot*. It never touches
bot_decisions_immutable. It only inspects claw_executions and advances
them to the ``reconciled`` state (or opens an incident if they diverge).

Design:
  * Accepts an injected ``fetch_order_status`` callable — keeps the module
    decoupled from any specific exchange adapter.
  * Pure function ``reconcile_row`` for unit testing without side effects.
  * ``reconcile_open_executions`` sweeps every non-terminal row and applies
    the resolution policy below.

Resolution policy (never mutates bot truth):
  exchange state  | local state     | action
  --------------- | --------------- | ------------------------------------------
  filled          | filled/partial  | mark_filled (if not already) + reconciled
  filled          | submitted/...   | mark_filled + reconciled + incident(warn)
  canceled        | any non-terminal| mark_canceled + reconciled
  rejected        | any non-terminal| mark_rejected + reconciled
  open            | submitted       | no-op (still in flight)
  missing         | submitted       | incident(warn) — exchange lost the order

Startup recovery: call ``startup_recover`` once at process boot with a
fetch_order_status callable; it reconciles every open execution row before
the trader accepts new intents.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from . import execution_tracker as tracker
from .db import claw_db_path
from .incidents import open_incident


log = logging.getLogger("claw.reconciler")


ExchangeStatus = Mapping[str, Any]   # expected keys: state, filled_qty, avg_fill_px, reason


@dataclass
class ReconcileResult:
    execution_id: int
    action: str                        # "no_op" | "filled" | "rejected" | "canceled" | "incident"
    exchange_state: Optional[str] = None
    local_state_before: Optional[str] = None
    local_state_after: Optional[str] = None
    note: str = ""


@dataclass
class ReconcileSummary:
    sweeps: int = 0
    reconciled: int = 0
    incidents: int = 0
    still_open: int = 0
    details: list[ReconcileResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure resolver
# ---------------------------------------------------------------------------

def reconcile_row(
    row: Mapping[str, Any],
    exchange_status: Optional[ExchangeStatus],
) -> ReconcileResult:
    """Classify the action to take for a local execution row vs exchange.

    Returns the classification only — callers apply side-effects via
    ``apply_resolution`` so tests can verify the decision without touching DB.
    """
    local_state = row["state"]
    if exchange_status is None:
        return ReconcileResult(
            execution_id=row["id"], action="incident",
            local_state_before=local_state,
            note="exchange_missing",
        )
    ex_state = str(exchange_status.get("state", "unknown")).lower()
    if ex_state == "filled":
        return ReconcileResult(
            execution_id=row["id"], action="filled",
            exchange_state=ex_state, local_state_before=local_state,
            local_state_after="filled",
        )
    if ex_state == "canceled":
        return ReconcileResult(
            execution_id=row["id"], action="canceled",
            exchange_state=ex_state, local_state_before=local_state,
            local_state_after="canceled",
        )
    if ex_state == "rejected":
        return ReconcileResult(
            execution_id=row["id"], action="rejected",
            exchange_state=ex_state, local_state_before=local_state,
            local_state_after="rejected",
            note=str(exchange_status.get("reason") or ""),
        )
    # "open", "partial", unknown — no-op (still in flight)
    return ReconcileResult(
        execution_id=row["id"], action="no_op",
        exchange_state=ex_state, local_state_before=local_state,
        local_state_after=local_state,
    )


def apply_resolution(
    result: ReconcileResult,
    exchange_status: Optional[ExchangeStatus] = None,
    *,
    db_path: Optional[str] = None,
) -> None:
    """Apply the side-effect chosen by ``reconcile_row``."""
    if result.action == "no_op":
        return
    if result.action == "filled":
        if exchange_status is None:
            return
        tracker.mark_filled(
            result.execution_id,
            filled_qty=float(exchange_status.get("filled_qty", 0.0)),
            avg_fill_px=float(exchange_status.get("avg_fill_px", 0.0)),
            db_path=db_path,
        )
        tracker.mark_reconciled(result.execution_id, detail="filled_via_reconciler",
                                db_path=db_path)
        return
    if result.action == "canceled":
        tracker.mark_canceled(
            result.execution_id,
            reason="canceled_on_exchange",
            db_path=db_path,
        )
        tracker.mark_reconciled(result.execution_id, detail="canceled_via_reconciler",
                                db_path=db_path)
        return
    if result.action == "rejected":
        tracker.mark_rejected(
            result.execution_id,
            reason=result.note or "rejected_on_exchange",
            db_path=db_path,
        )
        tracker.mark_reconciled(result.execution_id, detail="rejected_via_reconciler",
                                db_path=db_path)
        return
    if result.action == "incident":
        open_incident(
            kind="exchange", severity="warn",
            component="reconciler",
            message=f"execution {result.execution_id}: {result.note}",
            metadata={"execution_id": result.execution_id,
                      "local_state": result.local_state_before},
            db_path=db_path,
        )
        return


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------

def reconcile_open_executions(
    fetch_order_status: Callable[[Mapping[str, Any]], Optional[ExchangeStatus]],
    *,
    db_path: Optional[str] = None,
) -> ReconcileSummary:
    """Walk every open execution, apply the resolution policy.

    ``fetch_order_status(row)`` must return a mapping with at least
    ``{"state": "filled|canceled|rejected|open", ...}`` or None if the
    exchange has no knowledge of the order.
    """
    summary = ReconcileSummary()
    for row in tracker.list_open_executions(db_path=db_path):
        try:
            status = fetch_order_status(row)
        except Exception as exc:                        # pragma: no cover - defensive
            log.exception("reconcile fetch failed for %s", row["id"])
            open_incident(
                kind="exchange", severity="error",
                component="reconciler",
                message=f"fetch_order_status raised: {type(exc).__name__}: {exc}",
                metadata={"execution_id": row["id"]},
                db_path=db_path,
            )
            summary.incidents += 1
            continue
        result = reconcile_row(row, status)
        apply_resolution(result, status, db_path=db_path)
        summary.sweeps += 1
        if result.action in ("filled", "canceled", "rejected"):
            summary.reconciled += 1
        elif result.action == "incident":
            summary.incidents += 1
        else:
            summary.still_open += 1
        summary.details.append(result)
    return summary


def startup_recover(
    fetch_order_status: Callable[[Mapping[str, Any]], Optional[ExchangeStatus]],
    *,
    db_path: Optional[str] = None,
) -> ReconcileSummary:
    """Run once at boot. Reconciles every open execution before trading resumes.

    The returned summary tells the launcher how many were resolved / still open.
    """
    log.info("claw.reconciler: startup_recover begin")
    started = time.time()
    summary = reconcile_open_executions(fetch_order_status, db_path=db_path)
    log.info(
        "claw.reconciler: startup_recover done (%s reconciled, %s open, %s incidents, %.2fs)",
        summary.reconciled, summary.still_open, summary.incidents,
        time.time() - started,
    )
    return summary
