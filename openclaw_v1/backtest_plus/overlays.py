"""
Overlay engine — layered cost / liquidity / latency shocks applied on top
of a composed bar series. Overlays affect *execution*, not the price path
itself, so the underlying scenarios remain auditable.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class OverlayConfig:
    # Multiplicative scales (1.0 = baseline, 2.0 = 2x harsher)
    slippage_mult: float = 1.0
    spread_mult: float = 1.0
    # Probability that a fill is partial / rejected on any given trade attempt
    no_fill_prob: float = 0.0
    partial_fill_prob: float = 0.0
    # Liquidity vacuum: probability a bar has 10x normal slippage
    liquidity_vacuum_prob: float = 0.0
    # Funding drag: extra bps charged per 8h cycle on perp exposure
    funding_drag_bps_8h: float = 0.0
    # Latency: number of bars between signal and fill (>=0)
    latency_bars: int = 0
    # Black swan override: forces a single intra-window crash if true
    black_swan_inject: bool = False
    black_swan_drop: float = -0.30
    # Execution stress: probability stop loss slips by 2x on trigger
    exec_stress_prob: float = 0.0


@dataclass
class FillOutcome:
    filled: bool
    fill_qty_frac: float          # in [0, 1]; 1.0 = full
    extra_slippage_bps: float
    extra_funding_bps: float
    stop_slip_mult: float


def evaluate_fill(cfg: OverlayConfig, rng: random.Random) -> FillOutcome:
    """Decide per-trade fill outcome given overlay config + RNG."""
    if cfg.no_fill_prob > 0 and rng.random() < cfg.no_fill_prob:
        return FillOutcome(False, 0.0, 0.0, 0.0, 1.0)
    qty = 1.0
    if cfg.partial_fill_prob > 0 and rng.random() < cfg.partial_fill_prob:
        qty = max(0.1, rng.uniform(0.3, 0.8))
    extra = 0.0
    if cfg.liquidity_vacuum_prob > 0 and rng.random() < cfg.liquidity_vacuum_prob:
        extra += 100.0       # 100 bps additional one-time slippage
    extra += (cfg.spread_mult - 1.0) * 10.0
    extra += (cfg.slippage_mult - 1.0) * 20.0
    if extra < 0:
        extra = 0.0
    stop_mult = 1.0
    if cfg.exec_stress_prob > 0 and rng.random() < cfg.exec_stress_prob:
        stop_mult = 2.0
    return FillOutcome(True, qty, extra, cfg.funding_drag_bps_8h, stop_mult)


def apply_overlays(bars, cfg: OverlayConfig, seed: int = 0):
    """Optionally inject a black-swan crash into the bar stream.

    Returns the (possibly mutated) bar list. Slippage/latency/funding
    overlays are applied per-trade by the engine, not here.
    """
    if not cfg.black_swan_inject or len(bars) < 5:
        return bars
    rng = random.Random(f"{seed}:blackswan")
    idx = rng.randrange(len(bars) // 4, max(2, len(bars) - 1))
    drop = cfg.black_swan_drop
    if drop > 0:
        drop = -drop
    new = list(bars)
    target = new[idx]
    crashed_close = max(1e-6, target.close * (1.0 + drop))
    crashed_low = min(target.low, crashed_close * 0.95)
    new[idx] = type(target)(
        ts=target.ts,
        open=target.open,
        high=target.high,
        low=crashed_low,
        close=crashed_close,
        volume=target.volume * 5.0,
    )
    # Propagate price level forward
    cur_price = crashed_close
    for j in range(idx + 1, len(new)):
        b = new[j]
        rel = b.close / max(target.close, 1e-6)
        new_close = cur_price * rel
        new[j] = type(b)(
            ts=b.ts,
            open=cur_price if j == idx + 1 else b.open * (cur_price / max(target.close, 1e-6)),
            high=b.high * (cur_price / max(target.close, 1e-6)),
            low=b.low * (cur_price / max(target.close, 1e-6)),
            close=new_close,
            volume=b.volume,
        )
        cur_price = new_close
    return new
