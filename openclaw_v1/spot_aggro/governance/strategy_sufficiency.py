"""Phase 11n-9-ll — Strategy Sufficiency Test for >=2% per-trade target.

Mathematical check: given the current empirical distribution of realized
per-trade returns (net of fees + slippage), can the strategy plausibly
deliver >=2% return on each trade in expectation?

The target is per-trade, not aggregate. A 2%/trade bar is aggressive —
most spot crypto scalping strategies run 0.3-0.8% per winning trade
with losers capped at 1-1.5%. Hitting >=2% NET per trade requires
either: (a) much wider TPs (longer holds, regime-dependent), (b)
selective entry on high-asymmetry setups only, or (c) higher WR
offset by tight losers.

Outputs
-------
SufficiencyVerdict with:
  - target_pct_per_trade       float (default 0.02 = 2%)
  - n_observed                 int trades used for the test
  - pct_hitting_target         float fraction of closed trades with net_pnl_pct >= target
  - wilson_low / wilson_up     95% CI on that fraction
  - median_net_pct             float
  - p75_net_pct                float
  - avg_win_pct / avg_loss_pct floats
  - rr_ratio                   avg_win_pct / abs(avg_loss_pct)
  - required_wr_for_target     what WR would need to be to hit +2% avg net at current R/R
  - sufficient                 bool (pct_hitting_target >= 0.40 AND wilson_low > 0.20)
  - reason                     human explanation
  - recommendation             'keep' | 'tune' | 'replace' | 'insufficient_sample'

Read-only. Never raises. Fail-open.
"""
from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any


def _db_path() -> str:
    return (
        os.environ.get("TRADE_DB_PATH")
        or os.environ.get("CLAW_DB_PATH")
        or "trades.db"
    )


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(), isolation_level=None, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


# Phase 11n-9-mm — dual-band target model.
# FLOOR (1.5%) is the minimum real edge threshold.
# STRETCH (2.0%) is the excellence bar.
# Verdicts:
#   'keep'       — clearing FLOOR and approaching STRETCH
#   'tune'       — below FLOOR but avg_win > 1.0% (tunable via TP/SL/filter)
#   'replace'    — avg_win <= 1.0% (structural ceiling hit)
#   'excellent'  — clearing STRETCH
#   'insufficient_sample' — n < MIN_SAMPLE_FOR_VERDICT
TARGET_FLOOR_PCT = 0.015
TARGET_STRETCH_PCT = 0.02
DEFAULT_TARGET_PCT = TARGET_FLOOR_PCT  # sufficiency measured against floor
REPLACE_AVG_WIN_FLOOR = 0.010           # below this → structural replace
MIN_SAMPLE_FOR_VERDICT = 20

# Sufficiency band for the FLOOR target:
#   (a) >= 35% of trades clear FLOOR
#   (b) Wilson-95 lower bound on that rate > 15%
SUFFICIENT_HIT_RATE = 0.35
SUFFICIENT_WILSON_LOW = 0.15
# Excellence band (STRETCH target):
EXCELLENT_HIT_RATE = 0.45
EXCELLENT_WILSON_LOW = 0.25


@dataclass
class SufficiencyVerdict:
    target_pct_per_trade: float = DEFAULT_TARGET_PCT          # = FLOOR
    target_floor_pct: float = TARGET_FLOOR_PCT
    target_stretch_pct: float = TARGET_STRETCH_PCT
    n_observed: int = 0
    n_wins: int = 0
    n_losses: int = 0
    pct_hitting_target: float = 0.0                           # rate clearing FLOOR
    pct_hitting_stretch: float = 0.0                          # rate clearing STRETCH
    wilson_low: float = 0.0
    wilson_up: float = 0.0
    median_net_pct: float = 0.0
    p75_net_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    rr_ratio: float = 0.0
    required_wr_for_target: float = 0.0                       # WR for FLOOR
    required_wr_for_stretch: float = 0.0
    sufficient: bool = False
    reason: str = ""
    recommendation: str = "insufficient_sample"
    sample_ts_ms_min: int | None = None
    sample_ts_ms_max: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _wilson_95(wins: int, n: int) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 0.0
    p = wins / n
    z = 1.96
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, center - half), min(1.0, center + half)


