"""Opportunity Fabric — Sprint 3: Execution SLO promotion gate.

Extends the phase-vv Wilson-95 promotion rule with execution quality.

Old rule:  promote iff n >= 40 AND wilson_lower > 0 AND net_pnl > 0
New rule:  promote iff (old rule) AND realized_slippage_bp < 10
                                   AND fill_rate > 0.95

Promotion verdicts split:
    - promote_full          : statistics + execution both green
    - promote_statistical   : stats green but execution borderline
                              (elevated slippage or fill-rate <0.95).
                              Operator can still approve; alert raised.
    - permanent_disable     : Wilson-upper <= 0 (unchanged from phase-vv)
    - racing / insufficient : unchanged

Default-OFF: if SPOT_EXEC_SLO_GATE != "1", this module only reports
metrics; variant_trip_wire keeps the old two-rail rule. Activation
flips the stricter gate on.

Metrics come from spot_live_variant_entries.slippage_bp and .fill_rate
columns, which admission code populates. Fails-safe to the old rule if
either column is missing or null.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any

SLO_SLIPPAGE_BP_MAX = float(os.environ.get("SPOT_EXEC_SLO_SLIPPAGE_BP", "10"))
SLO_FILL_RATE_MIN = float(os.environ.get("SPOT_EXEC_SLO_FILL_RATE", "0.95"))


def gate_enabled() -> bool:
    return os.environ.get("SPOT_EXEC_SLO_GATE", "0").strip() == "1"


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


def _ensure_slo_columns() -> None:
    """Idempotent: add slippage_bp, fill_rate to spot_live_variant_entries."""
    try:
        con = _connect()
        try:
            cols = [
                r["name"] for r in con.execute(
                    "PRAGMA table_info(spot_live_variant_entries)"
                ).fetchall()
            ]
            if not cols:
                return
            if "slippage_bp" not in cols:
                con.execute(
                    "ALTER TABLE spot_live_variant_entries"
                    " ADD COLUMN slippage_bp REAL"
                )
            if "fill_rate" not in cols:
                con.execute(
                    "ALTER TABLE spot_live_variant_entries"
                    " ADD COLUMN fill_rate REAL"
                )
        finally:
            con.close()
    except Exception:
        pass


@dataclass
class ExecutionMetrics:
    variant: str
    n_exits_with_metrics: int
    slippage_bp_mean: float | None
    slippage_bp_p95: float | None
    fill_rate_mean: float | None
    slo_slippage_passes: bool
    slo_fill_rate_passes: bool
    slo_all_pass: bool
    reason: str = ""


def metrics_for(variant: str) -> ExecutionMetrics:
    """Aggregate realized execution quality over closed entries for a variant."""
    _ensure_slo_columns()
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT slippage_bp, fill_rate"
                " FROM spot_live_variant_entries"
                " WHERE variant = ? AND status = 'closed'",
                (variant,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError:
        return ExecutionMetrics(
            variant=variant, n_exits_with_metrics=0,
            slippage_bp_mean=None, slippage_bp_p95=None,
            fill_rate_mean=None,
            slo_slippage_passes=False,
            slo_fill_rate_passes=False,
            slo_all_pass=False,
            reason="no spot_live_variant_entries table",
        )

    slips = [float(r["slippage_bp"]) for r in rows
             if r["slippage_bp"] is not None]
    fills = [float(r["fill_rate"]) for r in rows
             if r["fill_rate"] is not None]
    n = min(len(slips), len(fills))

    slip_mean = sum(slips) / len(slips) if slips else None
    slip_p95 = None
    if slips:
        s = sorted(slips)
        idx = int(0.95 * (len(s) - 1))
        slip_p95 = s[idx]
    fill_mean = sum(fills) / len(fills) if fills else None

    slo_slip = slip_mean is not None and slip_mean < SLO_SLIPPAGE_BP_MAX
    slo_fill = fill_mean is not None and fill_mean > SLO_FILL_RATE_MIN
    slo_all = bool(slo_slip and slo_fill)

    reason_parts = []
    if slip_mean is None:
        reason_parts.append("no slippage data")
    else:
        reason_parts.append(
            f"slippage_bp mean={slip_mean:.1f} "
            f"(SLO < {SLO_SLIPPAGE_BP_MAX}) -> {'PASS' if slo_slip else 'FAIL'}"
        )
    if fill_mean is None:
        reason_parts.append("no fill_rate data")
    else:
        reason_parts.append(
            f"fill_rate mean={fill_mean:.3f} "
            f"(SLO > {SLO_FILL_RATE_MIN}) -> {'PASS' if slo_fill else 'FAIL'}"
        )

    return ExecutionMetrics(
        variant=variant,
        n_exits_with_metrics=n,
        slippage_bp_mean=round(slip_mean, 2) if slip_mean is not None else None,
        slippage_bp_p95=round(slip_p95, 2) if slip_p95 is not None else None,
        fill_rate_mean=round(fill_mean, 4) if fill_mean is not None else None,
        slo_slippage_passes=slo_slip,
        slo_fill_rate_passes=slo_fill,
        slo_all_pass=slo_all,
        reason=" · ".join(reason_parts),
    )


@dataclass
class PromotionUpgrade:
    """Output of upgrade_verdict(). Describes how the stricter SLO rule
    modifies the stats-only verdict from variant_trip_wire."""
    original_verdict: str       # from variant_trip_wire._standing_for()
    upgraded_verdict: str       # what the combined rule says
    original_reason: str
    upgraded_reason: str
    execution_metrics: ExecutionMetrics


def upgrade_verdict(original_verdict: str, original_reason: str,
                    variant: str) -> PromotionUpgrade:
    """Apply the execution SLO layer on top of the statistical verdict.

    If SPOT_EXEC_SLO_GATE is off: pass through unchanged (metrics still reported).
    If on:
        original=promote  + SLO pass -> promote_full
        original=promote  + SLO fail -> promote_statistical
        other verdicts -> unchanged
    """
    exec_m = metrics_for(variant)

    if not gate_enabled():
        return PromotionUpgrade(
            original_verdict=original_verdict,
            upgraded_verdict=original_verdict,
            original_reason=original_reason,
            upgraded_reason=original_reason + " (SLO gate off)",
            execution_metrics=exec_m,
        )

    if original_verdict != "promote":
        return PromotionUpgrade(
            original_verdict=original_verdict,
            upgraded_verdict=original_verdict,
            original_reason=original_reason,
            upgraded_reason=original_reason,
            execution_metrics=exec_m,
        )

    if exec_m.slo_all_pass:
        return PromotionUpgrade(
            original_verdict=original_verdict,
            upgraded_verdict="promote_full",
            original_reason=original_reason,
            upgraded_reason=f"{original_reason} · SLO GREEN ({exec_m.reason})",
            execution_metrics=exec_m,
        )
    return PromotionUpgrade(
        original_verdict=original_verdict,
        upgraded_verdict="promote_statistical",
        original_reason=original_reason,
        upgraded_reason=(
            f"{original_reason} · SLO FAIL ({exec_m.reason}) — "
            "operator approval required to promote_full"
        ),
        execution_metrics=exec_m,
    )
