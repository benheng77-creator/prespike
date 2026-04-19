"""
Shared operator infrastructure routes, mounted at /apex/* for historical
reasons.

IMPORTANT — NAMING vs OWNERSHIP:
    The /apex/* prefix is a HISTORICAL name, not an engine-ownership claim.
    These routes serve SHARED operator infrastructure (PnL, trades log,
    consensus log, LLM cost/health, notifications, kill switch, watchdog,
    governor, halt/pause/resume) that is read and written by BOTH
    SPOT AGGRO and apex_omega-era code.

    No SPOT AGGRO engine logic lives here. Every spot-owned endpoint was
    moved to openclaw_v1/spot_aggro/api/routes.py (mounted at
    /spot_aggro/*) in Phase 10. Any future spot-specific endpoint MUST be
    added to the spot router, not this file.

    The /apex/* prefix itself is retained for backward compatibility with
    dashboards, CI, and deployed worker URLs that bind to this path.
    Renaming to /ops/* or /infra/* is a separate migration with a large
    blast radius and is deferred.

Auth:
    GET routes are anonymous-readable (dashboard polls).
    POST routes require OPS_ADMIN_TOKEN via the X-Ops-Token header.

Endpoints (all shared ops infra — not engine-specific):
    GET  /apex/status           overall server state + open pairs snapshot
    GET  /apex/trades?limit=50  trade log rows (both engines write here)
    GET  /apex/consensus        consensus decisions (both engines)
    GET  /apex/llm/cost         LLM cost today + by provider
    GET  /apex/llm/health       last-call status per provider
    GET  /apex/notifications    notification history
    GET  /apex/pnl              PnL snapshot (shared equity source)
    GET  /apex/kill             kill-lock state + last event
    GET  /apex/governor         gate accuracy + posture
    GET  /apex/watchdog         incident findings (filter by engine=spot)
    POST /apex/halt             operator-triggered halt (admin)
    POST /apex/pause            operator pause (admin)
    POST /apex/resume           operator resume (admin)
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from fastapi import Body

from . import universe_discovery
from .config import load as load_cfg
from .core import governor
from .notifications import router as notify_router
from .persistence import settings as app_settings
from .persistence import state as persist
from .risk import kill_switch
from .scheduler import pnl_reporter, research_scanner
from .watchdog import llm_watchdog


router = APIRouter(prefix="/spot_aggro/ops", tags=["spot_aggro_ops"])


def _require_admin(x_ops_token: str | None) -> None:
    expected = os.environ.get("OPS_ADMIN_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=403,
                            detail="OPS_ADMIN_TOKEN not set on this host")
    if (x_ops_token or "").strip() != expected:
        raise HTTPException(status_code=401, detail="invalid admin token")


# ---------------------------------------------------------------------------
# GET — dashboard-readable
# ---------------------------------------------------------------------------

@router.get("/status")
def apex_status() -> dict[str, Any]:
    cfg = load_cfg()
    open_pairs = persist.list_open_pairs()
    peak = persist.latest_peak() or 0.0
    con = persist._connect()
    try:
        last_eq = con.execute(
            "SELECT equity_usd, peak_usd, drawdown_pct, positions_open, ts_ms "
            "FROM apex_equity_marks ORDER BY ts_ms DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    now_ms = int(time.time() * 1000)
    return {
        "ts_ms": now_ms,
        "working_usd_cfg": cfg["capital"]["working_usd"],
        "max_pairs": cfg["engine"]["max_concurrent_pairs"],
        "mode_hint": "live" if os.environ.get("OKX_API_KEY") else "paper",
        "kill_locked": kill_switch.is_locked(),
        "latest_equity": dict(last_eq) if last_eq else None,
        "peak_usd": peak,
        "open_pairs_count": len(open_pairs),
        "open_pairs": [
            {"symbol": p.symbol, "module": p.module, "side": p.side_perp,
             "notional_usd": p.notional_usd, "consensus": p.consensus,
             "conflict": p.conflict,
             "age_h": (now_ms - p.entry_ts_ms) / 3_600_000,
             "entry_ts_ms": p.entry_ts_ms}
            for p in open_pairs
        ],
        "llm_members": [m["role"] + ":" + m["provider"]
                        for m in cfg["llm"]["members"]],
    }


@router.get("/trades")
def apex_trades(limit: int = Query(50, ge=1, le=5000)) -> dict[str, Any]:
    """Phase 11b final — `tier` is now a top-level field on every row.

    Canonical tier (A+/A/B/C) for scored activity, "?" for reconciled,
    NULL for rows whose provenance cannot be resolved. The heatmap binds
    directly to this column and no longer regexes payload_json.

    Phase 11d — limit cap raised from 500 → 5000 so the heatmap's declared
    24h window is physically reachable when reject/skip rates are high
    (otherwise the last 500 rows span only ~30 minutes and historical
    canonical exits fall off the tail, making Lane 1 look empty).
    """
    persist.init_schema()  # ensures tier column + backfill applied.
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT ts_ms, symbol, module, action, side, notional_usd, "
            "avg_px, fee_usd, pnl_usd, correlation_id, payload_json, tier "
            "FROM apex_trade_log ORDER BY ts_ms DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()
    return {"count": len(rows), "rows": [dict(r) for r in rows]}


@router.get("/consensus")
def apex_consensus(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT ts_ms, symbol, consensus_score, conflict_score, vetoed, "
            "members_called, kl_stop_at "
            "FROM apex_consensus_log ORDER BY ts_ms DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()
    return {"count": len(rows), "rows": [dict(r) for r in rows]}


@router.get("/llm/cost")
def apex_llm_cost() -> dict[str, Any]:
    cutoff_24h = int((time.time() - 86400) * 1000)
    cutoff_3h  = int((time.time() - 10800) * 1000)
    con = persist._connect()
    try:
        total_24 = con.execute(
            "SELECT IFNULL(SUM(cost_usd), 0) AS c, COUNT(*) AS n "
            "FROM apex_llm_cost WHERE ts_ms >= ?", (cutoff_24h,),
        ).fetchone()
        total_3  = con.execute(
            "SELECT IFNULL(SUM(cost_usd), 0) AS c, COUNT(*) AS n "
            "FROM apex_llm_cost WHERE ts_ms >= ?", (cutoff_3h,),
        ).fetchone()
        by_prov = con.execute(
            "SELECT provider, model, COUNT(*) AS n, "
            "IFNULL(SUM(cost_usd), 0) AS cost, IFNULL(AVG(latency_ms), 0) AS lat, "
            "SUM(ok) AS ok "
            "FROM apex_llm_cost WHERE ts_ms >= ? GROUP BY provider, model",
            (cutoff_24h,),
        ).fetchall()
    finally:
        con.close()
    return {
        "last_24h": dict(total_24),
        "last_3h": dict(total_3),
        "by_provider": [dict(r) for r in by_prov],
    }


@router.get("/llm/health")
def apex_llm_health() -> dict[str, Any]:
    """Per-provider last-call status. 5-minute rolling window so the dashboard
    reflects current reality, not stale historic failures (e.g. pre-topup 402s)."""
    cfg_members = load_cfg()["llm"]["members"]
    cutoff = int((time.time() - 300) * 1000)        # last 5 min
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT provider, model, "
            "SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS ok_n, COUNT(*) AS n, "
            "MAX(ts_ms) AS last_ts, IFNULL(AVG(latency_ms), 0) AS lat "
            "FROM apex_llm_cost WHERE ts_ms >= ? GROUP BY provider, model",
            (cutoff,),
        ).fetchall()
    finally:
        con.close()
    by_key = {(r["provider"], r["model"]): dict(r) for r in rows}
    out = []
    for m in cfg_members:
        key = (m["provider"], m["model"])
        row = by_key.get(key) or {}
        n = int(row.get("n") or 0)
        ok_n = int(row.get("ok_n") or 0)
        out.append({
            "role": m["role"],
            "provider": m["provider"],
            "model": m["model"],
            # Legacy key kept for dashboard compat — now 5-min window.
            "calls_30m": n,
            "calls_5m": n,
            "ok_5m": ok_n,
            "ok_rate": (ok_n / n) if n else None,
            "avg_latency_ms": float(row.get("lat") or 0),
            "last_call_ts_ms": int(row.get("last_ts") or 0),
            "status": _health_tag(n, ok_n),
            "window_minutes": 5,
        })
    return {"members": out, "window_minutes": 5}


def _health_tag(n: int, ok_n: int) -> str:
    if n == 0:
        return "sleep"    # no calls = waiting for market conditions
    rate = ok_n / n
    if rate >= 0.9: return "ok"
    if rate >= 0.5: return "degraded"
    return "down"


@router.get("/notifications")
def apex_notifications(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT ts_ms, event_type, symbol, severity, title, body, "
            "channel_tg_ok, channel_wa_ok "
            "FROM apex_notifications ORDER BY ts_ms DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()
    return {"count": len(rows), "rows": [dict(r) for r in rows]}


@router.get("/pnl")
def apex_pnl() -> dict[str, Any]:
    return pnl_reporter.build_report()


@router.get("/kill")
def apex_kill() -> dict[str, Any]:
    return {
        "locked": kill_switch.is_locked(),
        "latest_event": persist.latest_unresolved_kill(),
    }


# ---------------------------------------------------------------------------
# POST — admin-gated
# ---------------------------------------------------------------------------

@router.post("/pause")
def apex_pause(x_ops_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Pause ALL engine activity — no LLM, no orders, no scanning."""
    _require_admin(x_ops_token)
    app_settings.save({"engine_paused": True})
    notify_router.send(notify_router.NotifyEvent(
        event_type="engine.halt", severity="warn",
        title="Engine PAUSED by operator", body="All LLM + trading activity stopped.",
    ))
    return {"ok": True, "engine_paused": True}


