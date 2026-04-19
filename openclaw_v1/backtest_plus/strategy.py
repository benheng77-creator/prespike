"""
Strategy adapter — pluggable signal source for the backtest engine.

`StrategyAdapter` is the contract:
    decide(state) -> Decision

`SimpleStrategy` is a deterministic momentum + mean-reversion shim that
makes the harness self-contained. Real model adapters (binary15m,
binary15, apex_v2, 99-X-Apex) plug into the same contract — see
`bind_existing_model()` for the integration pattern.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class BarState:
    closes: list[float]            # rolling history up to current bar (inclusive)
    highs: list[float]
    lows: list[float]
    bar_idx: int
    ts: int


@dataclass
class Decision:
    side: str                      # "LONG" | "SHORT" | "FLAT"
    size_frac: float               # fraction of capital (0..1) to allocate
    stop_pct: float                # stop distance as fraction of entry (0..1)
    take_pct: float                # take-profit distance fraction (0..1)
    confidence: float = 0.0        # informational, [0,1]


class StrategyAdapter(Protocol):
    def warmup_bars(self) -> int: ...
    def decide(self, state: BarState) -> Decision: ...


@dataclass
class SimpleStrategy:
    """Momentum + mean-reversion blend.

    Signal:
        z = (close - sma(N)) / std(N)      # mean-reversion signal
        m = (sma(short) - sma(long)) / sma(long)   # momentum signal
        score = w_mom * sign(m) - w_mr * z
    Long if score > entry_thresh, short if < -entry_thresh.
    Fixed risk per trade via stop_atr_mult and take_atr_mult on bar-range proxy.
    """
    short_window: int = 12
    long_window: int = 48
    z_window: int = 48
    entry_thresh: float = 0.6
    risk_per_trade: float = 0.01       # 1% of capital risked per trade
    stop_atr_mult: float = 2.0
    take_atr_mult: float = 3.0
    w_mom: float = 0.7
    w_mr: float = 0.3

    def warmup_bars(self) -> int:
        return max(self.long_window, self.z_window) + 4

    def decide(self, state: BarState) -> Decision:
        n = len(state.closes)
        if n < self.warmup_bars():
            return Decision("FLAT", 0.0, 0.0, 0.0, 0.0)
        c = state.closes
        sma_short = sum(c[-self.short_window:]) / self.short_window
        sma_long = sum(c[-self.long_window:]) / self.long_window
        m = (sma_short - sma_long) / max(abs(sma_long), 1e-9)
        zsl = c[-self.z_window:]
        mu = sum(zsl) / len(zsl)
        var = sum((x - mu) ** 2 for x in zsl) / len(zsl)
        sd = math.sqrt(var) if var > 0 else 1e-9
        z = (c[-1] - mu) / sd
        score = self.w_mom * _bounded_sign(m, 0.001) - self.w_mr * max(min(z, 3.0), -3.0) / 3.0
        # Bar-range ATR proxy (last 14)
        rng = state.highs[-14:]
        low = state.lows[-14:]
        atr_proxy = sum(h - l for h, l in zip(rng, low)) / max(len(rng), 1)
        atr_pct = atr_proxy / max(c[-1], 1e-9)
        stop_pct = max(0.002, self.stop_atr_mult * atr_pct)
        take_pct = max(stop_pct * 1.5, self.take_atr_mult * atr_pct)
        size_frac = self.risk_per_trade / max(stop_pct, 1e-6)
        size_frac = max(0.0, min(size_frac, 1.0))
        confidence = min(1.0, abs(score))
        if score > self.entry_thresh:
            return Decision("LONG", size_frac, stop_pct, take_pct, confidence)
        if score < -self.entry_thresh:
            return Decision("SHORT", size_frac, stop_pct, take_pct, confidence)
        return Decision("FLAT", 0.0, 0.0, 0.0, confidence)


def _bounded_sign(x: float, eps: float) -> float:
    if x > eps:
        return 1.0
    if x < -eps:
        return -1.0
    return x / eps


# ---------------------------------------------------------------------------
# Strategy profiles — selectable presets of the same SimpleStrategy engine.
# Exposed to the UI so non-technical users can pick a trading style without
# tuning knobs.
# ---------------------------------------------------------------------------

STRATEGY_PROFILES: dict[str, dict] = {
    "balanced": {
        "label": "Balanced (recommended)",
        "description": "Blends momentum and mean-reversion. Moderate risk per trade.",
        "params": {
            "risk_per_trade": 0.01, "stop_atr_mult": 2.0, "take_atr_mult": 3.0,
            "w_mom": 0.7, "w_mr": 0.3, "entry_thresh": 0.6,
        },
    },
    "conservative": {
        "label": "Conservative",
        "description": "Smaller size, wider stops, fewer trades. Protects capital first.",
        "params": {
            "risk_per_trade": 0.005, "stop_atr_mult": 3.0, "take_atr_mult": 4.5,
            "w_mom": 0.7, "w_mr": 0.3, "entry_thresh": 0.8,
        },
    },
    "aggressive": {
        "label": "Aggressive",
        "description": "Larger size, tighter stops, more trades. Higher upside + drawdown.",
        "params": {
            "risk_per_trade": 0.02, "stop_atr_mult": 1.5, "take_atr_mult": 2.5,
            "w_mom": 0.8, "w_mr": 0.2, "entry_thresh": 0.4,
        },
    },
    "momentum": {
        "label": "Momentum-only",
        "description": "Rides trends. Best in bull/bear markets, worst in chop.",
        "params": {
            "risk_per_trade": 0.01, "stop_atr_mult": 2.0, "take_atr_mult": 3.5,
            "w_mom": 1.0, "w_mr": 0.0, "entry_thresh": 0.55,
        },
    },
    "mean_reversion": {
        "label": "Mean-reversion",
        "description": "Fades extremes. Best in sideways chop, worst in strong trends.",
        "params": {
            "risk_per_trade": 0.01, "stop_atr_mult": 2.0, "take_atr_mult": 2.5,
            "w_mom": 0.0, "w_mr": 1.0, "entry_thresh": 0.55,
        },
    },
}


def build_strategy(profile: str = "balanced") -> SimpleStrategy:
    """Return a SimpleStrategy configured for the named profile."""
    spec = STRATEGY_PROFILES.get(profile) or STRATEGY_PROFILES["balanced"]
    return SimpleStrategy(**spec["params"])


def list_strategy_profiles() -> list[dict]:
    """UI-friendly list of available strategy profiles."""
    return [
        {"id": k, "label": v["label"], "description": v["description"]}
        for k, v in STRATEGY_PROFILES.items()
    ]
