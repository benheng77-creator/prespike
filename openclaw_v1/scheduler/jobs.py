"""
Default OpenClaw job registry. Wires monitors + reports onto a Scheduler.

Every handler is optional; if a component is missing, the job simply
does nothing and records a no-op in the audit ledger. This keeps the
add-only guarantee: the scheduler layer runs whether or not Phase 6+
components are present.
"""

from __future__ import annotations

from typing import Any, Optional

from .cron import Scheduler


def register_defaults(
    scheduler: Scheduler,
    *,
    monitors: Optional[dict] = None,
    reports: Optional[dict] = None,
    enable_eod: bool = True,
) -> None:
    """
    `monitors` may include any of: heartbeat, data_freshness, balance,
    txn_monitor, exchange_session, pnl_consistency, report_validator.
    Each value must be a coroutine function taking no args.

    `reports` may include: eod, activity_summary, deliver. Same shape.
    """
    m = monitors or {}
    r = reports or {}

    if "heartbeat" in m:
        scheduler.register("heartbeat", m["heartbeat"], interval_s=15,
                           description="loop liveness")
    if "data_freshness" in m:
        scheduler.register("data_freshness", m["data_freshness"], interval_s=30,
                           description="tick/candle age check")
    if "txn_monitor" in m:
        scheduler.register("txn_monitor", m["txn_monitor"], interval_s=60,
                           description="order reconciliation")
    if "exchange_session" in m:
        scheduler.register("exchange_session", m["exchange_session"], interval_s=60,
                           description="exchange + rate limits")
    if "balance" in m:
        scheduler.register("balance_monitor", m["balance"], interval_s=300,
                           description="account balance drift")
    if "pnl_consistency" in m:
        scheduler.register("pnl_consistency", m["pnl_consistency"], interval_s=900,
                           description="Σ trades vs balance")

    if enable_eod:
        if "eod" in r:
            scheduler.register("eod_report", r["eod"], at_utc="00:05",
                               description="daily EOD PnL report")
        if "activity_summary" in r:
            scheduler.register("activity_summary", r["activity_summary"], at_utc="00:10",
                               description="daily OpenClaw activity summary")
        if "deliver" in r:
            scheduler.register("report_deliver", r["deliver"], at_utc="00:15",
                               description="push to exporters")
