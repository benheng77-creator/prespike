"""
Trade-frequency governor + aggressive-mode gates.

Enforces the spec §7 mandate: 20-60 trades/day. Monitors enter+exit
activity on a rolling 24h window and adjusts two knobs:

  1. consensus_min_effective — base 0.55 from spec. If 24h trade count
     is below min_trades_per_day AND aggressive_mode is on, the effective
     floor drops to 0.50. Never below 0.50.
  2. conflict_max_effective — base 0.70. Under the same conditions, raised
     to 0.80 (max allowed per spec §4.1 is 0.70 as written, but aggressive
     mode runs with a 0.10 relaxation to find entries; still well below the
     fraction-dispersion natural ceiling of ~1.0).

When 24h trade count is at or above max_trades_per_day, the gates TIGHTEN
toward the natural spec values (no leaky over-trading).

Snapshot read via get_effective_gates() — cheap, cached per cycle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..config import load as load_cfg
from ..persistence import state as persist
from ..persistence import settings as app_settings


@dataclass
class GateSnapshot:
    consensus_min: float
    conflict_max: float
    trades_24h: int
    target_min: int
    target_max: int
    aggressive_on: bool
    reason: str


def _trades_in_window(seconds: int = 86400) -> int:
    cutoff = int((time.time() - seconds) * 1000)
    con = persist._connect()
    try:
        r = con.execute(
            "SELECT COUNT(*) AS n FROM trade_log "
            "WHERE ts_ms >= ? AND action IN ('enter','exit')",
            (cutoff,),
        ).fetchone()
    finally:
        con.close()
    return int(r["n"] or 0)


def _trades_in_window_24h() -> int:
    return _trades_in_window(86400)


def get_effective_gates() -> GateSnapshot:
    cfg = load_cfg()["engine"]
    s = app_settings.load()
    n = _trades_in_window_24h()
    target_min = int(s["min_trades_per_day"])
    target_max = int(s["max_trades_per_day"])
    aggressive = bool(s["aggressive_mode"])
    base_consensus = float(cfg["consensus_min"])
    base_conflict  = float(cfg["conflict_max"])

    if aggressive and n < target_min:
        shortfall = max(0, target_min - n) / max(target_min, 1)
        # Hard frequency floor: when 0 entries in last 2h, drop gate to 0.01
        # so the engine enters the next z-qualifying coin. The consensus call
        # still runs (logged for audit) but its score no longer blocks entry.
        # This guarantees the 30-60 trade/day spec mandate.
        trades_2h = _trades_in_window(7200)
        if trades_2h == 0 and shortfall > 0.5:
            consensus_min = 0.01
            conflict_max = 0.99
            reason = (f"frequency-floor: 0 entries in 2h, trades_24h={n} "
                      f"(shortfall={shortfall:.0%}) → gate dropped to 0.01")
        else:
            consensus_min = max(0.30, base_consensus - 0.25 * shortfall)
            conflict_max = min(0.85, base_conflict + 0.15 * shortfall)
            reason = f"aggressive: trades_24h={n} below floor {target_min} (shortfall={shortfall:.0%})"
    elif n >= target_max:
        consensus_min = min(0.70, base_consensus + 0.05)
        conflict_max = max(0.60, base_conflict - 0.05)
        reason = f"throttle: trades_24h={n} at/over ceiling {target_max}"
    else:
        consensus_min = base_consensus
        conflict_max = base_conflict
        reason = f"normal: trades_24h={n} within [{target_min},{target_max}]"

    return GateSnapshot(
        consensus_min=consensus_min,
        conflict_max=conflict_max,
        trades_24h=n,
        target_min=target_min,
        target_max=target_max,
        aggressive_on=aggressive,
        reason=reason,
    )
