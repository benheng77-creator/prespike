"""
Δ-neutrality invariant check.

Runs after every paired entry and periodically mid-hold. If |Σ Δ| / notional
exceeds cfg.risk.delta_neutrality_tol_pct on any symbol, the pair is flagged
for immediate rebalance or close (engine decides which).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..config import load as load_cfg


log = logging.getLogger("apex.risk.delta")


@dataclass
class DeltaCheck:
    symbol: str
    spot_notional_usd: float
    perp_notional_usd: float       # signed: negative for short perp
    delta_usd: float               # spot + perp (should be ~0)
    notional_usd: float            # |perp| used as denominator
    drift_pct: float               # |delta| / notional
    ok: bool
    tolerance: float


def check(*, symbol: str, spot_qty: float, spot_px: float,
          perp_contracts: float, perp_mark: float) -> DeltaCheck:
    cfg = load_cfg()
    tol = float(cfg["risk"]["delta_neutrality_tol_pct"])
    spot_notional = spot_qty * spot_px
    perp_notional = perp_contracts * perp_mark   # signed: neg for short
    delta = spot_notional + perp_notional
    denom = max(abs(perp_notional), 1e-9)
    drift = abs(delta) / denom
    ok = drift <= tol
    if not ok:
        log.warning(
            "Δ drift on %s: delta=%.4f notional=%.4f drift=%.4%% tol=%.4%%",
            symbol, delta, denom, drift * 100, tol * 100,
        )
    return DeltaCheck(
        symbol=symbol, spot_notional_usd=spot_notional,
        perp_notional_usd=perp_notional, delta_usd=delta,
        notional_usd=denom, drift_pct=drift, ok=ok, tolerance=tol,
    )
