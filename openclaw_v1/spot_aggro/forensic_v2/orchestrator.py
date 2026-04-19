"""
Forensic orchestrator.

Commit 1: builds SHARED_INPUT, persists a stub row (verdict =
INSUFFICIENT_DATA, no specialists called).
Commit 2: wires L1 (integrity) and L5 (adjudicator) end-to-end with the
quorum reject loop. L1 must succeed or the report is rejected.
Commits 3-5 add L2/L4/L3 + dashboard.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from .schema import SCHEMA_VERSION, build_shared_input
from .persistence import upsert_run, get_run, list_runs
from . import specialists

log = logging.getLogger("spot_aggro.forensic_v2")


def _new_report_id() -> str:
    # ULID-ish: ts + random hex. Sortable and unique.
    return f"fr_{int(time.time()*1000):x}_{uuid.uuid4().hex[:8]}"


def generate_report(
    window_h: float = 6.0,
    window_label: str = "rolling",
    engine_status: dict | None = None,
    config_snapshot: dict | None = None,
) -> dict:
    """Generate a forensic report for the last `window_h` hours.

    Commit-2 behaviour:
      1. Build SHARED_INPUT.
      2. Run L1 (integrity). If L1 fails or marks input UNUSABLE, return
         a rejected stub with overall_verdict=INSUFFICIENT_DATA.
      3. Run L5 (adjudicator) with whatever specialist outputs are
         available. With only L1 present, L5 gets degraded=True.
      4. Persist + return a compact summary.
    """
    report_id = _new_report_id()
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(window_h * 3_600_000)

    bundle = build_shared_input(
        report_id=report_id,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        window_label=window_label,
        engine_status=engine_status,
        config_snapshot=config_snapshot,
    )

    n_trades = len(bundle["trades_closed"])
    cost_total = 0.0
    latency_total = 0
    specialist_outputs: dict[str, Any] = {}

    # ---------- L1 — integrity (mandatory) ---------------------------------
    l1_result, l1_meta = specialists.run_integrity(bundle)
    cost_total += l1_meta.get("cost_usd", 0.0)
    latency_total += l1_meta.get("latency_ms", 0)
    integrity_ok = l1_meta.get("ok", False)
    if integrity_ok:
        specialist_outputs["integrity"] = l1_result

    # If L1 failed entirely, REJECT the report — do not call L5.
    if not integrity_ok:
        rejection_payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "shared_input": bundle,
            "specialist_outputs": {},
            "adjudication": {
                "role": "adjudicator",
                "report_id": report_id,
                "generated_ts_ms": bundle["generated_ts_ms"],
                "window": bundle["window"],
                "quorum_state": {
                    "specialists_returned": 0,
                    "specialists_required": 4,
                    "degraded": True,
                    "missing_roles": ["integrity", "physics", "regime", "calibration"],
                    "integrity_failure": True,
                    "integrity_error": l1_meta.get("error"),
                },
                "overall_verdict": "INSUFFICIENT_DATA",
                "verdict_justification": (
                    "L1 integrity check failed or unparseable — "
                    "report rejected per quorum rules"
                ),
                "headline_metrics": {
                    "expectancy_usd": None, "win_rate": None,
                    "profit_factor": None, "n_trades": n_trades,
                    "friction_ratio": None,
                },
                "ranked_root_causes": [],
                "anti_patterns": [],
                "evidence_gaps": list(bundle["evidence_gaps"]) + [
                    f"L1 integrity rejected: {l1_meta.get('error', 'unknown')}"
                ],
            },
        }
        upsert_run(
            report_id=report_id,
            window_start_ms=start_ms,
            window_end_ms=end_ms,
            n_trades=n_trades,
            overall_verdict="INSUFFICIENT_DATA",
            expectancy_usd=None,
            win_rate=None,
            profit_factor=None,
            friction_ratio=None,
            quorum_returned=0,
            quorum_required=4,
            degraded=True,
            cost_usd_total=cost_total,
            latency_ms_total=latency_total,
            evidence_gap_count=len(rejection_payload["adjudication"]["evidence_gaps"]),
            payload=rejection_payload,
            pdf_path=None,
        )
        return {
            "report_id": report_id,
            "n_trades": n_trades,
            "window_h": window_h,
            "verdict": "INSUFFICIENT_DATA",
            "rejected": True,
            "reason": "L1 integrity failure",
        }

    # ---------- L2 — physics ----------------------------------------------
    l2_result, l2_meta = specialists.run_physics(bundle)
    cost_total += l2_meta.get("cost_usd", 0.0)
    latency_total += l2_meta.get("latency_ms", 0)
    if l2_meta.get("ok"):
        specialist_outputs["physics"] = l2_result
    elif l2_result is not None:
        # LLM failed but deterministic-stub fallback returned — still
        # surface the facts so L5 can cite them. Counted in quorum
        # because the deterministic numbers are authoritative.
        specialist_outputs["physics"] = l2_result

    # ---------- L4 — calibration ------------------------------------------
    l4_result, l4_meta = specialists.run_calibration(bundle)
    cost_total += l4_meta.get("cost_usd", 0.0)
    latency_total += l4_meta.get("latency_ms", 0)
    if l4_meta.get("ok"):
        specialist_outputs["calibration"] = l4_result
    elif l4_result is not None:
        specialist_outputs["calibration"] = l4_result

    # ---------- L3 — regime (now live) -------------------------------------
    l3_result, l3_meta = specialists.run_regime(bundle)
    cost_total += l3_meta.get("cost_usd", 0.0)
    latency_total += l3_meta.get("latency_ms", 0)
    if l3_meta.get("ok"):
        specialist_outputs["regime"] = l3_result
    elif l3_result is not None:
        specialist_outputs["regime"] = l3_result

    quorum_returned = len(specialist_outputs)
    quorum_required = 4                                    # L1+L2+L3+L4
    degraded = quorum_returned < quorum_required

    # ---------- L5 — adjudicator -------------------------------------------
    l5_result, l5_meta = specialists.run_adjudicator(
        shared_input=bundle,
        specialist_outputs=specialist_outputs,
        quorum_state={
            "specialists_returned": quorum_returned,
            "specialists_required": quorum_required,
            "degraded": degraded,
            "missing_roles": [
                r for r in ("integrity", "physics", "regime", "calibration")
                if r not in specialist_outputs
            ],
        },
    )
    cost_total += l5_meta.get("cost_usd", 0.0)
    latency_total += l5_meta.get("latency_ms", 0)

    overall_verdict = (l5_result or {}).get("overall_verdict", "INSUFFICIENT_DATA")
    headline = (l5_result or {}).get("headline_metrics", {}) or {}
    evidence_gaps = (l5_result or {}).get("evidence_gaps", []) or []

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "shared_input": bundle,
        "specialist_outputs": specialist_outputs,
        "adjudication": l5_result or {
            "overall_verdict": "INSUFFICIENT_DATA",
            "verdict_justification": "L5 adjudicator returned no result",
        },
        "specialist_meta": {
            "L1": l1_meta,
            "L2": l2_meta,
            "L3": l3_meta,
            "L4": l4_meta,
            "L5": l5_meta,
        },
    }

    # Prefer deterministic physics numbers over whatever L5 echoes — they're
    # the source of truth for headline metrics on the DB row.
    phy_out = specialist_outputs.get("physics") or {}
    phy_overall = (
        (phy_out.get("computed_metrics") or {}).get("overall")
        or phy_out.get("metrics")
        or {}
    )
    headline_expectancy = phy_overall.get("expectancy_usd", headline.get("expectancy_usd"))
    headline_wr         = phy_overall.get("win_rate", headline.get("win_rate"))
    headline_pf         = phy_overall.get("profit_factor", headline.get("profit_factor"))
    headline_friction   = phy_overall.get("friction_ratio", headline.get("friction_ratio"))

    # Render PDF — best-effort; failure here MUST NOT break the API call.
    pdf_path: str | None = None
    try:
        from . import pdf_renderer
        # Feed the renderer the same shape get_run returns.
        pdf_path = pdf_renderer.render_pdf({
            "report_id": report_id,
            "generated_ts_ms": bundle["generated_ts_ms"],
            "window_start_ts_ms": start_ms,
            "window_end_ts_ms": end_ms,
            "n_trades_in_window": n_trades,
            "overall_verdict": overall_verdict,
            "expectancy_usd": headline_expectancy,
            "win_rate": headline_wr,
            "profit_factor": headline_pf,
            "friction_ratio": headline_friction,
            "quorum_returned": quorum_returned,
            "quorum_required": quorum_required,
            "degraded_confidence": int(bool(degraded)),
            "cost_usd_total": cost_total,
            "latency_ms_total": latency_total,
            "evidence_gap_count": len(evidence_gaps),
            "payload": payload,
        })
    except Exception:
        log.exception("forensic_v2 PDF render failed (non-fatal)")

    upsert_run(
        report_id=report_id,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        n_trades=n_trades,
        overall_verdict=str(overall_verdict),
        expectancy_usd=headline_expectancy,
        win_rate=headline_wr,
        profit_factor=headline_pf,
        friction_ratio=headline_friction,
        quorum_returned=quorum_returned,
        quorum_required=quorum_required,
        degraded=degraded,
        cost_usd_total=cost_total,
        latency_ms_total=latency_total,
        evidence_gap_count=len(evidence_gaps),
        payload=payload,
        pdf_path=pdf_path,
    )

    return {
        "report_id": report_id,
        "n_trades": n_trades,
        "window_h": window_h,
        "verdict": str(overall_verdict),
        "degraded": degraded,
        "specialists_returned": quorum_returned,
        "evidence_gap_count": len(evidence_gaps),
        "pdf_path": pdf_path,
    }


def get_report(report_id: str) -> dict | None:
    return get_run(report_id)


def list_reports(limit: int = 50) -> list[dict]:
    return list_runs(limit)
