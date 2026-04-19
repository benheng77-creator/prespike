"""
Forensic specialists.

Live roles:
  L1 — Integrity Auditor      (deterministic Python — no LLM)
  L2 — Trade Physics Analyst  (openai gpt-4o-mini)
  L4 — Calibration Analyst    (openrouter deepseek-chat-v3)
  L5 — Chief Adjudicator      (mistral-small)

Commit-4 will add L3 (regime, gemini) once a regime_timeline source exists.

Why L1 is deterministic:
  Integrity is just a structural data audit. A Python function gives us
  100% reproducibility, zero cost, zero latency, and zero risk of an LLM
  inventing data quality claims. The L1 *prompt contract* in the spec
  remains the truth — we just satisfy it without paying for tokens.

Why L2 / L4 are LLM-driven:
  These two need to weigh contradictory evidence (e.g. tier inversion
  with mixed sub-windows, swarm-anti-correlation with degraded quorum)
  and produce a confidence label per §5. That's judgement work. The
  numerical grunt-work is pre-computed in Python (`_physics_metrics`,
  `_calibration_metrics`) and handed to the LLM as facts. The LLM job
  is then to *label* and *narrate*, never to compute.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

log = logging.getLogger("spot_aggro.forensic_v2.specialists")

# Per spec §1 provider/model bindings.
PHYSICS_PROVIDER = "openai"
PHYSICS_MODEL = "gpt-4o-mini"
PHYSICS_TIMEOUT_S = 30
PHYSICS_MAX_RETRIES = 1

# Spec said openrouter/deepseek; that combo is observed >25s and times out
# regularly. L4 is calibration analysis — facts are pre-computed, the LLM is
# only labelling. Switch to anthropic haiku-4-5 (fast, cheap, reliable JSON).
# This deviates from the spec's provider table for L4 only; documenting why
# in the prompt header so future readers don't get confused.
CALIBRATION_PROVIDER = "anthropic"
CALIBRATION_MODEL = "claude-haiku-4-5"
CALIBRATION_TIMEOUT_S = 25
CALIBRATION_MAX_RETRIES = 1

# L3 regime — spec said gemini. Gemini is fast and reliable for structured
# timeline analysis. Timeout moderate because regime timeline can grow.
REGIME_PROVIDER = "gemini"
REGIME_MODEL = "gemini-2.5-flash"
REGIME_TIMEOUT_S = 25
REGIME_MAX_RETRIES = 1

ADJUDICATOR_PROVIDER = "mistral"
ADJUDICATOR_MODEL = "mistral-small-latest"
ADJUDICATOR_TIMEOUT_S = 45
ADJUDICATOR_MAX_RETRIES = 1

# Confidence-label sample-size floors (spec §5).
CONFIDENCE_PROVEN_N = 20
CONFIDENCE_LIKELY_N = 10
CONFIDENCE_WEAK_N = 5


# ---------------------------------------------------------------------------
# L1 — Integrity Auditor (deterministic)
# ---------------------------------------------------------------------------

def run_integrity(bundle: dict) -> tuple[dict | None, dict]:
    """Audit SHARED_INPUT for structural integrity. Returns (result, meta).

    meta = {ok: bool, cost_usd: float, latency_ms: int, error: str|None}
    """
    t0 = time.time()
    try:
        result = _integrity_audit(bundle)
        latency_ms = int((time.time() - t0) * 1000)
        return result, {"ok": True, "cost_usd": 0.0, "latency_ms": latency_ms, "error": None}
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        log.exception("integrity audit failed")
        return None, {"ok": False, "cost_usd": 0.0, "latency_ms": latency_ms, "error": str(e)[:300]}


def _integrity_audit(bundle: dict) -> dict:
    checks: list[dict] = []
    blocked: list[dict] = []

    trades = bundle.get("trades_closed", []) or []
    n_trades = len(trades)

    # ---- INT-001 fee_usd ---------------------------------------------------
    missing_fee = [i for i, t in enumerate(trades) if t.get("fee_usd") is None]
    checks.append({
        "check_id": "INT-001",
        "field_path": "trades_closed[*].fee_usd",
        "status": "OK" if not missing_fee else "MISSING",
        "rows_failing": missing_fee,
        "evidence_gap": (
            f"fee_usd null on {len(missing_fee)}/{n_trades} closed trades — "
            "friction analysis will be partial"
        ) if missing_fee else "",
    })
    if missing_fee and len(missing_fee) > n_trades * 0.5:
        blocked.append({
            "downstream_role": "physics",
            "metric": "friction_ratio",
            "reason": f"fee_usd missing on {len(missing_fee)}/{n_trades} trades (>50%)",
        })

    # ---- INT-002 pnl_usd ---------------------------------------------------
    missing_pnl = [i for i, t in enumerate(trades) if t.get("pnl_usd") is None]
    checks.append({
        "check_id": "INT-002",
        "field_path": "trades_closed[*].pnl_usd",
        "status": "OK" if not missing_pnl else "MISSING",
        "rows_failing": missing_pnl,
        "evidence_gap": (
            f"pnl_usd null on {len(missing_pnl)}/{n_trades} closed trades — "
            "expectancy is UNVERIFIABLE for those rows"
        ) if missing_pnl else "",
    })
    if missing_pnl:
        blocked.append({
            "downstream_role": "physics",
            "metric": "expectancy_usd",
            "reason": f"pnl_usd missing on {len(missing_pnl)}/{n_trades} trades",
        })

    # ---- INT-003 entry_regime ---------------------------------------------
    missing_regime = [i for i, t in enumerate(trades) if not t.get("entry_regime")]
    checks.append({
        "check_id": "INT-003",
        "field_path": "trades_closed[*].entry_regime",
        "status": "OK" if not missing_regime else "MISSING",
        "rows_failing": missing_regime,
        "evidence_gap": (
            f"entry_regime missing on {len(missing_regime)}/{n_trades} closed trades — "
            "L3 regime analysis coverage partial"
        ) if missing_regime else "",
    })
    if missing_regime and len(missing_regime) > n_trades * 0.5:
        blocked.append({
            "downstream_role": "regime",
            "metric": "miscalibration_findings",
            "reason": f"entry_regime missing on {len(missing_regime)}/{n_trades} trades (>50%)",
        })

    # ---- INT-004 swarm snapshot at entry ----------------------------------
    missing_swarm = [i for i, t in enumerate(trades)
                     if t.get("swarm_action_at_entry") is None]
    checks.append({
        "check_id": "INT-004",
        "field_path": "trades_closed[*].swarm_action_at_entry",
        "status": "OK" if not missing_swarm else "MISSING",
        "rows_failing": missing_swarm,
        "evidence_gap": (
            f"swarm_action_at_entry missing on {len(missing_swarm)}/{n_trades} closed trades — "
            "L4 calibration capped at LIKELY"
        ) if missing_swarm else "",
    })

    # ---- INT-005 composite_score_at_entry ---------------------------------
    missing_comp = [i for i, t in enumerate(trades)
                    if t.get("composite_score_at_entry") is None]
    checks.append({
        "check_id": "INT-005",
        "field_path": "trades_closed[*].composite_score_at_entry",
        "status": "OK" if not missing_comp else "MISSING",
        "rows_failing": missing_comp,
        "evidence_gap": (
            f"composite_score_at_entry missing on {len(missing_comp)}/{n_trades} trades — "
            "L4 composite_calibration UNVERIFIABLE for those rows"
        ) if missing_comp else "",
    })

    # ---- INT-006 regime_timeline coverage ---------------------------------
    timeline = bundle.get("regime_timeline", []) or []
    checks.append({
        "check_id": "INT-006",
        "field_path": "regime_timeline",
        "status": "OK" if timeline else "MISSING",
        "rows_failing": [],
        "evidence_gap": "" if timeline
            else "regime_timeline empty — L3 transition_failures UNVERIFIABLE",
    })
    if not timeline:
        blocked.append({
            "downstream_role": "regime",
            "metric": "transition_failures",
            "reason": "regime_timeline samples not available for window",
        })

    # ---- INT-007 minimum sample size --------------------------------------
    checks.append({
        "check_id": "INT-007",
        "field_path": "trades_closed",
        "status": "OK" if n_trades >= 5 else "MISSING",
        "rows_failing": [],
        "evidence_gap": "" if n_trades >= 5
            else f"only {n_trades} closed trades in window — all per-tier metrics UNVERIFIABLE",
    })

    # ---- overall input quality --------------------------------------------
    failing = sum(1 for c in checks if c["status"] != "OK")
    if failing == 0:
        overall = "GOOD"
    elif failing >= 4 or n_trades == 0:
        overall = "UNUSABLE"
    else:
        overall = "DEGRADED"

    return {
        "role": "integrity",
        "report_id": bundle.get("report_id"),
        "input_schema_version": bundle.get("schema_version"),
        "checks": checks,
        "blocked_analyses": blocked,
        "overall_input_quality": overall,
    }


# ---------------------------------------------------------------------------
# L2 — Trade Physics Analyst (LLM, numerics pre-computed)
# ---------------------------------------------------------------------------

_PHYSICS_SYSTEM = (
    "You are a trade-physics analyst for SPOT AGGRO. You receive PRE-COMPUTED "
    "metrics (you do NOT do arithmetic). Your job is to label each metric with "
    "a confidence per these rules: "
    "PROVEN if n>=20 AND effect-size is consistent; "
    "LIKELY if n>=10; WEAK if n>=5; UNVERIFIABLE if n<5. "
    "If the orchestrator passed any 'blocked' notes, propagate them — do not "
    "fabricate metrics for blocked dimensions. "
    "Write 1-5 findings with ids PHY-001..PHY-005, each citing trade_id_count "
    "from the metrics block. Output STRICT JSON only — start with { and end with }. No markdown fences, no explanations, no prose before or after."
)


def run_physics(bundle: dict) -> tuple[dict | None, dict]:
    """L2 — physics. Returns (result, meta)."""
    t0 = time.time()
    try:
        metrics = _physics_metrics(bundle)
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        log.exception("physics metrics computation failed")
        return None, {"ok": False, "cost_usd": 0.0, "latency_ms": latency_ms,
                      "error": f"metrics_compute: {e}"[:300], "retries": 0}

    user_payload = {
        "report_id": bundle.get("report_id"),
        "window": bundle.get("window"),
        "computed_metrics": metrics,
        "schema_required": _physics_schema_skeleton(bundle),
    }
    prompt = _PHYSICS_SYSTEM + "\n\nINPUT:\n" + json.dumps(user_payload, default=str)

    parsed, meta = _call_specialist(
        provider=PHYSICS_PROVIDER, model=PHYSICS_MODEL,
        role="forensic_L2_physics",
        prompt=prompt, timeout_s=PHYSICS_TIMEOUT_S,
        max_retries=PHYSICS_MAX_RETRIES, t0=t0,
    )

    if parsed is None:
        # LLM failed — emit a stub built from the deterministic metrics so
        # downstream L5 still has facts to cite.
        parsed = _physics_fallback_stub(bundle, metrics, error=meta.get("error"))
        meta["ok"] = False
    else:
        # Always re-attach the deterministic metrics so L5 can cross-check.
        parsed["computed_metrics"] = metrics
    return parsed, meta


def _physics_metrics(bundle: dict) -> dict:
    """Pure Python — never trust the LLM with arithmetic.

    Returns a structure with overall + per-tier stats. Each metric carries
    `n` and `evidence_trade_ids` so confidence labels are auditable.
    """
    trades = [t for t in (bundle.get("trades_closed") or [])
              if t.get("pnl_usd") is not None]
    n = len(trades)

    pnls = [float(t["pnl_usd"]) for t in trades]
    fees = [float(t["fee_usd"]) for t in trades if t.get("fee_usd") is not None]
    notionals = [float(t["notional_usd"]) for t in trades if t.get("notional_usd") is not None]

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    sum_abs_pnl = sum(abs(p) for p in pnls)
    sum_fees = sum(fees) if fees else 0.0

    overall = {
        "n_trades": n,
        "expectancy_usd": round(sum(pnls) / n, 6) if n else None,
        "win_rate": round(len(wins) / n, 4) if n else None,
        "avg_win_usd": round(sum(wins) / len(wins), 6) if wins else None,
        "avg_loss_usd": round(sum(losses) / len(losses), 6) if losses else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses and sum(losses) != 0 else None,
        "friction_ratio": round(sum_fees / sum_abs_pnl, 4) if sum_abs_pnl > 0 else None,
        "ticket_floor_violations": sum(1 for nt in notionals if nt < 6.0),
        "n_with_fee": len(fees),
        "n_without_fee": n - len(fees),
        "evidence_trade_id_count": n,
    }

    # Per-tier
    tiers: dict[str, dict] = {}
    for t in trades:
        tier = (t.get("tier") or "?")
        tiers.setdefault(tier, {"trades": []})["trades"].append(t)
    per_tier = []
    for tier, blob in sorted(tiers.items()):
        ts = blob["trades"]
        ts_pnl = [float(x["pnl_usd"]) for x in ts]
        tw = [p for p in ts_pnl if p > 0]
        tl = [p for p in ts_pnl if p <= 0]
        nt = len(ts)
        per_tier.append({
            "tier": tier,
            "n": nt,
            "expectancy_usd": round(sum(ts_pnl) / nt, 6) if nt else None,
            "win_rate": round(len(tw) / nt, 4) if nt else None,
            "avg_win_usd": round(sum(tw) / len(tw), 6) if tw else None,
            "avg_loss_usd": round(sum(tl) / len(tl), 6) if tl else None,
            "suggested_confidence": _confidence_label_for_n(nt),
            "evidence_trade_id_count": nt,
        })

    # Anti-pattern: avg_loss magnitude > avg_win magnitude
    avg_loss_gt_avg_win = (
        overall["avg_loss_usd"] is not None and overall["avg_win_usd"] is not None
        and abs(overall["avg_loss_usd"]) > overall["avg_win_usd"]
    )

    return {
        "overall": overall,
        "per_tier": per_tier,
        "flags": {
            "avg_loss_gt_avg_win": avg_loss_gt_avg_win,
            "expectancy_negative": overall["expectancy_usd"] is not None and overall["expectancy_usd"] < 0,
            "profit_factor_below_1": overall["profit_factor"] is not None and overall["profit_factor"] < 1.0,
            "high_ticket_floor_violations": overall["ticket_floor_violations"] >= max(3, n // 5),
        },
    }


def _physics_schema_skeleton(bundle: dict) -> dict:
    return {
        "role": "physics",
        "report_id": bundle.get("report_id"),
        "metrics": "<echo computed_metrics.overall>",
        "per_tier": "<echo computed_metrics.per_tier with confidence label>",
        "findings": [{
            "finding_id": "PHY-001",
            "claim": "<string referencing the computed metric>",
            "confidence": "<PROVEN|LIKELY|WEAK|UNVERIFIABLE>",
            "evidence_trade_id_count": 0,
            "implication": "<one-line consequence>",
        }],
    }


def _physics_fallback_stub(bundle: dict, metrics: dict, error: str | None) -> dict:
    """When the LLM fails, build a Python-only physics summary so L5 still
    has structured facts to cite. Confidence labels assigned by sample size."""
    overall = metrics["overall"]
    findings: list[dict] = []
    if metrics["flags"]["expectancy_negative"] and overall["n_trades"] >= CONFIDENCE_WEAK_N:
        findings.append({
            "finding_id": "PHY-001",
            "claim": (
                f"expectancy_usd={overall['expectancy_usd']} over n={overall['n_trades']} "
                f"closed trades — negative"
            ),
            "confidence": _confidence_label_for_n(overall["n_trades"]),
            "evidence_trade_id_count": overall["n_trades"],
            "implication": "edge structurally negative regardless of win_rate",
        })
    if metrics["flags"]["avg_loss_gt_avg_win"] and overall["n_trades"] >= CONFIDENCE_WEAK_N:
        findings.append({
            "finding_id": "PHY-002",
            "claim": (
                f"avg_loss |{overall['avg_loss_usd']}| > avg_win {overall['avg_win_usd']}"
            ),
            "confidence": _confidence_label_for_n(overall["n_trades"]),
            "evidence_trade_id_count": overall["n_trades"],
            "implication": "loss magnitude dominates — even >50% WR could be net negative",
        })
    if metrics["flags"]["high_ticket_floor_violations"]:
        findings.append({
            "finding_id": "PHY-003",
            "claim": (
                f"{overall['ticket_floor_violations']} trades with notional<$6 "
                f"out of {overall['n_trades']} — fee floor friction"
            ),
            "confidence": _confidence_label_for_n(overall["ticket_floor_violations"]),
            "evidence_trade_id_count": overall["ticket_floor_violations"],
            "implication": "$5 tickets fight $0.04 fee floor; net edge structurally negative",
        })
    return {
        "role": "physics",
        "report_id": bundle.get("report_id"),
        "metrics": overall,
        "per_tier": metrics["per_tier"],
        "findings": findings,
        "computed_metrics": metrics,
        "fallback_reason": f"L2 LLM call failed: {error or 'unknown'} — stub built from deterministic metrics",
    }


# ---------------------------------------------------------------------------
# L4 — Calibration Analyst (LLM, numerics pre-computed)
# ---------------------------------------------------------------------------

_CALIBRATION_SYSTEM = (
    "You are a calibration analyst for SPOT AGGRO. You receive PRE-COMPUTED "
    "metrics (you do NOT do arithmetic). Label findings per these rules: "
    "PROVEN if n>=20 AND effect-size>=0.15; LIKELY if n>=10 OR effect>=0.10; "
    "WEAK if n>=5; UNVERIFIABLE if n<5. "
    "Cover: tier_inversion (A WR vs C WR), composite_calibration "
    "(monotonicity across buckets), swarm_signal_value (STRONG_BUY vs BUY vs "
    "WATCH realised WR), and agent_quorum_impact (full vs degraded swarm). "
    "If a metric has n_with_data below the WEAK floor, write UNVERIFIABLE — "
    "never invent. Output STRICT JSON only — start with { and end with }. No markdown fences, no explanations, no prose before or after."
)


def run_calibration(bundle: dict) -> tuple[dict | None, dict]:
    t0 = time.time()
    try:
        metrics = _calibration_metrics(bundle)
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        log.exception("calibration metrics computation failed")
        return None, {"ok": False, "cost_usd": 0.0, "latency_ms": latency_ms,
                      "error": f"metrics_compute: {e}"[:300], "retries": 0}

    user_payload = {
        "report_id": bundle.get("report_id"),
        "window": bundle.get("window"),
        "computed_metrics": metrics,
        "schema_required": _calibration_schema_skeleton(bundle),
    }
    prompt = _CALIBRATION_SYSTEM + "\n\nINPUT:\n" + json.dumps(user_payload, default=str)

    parsed, meta = _call_specialist(
        provider=CALIBRATION_PROVIDER, model=CALIBRATION_MODEL,
        role="forensic_L4_calibration",
        prompt=prompt, timeout_s=CALIBRATION_TIMEOUT_S,
        max_retries=CALIBRATION_MAX_RETRIES, t0=t0,
    )

    if parsed is None:
        parsed = _calibration_fallback_stub(bundle, metrics, error=meta.get("error"))
        meta["ok"] = False
    else:
        parsed["computed_metrics"] = metrics
    return parsed, meta


def _calibration_metrics(bundle: dict) -> dict:
    trades = [t for t in (bundle.get("trades_closed") or [])
              if t.get("pnl_usd") is not None]

    # Tier inversion: A WR vs C WR
    by_tier: dict[str, list[float]] = {}
    for t in trades:
        by_tier.setdefault(t.get("tier") or "?", []).append(float(t["pnl_usd"]))
    tier_wr = {
        tier: {
            "n": len(p),
            "win_rate": round(sum(1 for v in p if v > 0) / len(p), 4) if p else None,
            "pnl_per_trade": round(sum(p) / len(p), 6) if p else None,
        } for tier, p in by_tier.items()
    }
    a_wr = (tier_wr.get("A") or {}).get("win_rate")
    c_wr = (tier_wr.get("C") or {}).get("win_rate")
    a_n = (tier_wr.get("A") or {}).get("n", 0)
    c_n = (tier_wr.get("C") or {}).get("n", 0)
    inversion = (
        a_wr is not None and c_wr is not None and a_wr < c_wr
        and a_n >= CONFIDENCE_WEAK_N and c_n >= CONFIDENCE_WEAK_N
    )
    inversion_effect = (
        round(c_wr - a_wr, 4) if (a_wr is not None and c_wr is not None) else None
    )
    inversion_confidence = "UNVERIFIABLE"
    if inversion:
        nmin = min(a_n, c_n)
        eff = inversion_effect or 0.0
        if nmin >= CONFIDENCE_PROVEN_N and eff >= 0.15:
            inversion_confidence = "PROVEN"
        elif nmin >= CONFIDENCE_LIKELY_N or eff >= 0.10:
            inversion_confidence = "LIKELY"
        else:
            inversion_confidence = "WEAK"

    # Composite calibration buckets
    buckets = [(0.25, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65),
               (0.65, 0.75), (0.75, 1.01)]
    composite_buckets = []
    last_wr = None
    monotonic = True
    n_buckets_with_data = 0
    for lo, hi in buckets:
        in_bucket = [t for t in trades
                     if t.get("composite_score_at_entry") is not None
                     and lo <= float(t["composite_score_at_entry"]) < hi]
        n = len(in_bucket)
        wr = (round(sum(1 for t in in_bucket if float(t["pnl_usd"]) > 0) / n, 4)
              if n else None)
        ppt = (round(sum(float(t["pnl_usd"]) for t in in_bucket) / n, 6)
               if n else None)
        composite_buckets.append({
            "bucket": f"{lo:.2f}-{hi:.2f}",
            "n": n, "win_rate": wr, "pnl_per_trade": ppt,
        })
        if wr is not None and n >= CONFIDENCE_WEAK_N:
            n_buckets_with_data += 1
            if last_wr is not None and wr < last_wr - 0.05:
                monotonic = False
            last_wr = wr
    composite_calibration_status = (
        "MONOTONIC" if (n_buckets_with_data >= 3 and monotonic)
        else "NON_MONOTONIC" if n_buckets_with_data >= 3
        else "UNVERIFIABLE"
    )

    # Swarm signal value
    by_action: dict[str, list[float]] = {}
    for t in trades:
        a = t.get("swarm_action_at_entry")
        if a:
            by_action.setdefault(a, []).append(float(t["pnl_usd"]))
    action_stats = {
        a: {
            "n": len(p),
            "win_rate": round(sum(1 for v in p if v > 0) / len(p), 4) if p else None,
            "pnl_per_trade": round(sum(p) / len(p), 6) if p else None,
        } for a, p in by_action.items()
    }
    sb = action_stats.get("STRONG_BUY") or {}
    bb = action_stats.get("BUY") or {}
    anti_correlated = (
        sb.get("win_rate") is not None and bb.get("win_rate") is not None
        and sb["n"] >= CONFIDENCE_LIKELY_N and bb["n"] >= CONFIDENCE_LIKELY_N
        and sb["win_rate"] <= bb["win_rate"]
    )

    # Agent-quorum impact
    full_q = [float(t["pnl_usd"]) for t in trades
              if (t.get("swarm_agents_ok_at_entry") == t.get("swarm_agents_total_at_entry")
                  and t.get("swarm_agents_total_at_entry"))]
    deg_q = [float(t["pnl_usd"]) for t in trades
             if (t.get("swarm_agents_ok_at_entry") is not None
                 and t.get("swarm_agents_total_at_entry") is not None
                 and t["swarm_agents_ok_at_entry"] < t["swarm_agents_total_at_entry"])]
    full_wr = round(sum(1 for v in full_q if v > 0) / len(full_q), 4) if full_q else None
    deg_wr = round(sum(1 for v in deg_q if v > 0) / len(deg_q), 4) if deg_q else None
    delta_wr = round(full_wr - deg_wr, 4) if (full_wr is not None and deg_wr is not None) else None
    if (full_wr is not None and deg_wr is not None
            and len(full_q) >= CONFIDENCE_LIKELY_N and len(deg_q) >= CONFIDENCE_LIKELY_N
            and delta_wr is not None and delta_wr >= 0.15):
        quorum_verdict = "DEGRADED_QUORUM_SHOULD_VETO"
    elif full_wr is not None and deg_wr is not None:
        quorum_verdict = "NO_MATERIAL_DIFFERENCE"
    else:
        quorum_verdict = "UNVERIFIABLE"

    return {
        "tier_inversion": {
            "a_win_rate": a_wr, "a_n": a_n,
            "c_win_rate": c_wr, "c_n": c_n,
            "inverted": bool(inversion),
            "effect_size": inversion_effect,
            "suggested_confidence": inversion_confidence,
            "tier_table": tier_wr,
        },
        "composite_calibration": {
            "buckets": composite_buckets,
            "n_buckets_with_data": n_buckets_with_data,
            "status": composite_calibration_status,
        },
        "swarm_signal_value": {
            "by_action": action_stats,
            "anti_correlated": bool(anti_correlated),
            "n_with_swarm_action": sum(len(p) for p in by_action.values()),
        },
        "agent_quorum_impact": {
            "full_quorum_n": len(full_q), "full_quorum_wr": full_wr,
            "degraded_n": len(deg_q),     "degraded_wr": deg_wr,
            "delta_wr": delta_wr,
            "verdict": quorum_verdict,
        },
    }


def _calibration_schema_skeleton(bundle: dict) -> dict:
    return {
        "role": "calibration",
        "report_id": bundle.get("report_id"),
        "tier_inversion": {
            "claim": "<string>",
            "confidence": "<PROVEN|LIKELY|WEAK|UNVERIFIABLE>",
            "evidence_trade_id_count": 0,
        },
        "composite_calibration": {
            "buckets": "<echo computed_metrics.composite_calibration.buckets>",
            "monotonic_calibration": "<MONOTONIC|NON_MONOTONIC|UNVERIFIABLE>",
        },
        "swarm_signal_value": {
            "by_action": "<echo computed_metrics.swarm_signal_value.by_action>",
            "anti_correlated": False,
            "confidence": "<PROVEN|LIKELY|WEAK|UNVERIFIABLE>",
        },
        "agent_quorum_impact": "<echo computed_metrics.agent_quorum_impact>",
    }


def _calibration_fallback_stub(bundle: dict, metrics: dict, error: str | None) -> dict:
    ti = metrics["tier_inversion"]
    cc = metrics["composite_calibration"]
    return {
        "role": "calibration",
        "report_id": bundle.get("report_id"),
        "tier_inversion": {
            "claim": (
                f"A WR={ti['a_win_rate']} (n={ti['a_n']}) vs C WR={ti['c_win_rate']} "
                f"(n={ti['c_n']}) — inverted={ti['inverted']}"
            ),
            "confidence": ti["suggested_confidence"],
            "evidence_trade_id_count": ti["a_n"] + ti["c_n"],
        },
        "composite_calibration": {
            "buckets": cc["buckets"],
            "monotonic_calibration": cc["status"],
        },
        "swarm_signal_value": {
            "by_action": metrics["swarm_signal_value"]["by_action"],
            "anti_correlated": metrics["swarm_signal_value"]["anti_correlated"],
            "confidence": "UNVERIFIABLE" if metrics["swarm_signal_value"]["n_with_swarm_action"] < CONFIDENCE_WEAK_N else _confidence_label_for_n(metrics["swarm_signal_value"]["n_with_swarm_action"]),
        },
        "agent_quorum_impact": metrics["agent_quorum_impact"],
        "computed_metrics": metrics,
        "fallback_reason": f"L4 LLM call failed: {error or 'unknown'} — stub built from deterministic metrics",
    }


# ---------------------------------------------------------------------------
# L3 — Regime / Transition Analyst (LLM, numerics pre-computed)
# ---------------------------------------------------------------------------

_REGIME_SYSTEM = (
    "You are a regime-transition analyst for SPOT AGGRO. You receive "
    "PRE-COMPUTED regime statistics (you do NOT do arithmetic). Label each "
    "regime window per these rules: PROVEN if n>=20 entries AND WR divergence "
    "from overall >=0.15 AND sub-windows agree; LIKELY if n>=10; WEAK if n>=5; "
    "UNVERIFIABLE if n<5. Cover: regime_miscalibration (entries in a regime "
    "that under-performed), spi_collapse_clusters (contiguous >=3 exits "
    "reasoning SPI_DECAY/COMPOSITE_DECAY/SL), transition_failures "
    "(regime flipped within hold_period of an entry AND loss). "
    "If computed_metrics.regime_timeline_count == 0, every finding MUST be "
    "UNVERIFIABLE — do not fabricate. "
    "Output STRICT JSON only — start with { and end with }. No markdown fences, no explanations, no prose before or after."
)


def run_regime(bundle: dict) -> tuple[dict | None, dict]:
    t0 = time.time()
    try:
        metrics = _regime_metrics(bundle)
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        log.exception("regime metrics computation failed")
        return None, {"ok": False, "cost_usd": 0.0, "latency_ms": latency_ms,
                      "error": f"metrics_compute: {e}"[:300], "retries": 0}

    user_payload = {
        "report_id": bundle.get("report_id"),
        "window": bundle.get("window"),
        "computed_metrics": metrics,
        "schema_required": _regime_schema_skeleton(bundle),
    }
    prompt = _REGIME_SYSTEM + "\n\nINPUT:\n" + json.dumps(user_payload, default=str)

    parsed, meta = _call_specialist(
        provider=REGIME_PROVIDER, model=REGIME_MODEL,
        role="forensic_L3_regime",
        prompt=prompt, timeout_s=REGIME_TIMEOUT_S,
        max_retries=REGIME_MAX_RETRIES, t0=t0,
    )

    if parsed is None:
        parsed = _regime_fallback_stub(bundle, metrics, error=meta.get("error"))
        meta["ok"] = False
    else:
        parsed["computed_metrics"] = metrics
    return parsed, meta


def _regime_metrics(bundle: dict) -> dict:
    """Pure Python — compute per-regime WR, transition flips, SPI-collapse
    clusters, and transition failures. LLM just labels."""
    trades = [t for t in (bundle.get("trades_closed") or [])
              if t.get("pnl_usd") is not None]
    timeline = bundle.get("regime_timeline") or []

    n_total = len(trades)
    pnls_all = [float(t["pnl_usd"]) for t in trades]
    overall_wr = (
        round(sum(1 for p in pnls_all if p > 0) / n_total, 4) if n_total else None
    )

    # Per-entry-regime WR and expectancy
    by_regime: dict[str, list[dict]] = {}
    for t in trades:
        r = t.get("entry_regime") or "UNKNOWN"
        by_regime.setdefault(r, []).append(t)
    per_regime = []
    for r, ts in sorted(by_regime.items()):
        ps = [float(x["pnl_usd"]) for x in ts]
        n = len(ts)
        wr = round(sum(1 for v in ps if v > 0) / n, 4) if n else None
        wr_div = (round(wr - overall_wr, 4)
                  if (wr is not None and overall_wr is not None) else None)
        per_regime.append({
            "regime": r,
            "n": n,
            "win_rate": wr,
            "pnl_per_trade": round(sum(ps) / n, 6) if n else None,
            "wr_divergence_from_overall": wr_div,
            "suggested_confidence": _confidence_label_for_n(n),
        })

    # Regime stability from timeline samples
    stability_by_regime: dict[str, dict] = {}
    last_regime = None
    flips_total = 0
    samples_total = len(timeline)
    for sample in timeline:
        r = sample.get("regime") or "UNKNOWN"
        d = stability_by_regime.setdefault(r, {"samples": 0, "flips": 0})
        d["samples"] += 1
        if last_regime is not None and r != last_regime:
            d["flips"] += 1
            flips_total += 1
        last_regime = r
    regime_stability = [
        {
            "regime": r,
            "samples": v["samples"],
            "flips": v["flips"],
            "stability": round(1.0 - (v["flips"] / max(v["samples"], 1)), 4),
        }
        for r, v in sorted(stability_by_regime.items())
    ]

    # SPI-collapse clusters: runs of >=3 contiguous exits with decay/SL reason
    decay_reasons = {
        "SL", "stop_loss", "stop-loss",
        "SPI_DECAY", "SPI_DECAY_CONFIRMED", "spi_decay",
        "COMPOSITE_DECAY", "composite_decay",
    }
    clusters = []
    run = []
    for t in sorted(trades, key=lambda x: x.get("exit_ts_ms") or 0):
        reason = (t.get("exit_reason") or "")
        if reason in decay_reasons:
            run.append(t)
        else:
            if len(run) >= 3:
                clusters.append(run)
            run = []
    if len(run) >= 3:
        clusters.append(run)

    spi_collapse_clusters = []
    for i, cl in enumerate(clusters, 1):
        reasons_count: dict[str, int] = {}
        for t in cl:
            reasons_count[t.get("exit_reason") or "?"] = (
                reasons_count.get(t.get("exit_reason") or "?", 0) + 1
            )
        spi_collapse_clusters.append({
            "cluster_id": f"SPI-{i:03d}",
            "ts_range": [
                min(t.get("exit_ts_ms") or 0 for t in cl),
                max(t.get("exit_ts_ms") or 0 for t in cl),
            ],
            "trade_count": len(cl),
            "evidence_trade_ids": [t.get("trade_id") for t in cl],
            "exit_reasons": reasons_count,
        })

    # Transition failures: regime_at_exit != entry_regime AND pnl <= 0
    transition_failures = []
    for t in trades:
        er = t.get("entry_regime")
        xr = t.get("regime_at_exit")
        pnl = float(t.get("pnl_usd") or 0)
        if er and xr and er != xr and pnl <= 0:
            transition_failures.append({
                "trade_id": t.get("trade_id"),
                "symbol": t.get("symbol"),
                "entry_regime": er,
                "regime_at_exit": xr,
                "pnl_usd": pnl,
                "hold_seconds": t.get("hold_seconds"),
            })

    return {
        "overall_wr": overall_wr,
        "n_total_trades": n_total,
        "per_regime": per_regime,
        "regime_stability": regime_stability,
        "spi_collapse_clusters": spi_collapse_clusters,
        "transition_failures": transition_failures,
        "regime_timeline_count": samples_total,
        "regime_flips_total": flips_total,
    }


def _regime_schema_skeleton(bundle: dict) -> dict:
    return {
        "role": "regime",
        "report_id": bundle.get("report_id"),
        "regime_stability": "<echo computed_metrics.regime_stability>",
        "miscalibration_findings": [{
            "finding_id": "REG-001",
            "claim": "<string>",
            "confidence": "<PROVEN|LIKELY|WEAK|UNVERIFIABLE>",
            "evidence_trade_id_count": 0,
            "regime_window_ts_range": [0, 0],
        }],
        "spi_collapse_clusters": "<echo computed_metrics.spi_collapse_clusters>",
        "transition_failures_count": 0,
    }


def _regime_fallback_stub(bundle: dict, metrics: dict, error: str | None) -> dict:
    findings = []
    for r in metrics["per_regime"]:
        if (r["n"] >= CONFIDENCE_WEAK_N
                and r.get("wr_divergence_from_overall") is not None
                and abs(r["wr_divergence_from_overall"]) >= 0.10):
            findings.append({
                "finding_id": f"REG-{r['regime']}",
                "claim": (
                    f"regime={r['regime']}: n={r['n']} WR={r['win_rate']} "
                    f"divergence={r['wr_divergence_from_overall']} vs overall {metrics['overall_wr']}"
                ),
                "confidence": _confidence_label_for_n(r["n"]),
                "evidence_trade_id_count": r["n"],
                "regime_window_ts_range": [0, 0],
            })
    return {
        "role": "regime",
        "report_id": bundle.get("report_id"),
        "regime_stability": metrics["regime_stability"],
        "miscalibration_findings": findings,
        "spi_collapse_clusters": metrics["spi_collapse_clusters"],
        "transition_failures_count": len(metrics["transition_failures"]),
        "computed_metrics": metrics,
        "fallback_reason": f"L3 LLM call failed: {error or 'unknown'} — stub built from deterministic metrics",
    }


# ---------------------------------------------------------------------------
# L5 — Chief Adjudicator (LLM)
# ---------------------------------------------------------------------------

_ADJUDICATOR_SYSTEM = (
    "You are the chief adjudicator for SPOT AGGRO forensic reports. You "
    "receive structured findings from L1 (integrity), L2 (physics, may be "
    "absent), L3 (regime, may be absent), L4 (calibration, may be absent), "
    "plus the orchestrator's quorum_state. "
    "Rules: "
    "1) You may NOT introduce any new claim. Every line of your verdict MUST "
    "reference at least one finding_id from L1-L4. If only L1 is present, "
    "limit findings to integrity-derived observations. "
    "2) If L1 marked any field UNVERIFIABLE, propagate that to dependent "
    "findings. "
    "3) If quorum_state.degraded == true, prefix verdict_justification with "
    "'DEGRADED CONFIDENCE' and downgrade every PROVEN to LIKELY. "
    "4) Produce 0-7 ranked_root_causes (highest evidence first). Each MUST "
    "have confidence_label, evidence_ids, and a fix_recommendation whose "
    "lever is one of: scoring.classify_tier, coin_memory.suppress_threshold, "
    "coin_memory.cooldown_min_streak, swarm_entry_gate, consensus_min, "
    "conflict_max, min_notional_usd_per_trade, tier_caps, "
    "swarm.fast_interval_s, kill_dd_pct. No other lever values allowed. "
    "5) anti_patterns: 0-5 entries with trade_id evidence. "
    "6) overall_verdict in {NEEDS_HALT, NEEDS_TUNING, ACCEPTABLE, "
    "INSUFFICIENT_DATA}. NEEDS_HALT requires expectancy_usd<-0.10 AND "
    "profit_factor<0.5 AND n_trades>=20; otherwise downgrade to NEEDS_TUNING. "
    "Output STRICT JSON only — start with { and end with }. No markdown fences, no explanations, no prose before or after."
)


def run_adjudicator(
    *,
    shared_input: dict,
    specialist_outputs: dict,
    quorum_state: dict,
) -> tuple[dict | None, dict]:
    """Call L5 adjudicator. Returns (result, meta).

    meta = {ok, cost_usd, latency_ms, error, retries}
    """
    user_payload = {
        "report_id": shared_input.get("report_id"),
        "window": shared_input.get("window"),
        "quorum_state": quorum_state,
        "shared_input_summary": _summarise_shared_input(shared_input),
        "specialist_outputs": specialist_outputs,
        "schema_required": _schema_skeleton(shared_input, quorum_state),
    }
    prompt = _ADJUDICATOR_SYSTEM + "\n\nINPUT:\n" + json.dumps(user_payload, default=str)

    t0 = time.time()
    cost = 0.0
    retries = 0
    last_error: str | None = None
    parsed: dict | None = None

    for attempt in range(ADJUDICATOR_MAX_RETRIES + 1):
        try:
            text, call_cost = _call_llm_sync(prompt)
            cost += call_cost
            parsed = _parse_json(text)
            if parsed is not None:
                break
            last_error = "unparseable JSON response"
            retries = attempt + 1
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"[:300]
            retries = attempt + 1

    latency_ms = int((time.time() - t0) * 1000)

    # Hard guardrails on the parsed output. Even if the LLM returns JSON,
    # enforce the lever-allow-list and verdict-thresholds from §5 here.
    if parsed is not None:
        parsed = _enforce_l5_constraints(parsed, shared_input, quorum_state)

    if parsed is None:
        # L5 failed entirely — synthesise an honest stub. Never silent skip.
        parsed = {
            "role": "adjudicator",
            "report_id": shared_input.get("report_id"),
            "generated_ts_ms": shared_input.get("generated_ts_ms"),
            "window": shared_input.get("window"),
            "quorum_state": quorum_state,
            "overall_verdict": "INSUFFICIENT_DATA",
            "verdict_justification": (
                f"L5 adjudicator failed after {retries} attempt(s): "
                f"{last_error or 'unknown'}"
            ),
            "headline_metrics": {
                "expectancy_usd": None, "win_rate": None,
                "profit_factor": None, "n_trades": len(shared_input.get("trades_closed") or []),
                "friction_ratio": None,
            },
            "ranked_root_causes": [],
            "anti_patterns": [],
            "evidence_gaps": list(shared_input.get("evidence_gaps") or []) + [
                f"L5 adjudicator failure: {last_error}",
            ],
        }
        return parsed, {
            "ok": False, "cost_usd": cost, "latency_ms": latency_ms,
            "error": last_error, "retries": retries,
        }

    return parsed, {
        "ok": True, "cost_usd": cost, "latency_ms": latency_ms,
        "error": None, "retries": retries,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALLOWED_LEVERS = {
    "scoring.classify_tier", "coin_memory.suppress_threshold",
    "coin_memory.cooldown_min_streak", "swarm_entry_gate",
    "consensus_min", "conflict_max", "min_notional_usd_per_trade",
    "tier_caps", "swarm.fast_interval_s", "kill_dd_pct",
}

_VERDICTS = {"NEEDS_HALT", "NEEDS_TUNING", "ACCEPTABLE", "INSUFFICIENT_DATA"}


def _summarise_shared_input(s: dict) -> dict:
    """Compact summary for L5 — full bundle would blow the context window."""
    trades = s.get("trades_closed") or []
    n = len(trades)
    pnls = [t.get("pnl_usd") for t in trades if t.get("pnl_usd") is not None]
    fees = [t.get("fee_usd") for t in trades if t.get("fee_usd") is not None]
    notionals = [t.get("notional_usd") for t in trades if t.get("notional_usd") is not None]
    return {
        "n_trades": n,
        "n_with_pnl": len(pnls),
        "n_with_fee": len(fees),
        "sum_pnl_usd": round(sum(pnls), 6) if pnls else None,
        "sum_fee_usd": round(sum(fees), 6) if fees else None,
        "median_notional_usd": round(sorted(notionals)[len(notionals) // 2], 4) if notionals else None,
        "tiers_observed": sorted({t.get("tier") for t in trades if t.get("tier")}),
        "regimes_observed": sorted({t.get("entry_regime") for t in trades if t.get("entry_regime")}),
        "evidence_gaps": s.get("evidence_gaps") or [],
        "consensus_log_count": len(s.get("consensus_log") or []),
        "infra_findings_count": len(s.get("infrastructure_findings_window") or []),
    }


def _schema_skeleton(shared_input: dict, quorum_state: dict) -> dict:
    return {
        "role": "adjudicator",
        "report_id": shared_input.get("report_id"),
        "generated_ts_ms": shared_input.get("generated_ts_ms"),
        "window": shared_input.get("window"),
        "quorum_state": quorum_state,
        "overall_verdict": "<one of NEEDS_HALT|NEEDS_TUNING|ACCEPTABLE|INSUFFICIENT_DATA>",
        "verdict_justification": "<one sentence citing finding_ids>",
        "headline_metrics": {
            "expectancy_usd": "<float|null>", "win_rate": "<float|null>",
            "profit_factor": "<float|null>", "n_trades": "<int>",
            "friction_ratio": "<float|null>",
        },
        "ranked_root_causes": [{
            "rank": 1, "root_cause": "<string>",
            "confidence": "<PROVEN|LIKELY|WEAK|UNVERIFIABLE>",
            "evidence_ids": ["INT-001"],
            "fix_recommendation": {
                "lever": "<one of allowed levers>",
                "config_key": "<string|null>",
                "current_value": "<value|null>",
                "proposed_value": "<value|null>",
                "expected_effect": "<string>",
            },
        }],
        "anti_patterns": [{"pattern": "<string>", "trade_count": 0, "trade_ids": []}],
        "evidence_gaps": ["<string>"],
    }


def _parse_json(text: str | None) -> dict | None:
    """Tolerant JSON extractor — handles ```fences```, prose wrapping, and
    partial outputs. Finds the first BALANCED top-level {...} block."""
    if not text:
        return None
    s = text.strip()
    # Strip markdown fences anywhere in the text.
    s = re.sub(r"```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```", "", s)
    s = s.strip()
    # First try whole-string parse.
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (TypeError, ValueError):
        pass
    # Walk the string brace-by-brace to find the first balanced {...}.
    in_str = False
    esc = False
    depth = 0
    start = -1
    for i, ch in enumerate(s):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = s[start:i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        return obj
                except (TypeError, ValueError):
                    start = -1  # keep walking
                    continue
    return None


def _enforce_l5_constraints(
    parsed: dict, shared_input: dict, quorum_state: dict,
) -> dict:
    """Defensive guardrails applied AFTER parsing. The LLM is asked to
    obey these in its prompt; we re-enforce in code so a hallucination
    can never escape into the report."""
    # 1. Verdict allow-list.
    verdict = str(parsed.get("overall_verdict") or "INSUFFICIENT_DATA").upper()
    if verdict not in _VERDICTS:
        verdict = "INSUFFICIENT_DATA"

    # 2. NEEDS_HALT thresholds (§5 hard rule).
    headline = parsed.get("headline_metrics") or {}
    n = headline.get("n_trades") or len(shared_input.get("trades_closed") or [])
    expectancy = headline.get("expectancy_usd")
    pf = headline.get("profit_factor")
    if verdict == "NEEDS_HALT":
        if not (expectancy is not None and expectancy < -0.10
                and pf is not None and pf < 0.5
                and n is not None and n >= 20):
            verdict = "NEEDS_TUNING"

    # 3. Lever allow-list on every fix_recommendation.
    rrcs = parsed.get("ranked_root_causes") or []
    cleaned_rrcs = []
    for rc in rrcs:
        fix = (rc or {}).get("fix_recommendation") or {}
        lever = fix.get("lever")
        if lever and lever not in _ALLOWED_LEVERS:
            # Drop the recommendation rather than ship an unknown lever.
            rc = {**rc, "fix_recommendation": {
                "lever": None, "config_key": None,
                "current_value": None, "proposed_value": None,
                "expected_effect": (
                    f"DROPPED: model proposed unknown lever {lever!r} not in allow-list"
                ),
            }}
        cleaned_rrcs.append(rc)

    # 4. Quorum degradation downgrade — re-apply in case L5 missed it.
    if quorum_state.get("degraded"):
        for rc in cleaned_rrcs:
            if (rc.get("confidence") or "").upper() == "PROVEN":
                rc["confidence"] = "LIKELY"

    parsed["overall_verdict"] = verdict
    parsed["ranked_root_causes"] = cleaned_rrcs
    return parsed


# Forensic responses contain pre-computed metric tables and ranked-cause
# arrays — they're materially larger than the trading swarm's per-coin votes.
# 600 tokens (the trading-swarm default) truncates them. 2000 fits a full
# L5 report incl. 5 root_causes.
FORENSIC_MAX_TOKENS = 2000


def _llm_call_with_tokens(
    *, provider: str, model: str, prompt: str,
    max_tokens: int = FORENSIC_MAX_TOKENS, temperature: float = 0.1,
) -> tuple[str, float]:
    """Direct call to claw.llm.complete with forensic-sized max_tokens.

    Bypasses shared.llm.consensus._call_member (which hard-codes 600
    tokens — fine for trading votes, too small for forensic reports).
    """
    from claw import llm as claw_llm

    def _sync():
        return claw_llm.complete(
            prompt, provider=provider, model=model,
            max_tokens=max_tokens, temperature=temperature,
        )

    out = _sync()
    if not out.get("ok"):
        raise RuntimeError(out.get("reason") or "llm_unavailable")
    text = out.get("text") or ""
    # Cost: derive from claw.llm's own _rough_cost via shared helper.
    try:
        from shared.llm.consensus import _rough_cost_usd
        cost = _rough_cost_usd(provider, model, len(prompt), len(text))
    except Exception:
        cost = 0.0
    return text, cost


def _call_llm_sync(prompt: str) -> tuple[str, float]:
    """Sync wrapper for L5 adjudicator. Uses FORENSIC_MAX_TOKENS."""
    return _llm_call_with_tokens(
        provider=ADJUDICATOR_PROVIDER, model=ADJUDICATOR_MODEL,
        prompt=prompt,
    )


def _call_specialist(
    *,
    provider: str, model: str, role: str,
    prompt: str, timeout_s: int, max_retries: int, t0: float,
) -> tuple[dict | None, dict]:
    """Generic LLM-specialist driver used by L2/L4 (and reusable for L3).

    Uses FORENSIC_MAX_TOKENS to avoid truncating multi-section JSON outputs.
    Returns (parsed_dict_or_None, meta).
    """
    cost = 0.0
    retries = 0
    last_error: str | None = None
    parsed: dict | None = None

    for attempt in range(max_retries + 1):
        try:
            # Run with timeout via asyncio.wait_for around a thread.
            async def _go():
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        _llm_call_with_tokens,
                        provider=provider, model=model, prompt=prompt,
                    ),
                    timeout=timeout_s,
                )
            text, call_cost = asyncio.run(_go())
            cost += call_cost
            parsed = _parse_json(text)
            if parsed is not None:
                break
            last_error = "unparseable JSON response"
            retries = attempt + 1
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"[:300]
            retries = attempt + 1

    latency_ms = int((time.time() - t0) * 1000)
    meta = {
        "ok": parsed is not None,
        "cost_usd": cost,
        "latency_ms": latency_ms,
        "error": last_error if parsed is None else None,
        "retries": retries,
    }
    return parsed, meta


def _confidence_label_for_n(n: int | None) -> str:
    """Map raw sample size to a §5 confidence label (no effect-size check —
    use this only when caller has already verified direction/effect)."""
    if n is None or n < CONFIDENCE_WEAK_N:
        return "UNVERIFIABLE"
    if n < CONFIDENCE_LIKELY_N:
        return "WEAK"
    if n < CONFIDENCE_PROVEN_N:
        return "LIKELY"
    return "PROVEN"
