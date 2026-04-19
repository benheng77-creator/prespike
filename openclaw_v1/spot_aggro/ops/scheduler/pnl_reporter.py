"""
Every-3-hour PnL reporter.

Aggregates:
    * current equity  (OKX total_eq if live adapter available, else last mark)
    * peak + drawdown
    * open pairs count
    * trade activity in last 3 h  (enters, exits, total)
    * realised pnl_3h  (sum of apex_trade_log.pnl_usd for action='exit')
    * fees_3h
    * LLM spend in 3 h  (sum apex_llm_cost)

Sends via notifications.router.pnl_report — Telegram + WhatsApp + DB.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from shared.adapters.okx_unified import OKXUnified
from ..config import load as load_cfg
from ..notifications import router as notify
from ..persistence import state as persist


log = logging.getLogger("apex.scheduler.pnl")
THREE_HOURS_S = 3 * 3600


_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def start(interval_s: int = THREE_HOURS_S) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()

    def _loop():
        log.info("PnL reporter started (interval=%ds)", interval_s)
        # Emit initial report ~90s after boot so there's some data
        for _ in range(90):
            if _stop.is_set():
                return
            time.sleep(1)
        while not _stop.is_set():
            try:
                report = build_report()
                notify.pnl_report(report)
            except Exception:
                log.exception("PnL report crashed (recovering)")
            for _ in range(interval_s):
                if _stop.is_set():
                    return
                time.sleep(1)
        log.info("PnL reporter stopped")

    _thread = threading.Thread(target=_loop, name="apex_pnl_reporter", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()


def build_report() -> dict[str, Any]:
    """Single-shot PnL snapshot. Safe to call manually from /apex/pnl endpoint."""
    cutoff_ms = int((time.time() - THREE_HOURS_S) * 1000)

    # Equity: try live OKX; fall back to last DB mark
    equity_usd, peak_usd = _equity_and_peak()
    drawdown_pct = (peak_usd - equity_usd) / peak_usd if peak_usd > 0 else 0.0
    open_pairs = len(persist.list_open_pairs())

    con = persist._connect()
    try:
        trades = con.execute(
            "SELECT action, COUNT(*) AS n, IFNULL(SUM(fee_usd), 0) AS fees, "
            "IFNULL(SUM(pnl_usd), 0) AS pnl "
            "FROM apex_trade_log WHERE ts_ms >= ? GROUP BY action",
            (cutoff_ms,),
        ).fetchall()
        llm = con.execute(
            "SELECT COUNT(*) AS n, IFNULL(SUM(cost_usd), 0) AS cost "
            "FROM apex_llm_cost WHERE ts_ms >= ?",
            (cutoff_ms,),
        ).fetchone()
    finally:
        con.close()

    # sqlite3.Row has no .get() — convert to plain dicts so .get() works.
    by_action = {r["action"]: dict(r) for r in trades}
    enters = int((by_action.get("enter") or {}).get("n", 0))
    exits  = int((by_action.get("exit")  or {}).get("n", 0))
    rejects = int((by_action.get("reject") or {}).get("n", 0))
    fees_3h = sum(float(r["fees"]) for r in trades)
    pnl_3h  = float((by_action.get("exit")  or {}).get("pnl", 0))

    return {
        "ts": int(time.time()),
        "equity_usd": equity_usd,
        "peak_usd": peak_usd,
        "drawdown_pct": drawdown_pct,
        "open_pairs": open_pairs,
        "trades_3h": enters + exits + rejects,
        "enters_3h": enters,
        "exits_3h": exits,
        "rejects_3h": rejects,
        "pnl_3h_usd": pnl_3h,
        "fees_3h_usd": fees_3h,
        "llm_calls_3h": int(llm["n"] or 0),
        "llm_cost_3h_usd": float(llm["cost"] or 0),
    }


def _equity_and_peak() -> tuple[float, float]:
    peak = persist.latest_peak() or 0.0
    # Try live adapter; if it fails or there are no keys, use the last recorded equity.
    try:
        eq = _try_live_equity()
        if eq > 0:
            if eq > peak:
                peak = eq
            return eq, peak
    except Exception as exc:
        log.debug("live equity unavailable: %s", exc)
    # Fallback: last equity mark in DB
    con = persist._connect()
    try:
        r = con.execute(
            "SELECT equity_usd FROM apex_equity_marks ORDER BY ts_ms DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    eq = float(r["equity_usd"]) if r else float(load_cfg()["capital"]["working_usd"])
    return eq, max(peak, eq)


def _try_live_equity() -> float:
    import asyncio
    async def _inner():
        a = OKXUnified()
        return await a.get_account_equity()
    return asyncio.run(_inner())