def evaluate(
    target_pct: float = DEFAULT_TARGET_PCT,
    window_n: int = 100,
) -> SufficiencyVerdict:
    """Run the sufficiency test over the last `window_n` closed trades."""
    v = SufficiencyVerdict(target_pct_per_trade=target_pct)
    try:
        con = _connect()
        try:
            # Pull recent closed-exit trades with notional + pnl.
            rows = con.execute(
                "SELECT ts_ms, notional_usd, pnl_usd, fee_usd"
                " FROM trade_log WHERE action='exit'"
                " AND notional_usd IS NOT NULL AND notional_usd > 0"
                " AND pnl_usd IS NOT NULL"
                " ORDER BY ts_ms DESC LIMIT ?",
                (int(window_n),),
            ).fetchall()
        finally:
            con.close()
    except Exception as e:
        v.reason = f"db read error: {str(e)[:120]}"
        return v

    if not rows:
        v.reason = "no closed trades in trade_log"
        return v

    # Compute per-trade net %: (pnl - fee) / notional
    nets: list[float] = []
    for r in rows:
        notional = float(r["notional_usd"] or 0)
        if notional <= 0:
            continue
        pnl = float(r["pnl_usd"] or 0)
        fee = float(r["fee_usd"] or 0)
        net_pct = (pnl - fee) / notional
        nets.append(net_pct)

    n = len(nets)
    v.n_observed = n
    if n == 0:
        v.reason = "no valid trades after filtering"
        return v
    v.sample_ts_ms_min = int(rows[-1]["ts_ms"])
    v.sample_ts_ms_max = int(rows[0]["ts_ms"])

    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    v.n_wins = len(wins)
    v.n_losses = len(losses)
    v.avg_win_pct = round(sum(wins) / len(wins), 6) if wins else 0.0
    v.avg_loss_pct = round(sum(losses) / len(losses), 6) if losses else 0.0
    if v.avg_loss_pct < 0:
        v.rr_ratio = round(v.avg_win_pct / abs(v.avg_loss_pct), 4)

    # target_pct is the FLOOR; stretch is STRETCH.
    v.target_floor_pct = target_pct
    v.target_stretch_pct = TARGET_STRETCH_PCT

    hits_floor = sum(1 for x in nets if x >= target_pct)
    hits_stretch = sum(1 for x in nets if x >= TARGET_STRETCH_PCT)
    v.pct_hitting_target = round(hits_floor / n, 4)
    v.pct_hitting_stretch = round(hits_stretch / n, 4)
    v.wilson_low, v.wilson_up = _wilson_95(hits_floor, n)
    v.wilson_low = round(v.wilson_low, 4)
    v.wilson_up = round(v.wilson_up, 4)

    nets_sorted = sorted(nets)
    v.median_net_pct = round(median(nets_sorted), 6)
    v.p75_net_pct = round(nets_sorted[int(n * 0.75)] if n >= 4 else nets_sorted[-1], 6)

    # Required WR at current avg_win/avg_loss for each target.
    # expectancy = wr*avg_win + (1-wr)*avg_loss  >=  target
    # => wr >= (target - avg_loss) / (avg_win - avg_loss)
    if v.avg_win_pct > v.avg_loss_pct:
        wr_floor = (target_pct - v.avg_loss_pct) / (v.avg_win_pct - v.avg_loss_pct)
        wr_stretch = (TARGET_STRETCH_PCT - v.avg_loss_pct) / (v.avg_win_pct - v.avg_loss_pct)
        v.required_wr_for_target = round(max(0.0, min(1.0, wr_floor)), 4)
        v.required_wr_for_stretch = round(max(0.0, min(1.0, wr_stretch)), 4)

    if n < MIN_SAMPLE_FOR_VERDICT:
        v.recommendation = "insufficient_sample"
        v.reason = (
            f"n={n} < {MIN_SAMPLE_FOR_VERDICT} minimum sample for a "
            f"sufficiency verdict. Collect more fills."
        )
        return v

    # Decision logic — BAND MODE (phase-mm).
    # Tier 1: excellent — clearing STRETCH target.
    if (v.pct_hitting_stretch >= EXCELLENT_HIT_RATE
            and v.wilson_low > EXCELLENT_WILSON_LOW):
        v.sufficient = True
        v.recommendation = "excellent"
        v.reason = (
            f"EXCELLENT: {v.pct_hitting_stretch * 100:.0f}% of last {n} trades hit "
            f">={TARGET_STRETCH_PCT * 100:.1f}% stretch target; Wilson-low "
            f"{v.wilson_low * 100:.0f}%. Strong edge — scale carefully."
        )
        return v
    # Tier 2: keep — clearing FLOOR target.
    if (v.pct_hitting_target >= SUFFICIENT_HIT_RATE
            and v.wilson_low > SUFFICIENT_WILSON_LOW):
        v.sufficient = True
        v.recommendation = "keep"
        v.reason = (
            f"KEEP: {v.pct_hitting_target * 100:.0f}% of last {n} trades hit "
            f">={target_pct * 100:.1f}% floor target; Wilson-low {v.wilson_low * 100:.0f}%. "
            f"Floor cleared; stretch {v.pct_hitting_stretch * 100:.0f}%."
        )
        return v
    # Tier 3: replace — structural ceiling hit.
    if v.avg_win_pct <= REPLACE_AVG_WIN_FLOOR:
        v.recommendation = "replace"
        v.reason = (
            f"REPLACE: avg_win {v.avg_win_pct * 100:.2f}% <= "
            f"{REPLACE_AVG_WIN_FLOOR * 100:.1f}% structural floor. "
            f"Current TP/SL/scorer combo cannot clear even floor. "
            f"Widen TP to 2.5-3.5%, extend holds, or replace scorer."
        )
        return v
    # Tier 4: tune — below floor but above structural floor.
    v.recommendation = "tune"
    v.reason = (
        f"TUNE: avg_win {v.avg_win_pct * 100:.2f}% above structural floor "
        f"{REPLACE_AVG_WIN_FLOOR * 100:.1f}% but floor hit-rate only "
        f"{v.pct_hitting_target * 100:.0f}% (need {SUFFICIENT_HIT_RATE * 100:.0f}%). "
        f"Required WR {v.required_wr_for_target * 100:.0f}% vs observed "
        f"{v.n_wins / n * 100:.0f}%. Tighten filter to high-conviction only "
        f"OR widen TP slightly to push wins above {target_pct * 100:.1f}%."
    )
    return v
