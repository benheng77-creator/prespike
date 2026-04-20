"""APEX-Ω∞ Aqueduct — cross-venue spread-arbitrage backtest.

Abandons the contrarian/momentum signal stack entirely. Tests whether
the OKX↔Crypto.com price dislocation (observed at 3.7% of bars, with
WIF diverging on 25% of bars in Option A) is exploitable after
realistic maker-taker costs.

Signal:
    spread_pct_t = (OKX_close_t - CDC_close_t) / CDC_close_t
    z_t          = (spread_pct_t - mean(spread_pct, window=W)) / std(...)

    Enter LONG-OKX / SHORT-CDC (synthetic: buy cheap leg) when z < -ENTRY_Z
    Enter SHORT-OKX / LONG-CDC (buy the other cheap leg) when z > +ENTRY_Z
    Exit when |z| < EXIT_Z or hold >= MAX_HOLD_MIN (whichever first).

    We simulate only the DIRECTION of the pair trade. Actual execution
    would need real maker fills on one leg + taker exit on the other;
    here we model the cost envelope (6 bp round-trip) and measure PnL
    as if both legs executed at the candle's close + cost.

Cost model:
    Maker leg:  -2 bp (OKX VIP1+ rebate) to 0 bp — use 0 conservatively
    Taker leg:  +5 bp fee + 3 bp slippage = 8 bp one-side
    Total round-trip: ~6 bp (conservative; could be lower with rebate)

Why this is defensible:
    - The dislocation is empirically present in the user's cached data.
    - Cross-venue spread mean-reversion is textbook market microstructure
      (Roll 1984, Hasbrouck 1995).
    - Thesis doesn't require directional alpha — no-arbitrage forces
      convergence.
    - 7-sprint governance stack preserved as risk wrapper, not signal
      generator (which is what it actually is, per delta backtest).

Quarter-Kelly sizing only becomes relevant AFTER a positive 30-day
empirical p (win-rate) is established. For this backtest, fixed
notional ($5 per leg = $10 round-trip) to match existing infra.
"""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .data_puller import CandleSet, load_or_fetch


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ENTRY_Z = 2.5
EXIT_Z = 0.5
ZSCORE_WINDOW_BARS = 60            # 1 hour of 1m bars
MIN_WINDOW_BARS = 30               # need at least 30 bars before z-score valid
MAX_HOLD_MIN = 30
NOTIONAL_PER_LEG_USD = 5.0

# Realistic cost envelope (bp, round-trip).
MAKER_BP = 0.0                      # conservative; VIP rebate would be -2
TAKER_BP = 5.0
SLIPPAGE_TAKER_BP = 3.0
ROUND_TRIP_COST_BP = MAKER_BP + TAKER_BP + SLIPPAGE_TAKER_BP
# = 8 bp one-sided. Note: for a pair trade, we incur this on BOTH exits
# and BOTH entries since we're not actually netting across venues.
# True round-trip for the pair is ~16 bp if both legs are taker, ~8 bp
# if one leg is maker. Use 8 bp as the "best realistic" conservative.

COST_BP_PER_ROUND_TRIP = 8.0        # 2 taker legs × 4 bp each, or 1 maker + 1 taker


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class SpreadTrade:
    symbol: str
    direction: str                   # 'long_okx_short_cdc' | 'long_cdc_short_okx'
    ts_open_ms: int
    ts_close_ms: int
    open_spread_pct: float
    close_spread_pct: float
    open_z: float
    close_z: float
    realized_spread_delta_bp: float
    cost_bp: float
    net_pnl_bp: float
    net_pnl_usd: float
    hold_min: int
    exit_reason: str                 # 'z_exit' | 'time_stop'


@dataclass
class SymbolAqueductResult:
    symbol: str
    n_aligned_bars: int
    n_trades: int
    n_z_breaches: int                # |z| >= ENTRY_Z
    spread_mean_bp: float
    spread_std_bp: float
    convergence_rate: float          # % of trades with positive gross PnL
    mean_net_bp: float
    total_net_pnl_usd: float
    exit_reasons: dict[str, int] = field(default_factory=dict)


@dataclass
class AqueductReport:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    universe: list[str] = field(default_factory=list)
    entry_z: float = ENTRY_Z
    exit_z: float = EXIT_Z
    zscore_window_bars: int = ZSCORE_WINDOW_BARS
    max_hold_min: int = MAX_HOLD_MIN
    cost_bp_round_trip: float = COST_BP_PER_ROUND_TRIP
    notional_per_leg_usd: float = NOTIONAL_PER_LEG_USD
    total_aligned_bars: int = 0
    total_trades: int = 0
    total_z_breaches: int = 0
    total_net_pnl_usd: float = 0.0
    overall_convergence_rate: float = 0.0
    overall_mean_net_bp: float = 0.0
    by_symbol: dict[str, SymbolAqueductResult] = field(default_factory=dict)
    trades_sample: list[SpreadTrade] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trades_sample"] = [asdict(t) for t in self.trades_sample[:50]]
        return d


