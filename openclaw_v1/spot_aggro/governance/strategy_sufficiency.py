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


# Default per-trade target: 2% net return.
DEFAULT_TARGET_PCT = 0.02
MIN_SAMPLE_FOR_VERDICT = 20

# Sufficiency band: verdict flips 'sufficient' when BOTH:
#   (a) >=40% of trades clear the target
#   (b) Wilson-95 lower bound on that rate > 20%
SUFFICIENT_HIT_RATE = 0.40
SUFFICIENT_WILSON_LOW = 0.20


@dataclass
class SufficiencyVerdict:
    target_pct_per_trade: float = DEFAULT_TARGET_PCT
    n_observed: int = 0
    n_wins: int = 0
    n_losses: int = 0
    pct_hitting_target: float = 0.0
    wilson_low: float = 0.0
    wilson_up: float = 0.0
    median_net_pct: float = 0.0
    p75_net_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    rr_ratio: float = 0.0
    required_wr_for_target: float = 0.0
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

    hits = sum(1 for x in nets if x >= target_pct)
    v.pct_hitting_target = round(hits / n, 4)
    v.wilson_low, v.wilson_up = _wilson_95(hits, n)
    v.wilson_low = round(v.wilson_low, 4)
    v.wilson_up = round(v.wilson_up, 4)

    nets_sorted = sorted(nets)
    v.median_net_pct = round(median(nets_sorted), 6)
    v.p75_net_pct = round(nets_sorted[int(n * 0.75)] if n >= 4 else nets_sorted[-1], 6)

    # Required WR so that expectancy = target.
    # expectancy = wr*avg_win + (1-wr)*avg_loss  >=  target
    # => wr >= (target - avg_loss) / (avg_win - avg_loss)
    if v.avg_win_pct > v.avg_loss_pct:
        wr_req = (target_pct - v.avg_loss_pct) / (v.avg_win_pct - v.avg_loss_pct)
        v.required_wr_for_target = round(max(0.0, min(1.0, wr_req)), 4)

    if n < MIN_SAMPLE_FOR_VERDICT:
        v.recommendation = "insufficient_sample"
        v.reason = (
            f"n={n} < {MIN_SAMPLE_FOR_VERDICT} minimum sample for a "
            f"sufficiency verdict. Collect more fills."
        )
        return v

    # Decision logic.
    if v.pct_hitting_target >= SUFFICIENT_HIT_RATE and v.wilson_low > SUFFICIENT_WILSON_LOW:
        v.sufficient = True
        v.recommendation = "keep"
        v.reason = (
            f"{v.pct_hitting_target * 100:.0f}% of last {n} trades hit >={target_pct * 100:.1f}%; "
            f"Wilson-low {v.wilson_low * 100:.0f}% > {SUFFICIENT_WILSON_LOW * 100:.0f}% bar."
        )
    elif v.pct_hitting_target >= 0.20:
        v.recommendation = "tune"
        v.reason = (
            f"{v.pct_hitting_target * 100:.0f}% hit rate is below {SUFFICIENT_HIT_RATE * 100:.0f}% bar. "
            f"Required WR {v.required_wr_for_target * 100:.0f}% vs observed "
            f"{v.n_wins / n * 100:.0f}%. Tune TP/SL, filters, or sizing."
        )
    elif v.avg_win_pct < target_pct:
        v.recommendation = "replace"
        v.reason = (
            f"avg_win={v.avg_win_pct * 100:.2f}% < target {target_pct * 100:.1f}%. "
            f"Structural: wins don't hit target even at 100% WR. "
            f"Needs wider TP / different signal / different regime."
        )
    else:
        v.recommendation = "tune"
        v.reason = (
            f"hit-rate {v.pct_hitting_target * 100:.0f}% very low but avg_win "
            f"{v.avg_win_pct * 100:.2f}% reaches target. Filter for high-conviction setups only."
        )
    return v
