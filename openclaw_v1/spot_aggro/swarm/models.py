"""
Swarm output data models — spot_aggro only.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CoinIntel:
    """Per-coin output from one swarm cycle."""
    symbol: str
    rank: int = 0
    tradable_state: str = "WATCH"       # BUY / WATCH / SKIP / AVOID
    buy_confidence: float = 0.50
    # Per-agent scores (0.0-1.0)
    structure_score: float = 0.50
    quant_score: float = 0.50
    liquidity_score: float = 0.50
    regime_score: float = 0.50
    # Adjudicator output
    final_action: str = "WATCH"         # STRONG_BUY / BUY / WATCH / SKIP / AVOID
    final_reason: str = ""
    sizing_hint: float = 1.0            # 0.5-1.5 multiplier
    exit_urgency: float = 0.0           # 0.0-1.0 (0=hold, 1=exit now)
    false_positive_risk: float = 0.50   # 0.0-1.0
    # Meta
    timestamp: float = 0.0
    layer: str = "standard"             # heavy / standard / fast
    cost_usd: float = 0.0
    agents_ok: int = 0
    agents_total: int = 5

    def age_s(self) -> float:
        return time.time() - self.timestamp if self.timestamp else 999999

    def is_fresh(self, max_age_s: float = 900) -> bool:
        """True if data is less than max_age_s old."""
        return self.age_s() < max_age_s

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "rank": self.rank,
            "tradable_state": self.tradable_state,
            "buy_confidence": round(self.buy_confidence, 3),
            "structure_score": round(self.structure_score, 3),
            "quant_score": round(self.quant_score, 3),
            "liquidity_score": round(self.liquidity_score, 3),
            "regime_score": round(self.regime_score, 3),
            "final_action": self.final_action,
            "final_reason": self.final_reason[:120],
            "sizing_hint": round(self.sizing_hint, 2),
            "exit_urgency": round(self.exit_urgency, 2),
            "false_positive_risk": round(self.false_positive_risk, 2),
            "layer": self.layer,
            "age_s": round(self.age_s()),
            "agents_ok": self.agents_ok,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class SwarmThreadHealth:
    """Observability for the swarm daemon thread.

    Distinguishes warming_up / running / stalled / erroring / crashed so the
    dashboard can show the operator what is actually happening instead of
    collapsing everything to 'sleep'.
    """
    alive: bool = False                    # thread.is_alive() snapshot
    started_at: float = 0.0                 # ts when daemon entered _loop
    last_heartbeat_ts: float = 0.0          # updated every outer-loop iteration
    last_cycle_ts: float = 0.0              # last successful cycle completion (any layer)
    last_error: Optional[str] = None
    last_error_ts: float = 0.0
    consecutive_errors: int = 0

    # Tunables (set by runner at start-up based on config)
    warmup_s: float = 5.0                   # how long "warming_up" is tolerated
    stall_after_s: float = 60.0             # no heartbeat for this long → stalled

    def status(self) -> str:
        now = time.time()
        if not self.alive:
            # Thread never started OR died. started_at distinguishes the two.
            return "crashed" if self.started_at > 0 else "not_started"
        if self.consecutive_errors >= 3:
            return "erroring"
        hb_age = now - self.last_heartbeat_ts if self.last_heartbeat_ts else 1e9
        if hb_age > self.stall_after_s:
            return "stalled"
        if self.last_cycle_ts == 0.0 and (now - self.started_at) < self.warmup_s:
            return "warming_up"
        return "running"

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        return {
            "status": self.status(),
            "alive": self.alive,
            "started_at": self.started_at,
            "uptime_s": round(now - self.started_at, 1) if self.started_at else 0,
            "last_heartbeat_ago_s": round(now - self.last_heartbeat_ts, 1)
                if self.last_heartbeat_ts else None,
            "last_cycle_ago_s": round(now - self.last_cycle_ts, 1)
                if self.last_cycle_ts else None,
            "last_error": self.last_error,
            "last_error_ago_s": round(now - self.last_error_ts, 1)
                if self.last_error_ts else None,
            "consecutive_errors": self.consecutive_errors,
        }


@dataclass
class SwarmState:
    """Aggregate swarm state for the entire universe."""
    coin_intels: Dict[str, CoinIntel] = field(default_factory=dict)
    fast_watchlist: List[str] = field(default_factory=list)
    last_heavy_ts: float = 0.0
    last_standard_ts: float = 0.0
    last_fast_ts: float = 0.0
    cycle_counts: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    total_cost_usd: float = 0.0
    enabled: bool = True
    health: SwarmThreadHealth = field(default_factory=SwarmThreadHealth)

    def get_intel(self, symbol: str) -> Optional[CoinIntel]:
        return self.coin_intels.get(symbol)

    def state_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "health": self.health.to_dict(),
            "cycle_counts": dict(self.cycle_counts),
            "fast_watchlist": self.fast_watchlist,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "last_heavy_ago": round(time.time() - self.last_heavy_ts) if self.last_heavy_ts else None,
            "last_standard_ago": round(time.time() - self.last_standard_ts) if self.last_standard_ts else None,
            "last_fast_ago": round(time.time() - self.last_fast_ts) if self.last_fast_ts else None,
            "coins": {
                sym: ci.to_dict() for sym, ci in self.coin_intels.items()
            },
        }
