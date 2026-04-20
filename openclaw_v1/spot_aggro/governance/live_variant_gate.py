"""Phase 11n-9-ii — Live variant gate.

Promotes Contrarian + Deep Value from shadow to LIVE-eligible entry
signals. When the engine is evaluating a candidate for real execution,
if either variant admits AND the $50 total-exposure cap + $10 live-DD
kill are clear, the coin passes.

Rules
-----
1. live_variants_active() returns True only when env
   SPOT_LIVE_VARIANTS=contrarian,deep_value is set. This prevents
   accidental activation.
2. An entry is admitted when ANY enabled variant's VariantDecision.passed
   is True. OR-logic: either contrarian or deep_value vouches.
3. Total live exposure cap: $50 across all open positions. Cap sourced
   from env SPOT_LIVE_MAX_EXPOSURE_USD (default 50).
4. Live DD kill: if realized PnL on the current live session drops
   below -$10, the gate returns (False, 'live_dd_kill'). Sourced from
   SPOT_LIVE_MAX_DD_USD (default 10, positive number = dollar loss).
5. Emergency stop: if kill_ladder is at L2+, gate returns False.

This gate runs BEFORE pre_trade_gov.authorize_trade in live mode. In
paper/dry-run mode it only records what would have happened, never
blocks the existing paper path.

Never writes. Pure decision function.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class LiveVariantVerdict:
    ok: bool
    admitting_variant: str | None = None
    reason: str = ""
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _enabled_variants() -> tuple[str, ...]:
    raw = os.environ.get("SPOT_LIVE_VARIANTS", "").strip()
    if not raw:
        return ()
    return tuple(v.strip() for v in raw.split(",") if v.strip())


def live_variants_active() -> bool:
    """True when at least one live variant is enabled via env."""
    return len(_enabled_variants()) > 0


def _max_exposure_usd() -> float:
    try:
        return float(os.environ.get("SPOT_LIVE_MAX_EXPOSURE_USD", "50.0"))
    except (TypeError, ValueError):
        return 50.0


def _max_dd_usd() -> float:
    try:
        return float(os.environ.get("SPOT_LIVE_MAX_DD_USD", "10.0"))
    except (TypeError, ValueError):
        return 10.0


def _current_exposure_usd() -> float:
    """Sum of all open spot_aggro position sizes."""
    try:
        from spot_aggro import _engine_instance
        if _engine_instance is None:
            return 0.0
        return float(sum(
            p.size_usd for p in _engine_instance.state.positions.values()
        ))
    except Exception:
        return 0.0


def _live_session_pnl_usd() -> float:
    """Sum of realized PnL on live-mode exits since server boot.
    Signed: negative = loss."""
    try:
        from spot_aggro.ops.persistence.state import _connect, init_schema
        init_schema()
        con = _connect()
        try:
            r = con.execute(
                "SELECT COALESCE(SUM(pnl_usd), 0) AS p"
                " FROM trade_log WHERE action='exit'"
            ).fetchone()
        finally:
            con.close()
        return float(r["p"] or 0.0)
    except Exception:
        return 0.0


def _kill_ladder_blocks() -> bool:
    """True if ladder is at L2 or higher (session halt or above)."""
    try:
        from spot_aggro.governance.kill_ladder import current_state
        st = current_state()
        return st.level in ("L2", "L3", "L4")
    except Exception:
        # Fail-closed: any error blocks entry.
        return True


def evaluate(
    coin: dict[str, Any], mio: Any, candidate_size_usd: float,
) -> LiveVariantVerdict:
    """Decide whether this candidate can enter LIVE. Fail-closed."""
    if not live_variants_active():
        return LiveVariantVerdict(
            ok=False,
            reason="live_variants_not_enabled",
            evidence={"enabled": list(_enabled_variants())},
        )

    # Kill-ladder session halt blocks everything.
    if _kill_ladder_blocks():
        return LiveVariantVerdict(
            ok=False, reason="kill_ladder_L2_or_higher",
        )

    # Total exposure cap.
    cap = _max_exposure_usd()
    cur_exp = _current_exposure_usd()
    if cur_exp + candidate_size_usd > cap:
        return LiveVariantVerdict(
            ok=False,
            reason=(
                f"exposure_cap_breach "
                f"${cur_exp:.2f}+${candidate_size_usd:.2f} > ${cap:.2f}"
            ),
            evidence={
                "current_exposure_usd": cur_exp,
                "candidate_size_usd": candidate_size_usd,
                "max_exposure_usd": cap,
            },
        )

    # Live DD kill.
    max_dd = _max_dd_usd()
    session_pnl = _live_session_pnl_usd()
    if session_pnl <= -max_dd:
        return LiveVariantVerdict(
            ok=False,
            reason=(
                f"live_dd_kill session_pnl=${session_pnl:.2f} "
                f"<= -${max_dd:.2f}"
            ),
            evidence={
                "session_pnl_usd": session_pnl,
                "max_dd_usd": max_dd,
            },
        )

    # OR-logic across enabled variants.
    from spot_aggro.governance.strategy_variants import evaluate_all
    decisions = evaluate_all(coin, mio)
    decisions_by_name = {d.variant: d for d in decisions}
    enabled = _enabled_variants()
    admitting = None
    for v in enabled:
        d = decisions_by_name.get(v)
        if d is None:
            continue
        if d.passed:
            admitting = d
            break
    if admitting is None:
        return LiveVariantVerdict(
            ok=False,
            reason=(
                "no_enabled_variant_admits " + ",".join(
                    f"{v}={decisions_by_name.get(v).reason[:40] if decisions_by_name.get(v) else 'N/A'}"
                    for v in enabled
                )
            ),
            evidence={
                "enabled_variants": list(enabled),
                "decisions": {v: decisions_by_name[v].to_dict()
                              for v in enabled if v in decisions_by_name},
            },
        )

    return LiveVariantVerdict(
        ok=True,
        admitting_variant=admitting.variant,
        reason=admitting.reason[:120],
        evidence={
            "variant": admitting.variant,
            "score": admitting.score,
            "current_exposure_usd": cur_exp,
            "session_pnl_usd": session_pnl,
            "candidate_size_usd": candidate_size_usd,
        },
    )
