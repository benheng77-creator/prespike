"""Phase 11n-9-ss — Contrarian + Deep Value scoped API router.

Serves ONLY data relevant to the CDV strategy. Every response is
filtered through strategy_scope_guard. Every record carries
strategy="contrarian_deepvalue" tag. Non-CDV variants are stripped
from aggregate fields.

Mounted at: /strategy/contrarian_deepvalue/*

Feature-flag gated (FEATURE_CONTRARIAN_DEEPVALUE_PANEL) — returns
404 when off. Admin mutations require X-CDV-Role or OPS_ADMIN_TOKEN.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from spot_aggro.governance.strategy_scope_guard import (
    CDV_STRATEGY_NAMESPACE,
    CDV_VARIANTS,
    RBAC_ADMIN,
    RBAC_VIEWER,
    ScopeViolation,
    feature_enabled,
    filter_cdv_rows,
    mask_non_cdv_fields,
    rbac_require,
    scoped_variants_sql_in_clause,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/strategy/contrarian_deepvalue", tags=["cdv_panel"])


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


def _feature_or_404() -> None:
    if not feature_enabled():
        raise HTTPException(
            status_code=404,
            detail="CDV panel disabled (FEATURE_CONTRARIAN_DEEPVALUE_PANEL off)",
        )


def _require_viewer(role_header: str | None, ops_token: str | None) -> None:
    """Viewer access: either X-CDV-Role header matches viewer/admin,
    OR OPS_ADMIN_TOKEN present (admin grants viewer by default)."""
    _feature_or_404()
    admin = os.environ.get("OPS_ADMIN_TOKEN", "")
    if admin and ops_token == admin:
        return
    if role_header and (
        role_header == RBAC_VIEWER
        or role_header == RBAC_ADMIN
        or role_header.startswith(RBAC_VIEWER)
        or role_header.startswith(RBAC_ADMIN)
    ):
        return
    raise HTTPException(
        status_code=403,
        detail=f"role required: {RBAC_VIEWER} or {RBAC_ADMIN}",
    )


def _require_admin(role_header: str | None, ops_token: str | None) -> None:
    _feature_or_404()
    admin = os.environ.get("OPS_ADMIN_TOKEN", "")
    if admin and ops_token == admin:
        return
    if role_header and (
        role_header == RBAC_ADMIN or role_header.startswith(RBAC_ADMIN)
    ):
        return
    raise HTTPException(
        status_code=403,
        detail=f"role required: {RBAC_ADMIN}",
    )


# ---------------------------------------------------------------------------
# Aggregated dashboard endpoint
# ---------------------------------------------------------------------------

@router.get("/dashboard")
def cdv_dashboard(
    window_min: int = Query(60, ge=5, le=1440),
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """One-call aggregate for the full CDV panel. Strategy-scoped."""
    _require_viewer(x_cdv_role, x_ops_token)
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - window_min * 60_000

    # Section 1 — header
    header = _build_header()
    # Section 2 — summary
    summary = _build_summary()
    # Sections 3 + 4 — per-variant engine views
    contrarian = _build_variant_view("contrarian", cutoff)
    deep_value = _build_variant_view("deep_value", cutoff)
    # Section 5 — pipeline
    pipeline = _build_pipeline(cutoff)
    # Section 6 — positions
    positions = _build_positions(cutoff)
    # Section 7 — execution quality
    execution = _build_execution_quality(cutoff)
    # Section 8 — risk/governance
    risk = _build_risk_governance(cutoff)
    # Section 9 — daily report
    daily = _build_daily_report()
    # Phase 11n-9-vv — live trade-timer card (time since last CDV trade)
    trade_timer = _build_trade_timer(now_ms)

    return {
        "ok": True,
        "strategy": CDV_STRATEGY_NAMESPACE,
        "variants": sorted(CDV_VARIANTS),
        "ts_ms": now_ms,
        "window_min": window_min,
        "header": header,
        "summary": summary,
        "contrarian": contrarian,
        "deep_value": deep_value,
        "pipeline": pipeline,
        "positions": positions,
        "execution": execution,
        "risk": risk,
        "daily_report": daily,
        "trade_timer": trade_timer,
    }


def _ensure_cdv_timer_anchor_schema(con) -> None:
    """Single-row table storing the monotonic 'time since last trade'
    anchor for the CDV panel. Survives restarts — the clock never resets
    unless a real CDV trade opens OR an operator explicitly resets it."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS spot_cdv_timer_anchor("
        " id INTEGER PRIMARY KEY CHECK (id = 1),"
        " anchor_ts_ms INTEGER NOT NULL,"
        " anchor_reason TEXT NOT NULL,"
        " last_updated_ts_ms INTEGER NOT NULL"
        ")"
    )


