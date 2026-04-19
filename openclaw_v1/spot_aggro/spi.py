"""
Squeeze Pressure Index (SPI) — spec §2.1.

SPI_i = 0.35 × max(0, -funding_z) / 5.0
      + 0.25 × clip(oi_growth, 0, 0.20) / 0.20
      + 0.25 × clip(ret_7d - funding_avg_7d×1000, 0, 0.10) / 0.10
      + 0.15 × liq_proximity

Range: [0, 1]. Higher = more squeeze pressure.
"""

from __future__ import annotations


def compute_spi(
    *,
    funding_z: float,
    oi_growth_rate: float,      # OI 24h change as fraction (0.05 = 5%)
    price_ret_7d: float,        # 7-day return as fraction (0.03 = 3%)
    funding_avg_7d: float,      # avg funding rate over 7d (e.g., -0.0002)
    price: float,
    liq_cluster_price: float,   # nearest large liquidation cluster price
) -> tuple[float, dict[str, float]]:
    """Return (spi, components_dict)."""
    # Component 1: funding z-score negativity (shorts paying)
    # SPEC §2.1 — NEVER MODIFIED. max(0, -fz) as written.
    fz_neg = max(0.0, -funding_z) / 5.0
    fz_neg = min(fz_neg, 1.0)

    # Component 2: open interest growth (new shorts entering = fuel)
    oi_g = min(max(oi_growth_rate, 0.0), 0.20) / 0.20

    # Component 3: price-funding divergence (price up while funding neg)
    div_raw = price_ret_7d - funding_avg_7d * 1000
    div = min(max(div_raw, 0.0), 0.10) / 0.10

    # Component 4: liquidation cluster proximity
    if price > 0 and liq_cluster_price > 0:
        dist_pct = abs(price - liq_cluster_price) / price
        liq_p = 1.0 - min(dist_pct, 0.05) / 0.05
    else:
        liq_p = 0.0
    liq_p = max(0.0, liq_p)

    spi = 0.35 * fz_neg + 0.25 * oi_g + 0.25 * div + 0.15 * liq_p

    components = {
        "fz": round(fz_neg, 4),
        "oi": round(oi_g, 4),
        "div": round(div, 4),
        "liq": round(liq_p, 4),
    }
    return round(spi, 4), components


def compute_tp_sl(spi_entry: float) -> tuple[float, float]:
    """Dynamic TP/SL from spec §2.4.

    Returns (tp_pct, sl_pct) where sl is negative.
    SPI 0.65 → TP 3.21%, SL -1.15%
    SPI 0.90 → TP 4.13%, SL -1.40%
    """
    tp = 0.008 + 0.037 * spi_entry    # 0.80% + 3.70% × SPI
    sl = -(0.005 + 0.010 * spi_entry) # -0.50% - 1.00% × SPI
    return tp, sl


def check_trailing_stop(current_ret: float, max_ret: float, tp: float) -> bool:
    """Spec §2.4 trailing stop: activate at 60% of TP, trail at 60% of peak."""
    if max_ret > tp * 0.60:
        trail = max_ret * 0.60
        return current_ret < trail
    return False


# ---------------------------------------------------------------------------
# v2 tier-aware wrappers — original functions above are UNTOUCHED
# ---------------------------------------------------------------------------

def compute_tp_sl_tiered(
    spi_entry: float,
    tp_mult: float = 1.0,
    sl_mult: float = 1.0,
) -> tuple[float, float]:
    """Tier-adjusted TP/SL. Wraps compute_tp_sl with tier multipliers.

    Tier A+: tp_mult=1.30, sl_mult=1.00 (wider TP for squeeze)
    Tier A:  tp_mult=1.10, sl_mult=1.00
    Tier B:  tp_mult=0.90, sl_mult=0.90 (tighter for flow)
    Tier C:  tp_mult=0.70, sl_mult=0.80 (tight scalp)
    """
    base_tp, base_sl = compute_tp_sl(spi_entry)
    return base_tp * tp_mult, base_sl * sl_mult


def check_trailing_stop_tiered(
    current_ret: float,
    max_ret: float,
    tp: float,
    activate_frac: float = 0.60,
    trail_frac: float = 0.60,
) -> bool:
    """Tier-adjusted trailing stop. Same logic, configurable thresholds.

    Tier A/A+: activate=0.60, trail=0.60 (original)
    Tier B:    activate=0.50, trail=0.50
    Tier C:    activate=0.40, trail=0.40
    """
    if max_ret > tp * activate_frac:
        trail = max_ret * trail_frac
        return current_ret < trail
    return False
