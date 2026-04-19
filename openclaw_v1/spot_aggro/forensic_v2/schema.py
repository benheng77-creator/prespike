"""
spot_forensic.v1 — SHARED_INPUT bundle assembler.

Pure data: pulls trades, consensus, regime, swarm health, and coin-memory
from the live DB into a single deterministic JSON-shaped dict that all five
LLM specialists consume. No LLM calls here.

Every required-but-missing field becomes an explicit entry in
`evidence_gaps` so L1 can reject downstream analyses cleanly.
"""

from __future__ import annotations

import json
import time
from typing import Any

from shared.persistence import state as persist

SCHEMA_VERSION = "spot_forensic.v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_payload(s: str | None) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# SHARED_INPUT builder
# ---------------------------------------------------------------------------

def build_shared_input(
    report_id: str,
    window_start_ms: int,
    window_end_ms: int,
    window_label: str = "rolling",
    engine_status: dict | None = None,
    config_snapshot: dict | None = None,
) -> dict:
    """Assemble the SHARED_INPUT bundle for a [start, end] window.

    Reads only DB state — never calls the live engine. `engine_status` and
    `config_snapshot` are optional pass-throughs for context.
    """
    now_ms = int(time.time() * 1000)
    duration_h = round((window_end_ms - window_start_ms) / 3_600_000.0, 2)
    gaps: list[str] = []

    # ---- closed trades in window ------------------------------------------
    trades_closed = _load_closed_trades(window_start_ms, window_end_ms, gaps)

    # ---- consensus log in window ------------------------------------------
    consensus_log = _load_consensus(window_start_ms, window_end_ms)

    # ---- regime timeline (best-effort) ------------------------------------
    regime_timeline = _load_regime_timeline(window_start_ms, window_end_ms, gaps)

    # ---- coin-memory snapshot (current state, not windowed) ---------------
    coin_memory_buckets = _load_coin_memory()

    # ---- infrastructure findings in window --------------------------------
    infra_findings = _load_infra_findings(window_start_ms, window_end_ms)

    bundle: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "report_id": report_id,
        "generated_ts_ms": now_ms,
        "window": {
            "start_ts_ms": window_start_ms,
            "end_ts_ms": window_end_ms,
            "duration_h": duration_h,
            "label": window_label,
        },
        "engine_state_snapshot": engine_status or {
            "note": "engine_status not provided; live engine state unavailable",
        },
        "config_snapshot": config_snapshot or {
            "note": "config_snapshot not provided; thresholds inferred at adjudication",
        },
        "trades_closed": trades_closed,
        "consensus_log": consensus_log,
        "regime_timeline": regime_timeline,
        "swarm_health_window": _load_swarm_health_window(),
        "coin_memory_buckets": coin_memory_buckets,
        "infrastructure_findings_window": infra_findings,
        "evidence_gaps": gaps,
    }
    return bundle


# ---------------------------------------------------------------------------
# Loaders (DB-only)
# ---------------------------------------------------------------------------

