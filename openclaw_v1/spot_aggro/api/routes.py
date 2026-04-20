"""
SPOT AGGRO HTTP routes — spot-owned router.

Mounted under /spot_aggro. Every spot-owned endpoint lives in this file.

URL prefix policy:
  - /spot_aggro/*      — spot engine endpoints. Spot-owned.
  - /spot_aggro/ops/*  — SHARED OPERATOR INFRASTRUCTURE (PnL, status,
                         trades, notifications, kill, llm cost, governor,
                         watchdog, consensus, halt). All legacy-engine
                         packages were purged in phase-11n-9-q/x.

Execution-only surface for the tier-toggle endpoints. None of the routes
here consult capital or account equity. None gate trading on a balance.
The forensic_v2/** package is read via callable imports; this file never
modifies forensic_v2 source.
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Query


router = APIRouter(prefix="/spot_aggro", tags=["spot_aggro"])


# ---------------------------------------------------------------------------
# Admin-token guard. Kept here (rather than pulling from
# spot_aggro.ops.routes_ops) so the engine router has no hard dep on the
# ops router.
# ---------------------------------------------------------------------------

def _require_admin(x_ops_token: str | None) -> None:
    expected = os.environ.get("OPS_ADMIN_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=403,
            detail="OPS_ADMIN_TOKEN not set on this host",
        )
    if (x_ops_token or "").strip() != expected:
        raise HTTPException(status_code=401, detail="invalid admin token")


# Phase 11d final — server-side build indicator. Dashboard reads this at
# load to cross-check against its own <meta name="dashboard-build">. If
# they disagree, the operator has a stale server OR a stale HTML and the
# dashboard shows a red banner identifying which side is behind.
# Execution-only. Never touches capital. Safe to expose (reveals only the
# build tag, which is already in the repo's HTML).
SERVER_BUILD = "phase-11n-9-ll-2026-04-20"


@router.get("/build")
def spot_aggro_build() -> dict[str, Any]:
    """Return the server build tag + a compact feature manifest so the
    dashboard can assert which patches are live without hitting each
    endpoint individually. Pure read; no auth required."""
    return {
        "build": SERVER_BUILD,
        "features": {
            "auth_ping": True,                   # Phase 11c
            "trades_tier_column": True,          # Phase 11b
            "trades_limit_max_5000": True,       # Phase 11d
            "reconciled_short_circuit_top": True,  # Phase 11d engine fix
            "daily_system_audit": True,          # Phase 11j
            "research_agent": True,              # Phase 11l
            "scenario_runner": True,             # Phase 11n
            "research_truth_gov": True,          # Phase 11n (Layer 4)
            "card_truth_gov": True,              # Phase 11n (Layer 5)
            "decision_engine": True,             # Phase 11n-2
            "decision_truth_gov": True,          # Phase 11n-2 (Layer 6)
            "conversion_rate": True,             # Phase 11n-2
            "loop_novelty_gov": True,            # Phase 11n-3 (Layer 7)
            "auto_orchestrator": True,           # Phase 11n-3
            "daily_alpha": True,                 # Phase 11n-9
            "daily_alpha_gov": True,             # Phase 11n-9 (Layer 8)
            "daily_alpha_executor": True,        # Phase 11n-9-c (opt-in)
            "pre_trade_gov": True,               # Phase 11n-9-b (universal gate)
            "reconciled_sweeper": True,          # Phase 11n-9-d (opt-in)
            "tp_agent": True,                    # Phase 11n-9-i (4-agent team)
            "tp_sell_gov": True,                 # Phase 11n-9-i (Layer 9)
            "alert_center": True,                # Phase 11n-9-k (P3)
            "incident_mode": True,               # Phase 11n-9-l (P4)
            "label_translator": True,            # Phase 11n-9-m (P5)
            "mobile_drilldown": True,            # Phase 11n-9-m (P5)
            "tab_aware_audit": True,             # Phase 11n-9-p (skip hidden tabs)
            "legacy_engines_purged": True,       # Phase 11n-9-q/x (all legacy-engine names removed)
            "mobile_single_overlay": True,       # Phase 11n-9-r (overlay state machine)
            "legacy_purge_gov": True,              # Phase 11n-9-s (Layer 10 trip-wire)
            "legacy_deep_forensic_gov": True,      # Phase 11n-9-t (Layer 11 deep scan)
            "mobile_tab_router": True,           # Phase 11n-9-u (mobile nav tabs route)
            "economic_truth_gov": True,          # Phase 11n-9-y (Layer 12 Wilson-bounded expectancy)
            "contradiction_freeze": True,        # Phase 11n-9-y (Layer 3 active freeze)
            "gate_enforcement_blocked": True,    # Phase 11n-9-y (GateBlocked named exception)
            "net_pnl_accounting": True,          # Phase 11n-9-y (fees+slippage subtracted)
            "shadow_scorer_ab": True,            # Phase 11n-9-z (Layer 6 A/B)
            "decision_quality_gov": True,        # Phase 11n-9-z (Layer 2 decile + rank)
            "card_truth_mismatch_m1_m7": True,   # Phase 11n-9-z (cross-card rules)
            "escalation_ladder_active": True,    # Phase 11n-9-z (T+0/15/30/60 rungs)
            "universe_gatekeeper": True,         # Phase 11n-9-aa (auto-admit/deprecate cells)
            "trade_readiness_mechanical": True,  # Phase 11n-9-aa (ready_to_trade flag)
            "start_endpoint_honors_readiness": True,  # Phase 11n-9-aa (409 when not ready)
            "swarm_prefilter": True,             # Phase 11n-9-bb (LLM cost gate)
            "llm_cost_telemetry": True,          # Phase 11n-9-bb (/gov/llm_cost_24h)
            "action_button_state_machine": True, # Phase 11n-9-cc (allowed/suggested/disabled buttons)
            "engine_state_source": True,         # Phase 11n-9-dd (canonical engine-state)
            "heartbeat_writer": True,            # Phase 11n-9-dd (equity_marks auto-heal 60s)
            "card_truth_respects_halt": True,    # Phase 11n-9-dd (IDLE not FAIL on halt)
            "strategy_variants_three_way": True, # Phase 11n-9-ee (control/contrarian/mean-rev horse race)
            "variant_horse_race": True,          # Phase 11n-9-ee (first-to-200 promotion)
            "kill_ladder_l1_l4": True,           # Phase 11n-9-ff (4-tier escalation)
            "exec_integrity_2pct_risk": True,    # Phase 11n-9-ff (per-trade risk cap)
            "exec_integrity_price_tol_15bp": True,  # Phase 11n-9-ff (price drift cap)
            "reject_storm_autopause": True,      # Phase 11n-9-ff (3-in-10min -> L1)
            "model_registry": True,              # Phase 11n-9-gg (versioned models + code hashes)
            "shadow_model_version_stamp": True,  # Phase 11n-9-gg (version on every shadow authz)
            "promotion_min_age_30d": True,       # Phase 11n-9-gg (anti-flash-promotion)
            "retrain_queue_on_freeze": True,     # Phase 11n-9-gg (auto-open tickets on T3 freeze)
            "immutable_ledger_hashchain": True,   # Phase 11n-9-hh (tamper-evident trade log)
            "canary_health_check": True,          # Phase 11n-9-hh (5-probe resilience check)
            "degraded_mode_auto_fallback": True,  # Phase 11n-9-hh (limit-only on canary fail)
            "recovery_playbook": True,            # Phase 11n-9-hh (post-restart forensic summary)
            "aml_audit_export": True,             # Phase 11n-9-hh (MAS-grade JSON bundle)
            "human_in_loop_L3_L4": True,          # Phase 11n-9-hh (two-person rule — shipped ff)
            "live_variant_gate": True,            # Phase 11n-9-ii (contrarian+deep_value live path)
            "deep_value_variant": True,           # Phase 11n-9-ii (WR>=55% filter)
            "live_exposure_cap_50usd": True,      # Phase 11n-9-ii ($50 total exposure cap)
            "live_dd_kill_10usd": True,           # Phase 11n-9-ii (-$10 session DD auto-halt)
            "market_verified_fills": True,        # Phase 11n-9-jj (poll fetch_order until filled; fixes phantom positions)
            "verified_exit_from_balance": True,   # Phase 11n-9-kk (sell from live OKX balance, not engine-tracked qty)
            "governance_board": True,             # Phase 11n-9-ll (strategy sufficiency, edge contribution, formula review, daily report)
            "target_2pct_per_trade": True,        # Phase 11n-9-ll (>=2% per-trade target)
            "daily_auto_report_24h": True,        # Phase 11n-9-ll (auto-generated daily governance report)
        },
    }


# Phase 11j — daily system-integrity audit.
# Pure read; no auth required. The audit itself is deterministic and only
# inspects the live engine + DB; it never mutates state beyond writing its
# own result row.
@router.get("/audit/system")
def spot_aggro_audit_system_latest() -> dict[str, Any]:
    """Return the most recent audit run + history summary. If no audit
    has run yet, returns a 'never run' status so the dashboard can prompt
    the operator to trigger the first run manually."""
    try:
        from spot_aggro.governance.daily_system_auditor import latest_run, history
        latest = latest_run()
        hist = history(limit=14)
        return {
            "ok": True,
            "latest": latest,
            "history": hist,
            "status": "never_run" if latest is None else "ready",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/audit/system/run")
def spot_aggro_audit_system_run(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Trigger a daily-audit run on demand. Admin-only because the run
    hits the DB, imports every engine module, and writes a result row."""
    _require_admin(x_ops_token)
    from spot_aggro.governance.daily_system_auditor import run_and_persist
    run = run_and_persist()
    return {"ok": True, "run": run.to_dict()}