@router.post("/resume")
def apex_resume(x_ops_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Resume engine activity after pause."""
    _require_admin(x_ops_token)
    app_settings.save({"engine_paused": False})
    notify_router.send(notify_router.NotifyEvent(
        event_type="engine.start", severity="info",
        title="Engine RESUMED by operator", body="Activity resumed.",
    ))
    return {"ok": True, "engine_paused": False}


@router.post("/flatten")
def apex_flatten(x_ops_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Pause + close all open positions."""
    _require_admin(x_ops_token)
    app_settings.save({"engine_paused": True})
    # Delete all open pairs from DB (engine will emergency-close on next cycle if any exist)
    for p in persist.list_open_pairs():
        persist.delete_pair(p.symbol)
    notify_router.send(notify_router.NotifyEvent(
        event_type="engine.halt", severity="warn",
        title="FLATTEN — paused + all pairs closed",
    ))
    return {"ok": True, "engine_paused": True, "pairs_cleared": True}


@router.post("/halt")
def apex_halt(reason: str = "manual",
              x_ops_token: str | None = Header(default=None)) -> dict[str, Any]:
    _require_admin(x_ops_token)
    app_settings.save({"engine_paused": True})
    kid = kill_switch.write_lock(
        reason=f"manual: {reason}", drawdown_pct=0.0,
        equity_usd=0.0, peak_usd=persist.latest_peak() or 0.0,
    )
    notify_router.kill_triggered(
        reason=f"manual: {reason}", drawdown_pct=0.0,
        equity_usd=0.0, peak_usd=persist.latest_peak() or 0.0,
    )
    return {"ok": True, "kill_event_id": kid, "engine_paused": True}


@router.post("/trigger_pnl")
def apex_trigger_pnl(x_ops_token: str | None = Header(default=None)) -> dict[str, Any]:
    _require_admin(x_ops_token)
    r = pnl_reporter.build_report()
    notify_router.pnl_report(r)
    return {"ok": True, "report": r}


# ---------------------------------------------------------------------------
# Settings (operator-editable, live, no restart)
# ---------------------------------------------------------------------------

@router.get("/settings")
def apex_settings_get() -> dict[str, Any]:
    return app_settings.load()


@router.post("/settings")
def apex_settings_patch(
    patch: dict[str, Any] = Body(...),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    return app_settings.save(patch)


# ---------------------------------------------------------------------------
# Governor snapshot — tells you the effective gates right now
# ---------------------------------------------------------------------------

@router.get("/governor")
def apex_governor() -> dict[str, Any]:
    g = governor.get_effective_gates()
    return {
        "consensus_min_effective": g.consensus_min,
        "conflict_max_effective": g.conflict_max,
        "trades_24h": g.trades_24h,
        "target_min_per_day": g.target_min,
        "target_max_per_day": g.target_max,
        "aggressive_on": g.aggressive_on,
        "reason": g.reason,
    }


# ---------------------------------------------------------------------------
# Live consensus feed — per-member scoring + "next move" reasoning
# ---------------------------------------------------------------------------

@router.get("/consensus/live")
def apex_consensus_live(limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    """
    Full per-member breakdown of the most recent consensus calls.
    Dashboard uses this to show LLM reasoning in real time.
    """
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT ts_ms, symbol, consensus_score, conflict_score, vetoed, "
            "members_called, kl_stop_at, payload_json "
            "FROM apex_consensus_log ORDER BY ts_ms DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()
    out = []
    import json as _j
    for r in rows:
        payload = _j.loads(r["payload_json"] or "{}")
        out.append({
            "ts_ms": r["ts_ms"],
            "symbol": r["symbol"],
            "consensus": r["consensus_score"],
            "conflict": r["conflict_score"],
            "vetoed": bool(r["vetoed"]),
            "members_called": r["members_called"],
            "kl_stop_at": r["kl_stop_at"],
            "per_member": payload.get("per_member", []),
            "ctx": payload.get("ctx", {}),
        })
    return {"count": len(out), "rows": out}


# ---------------------------------------------------------------------------
# Universe (current coin count + top candidates)
# ---------------------------------------------------------------------------

@router.get("/universe")
def apex_universe() -> dict[str, Any]:
    coins = universe_discovery.cached_universe()
    return {
        "mode": app_settings.load().get("universe_mode"),
        "cache_age_s": universe_discovery.cache_age_s(),
        "count": len(coins),
        "top20": [
            {"symbol": c.symbol, "category": c.category,
             "volume_24h_usd": c.volume_24h_usd,
             "funding_rate": c.funding_rate,
             "spread_bp": c.spread_bp, "score": c.score}
            for c in coins[:20]
        ],
    }


# ---------------------------------------------------------------------------
# Watchdog findings (LLM root-cause analyses)
# ---------------------------------------------------------------------------

@router.get("/watchdog")
def apex_watchdog(
    limit: int = Query(20, ge=1, le=200),
    engine: str | None = Query(None, description="Filter by engine, e.g. 'spot' hides perp-module noise"),
) -> dict[str, Any]:
    return {
        "findings": llm_watchdog.recent_findings(limit=limit, engine=engine),
        "queue_unprocessed": llm_watchdog.list_unprocessed(limit=20, engine=engine),
    }


@router.post("/watchdog/process")
def apex_watchdog_process(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    import asyncio
    n = asyncio.run(llm_watchdog.process_once(limit=5))
    return {"processed": n}


# ---------------------------------------------------------------------------
# Research scanner — 5-minute LLM coin analysis
# ---------------------------------------------------------------------------

@router.get("/research/latest")
def apex_research_latest() -> dict[str, Any]:
    """Latest 5-minute research report — top 3 coins + reasoning."""
    return research_scanner.get_latest()


@router.get("/research/history")
def apex_research_history(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    return {"reports": research_scanner.get_history(limit=limit)}


@router.post("/research/scan")
def apex_research_scan_now(x_ops_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Trigger one research scan immediately. Admin-only."""
    _require_admin(x_ops_token)
    import asyncio
    return asyncio.run(research_scanner.run_once())



# ===========================================================================
# Phase 10 — SPOT AGGRO routes RELOCATED
# ===========================================================================
# All /apex/spot_aggro/* endpoints have been moved to the spot-owned router
# at openclaw_v1/spot_aggro/api/routes.py (mounted under /spot_aggro/*).
# This file no longer carries any spot handler — the spot router is the
# single source of truth for the SPOT AGGRO HTTP surface.
#
# The /apex/* endpoints that REMAIN in this file are intentionally shared
# operator infrastructure (PnL, status, trades, notifications, kill, llm/*,
# governor, watchdog, consensus, halt). Per Phase 10 · Q1 = A, /apex/* here
# is treated as shared/operator infra, NOT as apex_omega engine ownership.
# Both engines may read from these endpoints; no engine owns them.

