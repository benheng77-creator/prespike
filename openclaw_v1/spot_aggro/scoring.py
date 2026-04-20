"""
SPOT AGGRO v2 — Composite Scoring, Tier Classification, Frequency Controller.

Replaces hard SPI/fz/depth gates with an 8-factor composite score.
Coins are classified into tiers A+/A/B/C with per-tier consensus depth,
sizing caps, TP/SL multipliers, and max hold times.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Tier parameters — exact per-tier rules
# ---------------------------------------------------------------------------

@dataclass
class TierConfig:
    tier: str
    consensus_depth: int     # how many LLMs to call
    consensus_min: float     # minimum posterior to pass
    conflict_max: float      # maximum conflict to pass
    veto_enabled: bool       # Opus veto?
    max_size_frac: float     # max fraction of capital
    tp_mult: float           # TP multiplier on SPI-based TP
    sl_mult: float           # SL multiplier on SPI-based SL
    trail_activate: float    # trail activates at this fraction of TP
    trail_pct: float         # trail at this fraction of peak
    max_hold_h: float        # time-stop hours
    invalidation: float      # exit if score drops below this
    module: str              # module name for logging
    cooldown_s: int          # consensus cooldown after rejection


TIER_PARAMS: dict[str, TierConfig] = {
    "A+": TierConfig(
        tier="A+", consensus_depth=5, consensus_min=0.35, conflict_max=0.80,
        veto_enabled=True, max_size_frac=0.40, tp_mult=1.30, sl_mult=1.00,
        trail_activate=0.60, trail_pct=0.60, max_hold_h=1.0,
        invalidation=0.20, module="M3_blitz", cooldown_s=180,
    ),
    "A": TierConfig(
        tier="A", consensus_depth=5, consensus_min=0.35, conflict_max=0.80,
        veto_enabled=True, max_size_frac=0.20, tp_mult=1.10, sl_mult=1.00,
        trail_activate=0.60, trail_pct=0.60, max_hold_h=4.0,
        invalidation=0.20, module="M1_squeeze_A", cooldown_s=120,
    ),
    # Phase 11n-9-mm — TP/SL recalibrated for 1.5-2% per-trade band target.
    # Previous tp_mult values (0.70-1.30) capped realized wins at ~0.4-1%
    # which made the 1.5% floor structurally unreachable. New multipliers
    # target ~2.5-3.0% TP at typical SPI, supporting wins in 1.5-2% band
    # after slippage + fees. SL widens proportionally to preserve R/R ~1.8x.
    # max_hold lengthened — mean-reversion bounces don't complete in 45 min.
    "B": TierConfig(
        tier="B", consensus_depth=3, consensus_min=0.25, conflict_max=0.85,
        veto_enabled=False, max_size_frac=0.12, tp_mult=1.20, sl_mult=1.20,
        trail_activate=0.50, trail_pct=0.55, max_hold_h=6.0,
        invalidation=0.30, module="M1_flow_B", cooldown_s=45,
    ),
    "C": TierConfig(
        tier="C", consensus_depth=1, consensus_min=0.20, conflict_max=1.00,
        veto_enabled=False, max_size_frac=0.08, tp_mult=1.30, sl_mult=1.40,
        trail_activate=0.50, trail_pct=0.50, max_hold_h=4.0,
        invalidation=0.20, module="M1_scalp_C", cooldown_s=30,
    ),
}

# Tier score thresholds (base — adjusted by frequency controller)
TIER_A_PLUS_MIN = 0.65   # also requires SPI >= 0.85
TIER_A_MIN = 0.60
TIER_B_MIN = 0.40
TIER_C_MIN = 0.25

# Per-tier daily caps — target 40-70 trades/day
TIER_DAILY_CAPS = {"A+": 5, "A": 10, "B": 25, "C": 40}
GLOBAL_DAILY_CAP = 70


# ---------------------------------------------------------------------------
# Composite score — 8 normalized [0,1] components
# ---------------------------------------------------------------------------

SQUEEZE_MAP = {"IMMINENT": 1.0, "NEAR": 0.7, "FAR": 0.3, "NONE": 0.0}
REGIME_BONUS = {
    "SQUEEZE_BUILDING": 0.25, "CRISIS": 0.30, "TRENDING_UP": 0.05,
}
REGIME_SCORE = {
    "SQUEEZE_BUILDING": 0.90, "CRISIS": 0.80, "TRENDING_UP": 0.60,
    "RECOVERING": 0.50, "RANGE_BOUND": 0.30, "POST_SQUEEZE": 0.15,
    "TRENDING_DOWN": 0.10, "DEAD": 0.00, "UNKNOWN": 0.30,
}


def compute_composite_score(coin: dict[str, Any], mio: Any) -> float:
    """
    8-factor composite score for spot_aggro. Returns float in [0, 1].
    All components independently normalized, then weighted.
    v2.1: fz penalty for positive funding, directional momentum,
          MIO cold-start fix, spread deduplication.
    """
    # 1. SPI structure (existing formula output, already 0–1)
    spi = float(coin.get("spi", 0))                                    # w=0.20

    # 2. Funding quality — negative fz = squeeze pressure (boost)
    #    Positive fz = anti-squeeze (penalty). Not neutral.
    fz = float(coin.get("funding_z", 0))
    if fz <= 0:
        fz_score = min(-fz / 3.0, 1.0)                                # 0→1 for fz 0→-3
    else:
        fz_score = max(-0.15 * min(fz / 2.0, 1.0), -0.15)            # 0→-0.15 for fz 0→+2
    #                                                                    w=0.15

    # 3. Squeeze state — from MIO regime + squeeze timing
    #    Cold-start fix: if MIO never fired, assume moderate (not dead)
    mio_ts = getattr(mio, "timestamp", 0)
    mio_regime = getattr(mio, "regime", "UNKNOWN")
    if mio_regime == "UNKNOWN" and mio_ts == 0:
        # MIO hasn't fired yet — use neutral-positive defaults
        squeeze = 0.30
    else:
        sq_base = SQUEEZE_MAP.get(getattr(mio, "squeeze_timing_window", "NONE"), 0.0)
        sq_bonus = REGIME_BONUS.get(mio_regime, 0.0)
        squeeze = min(sq_base + sq_bonus, 1.0)
    #                                                                    w=0.15

    # 4. Momentum — directional: negative fz trend = squeeze building
    #    NOT spread (spread is microstructure, was double-counted)
    if fz <= 0:
        momentum = min(abs(fz) / 2.0, 1.0)                            # 0→1 for fz 0→-2
    else:
        momentum = 0.0                                                 # no squeeze momentum
    #                                                                    w=0.10

    # 5. Liquidity — log-scale spot depth
    depth = max(float(coin.get("depth_usd", 0)), 1.0)
    liq = min(max(math.log(depth / 500.0) / math.log(100.0), 0.0), 1.0)
    #                                                                    w=0.15

    # 6. Volatility — funding sigma as opportunity proxy
    sigma = float(coin.get("sigma_30d", 0))
    vol = min(sigma * 10000 / 3.0, 1.0)                               # w=0.10

    # 7. Regime — from MIO (cold-start: neutral 0.50 instead of 0.30)
    if mio_regime == "UNKNOWN" and mio_ts == 0:
        regime = 0.50
    else:
        regime = REGIME_SCORE.get(mio_regime, 0.30)
    #                                                                    w=0.05

    # 8. Microstructure — spread quality (sole spread component now)
    spread = float(coin.get("spread_bp", 10))
    micro = min(max(1.0 - spread / 15.0, 0.0), 1.0)                   # w=0.10

    S = (0.20 * spi
       + 0.15 * fz_score
       + 0.15 * squeeze
       + 0.10 * momentum
       + 0.15 * liq
       + 0.10 * vol
       + 0.05 * regime
       + 0.10 * micro)

    # Phase 11n-9-h: historical-WR quality multiplier.
    # Rationale: the engine was routing buys to coins with strong composite
    # but terrible historical WR (SEI 8%, WIF 25%, FLOKI 31%). Every such
    # buy got blocked by the Layer-8 pre-trade gate. Result: 20 straight
    # BLOCKs in the pre-trade log, zero alpha admissions.
    #
    # Fix: fold the 7-day per-symbol WR from research into the composite.
    # Coins with proven ≥70% WR get +15% boost; coins below 40% get
    # capped at 80% of their raw score. Sample < 3 exits = neutral.
    sym = coin.get("symbol")
    if sym:
        try:
            from spot_aggro.governance.research_agent import latest_report
            rpt = latest_report() or {}
            rows = [r for r in (rpt.get("per_symbol") or [])
                    if r.get("symbol") == sym]
            if rows:
                exits = sum(int(r.get("exits", 0) or 0) for r in rows)
                wins = sum(int(r.get("wins", 0) or 0) for r in rows)
                if exits >= 3:
                    wr = wins / exits
                    # Smooth multiplier: 1.15× at WR=0.80, 1.0× at 0.55,
                    # 0.80× at 0.30, floor 0.70× at 0.0.
                    if wr >= 0.70:
                        mult = 1.15
                    elif wr >= 0.55:
                        mult = 1.00 + (wr - 0.55) * (0.15 / 0.15)
                    elif wr >= 0.40:
                        mult = 0.90 + (wr - 0.40) * (0.10 / 0.15)
                    elif wr >= 0.25:
                        mult = 0.80 + (wr - 0.25) * (0.10 / 0.15)
                    else:
                        mult = max(0.70, 0.70 + wr * 0.40)
                    S = S * mult
        except Exception:  # noqa: BLE001
            # Scoring must never throw. If research isn't available yet,
            # skip the multiplier (cold-start behavior matches old code).
            pass

    return round(min(max(S, 0.0), 1.0), 4)


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------

def classify_tier(
    composite: float,
    spi: float,
    thresholds: Optional[tuple[float, float, float]] = None,
) -> Optional[TierConfig]:
    """
    Classify a coin into a tier based on composite score.
    Returns TierConfig or None (skip).

    thresholds: optional (tier_a, tier_b, tier_c) overrides from frequency controller.
    """
    t_a, t_b, t_c = thresholds or (TIER_A_MIN, TIER_B_MIN, TIER_C_MIN)

    # A+ requires both high composite AND high SPI (squeeze snipe)
    if composite >= TIER_A_PLUS_MIN and spi >= 0.85:
        return TIER_PARAMS["A+"]

    if composite >= t_a:
        return TIER_PARAMS["A"]
    if composite >= t_b:
        return TIER_PARAMS["B"]
    if composite >= t_c:
        return TIER_PARAMS["C"]

    return None  # skip


# ---------------------------------------------------------------------------
# Daily frequency controller
# ---------------------------------------------------------------------------

@dataclass
class DailyFrequencyController:
    """
    Tracks entries per tier per day.
    Enforces per-tier caps and a global daily cap.
    Provides adaptive threshold loosening/tightening.
    """
    _entries: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _day_yday: int = -1  # day-of-year for reset detection
    _total: int = 0

    def reset_if_new_day(self) -> None:
        yday = time.gmtime().tm_yday
        if yday != self._day_yday:
            self._entries = defaultdict(int)
            self._total = 0
            self._day_yday = yday

    def can_enter(self, tier: str) -> bool:
        if self._total >= GLOBAL_DAILY_CAP:
            return False
        cap = TIER_DAILY_CAPS.get(tier, 0)
        return self._entries[tier] < cap

    def record_entry(self, tier: str) -> None:
        self._entries[tier] += 1
        self._total += 1

    def adaptive_thresholds(self) -> tuple[float, float, float]:
        """
        Returns (tier_a_min, tier_b_min, tier_c_min) adjusted by pace.
        Target: 40-70 trades/day (center 55).
        Loosens if behind, tightens if ahead.
        """
        hour = time.gmtime().tm_hour + time.gmtime().tm_min / 60.0
        day_frac = max(hour / 24.0, 0.01)
        target = 55.0  # center of 40-70 range
        expected = target * day_frac
        actual = self._total

        t_a, t_b, t_c = TIER_A_MIN, TIER_B_MIN, TIER_C_MIN

        if actual >= GLOBAL_DAILY_CAP:
            return 1.0, 1.0, 1.0  # hard cap hit — block all

        # Shortfall ratio
        shortfall = max(0, expected - actual) / max(expected, 1.0)

        # Behind pace — loosen thresholds to catch up
        if shortfall > 0.3:
            t_c = t_c - 0.08 * min(shortfall, 1.0)
            t_c = max(0.15, t_c)
        if shortfall > 0.5:
            t_b = t_b - 0.06 * (shortfall - 0.3)
            t_b = max(0.30, t_b)

        # Ahead of pace — tighten to avoid overshooting 70
        if actual > expected * 1.3 and actual >= 50:
            t_c = 0.35  # tighten C
            t_b = 0.50  # tighten B
        if actual >= 65:
            t_c = 0.40  # near cap — strong tighten
            t_b = 0.55

        return t_a, t_b, t_c

    def state_dict(self) -> dict[str, Any]:
        return {
            "entries_by_tier": dict(self._entries),
            "total_today": self._total,
            "global_cap": GLOBAL_DAILY_CAP,
            "tier_caps": TIER_DAILY_CAPS,
            "adaptive_thresholds": {
                "tier_a": self.adaptive_thresholds()[0],
                "tier_b": self.adaptive_thresholds()[1],
                "tier_c": self.adaptive_thresholds()[2],
            },
        }