def _load_closed_trades(start_ms: int, end_ms: int, gaps: list[str]) -> list[dict]:
    """Pull every action='exit' row in window, paired with its 'enter' row
    so we can compute hold_seconds + entry-time snapshots.

    Missing fee_usd / entry-time snapshots are captured as evidence_gaps.
    """
    persist.init_schema()
    con = persist._connect()
    try:
        # Exits in window.
        exits = con.execute(
            "SELECT id, ts_ms, symbol, module, side, notional_usd, avg_px, "
            "       fee_usd, pnl_usd, payload_json "
            "FROM apex_trade_log "
            "WHERE ts_ms >= ? AND ts_ms < ? AND action = 'exit' "
            "ORDER BY ts_ms ASC",
            (start_ms, end_ms),
        ).fetchall()

        out: list[dict] = []
        missing_fee = 0
        missing_swarm_snapshot = 0
        missing_regime = 0

        for ex in exits:
            ex_payload = _parse_payload(ex["payload_json"])
            symbol = ex["symbol"]

            # Locate the matching enter — most recent enter for this symbol
            # strictly before the exit timestamp.
            enter = con.execute(
                "SELECT id, ts_ms, avg_px, payload_json "
                "FROM apex_trade_log "
                "WHERE symbol = ? AND action = 'enter' AND ts_ms < ? "
                "ORDER BY ts_ms DESC LIMIT 1",
                (symbol, ex["ts_ms"]),
            ).fetchone()

            if enter is None:
                # Orphan exit — no preceding enter found. Worth recording.
                gaps.append(
                    f"trade exit id={ex['id']} symbol={symbol} ts={ex['ts_ms']}: "
                    f"no preceding enter row found"
                )
                continue

            en_payload = _parse_payload(enter["payload_json"])
            hold_seconds = max(0, (ex["ts_ms"] - enter["ts_ms"]) // 1000)

            entry_price = _safe_float(enter["avg_px"]) or _safe_float(en_payload.get("entry_price"))
            exit_price = _safe_float(ex["avg_px"])
            fee = _safe_float(ex["fee_usd"])
            if fee is None:
                missing_fee += 1

            # Tier — explicit field wins, fall back to module suffix heuristic.
            tier = (
                en_payload.get("tier")
                or ex_payload.get("tier")
                or _tier_from_module(ex["module"])
            )

            entry_regime = en_payload.get("entry_regime") or en_payload.get("regime")
            if not entry_regime:
                missing_regime += 1

            swarm_action = en_payload.get("swarm_action_at_entry")
            swarm_conf = _safe_float(en_payload.get("swarm_confidence_at_entry"))
            swarm_agents_ok = en_payload.get("swarm_agents_ok_at_entry")
            swarm_agents_total = en_payload.get("swarm_agents_total_at_entry")
            if swarm_action is None:
                missing_swarm_snapshot += 1

            out.append({
                "trade_id": f"T{ex['id']}",
                "symbol": symbol,
                "entry_ts_ms": int(enter["ts_ms"]),
                "exit_ts_ms": int(ex["ts_ms"]),
                "hold_seconds": int(hold_seconds),
                "tier": tier,
                "module": ex["module"],
                "notional_usd": _safe_float(ex["notional_usd"]),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "fee_usd": fee,
                "pnl_usd": _safe_float(ex["pnl_usd"]),
                "exit_reason": ex_payload.get("reason"),
                "composite_score_at_entry": _safe_float(en_payload.get("composite") or en_payload.get("composite_score")),
                "spi_at_entry": _safe_float(en_payload.get("spi") or en_payload.get("entry_spi")),
                "entry_regime": entry_regime,
                "regime_at_exit": ex_payload.get("regime"),
                "swarm_action_at_entry": swarm_action,
                "swarm_confidence_at_entry": swarm_conf,
                "swarm_agents_ok_at_entry": swarm_agents_ok,
                "swarm_agents_total_at_entry": swarm_agents_total,
                "is_blitz": bool(en_payload.get("blitz") or en_payload.get("is_blitz")),
            })

        if missing_fee:
            gaps.append(f"fee_usd missing on {missing_fee}/{len(out)} closed trades — friction analysis partial")
        if missing_swarm_snapshot:
            gaps.append(f"swarm_action_at_entry missing on {missing_swarm_snapshot}/{len(out)} closed trades — calibration L4 will be capped at LIKELY")
        if missing_regime:
            gaps.append(f"entry_regime missing on {missing_regime}/{len(out)} closed trades — regime L3 coverage partial")

        return out
    finally:
        con.close()


def _tier_from_module(module: str | None) -> str:
    """Heuristic fallback when payload doesn't carry tier explicitly."""
    if not module:
        return "?"
    m = module.upper()
    if "SQUEEZE_A" in m or "_A" in m or "BLITZ" in m:
        return "A"
    if "FLOW_B" in m or "_B" in m:
        return "B"
    if "SCALP_C" in m or "_C" in m:
        return "C"
    return "?"


def _load_consensus(start_ms: int, end_ms: int) -> list[dict]:
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT ts_ms, symbol, consensus_score, conflict_score, vetoed, "
            "       members_called "
            "FROM apex_consensus_log "
            "WHERE ts_ms >= ? AND ts_ms < ? "
            "ORDER BY ts_ms ASC",
            (start_ms, end_ms),
        ).fetchall()
    finally:
        con.close()
    return [{
        "ts_ms": int(r["ts_ms"]),
        "symbol": r["symbol"],
        "consensus": _safe_float(r["consensus_score"]),
        "conflict": _safe_float(r["conflict_score"]),
        "vetoed": bool(r["vetoed"]),
        "members_called": r["members_called"],
    } for r in rows]


def _load_regime_timeline(start_ms: int, end_ms: int, gaps: list[str]) -> list[dict]:
    """Pull regime samples from spot_aggro_regime_log."""
    try:
        rows = persist.fetch_regime_timeline(start_ms, end_ms, limit=5000)
    except Exception:
        rows = []
    if not rows:
        gaps.append(
            "spot_aggro_regime_log empty for window — L3 transition analysis "
            "will be UNVERIFIABLE for this report"
        )
    return [{
        "ts_ms": int(r["ts_ms"]),
        "regime": r.get("regime"),
        "regime_confidence": _safe_float(r.get("regime_confidence")),
        "squeeze_timing": r.get("squeeze_timing"),
        "edge_status": r.get("edge_status"),
        "universe_quality": r.get("universe_quality"),
    } for r in rows]


def _load_swarm_health_window() -> dict:
    """Pull current in-process swarm state; gracefully empty if unavailable."""
    try:
        from spot_aggro.swarm.runner import get_swarm_state
        ss = get_swarm_state()
        h = ss.health.to_dict() if hasattr(ss, "health") else {}
        return {
            "status": h.get("status"),
            "alive": h.get("alive"),
            "uptime_s": h.get("uptime_s"),
            "consecutive_errors": h.get("consecutive_errors"),
            "last_error": h.get("last_error"),
            "cycles": dict(getattr(ss, "cycle_counts", {}) or {}),
            "agent_outages": [],  # not yet tracked separately
        }
    except Exception:
        return {"status": None, "alive": None, "agent_outages": []}


def _load_coin_memory() -> list[dict]:
    try:
        from spot_aggro import coin_memory
        return coin_memory.list_buckets(min_trades=1, limit=500)
    except Exception:
        return []


def _load_infra_findings(start_ms: int, end_ms: int) -> list[dict]:
    """Pull spot-only findings from the watchdog table within window."""
    persist.init_schema()
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT w.ts_ms, q.source, w.severity, "
            "       w.root_cause, q.error_msg, q.error_class "
            "FROM apex_watchdog_findings w "
            "LEFT JOIN apex_watchdog_queue q ON w.queue_id = q.id "
            "WHERE w.ts_ms >= ? AND w.ts_ms < ? "
            "ORDER BY w.ts_ms ASC",
            (start_ms, end_ms),
        ).fetchall()
    finally:
        con.close()
    perp_prefixes = ("funding_hunt", "stat_arb", "triangular", "liq_fade")
    out = []
    for r in rows:
        src = r["source"] or ""
        if src.startswith(perp_prefixes):
            continue
        out.append({
            "ts_ms": int(r["ts_ms"]),
            "source": src,
            "severity": r["severity"],
            "message": r["root_cause"] or r["error_msg"] or r["error_class"] or "",
        })
    return out
