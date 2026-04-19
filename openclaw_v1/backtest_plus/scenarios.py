"""
Scenario registry — 10 built-in regimes with parametric synthesis.

Each scenario is a deterministic OHLCV generator driven by:
    mu_annual   — drift (annualised, as a fraction, e.g. 0.80 = +80 %/yr)
    sigma_daily — daily vol (fraction, e.g. 0.042 = 4.2 %/day)
    jump_prob   — per-bar probability of a fat-tail jump
    jump_scale  — typical jump size as a multiple of sigma_daily
    mean_rev    — strength of mean-reversion toward trend ([0, 1))
    funding     — bps per 8h (applied on perp positions only)

Synthesis produces a bar stream at a chosen timeframe. Bars are built from
a GBM+jump+mean-reversion process; intrabar high/low are sampled from a
bounded Brownian bridge so OHLC is internally consistent.

The generator is pure / deterministic given a seed. No wall-clock reads.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import List


BARS_PER_YEAR = 365 * 24 * 4          # 15-minute bars baseline
SEC_PER_BAR_DEFAULT = 15 * 60


@dataclass(frozen=True)
class ScenarioSpec:
    code: str
    name: str
    description: str
    mu_annual: float
    sigma_daily: float
    jump_prob: float
    jump_scale: float
    mean_rev: float
    funding_bps_8h: float          # positive = longs pay
    trade_freq_bias: float = 1.0   # informational hint for UI
    trend_persistence: float = 0.5 # informational hint for UI


SCENARIOS: dict[str, ScenarioSpec] = {
    "BLACK_SWAN": ScenarioSpec(
        code="BLACK_SWAN",
        name="The Black Swan",
        description="March 2020 / FTX-style liquidity vacuum, extreme taker slippage, failed exits.",
        mu_annual=-2.50, sigma_daily=0.090, jump_prob=0.012, jump_scale=6.0,
        mean_rev=0.02, funding_bps_8h=-20.0, trade_freq_bias=2.5, trend_persistence=0.15,
    ),
    "EUPHORIC_BULL_FLUSH": ScenarioSpec(
        code="EUPHORIC_BULL_FLUSH",
        name="The Euphoric Bull Flush",
        description="Slow grind up, violent 15% flushes down, retail long liquidations.",
        mu_annual=1.40, sigma_daily=0.050, jump_prob=0.0045, jump_scale=3.5,
        mean_rev=0.05, funding_bps_8h=12.0, trade_freq_bias=1.1, trend_persistence=0.80,
    ),
    "HIGH_VOL_RANGE": ScenarioSpec(
        code="HIGH_VOL_RANGE",
        name="The High-Vol Ranging Market",
        description="2021-style violent chop, 4-sigma events both directions, fake breakouts.",
        mu_annual=0.10, sigma_daily=0.055, jump_prob=0.004, jump_scale=4.0,
        mean_rev=0.35, funding_bps_8h=4.0, trade_freq_bias=1.8, trend_persistence=0.30,
    ),
    "BEAR_BLEED": ScenarioSpec(
        code="BEAR_BLEED",
        name="The Bear Market Bleed",
        description="Late 2022 slow grinding sell-off, weak bounces, persistent negative funding.",
        mu_annual=-0.70, sigma_daily=0.030, jump_prob=0.0015, jump_scale=2.5,
        mean_rev=0.08, funding_bps_8h=-8.0, trade_freq_bias=0.6, trend_persistence=0.70,
    ),
    "SIDEWAYS_CHOP": ScenarioSpec(
        code="SIDEWAYS_CHOP",
        name="The Sideways Chop",
        description="Summer 2023, near-zero vol, rare triggers, bot stays idle, patience tested.",
        mu_annual=0.05, sigma_daily=0.012, jump_prob=0.0005, jump_scale=2.0,
        mean_rev=0.45, funding_bps_8h=1.0, trade_freq_bias=0.3, trend_persistence=0.25,
    ),
    "S1_2021_BULL": ScenarioSpec(
        code="S1_2021_BULL",
        name="S1: 2021 Bull Euphoria",
        description="BTC 30K → 65K parabolic rally, positive funding, best-case for momentum.",
        mu_annual=2.40, sigma_daily=0.042, jump_prob=0.002, jump_scale=3.0,
        mean_rev=0.04, funding_bps_8h=15.0, trade_freq_bias=1.2, trend_persistence=0.85,
    ),
    "S2_2022_BEAR": ScenarioSpec(
        code="S2_2022_BEAR",
        name="S2: 2022 Bear Crash (Luna/FTX)",
        description="BTC 48K → 16K, extreme volatility, negative funding, DD stress test.",
        mu_annual=-1.80, sigma_daily=0.051, jump_prob=0.005, jump_scale=4.5,
        mean_rev=0.03, funding_bps_8h=-10.0, trade_freq_bias=1.5, trend_persistence=0.60,
    ),
    "S3_2023_RECOVERY": ScenarioSpec(
        code="S3_2023_RECOVERY",
        name="S3: 2023 Recovery Grind",
        description="BTC 16K → 44K slow grind, ~72% ranging, best-case for grid module.",
        mu_annual=0.95, sigma_daily=0.028, jump_prob=0.0010, jump_scale=2.5,
        mean_rev=0.25, funding_bps_8h=3.0, trade_freq_bias=0.9, trend_persistence=0.55,
    ),
    "S4_2024_HALVING_ETF": ScenarioSpec(
        code="S4_2024_HALVING_ETF",
        name="S4: 2024 Halving + ETF Bull",
        description="BTC 44K → 100K ETF-driven rally, institutional trend, momentum-friendly.",
        mu_annual=1.25, sigma_daily=0.035, jump_prob=0.0018, jump_scale=3.0,
        mean_rev=0.06, funding_bps_8h=10.0, trade_freq_bias=1.0, trend_persistence=0.78,
    ),
    "S5_2025_LATE_CYCLE": ScenarioSpec(
        code="S5_2025_LATE_CYCLE",
        name="S5: 2025 Late Cycle",
        description="BTC 100K → 126K → 60K distribution + sharp correction, unstable mixed regime.",
        mu_annual=-0.20, sigma_daily=0.045, jump_prob=0.004, jump_scale=3.5,
        mean_rev=0.15, funding_bps_8h=5.0, trade_freq_bias=1.4, trend_persistence=0.40,
    ),
}


@dataclass
class Bar:
    ts: int      # unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float


def synth_ohlcv(
    spec: ScenarioSpec,
    n_bars: int,
    *,
    start_price: float = 50_000.0,
    start_ts: int = 1_700_000_000,
    sec_per_bar: int = SEC_PER_BAR_DEFAULT,
    seed: int = 0,
) -> list[Bar]:
    """Deterministic OHLCV generator for a single regime.

    The process:
        r_t = mu_bar + mean_rev * (trend_t - r_{t-1}) + sigma_bar * z_t + jump_t
    where z_t is N(0,1), mean_rev nudges returns back to the regime drift,
    and jump_t is nonzero with probability jump_prob.
    """
    rng = random.Random(f"{seed}:{spec.code}:{n_bars}:{start_price}:{start_ts}:{sec_per_bar}")
    bars_per_year = 365 * 86400 / sec_per_bar
    mu_bar = spec.mu_annual / bars_per_year
    sigma_bar = spec.sigma_daily / math.sqrt(86400 / sec_per_bar)
    out: list[Bar] = []
    price = start_price
    prev_r = 0.0
    for i in range(n_bars):
        z = rng.gauss(0.0, 1.0)
        r = mu_bar + sigma_bar * z + spec.mean_rev * (mu_bar - prev_r)
        if rng.random() < spec.jump_prob:
            jsign = 1 if rng.random() < 0.5 else -1
            # Jumps skew in the direction of drift during hard trending regimes
            if abs(spec.mu_annual) > 1.0:
                jsign = 1 if spec.mu_annual > 0 else -1
                if rng.random() < 0.30:    # rare counter-trend flush
                    jsign = -jsign
            r += jsign * sigma_bar * spec.jump_scale
        prev_r = r
        new_price = max(price * math.exp(r), 1e-6)
        o = price
        c = new_price
        # Intrabar range: bounded brownian bridge proxy
        intrabar_vol = abs(sigma_bar) * (1.0 + 2.0 * rng.random())
        hi = max(o, c) * (1.0 + intrabar_vol * rng.random())
        lo = min(o, c) * (1.0 - intrabar_vol * rng.random())
        vol = 1_000.0 * (0.5 + 1.5 * rng.random()) * (1.0 + 3.0 * abs(z) if rng.random() < 0.1 else 1.0)
        out.append(Bar(ts=start_ts + i * sec_per_bar, open=o, high=hi, low=lo, close=c, volume=vol))
        price = new_price
    return out


def bars_per_day(sec_per_bar: int = SEC_PER_BAR_DEFAULT) -> int:
    return max(1, 86400 // sec_per_bar)


def bars_for_duration(days: float, sec_per_bar: int = SEC_PER_BAR_DEFAULT) -> int:
    return max(1, int(round(days * 86400 / sec_per_bar)))