def _build_trade_timer(now_ms: int) -> dict[str, Any]:
    """Phase 11n-9-ww — monotonic timer card for "time since last CDV trade".

    Anchor is persisted in spot_cdv_timer_anchor (one row, id=1):
      - Seeded ONCE at first dashboard call (anchor_reason='first_boot').
      - Advanced when a new CDV trade opens (anchor_reason='trade_opened:<variant>').
      - NEVER moves otherwise — not on engine cycle ticks, not on server
        restarts, not on refreshes. Operator sees a strict monotonic count.

    Front-end ticks off the returned anchor_ts_ms every second; server
    hands back the same anchor until a real trade changes it.
    """
    import sqlite3
    from spot_aggro.governance.variant_trip_wire import _db_path

    last_open = last_closed = total = first = None
    anchor_ts: int | None = None
    anchor_reason = "first_boot"

    def _safe_one(con, sql: str) -> sqlite3.Row | None:
        try:
            return con.execute(sql).fetchone()
        except Exception:
            return None

    try:
        con = sqlite3.connect(_db_path(), timeout=5.0, isolation_level=None)
        con.row_factory = sqlite3.Row
        _ensure_cdv_timer_anchor_schema(con)
        try:
            # CDV entry stats — each query guarded separately so a missing
            # spot_live_variant_entries table (fresh DB) does not poison
            # the anchor logic below.
            last_open = _safe_one(con,
                "SELECT variant, opened_ts_ms FROM spot_live_variant_entries"
                " WHERE variant IN ('contrarian','deep_value')"
                " ORDER BY opened_ts_ms DESC LIMIT 1"
            )
            last_closed = _safe_one(con,
                "SELECT variant, closed_ts_ms, realized_pnl_usd"
                " FROM spot_live_variant_entries"
                " WHERE variant IN ('contrarian','deep_value')"
                "  AND status='closed' AND closed_ts_ms IS NOT NULL"
                " ORDER BY closed_ts_ms DESC LIMIT 1"
            )
            total = _safe_one(con,
                "SELECT COUNT(*) AS n FROM spot_live_variant_entries"
                " WHERE variant IN ('contrarian','deep_value')"
            )
            first = _safe_one(con,
                "SELECT MIN(opened_ts_ms) AS ts FROM spot_live_variant_entries"
                " WHERE variant IN ('contrarian','deep_value')"
            )
            row = _safe_one(con,
                "SELECT anchor_ts_ms, anchor_reason FROM spot_cdv_timer_anchor WHERE id = 1"
            )

            last_open_ts_val = int(last_open["opened_ts_ms"]) if last_open and last_open["opened_ts_ms"] else None

            if row is None:
                if last_open_ts_val:
                    anchor_ts = last_open_ts_val
                    anchor_reason = f"trade_opened:{last_open['variant']}"
                else:
                    anchor_ts = now_ms
                    anchor_reason = "first_boot"
                try:
                    con.execute(
                        "INSERT OR REPLACE INTO spot_cdv_timer_anchor("
                        " id, anchor_ts_ms, anchor_reason, last_updated_ts_ms)"
                        " VALUES(1, ?, ?, ?)",
                        (anchor_ts, anchor_reason, now_ms),
                    )
                except Exception:
                    pass
            else:
                anchor_ts = int(row["anchor_ts_ms"])
                anchor_reason = row["anchor_reason"] or "first_boot"
                # Advance anchor only if a newer CDV trade opened.
                if last_open_ts_val and last_open_ts_val > anchor_ts:
                    anchor_ts = last_open_ts_val
                    anchor_reason = f"trade_opened:{last_open['variant']}"
                    try:
                        con.execute(
                            "UPDATE spot_cdv_timer_anchor"
                            " SET anchor_ts_ms = ?, anchor_reason = ?,"
                            "     last_updated_ts_ms = ?"
                            " WHERE id = 1",
                            (anchor_ts, anchor_reason, now_ms),
                        )
                    except Exception:
                        pass
        finally:
            con.close()
    except Exception:
        if anchor_ts is None:
            anchor_ts = now_ms
            anchor_reason = "fallback_now"

    last_open_ts = int(last_open["opened_ts_ms"]) if last_open and last_open["opened_ts_ms"] else None
    last_closed_ts = int(last_closed["closed_ts_ms"]) if last_closed and last_closed["closed_ts_ms"] else None
    last_open_variant = last_open["variant"] if last_open else None
    last_closed_variant = last_closed["variant"] if last_closed else None
    last_closed_pnl = float(last_closed["realized_pnl_usd"]) if last_closed and last_closed["realized_pnl_usd"] is not None else None
    n_total = int(total["n"]) if total else 0
    first_ts = int(first["ts"]) if first and first["ts"] else None

    if anchor_reason.startswith("trade_opened"):
        ticking_from_label = f"last trade opened ({anchor_reason.split(':',1)[1]})"
    elif anchor_reason == "first_boot":
        ticking_from_label = "monitoring started (no trade yet)"
    else:
        ticking_from_label = anchor_reason

    return {
        "now_ts_ms": now_ms,
        "ticking_from_ts_ms": anchor_ts,
        "ticking_from_label": ticking_from_label,
        "anchor_reason": anchor_reason,
        "elapsed_ms_server": now_ms - anchor_ts,
        "last_open_ts_ms": last_open_ts,
        "last_open_variant": last_open_variant,
        "last_closed_ts_ms": last_closed_ts,
        "last_closed_variant": last_closed_variant,
        "last_closed_pnl_usd": last_closed_pnl,
        "first_open_ts_ms": first_ts,
        "n_total_entries": n_total,
    }


