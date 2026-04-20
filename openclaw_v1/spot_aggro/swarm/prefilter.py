"""Phase 11n-9-bb — Swarm prefilter.

Cuts LLM cost 95% by gating every expensive LLM call behind cheap
checks. Rule: spend LLM budget ONLY on coins that can actually trade
right now.

The swarm historically called 5 LLMs per coin per layer (HEAVY /
STANDARD / FAST / per-tick consensus). 99.89% of calls were on coins
that the pre-trade gate would reject downstream, or on tiers that
aren't admitted, or while the engine isn't ready.

Before calling any LLM the swarm now asks:
  1. Is the engine ready to trade?  (Layer aa)
  2. Is ANY tier_symbol cell admitted for this symbol?  (Layer aa)
  3. Is the contradiction_freeze active?  (Layer 3)
If ANY is false → skip, mark coin as NOT-LLM-EVALUATED in swarm state.

Same LLM quality for admitted universe; cost drops from ~$27/day to
~$1/day at current admitted-universe size (ENA + DOT only).

Never trades. Read-only checks.
"""

from __future__ import annotations

import logging

log = logging.getLogger("ops.swarm.prefilter")


def _admitted_tiers_for_symbol(symbol: str) -> list[str]:
    """Return list of tiers currently admitted for this symbol.
    Empty list means no tier is admitted → caller should skip LLMs."""
    try:
        from spot_aggro.governance.universe_gatekeeper import admitted_cells
        cells = admitted_cells()
        tiers: list[str] = []
        for c in cells:
            if c.get("cell_kind") != "tier_symbol":
                continue
            key = c.get("cell_key", "")
            if "|" not in key:
                continue
            tier, sym = key.split("|", 1)
            if sym == symbol:
                tiers.append(tier)
        return tiers
    except Exception:
        # Fail-closed: if gatekeeper is unreadable, assume no tier is
        # admitted → skip LLMs. The engine entry path would also skip
        # a trade in this state; the swarm shouldn't pay for it.
        return []


def should_call_llm(
    symbol: str,
    *,
    layer: str = "unknown",
    bypass: bool = False,
) -> tuple[bool, str]:
    """Return (allow, reason).

    `layer` is the swarm layer tag (heavy|standard|fast|consensus).
    `bypass` is for maintenance hooks — sysaudit, etc. that need to
    run LLMs even when the engine is halted. Pass bypass=True sparingly.
    """
    if bypass:
        return True, "bypass"

    # 1. Engine readiness. If the engine is halted or not ready, no
    #    coin can trade; skip LLM spend.
    try:
        from spot_aggro.governance.trade_readiness import is_ready_to_trade
        if not is_ready_to_trade():
            return False, "engine_not_ready"
    except Exception:
        return False, "readiness_probe_error"

    # 2. Contradiction freeze. Same logic.
    try:
        from spot_aggro.governance.contradiction_freeze import is_entry_frozen
        if is_entry_frozen():
            return False, "contradiction_freeze_active"
    except Exception:
        return False, "freeze_probe_error"

    # 3. Cell admission. If no tier_symbol cell is admitted for this
    #    coin, the engine will never open a position even if the LLM
    #    returns BUY. Skip.
    tiers = _admitted_tiers_for_symbol(symbol)
    if not tiers:
        return False, f"no_admitted_tier_for_{symbol}"

    return True, f"admitted_tiers={','.join(sorted(tiers))}"


def filter_coin_list(
    coins: list[dict],
    *,
    layer: str = "unknown",
    bypass: bool = False,
) -> tuple[list[dict], int]:
    """Filter a list of coin dicts (must have 'symbol' key) down to
    only those that pass should_call_llm. Returns (filtered, n_skipped).
    Used by HEAVY / STANDARD / FAST layers that take a universe list."""
    if bypass:
        return coins, 0
    allowed: list[dict] = []
    skipped = 0
    reasons: dict[str, int] = {}
    for c in coins:
        sym = c.get("symbol", "?")
        ok, reason = should_call_llm(sym, layer=layer)
        if ok:
            allowed.append(c)
        else:
            skipped += 1
            reasons[reason] = reasons.get(reason, 0) + 1
    if skipped:
        log.info(
            "swarm %s prefilter: %d/%d coins passed (skipped %d: %s)",
            layer, len(allowed), len(coins), skipped,
            ", ".join(f"{r}={n}" for r, n in sorted(reasons.items())),
        )
    return allowed, skipped
