"""Phase 11n-9-ff — Execution Integrity pre-trade checks.

Three hard gates between the engine deciding to buy and the order
going out. All are fail-closed: any error returns reject.

  1. per_trade_risk_ok     — notional <= 2.0% × current equity
  2. price_tolerance_ok    — |requested - reference| / reference <= 15 bp
  3. ladder_allows_entry   — kill_ladder.is_entry_blocked() == False

Wired into engine.py M1 + BLITZ paths BEFORE authorize_trade(). Any
reject gets recorded via kill_ladder.record_reject() so the auto-pause
storm detector can escalate on repeated failures.

2% is the default; override with env `SPOT_RISK_PER_TRADE_PCT` (read at
check time so changes apply without restart).
15bp is the default; override with `SPOT_PRICE_TOLERANCE_BP`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_RISK_PCT = 0.02
DEFAULT_PRICE_TOL_BP = 15.0


@dataclass
class IntegrityVerdict:
    ok: bool
    reason: str = ""
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def risk_pct() -> float:
    try:
        v = float(os.environ.get("SPOT_RISK_PER_TRADE_PCT", DEFAULT_RISK_PCT))
        # Safety clamp: 0.05% .. 10%.
        return max(0.0005, min(v, 0.10))
    except (TypeError, ValueError):
        return DEFAULT_RISK_PCT


def price_tol_bp() -> float:
    try:
        v = float(os.environ.get("SPOT_PRICE_TOLERANCE_BP", DEFAULT_PRICE_TOL_BP))
        return max(1.0, min(v, 500.0))
    except (TypeError, ValueError):
        return DEFAULT_PRICE_TOL_BP


def check_per_trade_risk(
    notional_usd: float, equity_usd: float,
) -> IntegrityVerdict:
    """Reject if the order is larger than `risk_pct` of current equity.
    Fail-closed: equity<=0 or notional<=0 both reject."""
    try:
        n = float(notional_usd)
        e = float(equity_usd)
    except (TypeError, ValueError):
        return IntegrityVerdict(
            ok=False, reason="non_numeric_inputs",
            evidence={"notional": notional_usd, "equity": equity_usd},
        )
    if e <= 0:
        return IntegrityVerdict(
            ok=False, reason="non_positive_equity",
            evidence={"equity_usd": e},
        )
    if n <= 0:
        return IntegrityVerdict(
            ok=False, reason="non_positive_notional",
            evidence={"notional_usd": n},
        )
    cap = e * risk_pct()
    if n > cap:
        return IntegrityVerdict(
            ok=False,
            reason=f"per_trade_risk_breach ${n:.2f} > cap ${cap:.2f}",
            evidence={
                "notional_usd": n, "equity_usd": e,
                "risk_pct": risk_pct(), "cap_usd": cap,
            },
        )
    return IntegrityVerdict(
        ok=True,
        reason=f"within_cap ${n:.2f} <= ${cap:.2f}",
        evidence={"notional_usd": n, "cap_usd": cap},
    )


def check_price_tolerance(
    requested_px: float, reference_px: float,
) -> IntegrityVerdict:
    """Reject if |requested - reference| / reference > tolerance in bp.
    Fail-closed: reference<=0 or requested<=0 both reject."""
    try:
        q = float(requested_px)
        r = float(reference_px)
    except (TypeError, ValueError):
        return IntegrityVerdict(
            ok=False, reason="non_numeric_inputs",
            evidence={"requested": requested_px, "reference": reference_px},
        )
    if r <= 0 or q <= 0:
        return IntegrityVerdict(
            ok=False, reason="non_positive_price",
            evidence={"requested": q, "reference": r},
        )
    tol = price_tol_bp()
    drift_bp = abs(q - r) / r * 10_000.0
    if drift_bp > tol:
        return IntegrityVerdict(
            ok=False,
            reason=f"price_drift {drift_bp:.1f}bp > tolerance {tol:.1f}bp",
            evidence={
                "requested": q, "reference": r,
                "drift_bp": drift_bp, "tolerance_bp": tol,
            },
        )
    return IntegrityVerdict(
        ok=True,
        reason=f"within_tolerance {drift_bp:.1f}bp <= {tol:.1f}bp",
        evidence={"drift_bp": drift_bp, "tolerance_bp": tol},
    )


def check_ladder_allows_entry() -> IntegrityVerdict:
    """Reject if the kill-ladder is at L1 or higher. Fail-closed."""
    try:
        from spot_aggro.governance.kill_ladder import current_state
        st = current_state()
        if st.level != "L0":
            return IntegrityVerdict(
                ok=False,
                reason=f"kill_ladder_at_{st.level}:{st.reason}",
                evidence={"level": st.level, "ladder_reason": st.reason},
            )
        return IntegrityVerdict(ok=True, reason="ladder_L0", evidence={})
    except Exception as e:
        return IntegrityVerdict(
            ok=False, reason=f"ladder_error:{str(e)[:80]}",
            evidence={"error": str(e)[:160]},
        )


def check_all(
    *, notional_usd: float, equity_usd: float,
    requested_px: float, reference_px: float,
    symbol: str | None = None,
) -> IntegrityVerdict:
    """Single entrypoint for engine code. Records a reject event if any
    gate fails so the kill_ladder auto-pause detector can see it."""
    for fn, args in (
        (check_ladder_allows_entry, ()),
        (check_per_trade_risk, (notional_usd, equity_usd)),
        (check_price_tolerance, (requested_px, reference_px)),
    ):
        v = fn(*args)
        if not v.ok:
            try:
                from spot_aggro.governance.kill_ladder import record_reject
                record_reject(
                    kind="reject",
                    symbol=symbol,
                    detail={
                        "reason": v.reason,
                        "evidence": v.evidence or {},
                        "gate": fn.__name__,
                    },
                )
            except Exception:
                pass
            return v
    return IntegrityVerdict(ok=True, reason="all_gates_passed", evidence={})