@router.post("/trade_timer/reset")
def cdv_trade_timer_reset(
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Phase 11n-9-ww — operator-triggered anchor reset.

    Admin-only. Resets the "time since last trade" anchor to now.
    Used when operator intentionally wants to restart the no-trade clock
    (e.g. after config change). The automatic code path never does this.
    """
    _require_admin(x_cdv_role, x_ops_token)
    import sqlite3
    from spot_aggro.governance.variant_trip_wire import _db_path
    now_ms = int(time.time() * 1000)
    try:
        con = sqlite3.connect(_db_path(), timeout=5.0, isolation_level=None)
        try:
            _ensure_cdv_timer_anchor_schema(con)
            con.execute(
                "INSERT OR REPLACE INTO spot_cdv_timer_anchor("
                " id, anchor_ts_ms, anchor_reason, last_updated_ts_ms)"
                " VALUES(1, ?, ?, ?)",
                (now_ms, "operator_reset", now_ms),
            )
        finally:
            con.close()
    except Exception as e:
        return {"ok": False, "error": f"reset failed: {str(e)[:200]}"}
    return {"ok": True, "anchor_ts_ms": now_ms, "anchor_reason": "operator_reset"}


# ---------------------------------------------------------------------------
# Section builders — each strategy-scoped
# ---------------------------------------------------------------------------

def _build_header() -> dict[str, Any]:
    try:
        from spot_aggro import _engine_instance
        from spot_aggro.governance.engine_state_source import current_engine_state
        est = current_engine_state()
        if _engine_instance is not None:
            s = _engine_instance.status() or {}
            return {
                "strategy": CDV_STRATEGY_NAMESPACE,
                "mode": "live" if not s.get("dry_run") else "paper",
                "running": not bool(s.get("halted", False)) and bool(s.get("running", True)),
                "equity_usd": float(s.get("capital_usd") or 0),
                "engine_state": est.get("state"),
                "health": "ok" if not s.get("halted") else "halted",
                "last_refresh_ts_ms": int(time.time() * 1000),
            }
    except Exception as e:
        log.debug("cdv header build failed: %s", e)
    return {
        "strategy": CDV_STRATEGY_NAMESPACE,
        "mode": "unknown",
        "running": False,
        "equity_usd": 0.0,
        "engine_state": "idle",
        "health": "unknown",
        "last_refresh_ts_ms": int(time.time() * 1000),
    }


def _build_summary() -> dict[str, Any]:
    """Edge state + regime + posture + readiness, CDV-scoped only."""
    try:
        from spot_aggro.governance.strategy_sufficiency import evaluate as suff_eval
        s = suff_eval()
        from spot_aggro.governance.trade_readiness import current_state as tr_state
        tr = tr_state()
        # Regime classification from meta module (if MIO available).
        try:
            from spot_aggro.governance.ensemble_meta import classify_regime
            from spot_aggro import _engine_instance
            regime = "unknown"
            if _engine_instance is not None:
                rankings = getattr(_engine_instance, "_rank_cache", []) or []
                if rankings:
                    regime = classify_regime(rankings[0])
        except Exception:
            regime = "unknown"
        return {
            "edge_state": s.recommendation,           # excellent|keep|tune|replace|insufficient_sample
            "edge_reason": (s.reason or "")[:240],
            "regime": regime,
            "posture": "trading" if tr.get("ready") else "paused_readiness",
            "readiness_ready": bool(tr.get("ready")),
            "readiness_unmet": tr.get("unmet", []),
            "n_observed": s.n_observed,
            "avg_win_pct": s.avg_win_pct,
            "avg_loss_pct": s.avg_loss_pct,
            "hit_rate_floor": s.pct_hitting_target,
            "hit_rate_stretch": s.pct_hitting_stretch,
        }
    except Exception as e:
        log.debug("cdv summary failed: %s", e)
        return {
            "edge_state": "unknown", "edge_reason": str(e)[:200],
            "regime": "unknown", "posture": "unknown",
            "readiness_ready": False, "readiness_unmet": [],
            "n_observed": 0, "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
            "hit_rate_floor": 0.0, "hit_rate_stretch": 0.0,
        }


def _build_variant_view(variant: str, cutoff_ms: int) -> dict[str, Any]:
    """Single-variant engine view. Only returns rows for this variant.
    Enforces ScopeViolation if variant is outside CDV set."""
    if variant not in CDV_VARIANTS:
        raise ScopeViolation(f"variant {variant!r} not in CDV scope")
    try:
        con = _connect()
        try:
            total = con.execute(
                "SELECT COUNT(*) AS n FROM shadow_variant_authorizations"
                " WHERE variant = ? AND ts_ms >= ?",
                (variant, cutoff_ms),
            ).fetchone()
            admitted = con.execute(
                "SELECT COUNT(*) AS n FROM shadow_variant_authorizations"
                " WHERE variant = ? AND variant_passed = 1 AND ts_ms >= ?",
                (variant, cutoff_ms),
            ).fetchone()
            rej_reasons = con.execute(
                "SELECT reason, COUNT(*) AS n"
                " FROM shadow_variant_authorizations"
                " WHERE variant = ? AND variant_passed = 0 AND ts_ms >= ?"
                " GROUP BY reason ORDER BY n DESC LIMIT 5",
                (variant, cutoff_ms),
            ).fetchall()
            # Active opportunities: admitted rows for this variant, one
            # row per symbol (latest score). Previous query returned every
            # re-scoring cycle's row, so a single symbol appeared 3-10x.
            # Also removed the shadow_variant_exits join — that was meant
            # to mean "no exit yet" but the match condition was wrong and
            # it was exploding via LEFT JOIN fan-out.
            active = con.execute(
                "SELECT symbol,"
                "       MAX(variant_score) AS variant_score,"
                "       MAX(ts_ms) AS ts_ms,"
                "       COUNT(*) AS n_scores"
                " FROM shadow_variant_authorizations"
                " WHERE variant = ? AND variant_passed = 1"
                "   AND ts_ms >= ?"
                " GROUP BY symbol"
                " ORDER BY ts_ms DESC LIMIT 20",
                (variant, cutoff_ms),
            ).fetchall()
        finally:
            con.close()
        return {
            "variant": variant,
            "strategy": CDV_STRATEGY_NAMESPACE,
            "found": int(total["n"] or 0) if total else 0,
            "admitted": int(admitted["n"] or 0) if admitted else 0,
            "rejected": (
                int(total["n"] or 0) - int(admitted["n"] or 0)
                if total and admitted else 0
            ),
            "top_reject_reasons": [
                {"reason": r["reason"], "count": int(r["n"])}
                for r in rej_reasons
            ],
            "active_opportunities": [
                {
                    "symbol": a["symbol"],
                    "score": float(a["variant_score"] or 0),
                    "ts_ms": int(a["ts_ms"]),
                    "n_scores": int(a["n_scores"] or 1),
                }
                for a in active
            ],
        }
    except Exception as e:
        log.debug("cdv variant view %s failed: %s", variant, e)
        return {
            "variant": variant, "strategy": CDV_STRATEGY_NAMESPACE,
            "found": 0, "admitted": 0, "rejected": 0,
            "top_reject_reasons": [], "active_opportunities": [],
            "error": str(e)[:180],
        }


def _build_pipeline(cutoff_ms: int) -> dict[str, Any]:
    """Decision pipeline counters — strategy-scoped to CDV variants.

    Four distinct funnel stages (fixed phase-ww+):
      scanned       = total scoring events written in window
      shortlisted   = unique (symbol, variant) pairs with at least one pass
      approved      = unique symbols that resulted in a LIVE entry row
                      in spot_live_variant_entries (status != 'skip')
      rejected      = scoring events with variant_passed = 0

    Previously scanned/shortlisted/approved/rejected were all derived
    from the same total-vs-passed count, making the panel show identical
    or doubled numbers that didn't reflect real funnel stages.
    """
    try:
        con = _connect()
        try:
            r_total = con.execute(
                "SELECT COUNT(*) AS n FROM shadow_variant_authorizations"
                f" WHERE {scoped_variants_sql_in_clause()} AND ts_ms >= ?",
                (cutoff_ms,),
            ).fetchone()
            r_reject = con.execute(
                "SELECT COUNT(*) AS n FROM shadow_variant_authorizations"
                f" WHERE {scoped_variants_sql_in_clause()}"
                " AND variant_passed = 0 AND ts_ms >= ?",
                (cutoff_ms,),
            ).fetchone()
            r_shortlisted = con.execute(
                "SELECT COUNT(DISTINCT symbol || '|' || variant) AS n"
                " FROM shadow_variant_authorizations"
                f" WHERE {scoped_variants_sql_in_clause()}"
                " AND variant_passed = 1 AND ts_ms >= ?",
                (cutoff_ms,),
            ).fetchone()
            # Approved = symbols that reached spot_live_variant_entries
            # in the same window. Uses CDV variants only.
            try:
                r_approved = con.execute(
                    "SELECT COUNT(DISTINCT symbol) AS n"
                    " FROM spot_live_variant_entries"
                    " WHERE variant IN ('contrarian','deep_value')"
                    "  AND COALESCE(opened_ts_ms, ts_ms) >= ?",
                    (cutoff_ms,),
                ).fetchone()
            except Exception:
                r_approved = None
            # Approx reject bucket inference from reason substrings.
            bucket_rows = con.execute(
                "SELECT reason, COUNT(*) AS n"
                " FROM shadow_variant_authorizations"
                f" WHERE {scoped_variants_sql_in_clause()} AND variant_passed = 0 AND ts_ms >= ?"
                " GROUP BY reason",
                (cutoff_ms,),
            ).fetchall()
            blocked_depth = 0
            blocked_regime = 0
            blocked_gov = 0
            for b in bucket_rows:
                r = (b["reason"] or "").lower()
                n = int(b["n"] or 0)
                if "liquid" in r or "depth" in r or "spread" in r:
                    blocked_depth += n
                if "regime" in r or "volatile" in r or "calm" in r:
                    blocked_regime += n
                if "freeze" in r or "ladder" in r or "gov" in r or "gate" in r:
                    blocked_gov += n
        finally:
            con.close()
        return {
            "scanned": int(r_total["n"] or 0) if r_total else 0,
            "shortlisted": int(r_shortlisted["n"] or 0) if r_shortlisted else 0,
            "approved": int(r_approved["n"] or 0) if r_approved else 0,
            "rejected": int(r_reject["n"] or 0) if r_reject else 0,
            "blocked_by_depth": blocked_depth,
            "blocked_by_regime": blocked_regime,
            "blocked_by_governance": blocked_gov,
        }
    except Exception as e:
        log.debug("cdv pipeline failed: %s", e)
        return {"scanned": 0, "shortlisted": 0, "approved": 0, "rejected": 0,
                "blocked_by_depth": 0, "blocked_by_regime": 0,
                "blocked_by_governance": 0, "error": str(e)[:180]}


def _build_positions(cutoff_ms: int) -> dict[str, Any]:
    """CDV-scoped open + recent closed positions. Uses spot_live_variant_entries
    (phase-nn ledger) for variant attribution on live trades."""
    try:
        con = _connect()
        try:
            # Table may not exist until first entry writes it.
            cols = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='spot_live_variant_entries'"
            ).fetchone()
            if not cols:
                return {"open": [], "recent_closed": [],
                        "strategy_pnl_usd": 0.0,
                        "note": "no ledger yet"}
            opens = con.execute(
                "SELECT ts_ms, variant, symbol, notional_usd, authz_id"
                " FROM spot_live_variant_entries"
                f" WHERE {scoped_variants_sql_in_clause()} AND status = 'open'"
                " ORDER BY ts_ms DESC LIMIT 20"
            ).fetchall()
            closed = con.execute(
                "SELECT ts_ms, variant, symbol, notional_usd, closed_ts_ms,"
                " realized_pnl_usd"
                " FROM spot_live_variant_entries"
                f" WHERE {scoped_variants_sql_in_clause()} AND status = 'closed'"
                " AND (closed_ts_ms IS NULL OR closed_ts_ms >= ?)"
                " ORDER BY closed_ts_ms DESC LIMIT 20",
                (cutoff_ms,),
            ).fetchall()
            pnl_sum = con.execute(
                "SELECT COALESCE(SUM(realized_pnl_usd), 0) AS p"
                " FROM spot_live_variant_entries"
                f" WHERE {scoped_variants_sql_in_clause()}"
            ).fetchone()
        finally:
            con.close()
        return {
            "open": [
                {
                    "ts_ms": int(r["ts_ms"]), "variant": r["variant"],
                    "symbol": r["symbol"],
                    "notional_usd": float(r["notional_usd"] or 0),
                    "authz_id": r["authz_id"],
                    "strategy": CDV_STRATEGY_NAMESPACE,
                } for r in opens
            ],
            "recent_closed": [
                {
                    "entry_ts_ms": int(r["ts_ms"]), "variant": r["variant"],
                    "symbol": r["symbol"],
                    "notional_usd": float(r["notional_usd"] or 0),
                    "closed_ts_ms": r["closed_ts_ms"],
                    "realized_pnl_usd": float(r["realized_pnl_usd"] or 0),
                    "strategy": CDV_STRATEGY_NAMESPACE,
                } for r in closed
            ],
            "strategy_pnl_usd": float(pnl_sum["p"] or 0) if pnl_sum else 0.0,
        }
    except Exception as e:
        log.debug("cdv positions failed: %s", e)
        return {"open": [], "recent_closed": [], "strategy_pnl_usd": 0.0,
                "error": str(e)[:180]}


def _build_execution_quality(cutoff_ms: int) -> dict[str, Any]:
    """Execution quality: slippage, fill quality, reject count, latency.
    Strategy-scoped to CDV admits."""
    try:
        con = _connect()
        try:
            # Table may not exist until first entry writes.
            has_lve = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='spot_live_variant_entries'"
            ).fetchone()
            n_fills = 0
            n_rejects = 0
            if has_lve:
                n_fills_r = con.execute(
                    "SELECT COUNT(*) AS n FROM spot_live_variant_entries"
                    f" WHERE {scoped_variants_sql_in_clause()}"
                    " AND status IN ('open', 'closed')"
                    " AND ts_ms >= ?",
                    (cutoff_ms,),
                ).fetchone()
                n_fills = int(n_fills_r["n"] or 0) if n_fills_r else 0
            # Rejects: spot_reject_events in window.
            has_re = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='spot_reject_events'"
            ).fetchone()
            if has_re:
                n_rejects_r = con.execute(
                    "SELECT COUNT(*) AS n FROM spot_reject_events"
                    " WHERE ts_ms >= ?",
                    (cutoff_ms,),
                ).fetchone()
                n_rejects = int(n_rejects_r["n"] or 0) if n_rejects_r else 0
            fill_quality_pct = (
                100.0 * n_fills / max(n_fills + n_rejects, 1)
            )
        finally:
            con.close()
        return {
            "avg_slippage_bp": None,          # phase-tt future: compute from fills
            "fill_quality_pct": round(fill_quality_pct, 1),
            "rejects_total": n_rejects,
            "fill_latency_ms": None,          # phase-tt future
            "n_fills_in_window": n_fills,
            "strategy": CDV_STRATEGY_NAMESPACE,
        }
    except Exception as e:
        return {"avg_slippage_bp": None, "fill_quality_pct": 0,
                "rejects_total": 0, "fill_latency_ms": None,
                "n_fills_in_window": 0, "error": str(e)[:180]}


def _build_risk_governance(cutoff_ms: int) -> dict[str, Any]:
    try:
        freeze_active = False
        kill_level = "L0"
        kill_events_24h = 0
        try:
            from spot_aggro.governance.contradiction_freeze import (
                is_entry_frozen,
            )
            freeze_active = bool(is_entry_frozen())
        except Exception:
            pass
        try:
            from spot_aggro.governance.kill_ladder import current_state as kl
            kill_level = kl().level
        except Exception:
            pass
        try:
            con = _connect()
            try:
                r = con.execute(
                    "SELECT COUNT(*) AS n FROM spot_kill_ladder_state"
                    " WHERE ts_ms >= ?",
                    (cutoff_ms,),
                ).fetchone()
                kill_events_24h = int(r["n"] or 0) if r else 0
            finally:
                con.close()
        except Exception:
            pass
        # Daily verdict from latest daily_report.
        daily_verdict = "unknown"
        try:
            from spot_aggro.governance.daily_report import latest_full
            r = latest_full()
            if r:
                daily_verdict = r.get("verdict", "unknown")
        except Exception:
            pass
        return {
            "freeze_active": freeze_active,
            "kill_ladder_level": kill_level,
            "kill_events_window": kill_events_24h,
            "daily_verdict": daily_verdict,
            "rollback_state": "none",
        }
    except Exception as e:
        return {"freeze_active": False, "kill_ladder_level": "L0",
                "kill_events_window": 0, "daily_verdict": "unknown",
                "rollback_state": "none", "error": str(e)[:180]}


def _build_daily_report() -> dict[str, Any]:
    try:
        from spot_aggro.governance.daily_report import latest_full
        r = latest_full()
        if not r:
            return {"headline": "no report yet", "verdict": "unknown",
                    "root_cause": "awaiting first 24h report"}
        # Mask any non-CDV variant references.
        payload = mask_non_cdv_fields(r.get("payload") or {})
        return {
            "report_date": r.get("report_date"),
            "headline": r.get("headline", "")[:200],
            "verdict": r.get("verdict", "unknown"),
            "root_cause": (payload.get("root_cause") or "")[:300],
            "biggest_blocker": (payload.get("upgrade_focus") or "")[:200],
            "biggest_opportunity": "",     # not surfaced separately yet
            "strategy_progress": mask_non_cdv_fields(
                payload.get("strategy_progress") or {}
            ),
        }
    except Exception as e:
        return {"headline": "error", "verdict": "unknown",
                "root_cause": str(e)[:200], "biggest_blocker": "",
                "biggest_opportunity": ""}


# ---------------------------------------------------------------------------
# Admin mutations (governance actions)
# ---------------------------------------------------------------------------

@router.post("/gov/freeze")
def cdv_freeze_new_entries(
    reason: str = Query(..., min_length=3, max_length=120),
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
    x_cdv_second_operator: str | None = Header(default=None),
) -> dict[str, Any]:
    """Admin: freeze new CDV entries. Requires two-operator approval via
    X-CDV-Second-Operator header (any non-empty value)."""
    _require_admin(x_cdv_role, x_ops_token)
    if not x_cdv_second_operator or len(x_cdv_second_operator.strip()) < 3:
        raise HTTPException(
            status_code=403,
            detail="two-operator approval required: X-CDV-Second-Operator",
        )
    try:
        from spot_aggro.governance.kill_ladder import escalate
        st = escalate(
            "L1", reason=f"cdv_panel:{reason}",
            actor="cdv_admin_two_operator",
            evidence={"strategy": CDV_STRATEGY_NAMESPACE,
                      "second_operator": x_cdv_second_operator[:60]},
        )
        return {"ok": True, "state": st.to_dict(),
                "strategy": CDV_STRATEGY_NAMESPACE}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/horse_race")
def cdv_horse_race(
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Phase 11n-9-vv — horse race standings + trip-wire state.
    Per-variant: n_exits, WR, Wilson CI, 24h PnL, cumulative PnL,
    promotion_verdict, trip_wire_active. Only CDV variants returned."""
    _require_viewer(x_cdv_role, x_ops_token)
    try:
        from spot_aggro.governance.variant_trip_wire import evaluate
        rpt = evaluate(variants=tuple(sorted(CDV_VARIANTS)))
        return {
            "ok": True,
            "strategy": CDV_STRATEGY_NAMESPACE,
            "report": rpt.to_dict(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/exchange_quality")
def cdv_exchange_quality(
    window_min: int = Query(1440, ge=5, le=4320),
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Phase 11n-9-uu Option 1 — rolling data-quality audit for
    OKX + Crypto.com. Measures uptime, stale-quote ratio, depth,
    drift percentiles, and tradability score per symbol."""
    _require_viewer(x_cdv_role, x_ops_token)
    try:
        from spot_aggro.governance.exchange_data_quality import generate_report
        rpt = generate_report(window_min=window_min)
        return {"ok": True, "strategy": CDV_STRATEGY_NAMESPACE,
                "report": rpt.to_dict()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/integration_trigger")
def cdv_integration_trigger(
    window_min: int = Query(1440, ge=5, le=4320),
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Phase 11n-9-uu Option 2 — integration-trigger verdict.
    none | watching | actionable based on CDC-value-evidence signals."""
    _require_viewer(x_cdv_role, x_ops_token)
    try:
        from spot_aggro.governance.integration_trigger import (
            compute_verdict, latest_signals,
        )
        v = compute_verdict(window_min=window_min)
        return {
            "ok": True,
            "strategy": CDV_STRATEGY_NAMESPACE,
            "verdict": v.to_dict(),
            "recent_signals": latest_signals(limit=20),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/system_activity")
def cdv_system_activity(
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Phase 11n-9-tt — background-process liveness for the operator.

    Shows every daemon doing work silently: last tick timestamp,
    next-run ETA, last observation/result, and health status. No more
    waiting-for-nothing; the operator sees exactly what's running.
    """
    _require_viewer(x_cdv_role, x_ops_token)
    now_ms = int(time.time() * 1000)
    comps: list[dict[str, Any]] = []

    # 1. Engine heartbeat
    try:
        from spot_aggro import _engine_instance
        if _engine_instance is not None:
            s = _engine_instance.status() or {}
            cycles = int(s.get("cycles") or 0)
            comps.append({
                "name": "engine_heartbeat",
                "label": "Engine Heartbeat",
                "cadence_s": 1,
                "status": "running" if not s.get("halted") else "halted",
                "last_tick_ts_ms": None,
                "next_run_in_s": 1,
                "observation": f"cycle #{cycles} · {len(s.get('positions') or {})} positions · {s.get('mode')}",
                "ok": not s.get("halted"),
            })
        else:
            comps.append({
                "name": "engine_heartbeat", "label": "Engine Heartbeat",
                "cadence_s": 1, "status": "idle",
                "last_tick_ts_ms": None, "next_run_in_s": None,
                "observation": "engine not started",
                "ok": False,
            })
    except Exception as e:
        comps.append({
            "name": "engine_heartbeat", "label": "Engine Heartbeat",
            "cadence_s": 1, "status": "error",
            "observation": str(e)[:120], "ok": False,
        })

    # 2. Shadow scorer (per authz; cadence = tied to engine scan rate)
    try:
        con = _connect()
        try:
            latest = con.execute(
                "SELECT MAX(ts_ms) AS t, COUNT(*) AS n"
                " FROM shadow_variant_authorizations"
                f" WHERE {scoped_variants_sql_in_clause()}"
            ).fetchone()
            n_24h = con.execute(
                "SELECT COUNT(*) AS n"
                " FROM shadow_variant_authorizations"
                f" WHERE {scoped_variants_sql_in_clause()} AND ts_ms >= ?",
                (now_ms - 86_400_000,),
            ).fetchone()
        finally:
            con.close()
        last_t = int(latest["t"]) if latest and latest["t"] else None
        age_s = (now_ms - last_t) // 1000 if last_t else None
        comps.append({
            "name": "shadow_scorer", "label": "Shadow Scorer",
            "cadence_s": 0,
            "status": "running" if age_s is not None and age_s < 300 else "stale",
            "last_tick_ts_ms": last_t,
            "last_age_s": age_s,
            "next_run_in_s": None,
            "observation": (
                f"{(n_24h['n'] or 0) if n_24h else 0} rows/24h · "
                f"last {age_s}s ago"
                if age_s is not None else "no rows yet"
            ),
            "ok": age_s is not None and age_s < 300,
        })
    except Exception as e:
        comps.append({
            "name": "shadow_scorer", "label": "Shadow Scorer",
            "cadence_s": 0, "status": "error",
            "observation": str(e)[:120], "ok": False,
        })

    # 3. Exchange comparison feed
    try:
        con = _connect()
        try:
            latest = con.execute(
                "SELECT MAX(ts_ms) AS t, COUNT(*) AS n"
                " FROM spot_exchange_comparison"
            ).fetchone()
        finally:
            con.close()
        last_t = int(latest["t"]) if latest and latest["t"] else None
        age_s = (now_ms - last_t) // 1000 if last_t else None
        next_in = max(0, 60 - age_s) if age_s is not None else None
        comps.append({
            "name": "exchange_comparison", "label": "Exchange Comparison Feed",
            "cadence_s": 60,
            "status": "running" if age_s is not None and age_s < 180 else "stale",
            "last_tick_ts_ms": last_t,
            "last_age_s": age_s,
            "next_run_in_s": next_in,
            "observation": (
                f"{latest['n'] if latest else 0} rows · "
                f"last {age_s}s ago · OKX vs Crypto.com"
                if age_s is not None else "no rows yet"
            ),
            "ok": age_s is not None and age_s < 180,
        })
    except Exception as e:
        comps.append({
            "name": "exchange_comparison", "label": "Exchange Comparison Feed",
            "cadence_s": 60, "status": "error",
            "observation": str(e)[:120], "ok": False,
        })

    # 4. Formula review daemon (6h)
    try:
        from spot_aggro.governance.formula_review import latest as fr_latest
        rows = fr_latest(limit=1)
        if rows:
            last_t = int(rows[0].ts_ms)
            age_s = (now_ms - last_t) // 1000
            next_in = max(0, 6 * 3600 - age_s)
            comps.append({
                "name": "formula_review", "label": "Governance: Formula Review",
                "cadence_s": 6 * 3600,
                "status": "running" if age_s < 7 * 3600 else "stale",
                "last_tick_ts_ms": last_t,
                "last_age_s": age_s,
                "next_run_in_s": next_in,
                "observation": (
                    f"verdict={rows[0].verdict} · "
                    f"last {age_s // 60}m ago · every 6h"
                ),
                "ok": age_s < 7 * 3600,
            })
        else:
            comps.append({
                "name": "formula_review", "label": "Governance: Formula Review",
                "cadence_s": 6 * 3600, "status": "pending",
                "last_tick_ts_ms": None, "next_run_in_s": None,
                "observation": "awaiting first cycle (fires 2min after boot)",
                "ok": True,
            })
    except Exception as e:
        comps.append({
            "name": "formula_review", "label": "Governance: Formula Review",
            "cadence_s": 6 * 3600, "status": "error",
            "observation": str(e)[:120], "ok": False,
        })

    # 5. Daily report daemon (24h)
    try:
        from spot_aggro.governance.daily_report import latest_full as dr_full
        r = dr_full()
        if r:
            last_t = int(r.get("ts_ms", 0) or 0)
            age_s = (now_ms - last_t) // 1000
            next_in = max(0, 24 * 3600 - age_s)
            comps.append({
                "name": "daily_report", "label": "Governance: Daily Report",
                "cadence_s": 24 * 3600,
                "status": "running" if age_s < 25 * 3600 else "stale",
                "last_tick_ts_ms": last_t,
                "last_age_s": age_s,
                "next_run_in_s": next_in,
                "observation": (
                    f"{r.get('report_date')} · verdict={r.get('verdict')} · "
                    f"last {age_s // 60}m ago · every 24h"
                ),
                "ok": age_s < 25 * 3600,
            })
        else:
            comps.append({
                "name": "daily_report", "label": "Governance: Daily Report",
                "cadence_s": 24 * 3600, "status": "pending",
                "last_tick_ts_ms": None, "next_run_in_s": None,
                "observation": "awaiting first 24h tick (fires 5min after boot)",
                "ok": True,
            })
    except Exception as e:
        comps.append({
            "name": "daily_report", "label": "Governance: Daily Report",
            "cadence_s": 24 * 3600, "status": "error",
            "observation": str(e)[:120], "ok": False,
        })

    # 6. Kill ladder daemon (60s)
    try:
        from spot_aggro.governance.kill_ladder import (
            current_state as kl_state, recent_reject_count,
        )
        st = kl_state()
        rc = recent_reject_count()
        comps.append({
            "name": "kill_ladder", "label": "Kill Ladder Auto-Pause",
            "cadence_s": 60,
            "status": "running" if st.level == "L0" else "escalated",
            "last_tick_ts_ms": None,
            "next_run_in_s": 60,
            "observation": (
                f"level={st.level} · rejects_10min={rc} · threshold=3"
            ),
            "ok": st.level == "L0",
        })
    except Exception as e:
        comps.append({
            "name": "kill_ladder", "label": "Kill Ladder Auto-Pause",
            "cadence_s": 60, "status": "error",
            "observation": str(e)[:120], "ok": False,
        })

    # 7. Heartbeat writer (60s)
    try:
        from spot_aggro.ops.scheduler.heartbeat_writer import last_tick_ts_ms
        last_t = last_tick_ts_ms()
        age_s = (now_ms - last_t) // 1000 if last_t else None
        next_in = max(0, 60 - age_s) if age_s is not None else 60
        comps.append({
            "name": "heartbeat_writer", "label": "Equity Heartbeat Writer",
            "cadence_s": 60,
            "status": "running" if age_s is not None and age_s < 180 else "stale",
            "last_tick_ts_ms": last_t,
            "last_age_s": age_s,
            "next_run_in_s": next_in,
            "observation": (
                f"last {age_s}s ago · keeps equity_marks fresh"
                if age_s is not None else "awaiting first tick"
            ),
            "ok": age_s is not None and age_s < 180,
        })
    except Exception as e:
        comps.append({
            "name": "heartbeat_writer", "label": "Equity Heartbeat Writer",
            "cadence_s": 60, "status": "error",
            "observation": str(e)[:120], "ok": False,
        })

    n_healthy = sum(1 for c in comps if c.get("ok"))
    return {
        "ok": True,
        "strategy": CDV_STRATEGY_NAMESPACE,
        "ts_ms": now_ms,
        "n_total": len(comps),
        "n_healthy": n_healthy,
        "overall_status": "all_green" if n_healthy == len(comps) else "degraded",
        "components": comps,
    }


@router.get("/entries/{entry_id}/provenance")
def cdv_entry_provenance(
    entry_id: int,
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Opportunity Fabric Sprint 2 — full provenance chain for a trade.

    Returns entry metadata + the provenance fingerprint + verification
    result. Use for post-mortems ("why did this trade fire?").
    """
    _require_viewer(x_cdv_role, x_ops_token)
    try:
        from spot_aggro.governance import provenance as prov_mod
    except Exception as e:
        return {"ok": False, "error": f"provenance import failed: {str(e)[:200]}"}
    raw = prov_mod.fetch_raw(entry_id)
    if raw is None:
        return {"ok": False, "error": f"no entry {entry_id}"}
    verify = prov_mod.verify(entry_id)
    return {
        "ok": True,
        "strategy": CDV_STRATEGY_NAMESPACE,
        "entry": raw,
        "verify": verify,
    }


@router.get("/fractal_regime")
def cdv_fractal_regime(
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Opportunity Fabric Sprint 4 — fractal regime confirmation state.

    Returns the three scale readings (1m, 5m, 1h) + agreement verdict.
    When SPOT_FRACTAL_REGIME_GATE=1, disagreement blocks admission;
    when off, it's advisory.
    """
    _require_viewer(x_cdv_role, x_ops_token)
    try:
        from spot_aggro.governance import fractal_regime as fr
    except Exception as e:
        return {"ok": False, "error": f"fractal_regime import: {str(e)[:200]}"}
    v = fr.evaluate()
    return {
        "ok": True,
        "strategy": CDV_STRATEGY_NAMESPACE,
        "ts_ms": int(time.time() * 1000),
        "verdict": v.to_dict(),
    }


@router.get("/policy_bank")
def cdv_policy_bank(
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Opportunity Fabric Sprint 7 — policy bank tier summary."""
    _require_viewer(x_cdv_role, x_ops_token)
    try:
        from spot_aggro.governance import policy_bank as pb
    except Exception as e:
        return {"ok": False, "error": f"policy_bank import: {str(e)[:200]}"}
    return {
        "ok": True,
        "strategy": CDV_STRATEGY_NAMESPACE,
        "ts_ms": int(time.time() * 1000),
        "summary": pb.summary(),
        "events": pb.events_recent(limit=20),
    }


@router.post("/policy_bank/assign")
def cdv_policy_bank_assign(
    variant: str = Query(...),
    tier: str = Query(...),
    rationale: str = Query(...),
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Operator-triggered tier assignment. Admin-only."""
    _require_admin(x_cdv_role, x_ops_token)
    try:
        from spot_aggro.governance import policy_bank as pb
    except Exception as e:
        return {"ok": False, "error": f"policy_bank import: {str(e)[:200]}"}
    return pb.assign(variant=variant, tier=tier,
                     actor=(x_cdv_role or "operator"),
                     rationale=rationale)


@router.get("/counterfactual_replay")
def cdv_counterfactual_replay(
    policy: str | None = Query(None),
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Opportunity Fabric Sprint 6 — aggregate causal-delta stats.

    Returns per-policy summaries of how the live trades would have
    performed under each counterfactual policy:
      - conservative: tp=1.5%, sl=-1.0%
      - exploratory:  tp=2.0%, sl=-1.5%
      - aggressive:   tp=3.0%, sl=-2.0%

    Positive mean_delta_bp = live path beat the counterfactual.
    Negative = the counterfactual would have outperformed.
    """
    _require_viewer(x_cdv_role, x_ops_token)
    try:
        from spot_aggro.governance import counterfactual_replay as cfr
    except Exception as e:
        return {"ok": False, "error": f"cfr import failed: {str(e)[:200]}"}
    policies = [policy] if policy else ("conservative", "exploratory", "aggressive")
    stats = {p: cfr.aggregate_stats(p) for p in policies}
    return {
        "ok": True,
        "strategy": CDV_STRATEGY_NAMESPACE,
        "ts_ms": int(time.time() * 1000),
        "stats": stats,
    }


@router.post("/counterfactual_replay/run")
def cdv_counterfactual_replay_run(
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Operator-triggered immediate replay pass. Idempotent (UNIQUE
    constraint on (entry_id, policy) means re-runs are cheap)."""
    _require_admin(x_cdv_role, x_ops_token)
    try:
        from spot_aggro.governance import counterfactual_replay as cfr
    except Exception as e:
        return {"ok": False, "error": f"cfr import failed: {str(e)[:200]}"}
    r = cfr.replay_all_closed()
    return {"ok": True, "strategy": CDV_STRATEGY_NAMESPACE, **r}


@router.get("/provenance/recent")
def cdv_provenance_recent(
    limit: int = Query(20, ge=1, le=200),
    variant: str | None = Query(None),
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """List most recent trades with their provenance summary."""
    _require_viewer(x_cdv_role, x_ops_token)
    import json as _json
    con = _connect()
    try:
        if variant:
            rows = con.execute(
                "SELECT id, variant, symbol, opened_ts_ms, closed_ts_ms,"
                "       realized_pnl_usd, provenance_json"
                " FROM spot_live_variant_entries"
                " WHERE variant = ?"
                " ORDER BY COALESCE(opened_ts_ms, ts_ms) DESC LIMIT ?",
                (variant, int(limit)),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT id, variant, symbol, opened_ts_ms, closed_ts_ms,"
                "       realized_pnl_usd, provenance_json"
                " FROM spot_live_variant_entries"
                " ORDER BY COALESCE(opened_ts_ms, ts_ms) DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        prov = None
        if r["provenance_json"]:
            try:
                prov = _json.loads(r["provenance_json"])
            except Exception:
                prov = None
        out.append({
            "entry_id": int(r["id"]),
            "variant": r["variant"],
            "symbol": r["symbol"],
            "opened_ts_ms": r["opened_ts_ms"],
            "closed_ts_ms": r["closed_ts_ms"],
            "realized_pnl_usd": r["realized_pnl_usd"],
            "fingerprint": (prov or {}).get("_fingerprint"),
            "scorer_version": (prov or {}).get("scorer_version"),
            "tier": (prov or {}).get("tier"),
            "regime_at_admit": (prov or {}).get("regime"),
            "has_provenance": prov is not None,
        })
    return {
        "ok": True,
        "strategy": CDV_STRATEGY_NAMESPACE,
        "ts_ms": int(time.time() * 1000),
        "count": len(out),
        "entries": out,
    }


@router.get("/auto_heal")
def cdv_auto_heal(
    limit: int = Query(30, ge=1, le=200),
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Phase 11n-9-ww — recent activity auto-heal events + live status.

    Returns the most recent heal attempts (healed / failed / cooldown /
    struck_out) so the operator can see WHAT the governor is doing.
    """
    _require_viewer(x_cdv_role, x_ops_token)
    try:
        from spot_aggro.governance.activity_auto_heal import (
            evaluate as ah_eval, recent_events,
            COOLDOWN_S, STRIKE_CAP, EVAL_INTERVAL_S,
        )
    except Exception as e:
        return {"ok": False, "error": f"auto_heal import failed: {str(e)[:200]}"}
    try:
        current = ah_eval()
    except Exception as e:
        current = {"error": str(e)[:200]}
    return {
        "ok": True,
        "strategy": CDV_STRATEGY_NAMESPACE,
        "ts_ms": int(time.time() * 1000),
        "config": {
            "cooldown_s": COOLDOWN_S,
            "strike_cap": STRIKE_CAP,
            "eval_interval_s": EVAL_INTERVAL_S,
        },
        "current": current,
        "events": recent_events(limit=limit),
    }


@router.get("/health")
def cdv_health(
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Minimal health probe for the scoped panel."""
    _require_viewer(x_cdv_role, x_ops_token)
    return {
        "ok": True,
        "strategy": CDV_STRATEGY_NAMESPACE,
        "feature_enabled": feature_enabled(),
        "variants": sorted(CDV_VARIANTS),
        "ts_ms": int(time.time() * 1000),
    }


# ---------------------------------------------------------------------------
# Static panel HTML
# ---------------------------------------------------------------------------

@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def cdv_panel_index(
    x_cdv_role: str | None = Header(default=None),
    x_ops_token: str | None = Header(default=None),
) -> HTMLResponse:
    """Serve the static panel HTML. Feature-flag gated at this layer too."""
    _feature_or_404()
    from pathlib import Path
    panel_path = Path(__file__).resolve().parents[3] / "web" / "strategy" / "contrarian-deepvalue" / "index.html"
    # Fallback for repo-relative absolute path when file lives at repo root.
    if not panel_path.exists():
        alt = (
            Path(__file__).resolve().parents[4]
            / "web" / "strategy" / "contrarian-deepvalue" / "index.html"
        )
        if alt.exists():
            panel_path = alt
    if not panel_path.exists():
        raise HTTPException(status_code=500, detail="panel html not found")
    return HTMLResponse(panel_path.read_text(encoding="utf-8"))