# Phase 11n-9-s — Legacy Purge Governor (Layer 10).
# Read-only scan for legacy-engine package fragments or URL surfaces.
# Admin-only run endpoint performs deletion of stray filesystem artifacts.
@router.get("/gov/legacy_purge")
def spot_aggro_legacy_purge_latest() -> dict[str, Any]:
    """Latest legacy-purge scan result + short history."""
    try:
        from spot_aggro.governance.legacy_purge_gov import latest, history
        latest_row = latest()
        return {
            "ok": True,
            "latest": latest_row,
            "history": history(limit=20),
            "status": "never_run" if latest_row is None else "ready",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/gov/legacy_purge/run")
def spot_aggro_legacy_purge_run(
    purge: bool = Query(True, description="Delete stray artifacts (default true)"),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Trigger a legacy-purge scan on demand. Admin-only because
    purge=True deletes filesystem artifacts (legacy directories, logs,
    config)."""
    _require_admin(x_ops_token)
    from spot_aggro.governance.legacy_purge_gov import run_once
    r = run_once(purge=purge)
    return {"ok": True, "result": r.to_dict()}


# Phase 11n-9-t — Legacy Deep Forensic Governor (Layer 11).
# Superset of Layer 10: scans YAML/JSON/shell/.env/bytecode + string
# literals for every legacy-engine variant.
@router.get("/gov/legacy_deep")
def spot_aggro_legacy_deep_latest() -> dict[str, Any]:
    """Latest deep-forensic scan + short history."""
    try:
        from spot_aggro.governance.legacy_deep_forensic_gov import latest, history
        latest_row = latest()
        return {
            "ok": True,
            "latest": latest_row,
            "history": history(limit=20),
            "status": "never_run" if latest_row is None else "ready",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/gov/legacy_deep/run")
def spot_aggro_legacy_deep_run(
    purge: bool = Query(True, description="Delete stray artifacts"),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Trigger a deep-forensic scan on demand. Admin-only."""
    _require_admin(x_ops_token)
    from spot_aggro.governance.legacy_deep_forensic_gov import run_once
    r = run_once(purge=purge)
    return {"ok": True, "result": r.to_dict()}


# Phase 11n-9-y — Layer 12: Economic Truth Governor (non-enforcing).
# Returns Wilson-bounded per-cell expectancy. The Contradiction Freeze
# daemon consumes this to score econ_score. Dashboard shows cell-level
# pass/warn/fail so the operator sees which (tier, symbol, module)
# subset is bleeding.
@router.get("/gov/economic_truth")
def spot_aggro_economic_truth_latest() -> dict[str, Any]:
    """Latest Layer 12 verdict + 20-row history."""
    try:
        from spot_aggro.governance.economic_truth_gov import latest, history
        return {
            "ok": True,
            "latest": latest(),
            "history": history(limit=20),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/gov/economic_truth/run")
def spot_aggro_economic_truth_run(
    window: int = Query(500, ge=50, le=5000),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Trigger an economic-truth scan on demand. Admin-only because
    the result is persisted and can influence downstream layers."""
    _require_admin(x_ops_token)
    from spot_aggro.governance.economic_truth_gov import run_once
    v = run_once(window=window)
    return {"ok": True, "result": v.to_dict()}


# Phase 11n-9-y — Layer 3 active: Contradiction Freeze.
@router.get("/gov/contradiction_freeze")
def spot_aggro_contradiction_freeze_state() -> dict[str, Any]:
    """Returns the current freeze state + recent tick history."""
    try:
        from spot_aggro.governance.contradiction_freeze import (
            current_state, history, is_entry_frozen,
        )
        return {
            "ok": True,
            "entry_frozen": is_entry_frozen(),
            "state": current_state(),
            "ticks": history(limit=50),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/gov/contradiction_freeze/ack")
def spot_aggro_contradiction_freeze_ack(
    payload: dict[str, Any] = Body(...),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Operator acknowledgement: release the freeze iff the verbatim
    primary_cause string matches the one stored. Admin-only."""
    _require_admin(x_ops_token)
    from spot_aggro.governance.contradiction_freeze import ack
    cause = (payload.get("primary_cause") or "").strip()
    if not cause:
        raise HTTPException(status_code=422, detail="primary_cause required")
    ok, msg = ack(cause)
    return {"ok": ok, "message": msg}


@router.post("/gov/root_cause/ack")
def spot_aggro_root_cause_ack(
    payload: dict[str, Any] = Body(...),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Alias of /gov/contradiction_freeze/ack — phase-z spec naming.
    Operator acknowledgement of the root cause (verbatim primary_cause
    string) releases the freeze."""
    return spot_aggro_contradiction_freeze_ack(payload, x_ops_token)


# Phase 11n-9-z — Layer 2: Decision Quality Governor.
@router.get("/gov/decision_quality")
def spot_aggro_decision_quality_latest() -> dict[str, Any]:
    """Latest Layer 2 decile + rank-monotonicity verdict per cell."""
    try:
        from spot_aggro.governance.decision_quality_gov import latest, history
        return {
            "ok": True,
            "latest": latest(),
            "history": history(limit=20),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/gov/decision_quality/run")
def spot_aggro_decision_quality_run(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Trigger a Layer 2 scan on demand. Admin-only."""
    _require_admin(x_ops_token)
    from spot_aggro.governance.decision_quality_gov import run_once
    v = run_once()
    return {"ok": True, "result": v.to_dict()}


# Phase 11n-9-bb — LLM cost telemetry.
#
# Reports REAL-pricing estimated cost, not the historical `cost_usd`
# column (which under-reported by ~400× until this phase's fix).
# Call count × per-model real price is the operator's authoritative view.
_REAL_PRICE_PER_CALL = {
    # Approximate call-volume-weighted avg cost per call given the
    # swarm's ~500-char prompts + 200-char responses (175 tokens avg):
    # haiku $1.5/MTok -> $0.000263 per call
    # opus  $25/MTok  -> $0.00438  per call
    # gpt-4o-mini $0.30/MTok -> $0.0000525 per call
    # gemini-flash $0.20/MTok -> $0.000035 per call
    # mistral-small $0.30/MTok -> $0.0000525 per call
    # deepseek $0.20/MTok -> $0.000035 per call
    ("anthropic", "claude-haiku-4-5"):            0.000263,
    ("anthropic", "claude-opus-4-6"):             0.00438,
    ("openai",    "gpt-4o-mini"):                 0.0000525,
    ("gemini",    "gemini-2.5-flash"):            0.000035,
    ("mistral",   "mistral-small-latest"):        0.0000525,
    ("openrouter","deepseek/deepseek-chat-v3"):   0.000035,
}


@router.get("/gov/llm_cost_24h")
def spot_aggro_llm_cost_24h() -> dict[str, Any]:
    """Aggregate llm_cost over the last 24h. Returns BOTH the stored
    cost (may be historical / under-reported) and a real-pricing
    estimate (volume × real per-call cost)."""
    try:
        import os, sqlite3, time
        db = (os.environ.get("TRADE_DB_PATH")
              or os.environ.get("CLAW_DB_PATH") or "trades.db")
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        cut = int(time.time() * 1000) - 24 * 3600 * 1000
        rows = con.execute(
            "SELECT provider, model, COUNT(*) n,"
            " SUM(cost_usd) cost_usd,"
            " AVG(latency_ms) avg_lat"
            " FROM llm_cost WHERE ts_ms >= ?"
            " GROUP BY provider, model ORDER BY n DESC", (cut,),
        ).fetchall()
        stored_cost = 0.0
        real_cost = 0.0
        total_calls = 0
        per = []
        for r in rows:
            key = (r["provider"], r["model"])
            n = r["n"] or 0
            real_per_call = _REAL_PRICE_PER_CALL.get(key, 0.0005)
            row_real = n * real_per_call
            per.append({
                "provider": r["provider"], "model": r["model"],
                "n": n,
                "stored_cost_usd": round(r["cost_usd"] or 0, 4),
                "real_cost_usd": round(row_real, 4),
                "avg_latency_ms": int(r["avg_lat"] or 0),
            })
            stored_cost += r["cost_usd"] or 0
            real_cost += row_real
            total_calls += n
        con.close()
        return {
            "ok": True,
            "window_hours": 24,
            "total_calls": total_calls,
            "stored_cost_usd": round(stored_cost, 4),
            "real_cost_usd": round(real_cost, 4),
            "projected_monthly_usd": round(real_cost * 30, 2),
            "by_provider_model": per,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


# Phase 11n-9-z — Card-Truth Mismatch Detector (M1-M7).
@router.get("/gov/card_truth_mismatch")
def spot_aggro_card_truth_mismatch_latest() -> dict[str, Any]:
    """Latest findings from the M1-M7 mismatch detector."""
    try:
        from spot_aggro.governance.card_truth_mismatch import latest_findings
        return {"ok": True, "findings": latest_findings(limit=50)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/gov/card_truth_mismatch/run")
def spot_aggro_card_truth_mismatch_run(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Trigger a mismatch scan on demand. Admin-only."""
    _require_admin(x_ops_token)
    from spot_aggro.governance.card_truth_mismatch import run_once
    s = run_once()
    return {"ok": True, "result": s.to_dict()}


# Phase 11n-9-z — Shadow Scorer A/B validation.
@router.get("/gov/shadow_scorer")
def spot_aggro_shadow_scorer_latest() -> dict[str, Any]:
    """Latest A/B comparison verdict + history. Non-enforcing — the
    operator reviews this table before deciding whether to commit a
    scoring.py sign flip."""
    try:
        from spot_aggro.governance.shadow_scorer import latest, history
        return {"ok": True, "latest": latest(), "history": history(limit=20)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/gov/shadow_scorer/run")
def spot_aggro_shadow_scorer_run(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Trigger an A/B comparison on demand. Admin-only."""
    _require_admin(x_ops_token)
    from spot_aggro.governance.shadow_scorer import run_comparison
    ab = run_comparison()
    return {"ok": True, "result": ab.to_dict()}


# Phase 11n-9-aa — Trade Readiness flag (mechanical release).
# Phase 11n-9-dd — Engine-state source + heartbeat visibility.
@router.get("/gov/engine_state")
def spot_aggro_engine_state() -> dict[str, Any]:
    """Canonical engine state + last heartbeat tick.

    Lets the dashboard decide whether to show RUNNING / IDLE / HALTED /
    CRASHED pills instead of mistakenly flipping to STALE/FAIL when the
    operator has intentionally stopped the engine.
    """
    try:
        from spot_aggro.governance.engine_state_source import (
            current_engine_state,
        )
        state = current_engine_state()
    except Exception as exc:  # noqa: BLE001
        state = {"state": "idle", "error": str(exc)[:160]}
    try:
        from spot_aggro.ops.scheduler import heartbeat_writer
        hb_ts = heartbeat_writer.last_tick_ts_ms()
    except Exception:
        hb_ts = None
    return {
        "ok": True,
        "engine_state": state,
        "heartbeat": {
            "last_tick_ts_ms": hb_ts,
            "interval_s": 60,
        },
        "ts_ms": int(time.time() * 1000),
    }


# Phase 11n-9-ll — Governance Board endpoints.
@router.get("/gov/strategy_sufficiency")
def spot_aggro_strategy_sufficiency(
    target_pct: float = Query(0.02, ge=0.0, le=1.0),
    window_n: int = Query(100, ge=5, le=1000),
) -> dict[str, Any]:
    """Sufficiency test: can the current strategy deliver >=target_pct
    per closed trade? Default target: 2%/trade."""
    try:
        from spot_aggro.governance.strategy_sufficiency import evaluate
        v = evaluate(target_pct=target_pct, window_n=window_n)
        return {"ok": True, "verdict": v.to_dict()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/gov/edge_contribution")
def spot_aggro_edge_contribution(
    window_n: int = Query(200, ge=10, le=2000),
) -> dict[str, Any]:
    """Spearman per-factor vs realized PnL. Identifies which signal
    components are creating vs destroying edge."""
    try:
        from spot_aggro.governance.edge_contribution import analyze
        r = analyze(window_n=window_n)
        return {"ok": True, "report": r.to_dict()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/gov/formula_review/latest")
def spot_aggro_formula_review_latest(
    limit: int = Query(4, ge=1, le=50),
) -> dict[str, Any]:
    """Last N formula-review verdicts. Daemon runs this every 6h."""
    try:
        from spot_aggro.governance.formula_review import latest
        return {
            "ok": True,
            "verdicts": [v.to_dict() for v in latest(limit=limit)],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/gov/formula_review/run")
def spot_aggro_formula_review_run(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Force an immediate formula-review brainstorm cycle."""
    _require_admin(x_ops_token)
    try:
        from spot_aggro.governance.formula_review import run
        v = run()
        return {"ok": True, "verdict": v.to_dict()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/gov/daily_report/latest")
def spot_aggro_daily_report_latest() -> dict[str, Any]:
    """The most recent 24h governance report (full markdown + payload)."""
    try:
        from spot_aggro.governance.daily_report import latest_full
        r = latest_full()
        if r is None:
            return {"ok": True, "report": None,
                    "note": "no reports generated yet"}
        return {"ok": True, "report": r}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/gov/daily_report/list")
def spot_aggro_daily_report_list(
    limit: int = Query(30, ge=1, le=365),
) -> dict[str, Any]:
    """Recent 30 daily report summaries."""
    try:
        from spot_aggro.governance.daily_report import latest
        return {"ok": True, "reports": latest(limit=limit)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/gov/daily_report/run")
def spot_aggro_daily_report_run(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Force immediate generation of today's daily report."""
    _require_admin(x_ops_token)
    try:
        from spot_aggro.governance.daily_report import generate
        r = generate()
        return {
            "ok": True,
            "report_date": r.report_date,
            "verdict": r.verdict,
            "headline": r.headline,
            "markdown": r.markdown,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


# Phase 11n-9-ii — Live variant gate state.
@router.get("/gov/live_variant_gate")
def spot_aggro_live_variant_gate() -> dict[str, Any]:
    """Current state of the live-variant gate: enabled variants,
    current exposure, session PnL, headroom to caps."""
    try:
        from spot_aggro.governance import live_variant_gate as _lvg
        return {
            "ok": True,
            "active": _lvg.live_variants_active(),
            "enabled_variants": list(_lvg._enabled_variants()),
            "current_exposure_usd": _lvg._current_exposure_usd(),
            "max_exposure_usd": _lvg._max_exposure_usd(),
            "session_pnl_usd": _lvg._live_session_pnl_usd(),
            "max_dd_usd": _lvg._max_dd_usd(),
            "kill_ladder_blocks": _lvg._kill_ladder_blocks(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


# Phase 11n-9-hh — Layer 3 Resilience & Compliance.
@router.get("/gov/canary_health")
def spot_aggro_canary_health() -> dict[str, Any]:
    """Five-probe resilience check. degraded_mode auto-activates on fail.
    Note: `ok` means "endpoint succeeded"; `healthy` means all probes passed.
    """
    try:
        from spot_aggro.governance.resilience import canary_health
        body = canary_health()
        # Promote the inner `ok` to `healthy` so the endpoint-level `ok`
        # always reflects endpoint success, not probe pass-state.
        healthy = bool(body.pop("ok", False))
        return {"ok": True, "healthy": healthy, **body}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/gov/immutable_ledger/verify")
def spot_aggro_ledger_verify() -> dict[str, Any]:
    """Run verify_chain() over the entire ledger. Returns chain verdict."""
    try:
        from spot_aggro.governance.immutable_ledger import (
            verify_chain, head_hash,
        )
        v = verify_chain()
        return {"ok": True, "verdict": v.to_dict(), "head": head_hash()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/gov/recovery_playbook")
def spot_aggro_recovery_playbook(
    window_min: int = Query(60, ge=1, le=1440),
) -> dict[str, Any]:
    """Post-restart forensic summary. Read-only; no replay."""
    try:
        from spot_aggro.governance.resilience import recovery_playbook
        return recovery_playbook(window_min=window_min)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/gov/aml_audit_export")
def spot_aggro_aml_audit_export(
    start_ts_ms: int | None = Query(None),
    end_ts_ms: int | None = Query(None),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """AML/MAS-grade audit bundle. Admin-only because it includes full
    ledger rows + kill-ladder history. Returns canonical JSON doc."""
    _require_admin(x_ops_token)
    try:
        from spot_aggro.governance.resilience import export_aml_audit
        return export_aml_audit(start_ts_ms=start_ts_ms, end_ts_ms=end_ts_ms)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


# Phase 11n-9-gg — Layer 2 Model Governance: registry + retrain queue.
@router.get("/gov/model_registry")
def spot_aggro_model_registry() -> dict[str, Any]:
    """List every registered model + its current version + code hash."""
    try:
        from spot_aggro.governance.model_registry import all_models
        return {
            "ok": True,
            "models": [m.to_dict() for m in all_models()],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/gov/retrain_queue")
def spot_aggro_retrain_queue() -> dict[str, Any]:
    """Retrain tickets (pending + recent 50 resolved/cancelled)."""
    try:
        from spot_aggro.governance.retrain_queue import all_tickets
        return {
            "ok": True,
            "tickets": [t.to_dict() for t in all_tickets(limit=50)],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/gov/retrain_queue/open")
def spot_aggro_retrain_queue_open(
    target_model: str = Query(...),
    reason: str = Query("operator"),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro.governance.retrain_queue import open_ticket
    jid = open_ticket(target_model, reason, payload={"source": "operator"})
    return {"ok": bool(jid), "job_id": jid}


@router.post("/gov/retrain_queue/resolve")
def spot_aggro_retrain_queue_resolve(
    job_id: int = Query(...),
    resolution: str = Query(...),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro.governance.retrain_queue import resolve_ticket
    ok = resolve_ticket(job_id, resolution)
    return {"ok": ok}


# Phase 11n-9-ff — Layer 1 Execution Integrity + 4-tier kill ladder.
@router.get("/gov/kill_ladder")
def spot_aggro_kill_ladder() -> dict[str, Any]:
    """Current ladder rung (L0..L4) + reject-storm count."""
    try:
        from spot_aggro.governance.kill_ladder import (
            current_state, recent_reject_count,
        )
        st = current_state()
        return {
            "ok": True,
            "state": st.to_dict(),
            "reject_count_10min": recent_reject_count(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/gov/kill_ladder/escalate")
def spot_aggro_kill_ladder_escalate(
    level: str = Query(..., description="L1 | L2 | L3 | L4"),
    reason: str = Query(..., description="Reason code"),
    x_ops_token: str | None = Header(default=None),
    x_oversight_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Operator-driven escalation. L1/L2 require ops token only; L3/L4
    require BOTH ops + oversight tokens (two-person rule)."""
    _require_admin(x_ops_token)
    lv = level.upper()
    if lv not in ("L1", "L2", "L3", "L4"):
        raise HTTPException(status_code=400, detail="level must be L1..L4")
    if lv in ("L3", "L4"):
        expected = os.environ.get("OPS_OVERSIGHT_TOKEN", "")
        if not expected or x_oversight_token != expected:
            raise HTTPException(
                status_code=403,
                detail="L3/L4 requires X-Oversight-Token (two-person rule)",
            )
    from spot_aggro.governance.kill_ladder import escalate
    st = escalate(lv, reason=reason, actor="operator")  # type: ignore[arg-type]
    return {"ok": True, "state": st.to_dict()}


@router.post("/gov/kill_ladder/release")
def spot_aggro_kill_ladder_release(
    target: str = Query("L0", description="Target rung (usually L0)"),
    x_ops_token: str | None = Header(default=None),
    x_oversight_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Operator release. Releasing from L3/L4 requires the two-person rule."""
    _require_admin(x_ops_token)
    from spot_aggro.governance.kill_ladder import current_state, release
    cur = current_state()
    if cur.level in ("L3", "L4"):
        expected = os.environ.get("OPS_OVERSIGHT_TOKEN", "")
        if not expected or x_oversight_token != expected:
            raise HTTPException(
                status_code=403,
                detail="releasing from L3/L4 requires X-Oversight-Token",
            )
    st = release(target.upper(), actor="operator", reason="manual_release")  # type: ignore[arg-type]
    return {"ok": True, "state": st.to_dict()}


# Phase 11n-9-ee — Three-way strategy-variant horse race.
@router.get("/gov/three_way_shadow")
def spot_aggro_three_way_shadow() -> dict[str, Any]:
    """Current standings of control / contrarian / mean_reversion.

    Returns the most recent persisted verdict (or computes one if none
    exists yet). Read-only — does not trigger a new evaluation. Use
    /gov/three_way_shadow/run to force one.
    """
    try:
        from spot_aggro.governance.three_way_shadow import current_state
        return {"ok": True, "state": current_state()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/gov/three_way_shadow/run")
def spot_aggro_three_way_shadow_run(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Force a fresh evaluation of the three-way horse race. Admin-only."""
    _require_admin(x_ops_token)
    try:
        from spot_aggro.governance.three_way_shadow import evaluate
        v = evaluate()
        return {"ok": True, "verdict": v.to_dict()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/gov/trade_readiness")
def spot_aggro_trade_readiness_state() -> dict[str, Any]:
    """Returns the current ready_to_trade flag + unmet conditions +
    recent history. Dashboard should render a green/red pill bound to
    this."""
    try:
        from spot_aggro.governance.trade_readiness import (
            current_state, history,
        )
        return {"ok": True, "state": current_state(), "ticks": history(limit=50)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/gov/trade_readiness/run")
def spot_aggro_trade_readiness_run(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Trigger a readiness re-evaluation on demand. Admin-only."""
    _require_admin(x_ops_token)
    from spot_aggro.governance.trade_readiness import evaluate
    t = evaluate()
    return {"ok": True, "result": t.to_dict()}


# Phase 11n-9-aa — Universe Gatekeeper.
@router.get("/gov/universe")
def spot_aggro_universe_state() -> dict[str, Any]:
    """Full admissions table (admitted + deprecated_auto + deprecated_manual)."""
    try:
        from spot_aggro.governance.universe_gatekeeper import all_admissions
        return {
            "ok": True,
            "admissions": [a.to_dict() for a in all_admissions()],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/gov/universe/run")
def spot_aggro_universe_run(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Trigger a gatekeeper tick. Applies admission + deprecation
    lifecycle rules based on the latest Layer 1 verdict. Admin-only."""
    _require_admin(x_ops_token)
    from spot_aggro.governance.universe_gatekeeper import run_tick
    t = run_tick()
    return {"ok": True, "result": t.to_dict()}


# Phase 11l — Win-Rate Research Agent.
# GET is public (dashboard reads latest + history). POST is admin-only
# because running the agent can soft-halt tier toggles (execution-lane
# state change — matches the tier-toggle POST auth rule).
@router.get("/research/latest")
def spot_aggro_research_latest() -> dict[str, Any]:
    """Return the most recent research report + history summary."""
    try:
        from spot_aggro.governance.research_agent import latest_report, history
        latest = latest_report()
        hist = history(limit=24)
        return {
            "ok": True,
            "latest": latest,
            "history": hist,
            "status": "never_run" if latest is None else "ready",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/research/history")
def spot_aggro_research_history(
    start: str | None = Query(None, description="ISO-8601 start (inclusive)"),
    end: str | None = Query(None, description="ISO-8601 end (inclusive)"),
    tier: str | None = Query(None, description="Filter: A+ | A | B | C"),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Phase 11m — history endpoint with start/end/tier filters.

    Dates are ISO-8601 (e.g. '2026-04-19T00:00:00Z'). Returns a list of
    summary rows each carrying `audit_rollup_id` + `snapshot_id` so the
    caller can cross-reference evidence."""
    from spot_aggro.governance.research_agent import history
    start_ms: int | None = None
    end_ms: int | None = None
    if start:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            start_ms = int(dt.timestamp() * 1000)
        except Exception:
            return {"ok": False, "error": f"bad start: {start!r}"}
    if end:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            end_ms = int(dt.timestamp() * 1000)
        except Exception:
            return {"ok": False, "error": f"bad end: {end!r}"}
    if tier is not None and tier not in ("A+", "A", "B", "C"):
        return {"ok": False, "error": f"bad tier: {tier!r}"}
    rows = history(limit=limit, start_ts_ms=start_ms, end_ts_ms=end_ms, tier=tier)
    return {"ok": True, "history": rows, "count": len(rows)}


@router.post("/research/run")
def spot_aggro_research_run(
    x_ops_token: str | None = Header(default=None),
    window_h: int = Query(24, ge=1, le=168),
    status: str = Query("interim", pattern="^(interim|final)$"),
) -> dict[str, Any]:
    """Trigger a research pass on demand. Admin-only — the pass can
    flip tier execution toggles based on win-rate thresholds."""
    _require_admin(x_ops_token)
    from spot_aggro.governance.research_agent import run_and_persist
    r = run_and_persist(window_h=int(window_h), status=status)
    return {"ok": True, "report": r.to_dict()}


# Phase 11c — lightweight auth probe. Never raises; always returns 200 with
# a structured reason so the dashboard can tell the operator exactly what's
# wrong without needing to read server logs. Safe to expose: it confirms
# match/mismatch but does NOT echo the expected secret. Execution-only
# surface — this endpoint never touches capital or trading state.
@router.get("/auth/ping")
def spot_aggro_auth_ping(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    expected = os.environ.get("OPS_ADMIN_TOKEN", "").strip()
    if not expected:
        return {
            "ok": False,
            "reason": "no_server_secret",
            "hint": "OPS_ADMIN_TOKEN is not set on the host. Add it to .env and restart uvicorn.",
        }
    supplied = (x_ops_token or "").strip()
    if not supplied:
        return {
            "ok": False,
            "reason": "no_client_token",
            "hint": "Click 'Set Token' on the dashboard and paste your OPS_ADMIN_TOKEN.",
        }
    if supplied != expected:
        return {
            "ok": False,
            "reason": "mismatch",
            "hint": "The token sent by the dashboard does not match this host's OPS_ADMIN_TOKEN.",
        }
    return {"ok": True, "reason": "valid"}


# ===========================================================================
# Engine status / lifecycle
# ===========================================================================

@router.get("/status")
def spot_aggro_status() -> dict[str, Any]:
    """Status of the SPOT AGGRO squeeze-pressure engine.

    Always returns the same canonical shape regardless of engine state
    so downstream governors / cards have one contract. When engine is
    not started, numeric fields read 0 and `mode="idle"`.
    """
    try:
        from spot_aggro import _engine_instance
        if _engine_instance is None:
            return {
                "engine": "spot_aggro",
                "mode": "idle",
                "cycles": 0,
                "capital_usd": 0.0,
                "halted": False,
                "trades_today": 0,
                "positions": {},
                "running": False,
                "reason": "not started",
            }
        return _engine_instance.status()
    except Exception as e:
        return {"running": False, "error": str(e)[:120]}


@router.post("/start")
def spot_aggro_start(
    force: bool = Query(False, description="Admin bypass; requires OPS_ADMIN_TOKEN. Do not use unless you understand the implication."),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Start the SPOT AGGRO engine in background.

    Phase 11n-9-aa: honors the mechanical trade-readiness flag. If
    ready_to_trade=False, returns HTTP 409 with the unmet-condition
    list. `force=true` is accepted but logged as a manual override;
    the flag itself still applies at entry time, so force only
    affects the start-up acceptance, not whether trades fire.
    """
    _require_admin(x_ops_token)
    from spot_aggro.governance.trade_readiness import current_state
    s = current_state()
    if not s.get("ready") and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "engine_not_ready_to_trade",
                "unmet": s.get("unmet", []),
                "last_evaluated_ts_ms": s.get("last_evaluated_ts_ms"),
                "hint": ("Fix each unmet condition, wait for the "
                         "readiness daemon to re-evaluate, then retry. "
                         "Pass force=true to start anyway; the engine "
                         "entry path still honors the flag so trades "
                         "will be skipped until ready."),
            },
        )
    from spot_aggro import start_engine
    # Phase 11n-9-hh follow-up: honor SPOT_DRY_RUN / TRADE_DRY_RUN env so
    # paper-mode operators don't accidentally hit live when they restart.
    _dry = (
        os.environ.get("SPOT_DRY_RUN", "0").strip() == "1"
        or os.environ.get("TRADE_DRY_RUN", "0").strip() == "1"
    )
    start_engine(dry_run=_dry)
    return {
        "ok": True, "engine": "spot_aggro",
        "forced": bool(force), "dry_run": _dry,
    }


# Phase 11n-9-ii — emergency close-all. Used when transitioning from
# inherited-positions state into a clean-slate live run. Admin-only.
# Uses the engine's _close_position which honors dry_run; in LIVE mode
# this sends real OKX sells.
@router.post("/positions/close_all")
def spot_aggro_positions_close_all(
    reason: str = Query("operator_close_all", description="Audit reason"),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    try:
        from spot_aggro import _engine_instance
        if _engine_instance is None:
            return {"ok": False, "error": "engine_not_started",
                    "closed": []}
        # Snapshot symbols so we don't mutate while iterating.
        syms = list(_engine_instance.state.positions.keys())
        if not syms:
            return {"ok": True, "closed": [], "note": "no open positions"}
        # Schedule closures on the engine's asyncio loop so OKX calls
        # happen in the correct thread. Best-effort fire-and-forget;
        # we return the list of intents. Each close logs + persists.
        import asyncio
        import threading
        results: dict[str, str] = {}

        def _run_closures() -> None:
            async def _do() -> None:
                for sym in syms:
                    try:
                        await _engine_instance._close_position(sym, reason)
                        results[sym] = "closed"
                    except Exception as e:  # noqa: BLE001
                        results[sym] = f"error: {str(e)[:80]}"
            asyncio.run(_do())

        t = threading.Thread(target=_run_closures, name="spot-close-all", daemon=True)
        t.start()
        t.join(timeout=30.0)
        return {
            "ok": True,
            "requested": syms,
            "results": results,
            "reason": reason,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/stop")
def spot_aggro_stop(x_ops_token: str | None = Header(default=None)) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro import stop_engine
    stop_engine()
    return {"ok": True, "stopped": True}


# ===========================================================================
# Decision funnel
# ===========================================================================

@router.get("/funnel")
def spot_aggro_funnel(
    window_min: int = Query(60, ge=1, le=1440,
                             description="Time window in minutes"),
) -> dict[str, Any]:
    """Unified decision-funnel counters over a single time window.

    All 7 stages are computed from the same [now - window_min, now] slice so
    the ratios are mathematically comparable. `scored` and `tier_passed` come
    from the engine's in-memory scoring ring buffer; consensus and order
    counters come from DB tables filtered on `ts_ms`.
    """
    from shared.persistence import state as persist
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - window_min * 60 * 1000

    # --- scored + tier_passed from engine ring buffer ---
    scored = 0
    tier_passed = 0
    try:
        from spot_aggro import _engine_instance
        if _engine_instance is not None:
            for (ts_ms, _sym, _comp, tier) in list(
                _engine_instance.state.scoring_window
            ):
                if ts_ms >= since_ms:
                    scored += 1
                    if tier is not None:
                        tier_passed += 1
    except Exception:
        pass

    con = persist._connect()
    try:
        consensus_fired = con.execute(
            "SELECT COUNT(*) FROM consensus_log WHERE ts_ms >= ?",
            (since_ms,),
        ).fetchone()[0]
        consensus_passed = con.execute(
            "SELECT COUNT(*) FROM consensus_log "
            "WHERE ts_ms >= ? AND vetoed = 0 AND consensus_score >= 0.20",
            (since_ms,),
        ).fetchone()[0]
        orders_entered = con.execute(
            "SELECT COUNT(*) FROM trade_log "
            "WHERE ts_ms >= ? AND action = 'enter'",
            (since_ms,),
        ).fetchone()[0]
        orders_rejected = con.execute(
            "SELECT COUNT(*) FROM trade_log "
            "WHERE ts_ms >= ? AND action = 'reject'",
            (since_ms,),
        ).fetchone()[0]
        orders_exited = con.execute(
            "SELECT COUNT(*) FROM trade_log "
            "WHERE ts_ms >= ? AND action = 'exit'",
            (since_ms,),
        ).fetchone()[0]
    finally:
        con.close()

    return {
        "window_min": window_min,
        "window_start_ms": since_ms,
        "window_end_ms": now_ms,
        "scored": int(scored),
        "tier_passed": int(tier_passed),
        "consensus_fired": int(consensus_fired),
        "consensus_passed": int(consensus_passed),
        "orders_entered": int(orders_entered),
        "orders_rejected": int(orders_rejected),
        "orders_exited": int(orders_exited),
    }


# ===========================================================================
# Forensic Truth (v2) — frozen package, called via imports only
# ===========================================================================

@router.get("/forensic_v2/list")
def spot_aggro_forensic_v2_list(
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """List recent 5-LLM forensic-truth runs."""
    from spot_aggro.forensic_v2 import list_reports
    return {"runs": list_reports(limit=limit)}


@router.get("/forensic_v2/{report_id}")
def spot_aggro_forensic_v2_get(report_id: str) -> dict[str, Any]:
    from spot_aggro.forensic_v2 import get_report
    rec = get_report(report_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="report not found")
    return rec


@router.get("/forensic_v2/{report_id}/pdf")
def spot_aggro_forensic_v2_pdf(report_id: str):
    """Stream the PDF for a forensic report. Renders on demand if the
    stored pdf_path is missing or the file has been deleted."""
    from fastapi.responses import FileResponse
    from spot_aggro.forensic_v2 import get_report
    from spot_aggro.forensic_v2 import pdf_renderer
    rec = get_report(report_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="report not found")
    pdf_path = rec.get("pdf_path")
    if not pdf_path or not os.path.exists(pdf_path):
        try:
            pdf_path = pdf_renderer.render_pdf(rec)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"render failed: {e}")
    return FileResponse(
        pdf_path, media_type="application/pdf",
        filename=f"forensic_v2_{report_id}.pdf",
    )


@router.post("/forensic_v2/run")
def spot_aggro_forensic_v2_run(
    window_h: float = Query(6.0, ge=0.25, le=72.0),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Generate a new forensic-truth report. Admin-gated (LLM cost)."""
    _require_admin(x_ops_token)
    from spot_aggro.forensic_v2 import generate_report
    engine_status = None
    try:
        from spot_aggro import _engine_instance
        if _engine_instance is not None:
            engine_status = _engine_instance.status()
    except Exception:
        pass
    return generate_report(
        window_h=window_h,
        window_label="manual" if window_h != 6.0 else "rolling_6h",
        engine_status=engine_status,
    )


# ===========================================================================
# Coin memory
# ===========================================================================

@router.get("/coin_memory")
def spot_aggro_coin_memory(
    min_trades: int = Query(1, ge=0, le=1000),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """List (symbol, tier, regime) accuracy buckets, worst pnl first."""
    from spot_aggro import coin_memory
    return {
        "enabled": coin_memory.enabled(),
        "buckets": coin_memory.list_buckets(min_trades=min_trades, limit=limit),
    }


@router.post("/coin_memory/clear")
def spot_aggro_coin_memory_clear(
    symbol: str = Query(...),
    tier: str = Query(...),
    regime: str = Query(...),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Admin escape hatch: drop a specific memory bucket."""
    _require_admin(x_ops_token)
    from spot_aggro import coin_memory
    cleared = coin_memory.clear_bucket(symbol=symbol, tier=tier, regime=regime)
    return {"ok": True, "cleared": cleared,
            "symbol": symbol, "tier": tier, "regime": regime}


# ===========================================================================
# Swarm research / stats / holdings
# ===========================================================================

@router.get("/swarm")
def spot_aggro_swarm() -> dict[str, Any]:
    """5-LLM swarm intelligence — per-coin verdicts + cycle stats."""
    try:
        from spot_aggro.swarm.runner import get_swarm_state
        return get_swarm_state().state_dict()
    except Exception as e:
        return {"error": str(e)}


@router.get("/stats")
def spot_aggro_stats() -> dict[str, Any]:
    """Lifetime stats from DB — survives restarts."""
    try:
        from shared.persistence.state import _connect
        con = _connect()
        enters = con.execute("SELECT COUNT(*) as n FROM trade_log WHERE action='enter'").fetchone()["n"]
        exits = con.execute("SELECT COUNT(*) as n FROM trade_log WHERE action='exit'").fetchone()["n"]
        rejects = con.execute("SELECT COUNT(*) as n FROM trade_log WHERE action='reject'").fetchone()["n"]
        total_pnl = con.execute("SELECT COALESCE(SUM(pnl_usd),0) as s FROM trade_log WHERE action='exit'").fetchone()["s"]
        total_fee = con.execute("SELECT COALESCE(SUM(ABS(fee_usd)),0) as s FROM trade_log").fetchone()["s"]
        wins = con.execute("SELECT COUNT(*) as n FROM trade_log WHERE action='exit' AND pnl_usd > 0.001").fetchone()["n"]
        losses = con.execute("SELECT COUNT(*) as n FROM trade_log WHERE action='exit' AND pnl_usd < -0.001").fetchone()["n"]
        fee_rows = con.execute(
            "SELECT symbol, COUNT(*) as txns, COALESCE(SUM(ABS(fee_usd)),0) as fees, "
            "COALESCE(SUM(ABS(notional_usd)),0) as volume "
            "FROM trade_log WHERE action IN ('enter','exit') GROUP BY symbol ORDER BY volume DESC"
        ).fetchall()
        est_fee = sum(float(r["volume"]) * 0.0002 for r in fee_rows)
        actual_fee = float(total_fee)
        use_fee = actual_fee if actual_fee > 0.01 else est_fee
        unique_coins = len(fee_rows)
        con.close()
        return {
            "total_txns": enters + exits,
            "total_enters": enters,
            "total_exits": exits,
            "total_rejects": rejects,
            "wins": wins,
            "losses": losses,
            "flats": exits - wins - losses,
            "win_rate": round(wins / max(exits, 1), 4),
            "total_pnl": round(total_pnl, 4),
            "total_fee_actual": round(actual_fee, 4),
            "total_fee_estimated": round(est_fee, 4),
            "total_fee": round(use_fee, 4),
            "avg_fee_per_coin": round(use_fee / max(unique_coins, 1), 4),
            "unique_coins": unique_coins,
            "avg_pnl_per_trade": round(total_pnl / max(exits, 1), 4),
            "avg_win": round(total_pnl / max(wins, 1), 4) if wins > 0 else 0,
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/holdings")
def spot_aggro_holdings() -> dict[str, Any]:
    """ALL actual OKX spot holdings — not just engine-tracked positions."""
    try:
        import asyncio
        from shared.adapters.okx_unified import OKXUnified
        a = OKXUnified(engine="spot_aggro")
        bal = asyncio.run(asyncio.to_thread(a._client.fetch_balance))
        total = bal.get("total", {})
        from spot_aggro import _engine_instance
        engine_pos = {}
        if _engine_instance:
            engine_pos = _engine_instance.state.positions
        holdings = []
        for coin, amt in sorted(total.items()):
            amt = float(amt or 0)
            if amt <= 0 or coin in ("USDT", "SGD", "USD"):
                continue
            sym = f"{coin}-USDT"
            ep = engine_pos.get(sym, None)
            try:
                ticker = asyncio.run(asyncio.to_thread(
                    a._client.fetch_ticker, f"{coin}/USDT"))
                price = float(ticker.get("last") or 0)
            except Exception:
                price = 0
            value_usd = amt * price
            holdings.append({
                "coin": coin,
                "symbol": sym,
                "amount": amt,
                "price": price,
                "value_usd": round(value_usd, 2),
                "tracked": sym in engine_pos,
                "tier": ep.tier if ep else None,
                "entry_price": ep.entry_price if ep else None,
                "tp": ep.tp if ep else None,
                "sl": ep.sl if ep else None,
                "tp_price": round(ep.entry_price * (1 + ep.tp), 6) if ep else None,
                "sl_price": round(ep.entry_price * (1 + ep.sl), 6) if ep else None,
                "composite": ep.composite_score if ep else None,
                "module": ep.module if ep else None,
                "age_h": round(((time.time() - ep.entry_time) / 3600), 2) if ep else None,
                "max_hold_h": ep.max_hold_h if ep else None,
                "pnl_usd": round(value_usd - ep.size_usd, 2) if ep else None,
                "pnl_pct": round((price / ep.entry_price - 1) * 100, 2) if ep and ep.entry_price > 0 else None,
            })
        holdings.sort(key=lambda h: -h["value_usd"])
        usdt = float(total.get("USDT", 0))
        return {
            "usdt_free": round(usdt, 2),
            "total_holdings": len(holdings),
            "total_value_usd": round(sum(h["value_usd"] for h in holdings), 2),
            "tracked_count": sum(1 for h in holdings if h["tracked"]),
            "untracked_count": sum(1 for h in holdings if not h["tracked"]),
            "holdings": holdings,
        }
    except Exception as e:
        return {"error": str(e)}


# ===========================================================================
# Forensic v1 (legacy, hidden until data arrives)
# ===========================================================================

@router.get("/forensic")
def spot_aggro_forensic() -> dict[str, Any]:
    """Forensic report list — history of all generated reports."""
    try:
        from spot_aggro.forensic.runner import get_reports
        return {"reports": get_reports()}
    except Exception as e:
        return {"error": str(e)}


@router.get("/forensic/{report_id}")
def spot_aggro_forensic_detail(report_id: str) -> dict[str, Any]:
    """Full forensic report detail."""
    try:
        from spot_aggro.forensic.runner import get_report
        r = get_report(report_id)
        if not r:
            raise HTTPException(status_code=404, detail="report not found")
        return r.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        return {"error": str(e)}


@router.get("/forensic/{report_id}/pdf")
def spot_aggro_forensic_pdf(report_id: str):
    """Download forensic report as PDF."""
    from fastapi.responses import FileResponse
    try:
        from spot_aggro.forensic.runner import get_report
        r = get_report(report_id)
        if not r or not r.pdf_path:
            raise HTTPException(status_code=404, detail="PDF not found")
        if not os.path.exists(r.pdf_path):
            raise HTTPException(status_code=404, detail="PDF file missing")
        return FileResponse(
            r.pdf_path,
            media_type="application/pdf",
            filename=f"spot_aggro_forensic_{report_id}.pdf",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================================================
# Tier execution toggles (Phase 9d → relocated in Phase 9e)
# ===========================================================================

_SPOT_TIER_TOGGLE: Any = None


def _spot_tier_toggle():
    """Lazy module-level singleton so requests share state and the YAML file
    is loaded once. Engine-router only; the ops router never touches this."""
    global _SPOT_TIER_TOGGLE
    if _SPOT_TIER_TOGGLE is None:
        from spot_aggro.gates.tier_toggle import TierExecutionToggle
        _SPOT_TIER_TOGGLE = TierExecutionToggle()
    return _SPOT_TIER_TOGGLE


@router.get("/tier_toggles")
def spot_aggro_tier_toggles() -> dict[str, Any]:
    """Return the current execution-only tier toggle state.

    Execution-only: toggling stops orders, not scoring/ranking/analytics/
    funnel/heatmap/forensic visibility.
    """
    from spot_aggro.gates.tier_toggle import KNOWN_TIERS, REASON_CODES
    try:
        t = _spot_tier_toggle()
        snap = t.snapshot()
        audit = [
            {
                "ts": e.ts, "tier": e.tier,
                "old": e.old_value, "new": e.new_value,
                "actor": e.actor, "note": e.note,
            }
            for e in t.audit_log(limit=20)
        ]
        return {
            "ok": True,
            "engine": "spot_aggro",
            "tiers": list(KNOWN_TIERS),
            "execution": snap,
            "reason_codes": REASON_CODES,
            "note": "Execution only. Analysis still includes this tier.",
            "audit": audit,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@router.post("/tier_toggles")
def spot_aggro_tier_toggles_set(
    body: dict[str, Any] = Body(...),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Flip a single tier's execution toggle.

    Body: {"tier": "A+"|"A"|"B"|"C", "enabled": bool, "note": "<optional>"}
    Persists the change to spot_aggro/config/tiers.yml so the state
    survives restarts.
    """
    _require_admin(x_ops_token)
    from spot_aggro.gates.tier_toggle import KNOWN_TIERS

    tier = str(body.get("tier") or "").strip()
    enabled = bool(body.get("enabled"))
    note = str(body.get("note") or "")
    if tier not in KNOWN_TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"tier must be one of {list(KNOWN_TIERS)} — never delete a tier",
        )

    t = _spot_tier_toggle()
    entry = t.set_enabled(
        tier, enabled,
        actor="api:ops_admin",
        note=note,
        persist=True,
    )
    return {
        "ok": True,
        "engine": "spot_aggro",
        "tier": entry.tier,
        "old": entry.old_value,
        "new": entry.new_value,
        "actor": entry.actor,
        "note": entry.note,
        "execution": t.snapshot(),
    }


# ---------------------------------------------------------------------------
# Phase 11n — Auto Scenario Lab + Governance Layers 4 & 5.
# All GETs public (dashboard reads); POSTs admin-guarded because they run
# the simulator / governors and write result rows.
# ---------------------------------------------------------------------------

@router.get("/scenarios/latest")
def spot_aggro_scenarios_latest(
    tier: str = Query("B", description="A+ | A | B | C"),
) -> dict[str, Any]:
    try:
        from spot_aggro.governance.scenario_runner import latest_batch_for_tier
        latest = latest_batch_for_tier(tier)
        return {
            "ok": True, "tier": tier, "latest": latest,
            "status": "never_run" if latest is None else "ready",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/scenarios/history")
def spot_aggro_scenarios_history(
    tier: str | None = Query(None),
    limit: int = Query(30, ge=1, le=200),
) -> dict[str, Any]:
    try:
        from spot_aggro.governance.scenario_runner import history
        return {"ok": True, "rows": history(tier=tier, limit=limit)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/scenarios/run")
def spot_aggro_scenarios_run(
    body: dict[str, Any] = Body(default_factory=dict),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro.governance.scenario_runner import run_batch_and_persist
    tier = str(body.get("tier") or "B").strip()
    non_stop = bool(body.get("non_stop", False))
    cap = int(body.get("cap") or 500)
    batch = run_batch_and_persist(tier=tier, non_stop=non_stop, cap=cap)
    return {"ok": True, "batch": batch.to_dict()}


@router.get("/research/truth")
def spot_aggro_research_truth_latest() -> dict[str, Any]:
    try:
        from spot_aggro.governance.research_truth_gov import latest_verdict
        v = latest_verdict()
        return {
            "ok": True, "latest": v,
            "status": "never_run" if v is None else "ready",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/research/truth/run")
def spot_aggro_research_truth_run(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Validate the most recent persisted research report."""
    _require_admin(x_ops_token)
    from spot_aggro.governance.research_agent import latest_report
    from spot_aggro.governance.research_truth_gov import validate_and_persist
    report = latest_report()
    if report is None:
        raise HTTPException(status_code=404, detail="no research report yet")
    v = validate_and_persist(report)
    return {"ok": True, "verdict": v.to_dict()}


@router.get("/cards/truth")
def spot_aggro_cards_truth_latest() -> dict[str, Any]:
    try:
        from spot_aggro.governance.card_truth_gov import latest_audit, history
        latest = latest_audit()
        hist = history(limit=14)
        return {
            "ok": True, "latest": latest, "history": hist,
            "status": "never_run" if latest is None else "ready",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/cards/truth/run")
def spot_aggro_cards_truth_run(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro.governance.card_truth_gov import run_and_persist
    audit = run_and_persist()
    return {"ok": True, "audit": audit.to_dict()}


# ---------------------------------------------------------------------------
# Phase 11n-2 — Decision Card + Decision Truth Governor.
# Every buy/sell/hold has factor-by-factor evidence. Conversion rate
# (signals → trades → wins) is exposed here. The Decision Truth Governor
# (Layer 6) audits each bundle. No orders placed; read-only.
# ---------------------------------------------------------------------------

@router.get("/decision/latest")
def spot_aggro_decision_latest() -> dict[str, Any]:
    try:
        from spot_aggro.governance.decision_engine import (
            latest_bundle, build_and_persist,
        )
        b = latest_bundle()
        if b is None:
            b = build_and_persist().to_dict()
        return {"ok": True, "bundle": b,
                "status": "ready"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/decision/run")
def spot_aggro_decision_run(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro.governance.decision_engine import build_and_persist
    from spot_aggro.governance.decision_truth_gov import validate_and_persist
    b = build_and_persist()
    v = validate_and_persist(b.to_dict())
    return {"ok": True, "bundle": b.to_dict(), "verdict": v.to_dict()}


@router.get("/decision/truth")
def spot_aggro_decision_truth() -> dict[str, Any]:
    try:
        from spot_aggro.governance.decision_truth_gov import latest_verdict
        v = latest_verdict()
        return {"ok": True, "latest": v,
                "status": "never_run" if v is None else "ready"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Phase 11n-3 — Loop Novelty Governor (Layer 7) + Auto Orchestrator.
# ---------------------------------------------------------------------------

@router.get("/loop/novelty")
def spot_aggro_loop_novelty(
    tier: str | None = Query(None, description="Filter by tier"),
) -> dict[str, Any]:
    try:
        from spot_aggro.governance.loop_novelty_gov import latest_verdict, history
        latest = latest_verdict(tier)
        hist = history(tier, limit=20)
        return {"ok": True, "latest": latest, "history": hist,
                "status": "never_run" if latest is None else "ready"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/auto/status")
def spot_aggro_auto_status() -> dict[str, Any]:
    try:
        from spot_aggro.governance import auto_orchestrator as ao
        return {"ok": True,
                "running": ao.is_running(),
                "last_tick": ao.last_tick(),
                "latest_persisted": ao.latest_tick(),
                "history": ao.history(limit=20)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/auto/start")
def spot_aggro_auto_start(
    body: dict[str, Any] = Body(default_factory=dict),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro.governance import auto_orchestrator as ao
    interval_s = body.get("interval_s")
    try:
        interval = float(interval_s) if interval_s is not None else None
    except (ValueError, TypeError):
        interval = None
    return ao.start(interval_s=interval)


@router.post("/auto/stop")
def spot_aggro_auto_stop(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro.governance import auto_orchestrator as ao
    return ao.stop()


@router.post("/auto/tick")
def spot_aggro_auto_tick_once(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Synchronously run one orchestrator tick (admin-only). Useful for
    operator testing + the dashboard 'Run Once' button."""
    _require_admin(x_ops_token)
    from spot_aggro.governance import auto_orchestrator as ao
    tick = ao.run_tick()
    return {"ok": True, "tick": tick.to_dict()}


# ---------------------------------------------------------------------------
# Phase 11n-9 — Daily Alpha picker + Layer 8 governor. Up to 2 buys + 2
# sells per day, admitted only when proj WR ≥ 70% AND the 12-item
# pre-trade checklist passes every item.
# ---------------------------------------------------------------------------

@router.get("/daily_alpha/latest")
def spot_aggro_daily_alpha_latest() -> dict[str, Any]:
    try:
        from spot_aggro.governance.daily_alpha import latest_bundle
        b = latest_bundle()
        return {"ok": True, "latest": b,
                "status": "never_run" if b is None else "ready"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/daily_alpha/run")
def spot_aggro_daily_alpha_run(
    body: dict[str, Any] = Body(default_factory=dict),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro.governance.daily_alpha import build_and_persist
    target = body.get("target_proj_wr")
    try:
        tgt = float(target) if target is not None else 0.70
    except (ValueError, TypeError):
        tgt = 0.70
    b = build_and_persist(target_proj_wr=tgt)
    return {"ok": True, "bundle": b.to_dict()}


@router.get("/pre_trade/latest")
def spot_aggro_pre_trade_latest(
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Every trade the engine tried to place, tagged pass/fail by the
    Pre-Trade Governor (Layer 8). Read-only history for the dashboard."""
    try:
        from spot_aggro.governance.pre_trade_gov import (
            latest, latest_with_checklist,
        )
        return {
            "ok": True,
            "rows": latest(limit=limit),
            "full": latest_with_checklist(limit=min(5, limit)),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/daily_alpha/executions")
def spot_aggro_daily_alpha_executions() -> dict[str, Any]:
    """Phase 11n-9-c: list of today's alpha auto-executor actions
    (whether placed or skipped). Read-only."""
    try:
        from spot_aggro.governance.daily_alpha_executor import (
            executions_today, is_enabled,
        )
        return {"ok": True,
                "enabled": is_enabled(),
                "rows": executions_today()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Phase 11n-9-d — Reconciled Sweeper: bring orphan positions under
# spot_aggro governance (keep / sell / link). DRY RUN by default; real
# ship requires SPOT_RECON_SWEEP_EXECUTE=1.
# ---------------------------------------------------------------------------

@router.get("/recon/sweep/plan")
def spot_aggro_recon_sweep_plan() -> dict[str, Any]:
    """Compute today's sweep plan without executing. Read-only."""
    try:
        from spot_aggro.governance.reconciled_sweeper import (
            build_and_persist, is_execute_enabled,
        )
        p = build_and_persist()
        return {"ok": True, "plan": p.to_dict(),
                "execute_enabled": is_execute_enabled()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/recon/sweep")
def spot_aggro_recon_sweep_run(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Build + execute the reconciled sweep. Actual order placement
    only happens when SPOT_RECON_SWEEP_EXECUTE=1; otherwise returns
    the dry-run plan unchanged. Admin-only."""
    _require_admin(x_ops_token)
    from spot_aggro.governance.reconciled_sweeper import (
        build_and_execute, is_execute_enabled,
    )
    p = build_and_execute()
    return {"ok": True, "plan": p.to_dict(),
            "execute_enabled": is_execute_enabled()}


# ---------------------------------------------------------------------------
# Phase 11n-9-i — Take-Profit Agent + Layer 9 Sell Governor. Sells any
# position with live_ret ≥ 2% after 4-agent ranking + evidence audit.
# ---------------------------------------------------------------------------

@router.get("/tp/latest")
def spot_aggro_tp_latest() -> dict[str, Any]:
    try:
        from spot_aggro.governance.tp_agent import (
            latest_proposal, executions_today, tp_target, is_execute_enabled,
        )
        from spot_aggro.governance.tp_sell_gov import latest_verdicts
        return {
            "ok": True,
            "target": tp_target(),
            "execute_enabled": is_execute_enabled(),
            "latest_proposal": latest_proposal(),
            "executions_today": executions_today(),
            "sell_verdicts": latest_verdicts(limit=20),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/tp/run")
def spot_aggro_tp_run(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro.governance.tp_agent import build_and_execute
    p = build_and_execute()
    return {"ok": True, "proposal": p.to_dict()}


# ---------------------------------------------------------------------------
# Phase 11n-9-k — Alert Center (P3). Unified ingest + severity +
# aggregation + correlation + acknowledgement.
# ---------------------------------------------------------------------------

@router.get("/alerts/active")
def spot_aggro_alerts_active(
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    try:
        from spot_aggro.governance import alert_center
        return {
            "ok": True,
            "alerts": alert_center.active(limit=limit),
            "groups": alert_center.correlation_groups(limit=20),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/alerts/history")
def spot_aggro_alerts_history(
    limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    try:
        from spot_aggro.governance import alert_center
        return {"ok": True, "alerts": alert_center.history(limit=limit)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/alerts/{alert_id}/occurrences")
def spot_aggro_alerts_occurrences(
    alert_id: str, limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    try:
        from spot_aggro.governance import alert_center
        return {"ok": True,
                "occurrences": alert_center.occurrences_for(alert_id, limit=limit)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/alerts/{alert_id}/ack")
def spot_aggro_alerts_ack(
    alert_id: str,
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro.governance import alert_center
    ok = alert_center.ack(alert_id, actor="ops")
    return {"ok": ok, "alert_id": alert_id}


@router.post("/alerts/{alert_id}/mute")
def spot_aggro_alerts_mute(
    alert_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro.governance import alert_center
    until = body.get("until_ms")
    try:
        until_int = int(until) if until else None
    except (ValueError, TypeError):
        until_int = None
    ok = alert_center.mute(alert_id, until_ms=until_int, actor="ops")
    return {"ok": ok, "alert_id": alert_id}


# ---------------------------------------------------------------------------
# Phase 11n-9-l — Incident Mode (P4). State + transitions + timeline +
# quick diagnostics. Auto-enters on P0, auto-exits when P0 queue clears.
# ---------------------------------------------------------------------------

@router.get("/incident/status")
def spot_aggro_incident_status() -> dict[str, Any]:
    try:
        from spot_aggro.governance import incident_mode
        return {"ok": True,
                "state": incident_mode.status(),
                "timeline": incident_mode.timeline(limit=50)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


@router.post("/incident/enter")
def spot_aggro_incident_enter(
    body: dict[str, Any] = Body(default_factory=dict),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro.governance import incident_mode
    return incident_mode.enter(
        actor="ops",
        trigger_alert_id=body.get("trigger_alert_id"),
        trigger_kind=body.get("trigger_kind"),
        message=body.get("message"),
    )


@router.post("/incident/exit")
def spot_aggro_incident_exit(
    body: dict[str, Any] = Body(default_factory=dict),
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro.governance import incident_mode
    return incident_mode.exit_(
        actor="ops",
        force=bool(body.get("force", True)),  # manual exit defaults to force=True
        message=body.get("message"),
    )


@router.post("/incident/diagnostics")
def spot_aggro_incident_diagnostics(
    x_ops_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_ops_token)
    from spot_aggro.governance import incident_mode
    return incident_mode.run_diagnostics()


# ---------------------------------------------------------------------------
# Phase 11n-9-m — Label translator. Plain-English labels for any
# technical kind. Read-only helper for the dashboard.
# ---------------------------------------------------------------------------

@router.get("/labels/translate")
def spot_aggro_labels_translate(
    key: str = Query(..., description="technical label to translate"),
) -> dict[str, Any]:
    from spot_aggro.governance.label_translator import translate
    return {"ok": True, "key": key, "label": translate(key)}
