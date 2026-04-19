"""
Swarm → Engine integration functions.

Pure functions that translate SwarmState/CoinIntel into engine-consumable
adjustments. Keeps engine.py modifications minimal.

SPOT_AGGRO ONLY. No apex_omega contamination.
"""

from __future__ import annotations

from typing import Optional

from .runner import get_coin_intel, get_swarm_state
from .models import CoinIntel

# How much swarm buy_confidence blends into composite score
COMPOSITE_BLEND_WEIGHT = 0.30

# Maximum intel age before we ignore it (seconds)
MAX_INTEL_AGE_S = 900  # 15 min


def swarm_composite_adjustment(symbol: str, base_composite: float) -> float:
    """Blend swarm buy_confidence into composite score.

    Returns adjusted composite.
    If no fresh swarm data, returns base_composite unchanged.
    """
    ci = get_coin_intel(symbol)
    if ci is None or not ci.is_fresh(MAX_INTEL_AGE_S):
        return base_composite

    w = COMPOSITE_BLEND_WEIGHT
    adjusted = base_composite * (1.0 - w) + ci.buy_confidence * w
    return round(max(0.0, min(1.0, adjusted)), 4)


def swarm_entry_gate(symbol: str) -> tuple[bool, str]:
    """Check if swarm allows entry for this symbol.

    Returns (allowed, reason).
    If no swarm data, allows entry (fail-open).
    """
    ci = get_coin_intel(symbol)
    if ci is None or not ci.is_fresh(MAX_INTEL_AGE_S):
        return True, "no_swarm_data"

    if ci.final_action == "AVOID":
        return False, f"swarm_AVOID: {ci.final_reason[:60]}"

    if ci.tradable_state == "AVOID":
        return False, f"swarm_state_AVOID: {ci.final_reason[:60]}"

    # SKIP with high confidence = soft block
    if ci.final_action == "SKIP" and ci.buy_confidence < 0.20:
        return False, f"swarm_SKIP_low_conf: {ci.buy_confidence:.2f}"

    return True, "swarm_ok"


def swarm_sizing_multiplier(symbol: str) -> float:
    """Returns sizing_hint from latest CoinIntel (default 1.0)."""
    ci = get_coin_intel(symbol)
    if ci is None or not ci.is_fresh(MAX_INTEL_AGE_S):
        return 1.0
    return ci.sizing_hint


def swarm_exit_urgency(symbol: str) -> float:
    """Returns exit_urgency from latest CoinIntel (default 0.0)."""
    ci = get_coin_intel(symbol)
    if ci is None or not ci.is_fresh(MAX_INTEL_AGE_S):
        return 0.0
    return ci.exit_urgency


def swarm_tier_override(symbol: str, base_tier: str) -> str:
    """Can promote/demote tier based on swarm signals.

    - STRONG_BUY: promote B→A or C→B
    - AVOID: demote any tier to skip (returns "SKIP")
    """
    ci = get_coin_intel(symbol)
    if ci is None or not ci.is_fresh(MAX_INTEL_AGE_S):
        return base_tier

    if ci.final_action == "STRONG_BUY":
        promotion = {"C": "B", "B": "A"}
        return promotion.get(base_tier, base_tier)

    if ci.final_action == "AVOID":
        return "SKIP"

    return base_tier


def swarm_summary_for_status() -> dict:
    """Compact summary for engine status() endpoint.

    Includes the thread-health block so the dashboard can distinguish
    warming_up / running / stalled / erroring / crashed / not_started
    without calling a separate endpoint.
    """
    ss = get_swarm_state()
    health = ss.health.to_dict()
    base = {
        "active": ss.enabled,
        "health": health,
        "cycles": dict(ss.cycle_counts),
        "cost_usd": round(ss.total_cost_usd, 4),
        "fast_watchlist": ss.fast_watchlist,
    }
    if not ss.coin_intels:
        # Warming_up is now driven by health.status(); keep the legacy key
        # for any UI still reading it during rollout.
        base["warming_up"] = health["status"] == "warming_up"
        base["verdicts"] = {}
        return base

    base["verdicts"] = {
        sym: {
            "action": ci.final_action,
            "confidence": round(ci.buy_confidence, 2),
            "sizing": round(ci.sizing_hint, 2),
            "exit_urg": round(ci.exit_urgency, 2),
            "age_s": round(ci.age_s()),
            "layer": ci.layer,
        }
        for sym, ci in ss.coin_intels.items()
    }
    return base
