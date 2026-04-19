"""
Governance runtime — cadence tick + per-report hook.

Non-trading. Non-UI. No edits to engine.py. No imports of forensic_v2.

The engine loop (Phase 8b, later) calls `tick(now_ms)` every heartbeat.
This function is safe to call every second — it only performs work when
at least one cadence is actually due, and it returns a compact summary
the caller can publish on a dashboard without interpretation.

Per-report hook: `on_forensic_report(payload)` wraps a forensic_v2 report
payload handed in by the caller and attaches the governance verdict.
Does not import forensic_v2.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import store
from .trade_source import load_closed_trades
from .wri import analyzer as wri_an
from .wri import scheduler as wri_sched
from .governor import approver as gov_ap

log = logging.getLogger("spot_aggro.governance.runtime")


@dataclass(frozen=True)
class TickResult:
    now_ms: int
    wri_runs_fired: list[str]           # cadence names
    governor_meta_fired: bool
    errors: list[str]


# ---------------------------------------------------------------------------
# WRI cadence tick
# ---------------------------------------------------------------------------

def _run_wri_cadence(
    *,
    cadence: wri_sched.CadenceSpec,
    now_ms: int,
    wri_cfg: wri_an.WRIConfig,
    trade_loader: Callable[[int, int], list[dict[str, Any]]] = load_closed_trades,
) -> store.WRIRow:
    window_end = now_ms
    window_start = now_ms - cadence.window_s * 1000
    trades = trade_loader(window_start, window_end)
    report = wri_an.analyze(
        trades,
        window_start_ms=window_start,
        window_end_ms=window_end,
        cadence=cadence.name,
        cfg=wri_cfg,
    )
    wins = report["totals"]["wins"]
    n = report["totals"]["n_trades"]
    wr = (wins / n) if n > 0 else None
    row = store.WRIRow(
        id=None,
        ts_ms=now_ms,
        cadence=cadence.name,
        window_start_ms=window_start,
        window_end_ms=window_end,
        n_trades=n,
        win_rate=wr,
        report=report,
        confidence=report["confidence"],
    )
    store.write_wri_run(row)
    log.info(
        "[wri] %s n=%d wr=%s conf=%s",
        cadence.name, n,
        f"{wr:.3f}" if wr is not None else "None",
        report["confidence"],
    )
    return row


# ---------------------------------------------------------------------------
# Governor meta-review cadence
# ---------------------------------------------------------------------------

_META_CADENCE_KIND = "meta_24h"


def _meta_due(now_ms: int, cfg: gov_ap.GovernorConfig) -> bool:
    if not cfg.meta_enabled:
        return False
    # Last meta row from the store's meta_24h rows
    rows = store.list_governance_runs(kind=_META_CADENCE_KIND, limit=1)
    if not rows:
        return True
    last_ts = rows[0].ts_ms
    return (now_ms - last_ts) >= cfg.meta_interval_s * 1000


def _run_governor_meta(
    *, now_ms: int, cfg: gov_ap.GovernorConfig,
) -> store.GovernanceRow:
    window_start = now_ms - cfg.meta_window_s * 1000
    rows = store.list_governance_runs(
        kind="per_report", since_ts_ms=window_start, limit=5000,
    )
    payloads = [r.findings for r in rows]
    meta = gov_ap.meta_review(payloads, cfg=cfg)

    # Pick a coarse trust signal for the meta row: 1 - rejected_share
    total = meta.get("total_reports", 0)
    rejected = meta.get("rejected", 0)
    trust = 1.0 - (rejected / total) if total else 1.0
    row = store.GovernanceRow(
        id=None, ts_ms=now_ms, kind=_META_CADENCE_KIND, report_id=None,
        verdict=(
            gov_ap.VERDICT_APPROVED if not meta["recurring_defects"]
            else gov_ap.VERDICT_APPROVED_WARNINGS
        ),
        trust_score=round(trust, 4),
        findings=meta,
    )
    store.write_governance_run(row)
    log.info(
        "[governor:meta] total=%d rejected=%d recurring=%d trust=%.2f",
        meta.get("total_reports", 0), meta.get("rejected", 0),
        len(meta.get("recurring_defects", [])), trust,
    )
    return row


# ---------------------------------------------------------------------------
# Public tick entry
# ---------------------------------------------------------------------------

def tick(
    *,
    now_ms: Optional[int] = None,
    schedule: Optional[wri_sched.WRISchedule] = None,
    governor_cfg: Optional[gov_ap.GovernorConfig] = None,
    trade_loader: Callable[[int, int], list[dict[str, Any]]] = load_closed_trades,
) -> TickResult:
    """Fire any due WRI cadence + due governor meta-review. Safe to call
    every engine heartbeat; no-op when nothing is due."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    schedule = schedule or wri_sched.WRISchedule.load()
    governor_cfg = governor_cfg or gov_ap.GovernorConfig.load()

    fired: list[str] = []
    errors: list[str] = []

    wri_cfg = wri_an.WRIConfig(
        cluster_min_trades=schedule.cluster_min_trades,
        drag_floor_pct=schedule.drag_floor_pct,
        tier_min_trades_for_likely=schedule.tier_min_trades_for_likely,
        tier_min_trades_for_proven=schedule.tier_min_trades_for_proven,
    )

    for cadence in wri_sched.due_cadences(now_ms=now_ms, schedule=schedule):
        try:
            _run_wri_cadence(
                cadence=cadence,
                now_ms=now_ms,
                wri_cfg=wri_cfg,
                trade_loader=trade_loader,
            )
            fired.append(cadence.name)
        except Exception as exc:  # noqa: BLE001 — we must not crash the loop
            log.exception("[wri] cadence %s failed", cadence.name)
            errors.append(f"wri:{cadence.name}: {type(exc).__name__}: {exc}")

    meta_fired = False
    if _meta_due(now_ms, governor_cfg):
        try:
            _run_governor_meta(now_ms=now_ms, cfg=governor_cfg)
            meta_fired = True
        except Exception as exc:  # noqa: BLE001
            log.exception("[governor:meta] failed")
            errors.append(f"governor:meta: {type(exc).__name__}: {exc}")

    return TickResult(
        now_ms=now_ms,
        wri_runs_fired=fired,
        governor_meta_fired=meta_fired,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Per-report hook (Forensic Governor, per-report cadence)
# ---------------------------------------------------------------------------

def on_forensic_report(
    payload: dict[str, Any],
    *,
    now_ms: Optional[int] = None,
    cfg: Optional[gov_ap.GovernorConfig] = None,
) -> dict[str, Any]:
    """Attach a governance verdict to a forensic_v2 report payload.

    Input: a forensic report dict handed in by the caller. The caller is
    responsible for having generated the payload from forensic_v2; this
    function NEVER imports forensic_v2.

    Output: a NEW dict equal to `payload` plus a top-level `"governance"`
    block carrying verdict, trust_score, flags, and the approval note.
    The original payload is not mutated.
    """
    if not isinstance(payload, dict):
        raise TypeError("forensic report payload must be a dict")
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    cfg = cfg or gov_ap.GovernorConfig.load()

    verdict = gov_ap.approve(payload, cfg=cfg)

    row = store.GovernanceRow(
        id=None, ts_ms=now_ms, kind="per_report",
        report_id=verdict.get("report_id") or None,
        verdict=verdict["verdict"],
        trust_score=verdict["trust_score"],
        findings=verdict,
    )
    store.write_governance_run(row)

    # Return an augmented payload. We do NOT mutate the caller's dict; we
    # return a shallow copy with the governance block added. Callers that
    # want to publish the trusted payload can inspect
    # `out["governance"]["verdict"]` before showing the report.
    out = dict(payload)
    out["governance"] = verdict
    log.info(
        "[governor:report] id=%s verdict=%s trust=%.2f",
        verdict.get("report_id") or "?", verdict["verdict"],
        verdict["trust_score"],
    )
    return out