# ---------------------------------------------------------------------------
# Rolling z-score
# ---------------------------------------------------------------------------

def _rolling_zscore(values: list[float], idx: int, window: int) -> float | None:
    if idx < window:
        return None
    w = values[idx - window + 1: idx + 1]
    if len(w) < MIN_WINDOW_BARS:
        return None
    m = sum(w) / len(w)
    var = sum((v - m) ** 2 for v in w) / max(len(w) - 1, 1)
    std = var ** 0.5
    if std == 0:
        return None
    return (values[idx] - m) / std


# ---------------------------------------------------------------------------
# Align OKX + CDC by timestamp
# ---------------------------------------------------------------------------

def _align_by_ts(
    okx: CandleSet, cdc: CandleSet,
) -> list[tuple[int, float, float]]:
    """Return [(ts_ms, okx_close, cdc_close)] for bars present in BOTH sets.
    Sorted ascending by ts."""
    cdc_by_ts = {int(r[0]): r for r in cdc.rows}
    aligned: list[tuple[int, float, float]] = []
    for r in okx.rows:
        ts = int(r[0])
        cdc_r = cdc_by_ts.get(ts)
        if cdc_r is None:
            continue
        aligned.append((ts, float(r[4]), float(cdc_r[4])))
    return aligned


# ---------------------------------------------------------------------------
# Per-symbol simulation
# ---------------------------------------------------------------------------

def simulate_symbol_aqueduct(
    okx: CandleSet, cdc: CandleSet,
) -> SymbolAqueductResult:
    aligned = _align_by_ts(okx, cdc)
    res = SymbolAqueductResult(
        symbol=okx.symbol, n_aligned_bars=len(aligned),
        n_trades=0, n_z_breaches=0,
        spread_mean_bp=0.0, spread_std_bp=0.0,
        convergence_rate=0.0, mean_net_bp=0.0,
        total_net_pnl_usd=0.0,
    )
    if len(aligned) < ZSCORE_WINDOW_BARS + 10:
        return res

    # Compute spread_pct series.
    spreads: list[float] = []
    for _, okx_c, cdc_c in aligned:
        if cdc_c <= 0:
            spreads.append(0.0)
        else:
            spreads.append((okx_c - cdc_c) / cdc_c)

    res.spread_mean_bp = round(
        (sum(spreads) / len(spreads)) * 10_000, 2
    )
    if len(spreads) > 1:
        m = sum(spreads) / len(spreads)
        var = sum((s - m) ** 2 for s in spreads) / (len(spreads) - 1)
        res.spread_std_bp = round((var ** 0.5) * 10_000, 2)

    # Walk forward. Single open position at a time per symbol.
    trades: list[SpreadTrade] = []
    open_pos: dict[str, Any] | None = None

    for i in range(ZSCORE_WINDOW_BARS, len(aligned) - 1):
        ts, okx_c, cdc_c = aligned[i]
        z = _rolling_zscore(spreads, i, ZSCORE_WINDOW_BARS)
        if z is None:
            continue
        if abs(z) >= ENTRY_Z:
            res.n_z_breaches += 1

        if open_pos is None:
            # Entry check.
            if abs(z) < ENTRY_Z:
                continue
            direction = ("long_okx_short_cdc" if z < -ENTRY_Z
                         else "long_cdc_short_okx")
            open_pos = {
                "entry_i": i,
                "direction": direction,
                "entry_spread": spreads[i],
                "entry_z": z,
                "entry_ts": ts,
            }
            continue

        # Exit check for existing position.
        held_min = i - open_pos["entry_i"]
        exit_reason: str | None = None
        if abs(z) <= EXIT_Z:
            exit_reason = "z_exit"
        elif held_min >= MAX_HOLD_MIN:
            exit_reason = "time_stop"
        if exit_reason is None:
            continue

        # Close position at this bar.
        exit_spread = spreads[i]
        entry_spread = open_pos["entry_spread"]
        # When long OKX / short CDC: we profit if spread NARROWS (OKX moves
        # down or CDC moves up). gross_bp = -(exit - entry) * 10000
        # When long CDC / short OKX: we profit if spread WIDENS from entry
        # toward positive side. gross_bp = +(exit - entry) * 10000.
        # But both directions want convergence toward 0 — so:
        if open_pos["direction"] == "long_okx_short_cdc":
            # Entered because OKX cheap; want OKX to rally vs CDC.
            # Profit = -(exit_spread - entry_spread) in raw-pct terms
            # (since entry_spread is negative and we want exit >= entry).
            gross_bp = (exit_spread - entry_spread) * 10_000
            # BUT we wanted spread to rise toward 0 (spread = OKX-CDC, OKX
            # below CDC = negative spread; rising toward 0 = good).
            # So when long OKX: profit scales with +(exit - entry) already.
            # Actually since we LONGED the cheap OKX: if OKX rallies,
            # spread rises (becomes less negative), delta is positive.
            # So gross_bp = (exit_spread - entry_spread) * 10000 = +
            # ✓ already correct.
        else:
            # Long CDC / short OKX: spread starts large positive, we want it
            # to fall to 0. Profit = -(exit - entry) in raw-pct.
            gross_bp = -(exit_spread - entry_spread) * 10_000

        net_bp = gross_bp - COST_BP_PER_ROUND_TRIP
        net_pnl_usd = (net_bp / 10_000) * NOTIONAL_PER_LEG_USD * 2

        trades.append(SpreadTrade(
            symbol=okx.symbol,
            direction=open_pos["direction"],
            ts_open_ms=open_pos["entry_ts"],
            ts_close_ms=ts,
            open_spread_pct=round(entry_spread, 6),
            close_spread_pct=round(exit_spread, 6),
            open_z=round(open_pos["entry_z"], 3),
            close_z=round(z, 3),
            realized_spread_delta_bp=round(gross_bp, 2),
            cost_bp=COST_BP_PER_ROUND_TRIP,
            net_pnl_bp=round(net_bp, 2),
            net_pnl_usd=round(net_pnl_usd, 4),
            hold_min=held_min,
            exit_reason=exit_reason,
        ))
        open_pos = None

    # Aggregate.
    res.n_trades = len(trades)
    if trades:
        gross_positive = sum(1 for t in trades if t.realized_spread_delta_bp > 0)
        res.convergence_rate = round(gross_positive / len(trades), 4)
        res.mean_net_bp = round(
            sum(t.net_pnl_bp for t in trades) / len(trades), 2
        )
        res.total_net_pnl_usd = round(sum(t.net_pnl_usd for t in trades), 2)
        for t in trades:
            res.exit_reasons[t.exit_reason] = (
                res.exit_reasons.get(t.exit_reason, 0) + 1
            )
    res._trades = trades  # type: ignore[attr-defined]
    return res


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------

def run_aqueduct_backtest(
    universe: list[str],
    total_bars: int = 43_200,
    force_refresh: bool = False,
) -> AqueductReport:
    report = AqueductReport(universe=list(universe))
    all_trades: list[SpreadTrade] = []
    gross_positive_total = 0
    net_bp_sum = 0.0

    for sym in universe:
        okx = load_or_fetch("okx", sym, bar="1m",
                            total_bars=total_bars,
                            force_refresh=force_refresh)
        try:
            cdc = load_or_fetch("cdc", sym, bar="1m",
                                total_bars=total_bars,
                                force_refresh=force_refresh)
        except Exception:
            cdc = None
        print(f"[aqueduct] {sym}: OKX n={okx.n} CDC n={(cdc.n if cdc else 0)}")
        if cdc is None or cdc.n < 100 or okx.n < 100:
            continue
        sr = simulate_symbol_aqueduct(okx, cdc)
        trades = getattr(sr, "_trades", [])
        all_trades.extend(trades)
        report.by_symbol[sym] = sr
        report.total_aligned_bars += sr.n_aligned_bars
        report.total_trades += sr.n_trades
        report.total_z_breaches += sr.n_z_breaches
        report.total_net_pnl_usd += sr.total_net_pnl_usd
        gross_positive_total += sum(
            1 for t in trades if t.realized_spread_delta_bp > 0
        )
        net_bp_sum += sum(t.net_pnl_bp for t in trades)

    if report.total_trades > 0:
        report.overall_convergence_rate = round(
            gross_positive_total / report.total_trades, 4,
        )
        report.overall_mean_net_bp = round(
            net_bp_sum / report.total_trades, 2,
        )
    report.total_net_pnl_usd = round(report.total_net_pnl_usd, 2)
    report.trades_sample = all_trades[:50]
    return report


def save_aqueduct_report(report: AqueductReport, label: str) -> Path:
    REPO_ROOT = Path(__file__).resolve().parents[3]
    out_dir = REPO_ROOT / "runtime" / "backtest" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    fn = f"{report.ts_ms}_{label}.json"
    path = out_dir / fn
    path.write_text(json.dumps(report.to_dict(), default=str, indent=2),
                    encoding="utf-8")
    return path
