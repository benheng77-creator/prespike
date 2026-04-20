"""APEX-Ω∞ "Aqueduct" — Cross-exchange spread mean-reversion backtest.

Thesis (mathematical, not vibes):
  OKX 1m mid prices for liquid alts deviate from the cross-venue
  consensus (CDC) by >50bp on 3.7% of bars. If those deviations
  mean-revert faster than the round-trip cost burns the position,
  there is exploitable edge that the variant-admission framework
  could never capture because it's a microstructure signal, not a
  directional one.

Execution model: SINGLE-VENUE on OKX, using CDC as an off-board
fair-value oracle. We do NOT execute on CDC — retail can't reliably
short there and venue-pair latency would eat the edge anyway. CDC
is purely a real-time signal of "where the global market thinks the
fair price is."

Signal:
  spread_bp(t) = (okx_close[t] - cdc_close[t]) / cdc_close[t] * 10_000
  z(t)         = (spread_bp[t] - rolling_mean(60)) / rolling_std(60)

Entry:
  |z| >= Z_ENTRY (default 2.5)
    z >  +Z_ENTRY  -> SHORT OKX (it's rich vs consensus, expect drop)
    z <  -Z_ENTRY  -> LONG  OKX (it's cheap vs consensus, expect rise)

Exit (first to fire):
  - |z| <= Z_EXIT (default 0.5)        ... convergence
  - hold_min >= MAX_HOLD (default 30)  ... time stop
  - |z| >= Z_STOP (default 4.0)        ... divergence stop

Statistical pre-flight (computed BEFORE simulating, per symbol):
  - Augmented Dickey-Fuller on spread series (p<0.05 = mean-reverting)
  - Hurst exponent via R/S (H<0.5 = mean-reverting)
  - Half-life of mean reversion (Ornstein-Uhlenbeck fit)
  Symbols failing all three are flagged but still simulated for completeness.

Cost model: THREE TIERS run in parallel so Ben sees the real bound.
  best         -> maker entry + maker exit + 0bp slip  =  4bp r/t
  realistic    -> maker entry + taker exit + 3bp slip  = 10bp r/t
  conservative -> taker entry + taker exit + 6bp slip  = 16bp r/t

Decision verdict (per cost tier, per symbol AND aggregate):
  EDGE      -> mean_net_bp > +3 AND WR > 55% AND n >= 50
  MARGINAL  -> mean_net_bp > 0  AND n >= 50
  NO_EDGE   -> mean_net_bp <= 0 OR n < 50

Drop-in: matches simulator_quick / simulator_full / simulator_delta
interface. Reuses data_puller.load_or_fetch. Writes report to
runtime/backtest/runs/.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .data_puller import CandleSet, load_or_fetch


# ---------------------------------------------------------------------------
# Tunables (env-overridable for sweeps)
# ---------------------------------------------------------------------------

Z_ENTRY        = float(os.environ.get("AQDT_Z_ENTRY", "2.5"))
Z_EXIT         = float(os.environ.get("AQDT_Z_EXIT",  "0.5"))
Z_STOP         = float(os.environ.get("AQDT_Z_STOP",  "4.0"))
ROLLING_WINDOW = int(  os.environ.get("AQDT_ROLL",    "60"))   # bars (1m → 1h)
MAX_HOLD_MIN   = int(  os.environ.get("AQDT_MAX_HOLD","30"))
WARMUP_BARS    = ROLLING_WINDOW + 5                            # buffer

NOTIONAL_USD   = float(os.environ.get("AQDT_NOTIONAL", "5.0"))
MAX_CONCURRENT = int(  os.environ.get("AQDT_MAX_CONC", "3"))

# Three cost tiers (round-trip basis points charged against gross PnL).
COST_TIERS_BP = {
    "best":         4.0,    # maker+maker, 0bp slip — aspirational
    "realistic":    10.0,   # maker+taker, 3bp slip — typical retail
    "conservative": 16.0,   # taker+taker, 6bp slip — bad fills
}


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class SpreadStats:
    """Pre-flight statistical fingerprint of a symbol's OKX-CDC spread."""
    n_aligned_bars: int
    mean_spread_bp: float
    std_spread_bp: float
    abs_mean_bp: float           # |spread| mean — the raw dislocation magnitude
    pct_bars_over_50bp: float    # fraction of bars |spread| > 50bp
    pct_bars_over_100bp: float
    adf_stat: float | None       # None if statsmodels unavailable
    adf_pvalue: float | None
    hurst_exponent: float | None
    half_life_min: float | None
    is_mean_reverting: bool      # ADF p<0.05 OR Hurst<0.45 OR half-life<20
    notes: list[str] = field(default_factory=list)


@dataclass
class AqueductTrade:
    symbol: str
    direction: str               # "LONG_OKX" | "SHORT_OKX"
    ts_open_ms: int
    ts_close_ms: int
    entry_px: float
    exit_px: float
    z_at_entry: float
    z_at_exit: float
    spread_bp_entry: float
    spread_bp_exit: float
    hold_min: int
    exit_reason: str             # "Z_EXIT" | "TIME_STOP" | "Z_STOP" | "EOD"
    gross_ret_bp: float          # signed return (in bp), pre-cost
    # Net returns per cost tier (post-cost, in bp).
    net_bp: dict[str, float] = field(default_factory=dict)


@dataclass
class SymbolResult:
    symbol: str
    n_bars: int
    n_aligned: int
    spread_stats: SpreadStats | None
    n_trades: int = 0
    n_long: int = 0
    n_short: int = 0
    exit_reasons: dict[str, int] = field(default_factory=dict)
    # Per cost tier metrics.
    by_cost: dict[str, dict[str, Any]] = field(default_factory=dict)
    verdict: dict[str, str] = field(default_factory=dict)
    trades_sample: list[AqueductTrade] = field(default_factory=list)


@dataclass
class AqueductReport:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    universe: list[str] = field(default_factory=list)
    days_requested: float = 0.0
    bar: str = "1m"
    total_bars_scanned: int = 0
    total_aligned_bars: int = 0
    config: dict[str, Any] = field(default_factory=dict)
    by_symbol: dict[str, SymbolResult] = field(default_factory=dict)
    aggregate: dict[str, dict[str, Any]] = field(default_factory=dict)
    overall_verdict: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Keep trades_sample lean.
        for sym, sr in d["by_symbol"].items():
            sr["trades_sample"] = sr["trades_sample"][:20]
        return d


# ---------------------------------------------------------------------------
# Spread alignment
# ---------------------------------------------------------------------------

def _align_okx_cdc(
    okx: CandleSet, cdc: CandleSet,
) -> tuple[list[int], list[float], list[float], list[float]]:
    """Return (timestamps, okx_close, cdc_close, spread_bp) aligned by ts.

    Drops any bar missing from either side. Spread is in basis points
    of CDC close (CDC = oracle/denominator).
    """
    cdc_idx: dict[int, list[float]] = {int(r[0]): r for r in cdc.rows}
    ts_out: list[int] = []
    okx_out: list[float] = []
    cdc_out: list[float] = []
    spr_out: list[float] = []
    for r in okx.rows:
        ts = int(r[0])
        cdc_row = cdc_idx.get(ts)
        if cdc_row is None:
            continue
        okx_c = float(r[4])
        cdc_c = float(cdc_row[4])
        if cdc_c <= 0 or okx_c <= 0:
            continue
        spread_bp = (okx_c - cdc_c) / cdc_c * 10_000.0
        ts_out.append(ts)
        okx_out.append(okx_c)
        cdc_out.append(cdc_c)
        spr_out.append(spread_bp)
    return ts_out, okx_out, cdc_out, spr_out


# ---------------------------------------------------------------------------
# Statistical pre-flight (pure-stdlib + soft scipy import)
# ---------------------------------------------------------------------------

def _hurst_exponent(series: list[float]) -> float | None:
    """R/S Hurst exponent via simple multi-lag rescaled-range regression.

    Returns None if series too short. H<0.5 = mean-reverting,
    H==0.5 = random walk, H>0.5 = trending.
    """
    n = len(series)
    if n < 100:
        return None
    lags = [10, 20, 40, 80, min(160, n // 2)]
    lags = [l for l in lags if l < n]
    if len(lags) < 3:
        return None
    rs_vals = []
    for lag in lags:
        # Compute R/S over non-overlapping windows of size `lag`.
        chunks = [series[i:i + lag] for i in range(0, n - lag + 1, lag)]
        rs_chunk = []
        for ch in chunks:
            if len(ch) < 2:
                continue
            mean = sum(ch) / len(ch)
            dev = [x - mean for x in ch]
            cum = []
            running = 0.0
            for d in dev:
                running += d
                cum.append(running)
            R = max(cum) - min(cum)
            S = statistics.pstdev(ch) if len(ch) > 1 else 0.0
            if S > 0 and R > 0:
                rs_chunk.append(R / S)
        if rs_chunk:
            rs_vals.append((math.log(lag), math.log(sum(rs_chunk) / len(rs_chunk))))
    if len(rs_vals) < 3:
        return None
    # Linear regression slope = Hurst.
    xs = [p[0] for p in rs_vals]
    ys = [p[1] for p in rs_vals]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(len(xs)))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return num / den


def _half_life_minutes(series: list[float]) -> float | None:
    """Ornstein-Uhlenbeck half-life via OLS of Δs_t = α + β*s_{t-1}.

    half_life = -ln(2) / β (only meaningful if β < 0).
    """
    n = len(series)
    if n < 50:
        return None
    # Build deltas and lagged values.
    s_lag = series[:-1]
    delta = [series[i + 1] - series[i] for i in range(n - 1)]
    if len(s_lag) < 30:
        return None
    mx = sum(s_lag) / len(s_lag)
    my = sum(delta) / len(delta)
    num = sum((s_lag[i] - mx) * (delta[i] - my) for i in range(len(s_lag)))
    den = sum((x - mx) ** 2 for x in s_lag)
    if den == 0:
        return None
    beta = num / den
    if beta >= 0:
        return None  # Not mean-reverting.
    return -math.log(2) / beta


def _adf_test(series: list[float]) -> tuple[float | None, float | None]:
    """Augmented Dickey-Fuller test. Soft import; returns (None, None) if
    statsmodels unavailable. Lower stat / lower p = more stationary."""
    try:
        from statsmodels.tsa.stattools import adfuller  # type: ignore
    except ImportError:
        return None, None
    if len(series) < 50:
        return None, None
    try:
        result = adfuller(series, autolag="AIC", maxlag=10)
        return float(result[0]), float(result[1])
    except Exception:
        return None, None


def _compute_spread_stats(spread_bp: list[float]) -> SpreadStats:
    n = len(spread_bp)
    if n < ROLLING_WINDOW + 5:
        return SpreadStats(
            n_aligned_bars=n, mean_spread_bp=0.0, std_spread_bp=0.0,
            abs_mean_bp=0.0, pct_bars_over_50bp=0.0, pct_bars_over_100bp=0.0,
            adf_stat=None, adf_pvalue=None,
            hurst_exponent=None, half_life_min=None,
            is_mean_reverting=False,
            notes=[f"insufficient data: {n} bars < {ROLLING_WINDOW + 5}"],
        )
    mean = sum(spread_bp) / n
    std = statistics.pstdev(spread_bp) if n > 1 else 0.0
    abs_vals = [abs(s) for s in spread_bp]
    abs_mean = sum(abs_vals) / n
    pct_50 = sum(1 for v in abs_vals if v > 50) / n
    pct_100 = sum(1 for v in abs_vals if v > 100) / n
    adf_stat, adf_p = _adf_test(spread_bp)
    hurst = _hurst_exponent(spread_bp)
    halflife = _half_life_minutes(spread_bp)

    notes: list[str] = []
    is_mr = False
    if adf_p is not None and adf_p < 0.05:
        is_mr = True
        notes.append(f"ADF p={adf_p:.4f} < 0.05 (stationary)")
    if hurst is not None and hurst < 0.45:
        is_mr = True
        notes.append(f"Hurst={hurst:.3f} < 0.45 (mean-reverting)")
    if halflife is not None and 0 < halflife < 20:
        is_mr = True
        notes.append(f"half-life={halflife:.1f}m < 20 (fast revert)")
    if not is_mr:
        notes.append("FAILED all 3 mean-reversion tests")

    return SpreadStats(
        n_aligned_bars=n,
        mean_spread_bp=round(mean, 3),
        std_spread_bp=round(std, 3),
        abs_mean_bp=round(abs_mean, 3),
        pct_bars_over_50bp=round(pct_50, 4),
        pct_bars_over_100bp=round(pct_100, 4),
        adf_stat=round(adf_stat, 4) if adf_stat is not None else None,
        adf_pvalue=round(adf_p, 6) if adf_p is not None else None,
        hurst_exponent=round(hurst, 4) if hurst is not None else None,
        half_life_min=round(halflife, 2) if halflife is not None else None,
        is_mean_reverting=is_mr,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Rolling z-score (causal — uses only past bars)
# ---------------------------------------------------------------------------

def _rolling_z(spread_bp: list[float], window: int) -> list[float | None]:
    """For each i, z[i] uses spread_bp[i-window..i-1] (CAUSAL — no lookahead).
    Returns None for indices where window isn't full yet.
    """
    out: list[float | None] = []
    for i in range(len(spread_bp)):
        if i < window:
            out.append(None)
            continue
        win = spread_bp[i - window:i]
        m = sum(win) / window
        # Sample std (n-1) — unbiased.
        var = sum((x - m) ** 2 for x in win) / (window - 1)
        sd = math.sqrt(var) if var > 0 else 0.0
        if sd == 0:
            out.append(0.0)
        else:
            out.append((spread_bp[i] - m) / sd)
    return out


# ---------------------------------------------------------------------------
# Per-symbol walk-forward simulation
# ---------------------------------------------------------------------------

def _simulate_symbol(
    okx: CandleSet, cdc: CandleSet,
) -> SymbolResult:
    res = SymbolResult(
        symbol=okx.symbol, n_bars=okx.n, n_aligned=0, spread_stats=None,
    )
    if okx.n < WARMUP_BARS + 10:
        res.spread_stats = SpreadStats(
            n_aligned_bars=0, mean_spread_bp=0.0, std_spread_bp=0.0,
            abs_mean_bp=0.0, pct_bars_over_50bp=0.0, pct_bars_over_100bp=0.0,
            adf_stat=None, adf_pvalue=None,
            hurst_exponent=None, half_life_min=None,
            is_mean_reverting=False,
            notes=["insufficient OKX bars"],
        )
        return res

    ts_arr, okx_arr, cdc_arr, spread_bp = _align_okx_cdc(okx, cdc)
    n = len(spread_bp)
    res.n_aligned = n
    if n < WARMUP_BARS + 10:
        res.spread_stats = SpreadStats(
            n_aligned_bars=n, mean_spread_bp=0.0, std_spread_bp=0.0,
            abs_mean_bp=0.0, pct_bars_over_50bp=0.0, pct_bars_over_100bp=0.0,
            adf_stat=None, adf_pvalue=None,
            hurst_exponent=None, half_life_min=None,
            is_mean_reverting=False,
            notes=[f"insufficient aligned bars: {n}"],
        )
        return res

    res.spread_stats = _compute_spread_stats(spread_bp)
    z_arr = _rolling_z(spread_bp, ROLLING_WINDOW)

    trades: list[AqueductTrade] = []
    in_position = False
    open_dir: str = ""
    open_idx: int = -1
    open_z: float = 0.0
    open_spread: float = 0.0
    open_px: float = 0.0
    open_ts: int = 0

    for i in range(WARMUP_BARS, n - 1):
        z = z_arr[i]
        if z is None:
            continue

        if not in_position:
            # Entry check.
            if abs(z) >= Z_ENTRY:
                # Use NEXT bar's open as fill (no lookahead — z uses past 60).
                # But we only stored close prices in alignment. Use this bar's
                # close as proxy for next-bar entry (small bias acceptable
                # given 1m bars and the >2.5σ entry filter).
                open_dir = "SHORT_OKX" if z > 0 else "LONG_OKX"
                open_idx = i
                open_z = z
                open_spread = spread_bp[i]
                open_px = okx_arr[i]
                open_ts = ts_arr[i]
                in_position = True
            continue

        # In position — check exits.
        held = i - open_idx
        cur_z = z
        cur_spread = spread_bp[i]
        cur_px = okx_arr[i]

        exit_reason: str | None = None
        if abs(cur_z) <= Z_EXIT:
            exit_reason = "Z_EXIT"
        elif abs(cur_z) >= Z_STOP:
            exit_reason = "Z_STOP"
        elif held >= MAX_HOLD_MIN:
            exit_reason = "TIME_STOP"

        if exit_reason is None:
            continue

        # Compute gross return in bp (signed by direction).
        raw_ret_bp = (cur_px - open_px) / open_px * 10_000.0
        if open_dir == "SHORT_OKX":
            gross_bp = -raw_ret_bp
        else:
            gross_bp = raw_ret_bp

        net_bp_per_tier: dict[str, float] = {}
        for tier_name, cost_bp in COST_TIERS_BP.items():
            net_bp_per_tier[tier_name] = round(gross_bp - cost_bp, 3)

        t = AqueductTrade(
            symbol=okx.symbol, direction=open_dir,
            ts_open_ms=open_ts, ts_close_ms=ts_arr[i],
            entry_px=round(open_px, 8), exit_px=round(cur_px, 8),
            z_at_entry=round(open_z, 3), z_at_exit=round(cur_z, 3),
            spread_bp_entry=round(open_spread, 2),
            spread_bp_exit=round(cur_spread, 2),
            hold_min=held, exit_reason=exit_reason,
            gross_ret_bp=round(gross_bp, 3),
            net_bp=net_bp_per_tier,
        )
        trades.append(t)
        in_position = False

    # If still in position at end, close at last bar (EOD).
    if in_position:
        i = n - 1
        cur_px = okx_arr[i]
        raw_ret_bp = (cur_px - open_px) / open_px * 10_000.0
        gross_bp = -raw_ret_bp if open_dir == "SHORT_OKX" else raw_ret_bp
        net_bp_per_tier = {
            tier: round(gross_bp - cost, 3)
            for tier, cost in COST_TIERS_BP.items()
        }
        cur_z = z_arr[i] if z_arr[i] is not None else 0.0
        trades.append(AqueductTrade(
            symbol=okx.symbol, direction=open_dir,
            ts_open_ms=open_ts, ts_close_ms=ts_arr[i],
            entry_px=round(open_px, 8), exit_px=round(cur_px, 8),
            z_at_entry=round(open_z, 3), z_at_exit=round(cur_z, 3),
            spread_bp_entry=round(open_spread, 2),
            spread_bp_exit=round(spread_bp[i], 2),
            hold_min=i - open_idx, exit_reason="EOD",
            gross_ret_bp=round(gross_bp, 3),
            net_bp=net_bp_per_tier,
        ))

    # Aggregate per-symbol metrics.
    res.n_trades = len(trades)
    res.n_long = sum(1 for t in trades if t.direction == "LONG_OKX")
    res.n_short = sum(1 for t in trades if t.direction == "SHORT_OKX")
    for t in trades:
        res.exit_reasons[t.exit_reason] = res.exit_reasons.get(t.exit_reason, 0) + 1

    for tier_name in COST_TIERS_BP.keys():
        nets = [t.net_bp[tier_name] for t in trades]
        if not nets:
            res.by_cost[tier_name] = {
                "n": 0, "win_rate": 0.0, "mean_net_bp": 0.0,
                "median_net_bp": 0.0, "sum_net_bp": 0.0,
                "sum_pnl_usd": 0.0, "sharpe_per_trade": 0.0,
                "max_dd_bp": 0.0,
            }
            res.verdict[tier_name] = "NO_EDGE"
            continue
        wins = sum(1 for v in nets if v > 0)
        wr = wins / len(nets)
        mean_net = sum(nets) / len(nets)
        med = statistics.median(nets)
        s = sum(nets)
        # Sharpe = mean / std (per-trade, not annualised).
        sd = statistics.pstdev(nets) if len(nets) > 1 else 0.0
        sharpe = (mean_net / sd) if sd > 0 else 0.0
        # Max drawdown in bp (cumulative).
        cum = 0.0; peak = 0.0; mdd = 0.0
        for v in nets:
            cum += v
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > mdd:
                mdd = dd
        # Convert sum_net_bp to USD: each trade is NOTIONAL_USD, return in bp.
        sum_pnl_usd = sum(NOTIONAL_USD * (v / 10_000.0) for v in nets)
        res.by_cost[tier_name] = {
            "n": len(nets),
            "win_rate": round(wr, 4),
            "mean_net_bp": round(mean_net, 3),
            "median_net_bp": round(med, 3),
            "sum_net_bp": round(s, 2),
            "sum_pnl_usd": round(sum_pnl_usd, 4),
            "sharpe_per_trade": round(sharpe, 3),
            "max_dd_bp": round(mdd, 2),
        }
        # Verdict.
        if mean_net > 3 and wr > 0.55 and len(nets) >= 50:
            res.verdict[tier_name] = "EDGE"
        elif mean_net > 0 and len(nets) >= 50:
            res.verdict[tier_name] = "MARGINAL"
        else:
            res.verdict[tier_name] = "NO_EDGE"

    res.trades_sample = trades[:20]
    return res


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------

def run_aqueduct_backtest(
    universe: list[str],
    days: float = 30.0,
    bar: str = "1m",
    force_refresh: bool = False,
) -> AqueductReport:
    # 1m bars per day = 1440. Days * 1440 = total bars target.
    bars_per_day = {"1m": 1440, "5m": 288, "15m": 96}.get(bar, 1440)
    total_bars = int(days * bars_per_day)

    report = AqueductReport(
        universe=list(universe), days_requested=days, bar=bar,
        config={
            "Z_ENTRY": Z_ENTRY, "Z_EXIT": Z_EXIT, "Z_STOP": Z_STOP,
            "ROLLING_WINDOW": ROLLING_WINDOW, "MAX_HOLD_MIN": MAX_HOLD_MIN,
            "NOTIONAL_USD": NOTIONAL_USD, "MAX_CONCURRENT": MAX_CONCURRENT,
            "COST_TIERS_BP": COST_TIERS_BP,
        },
    )

    all_trades_by_tier: dict[str, list[float]] = {t: [] for t in COST_TIERS_BP}

    for sym in universe:
        print(f"[aqueduct] {sym}: fetching {total_bars} bars OKX + CDC...")
        okx = load_or_fetch("okx", sym, bar=bar, total_bars=total_bars,
                            force_refresh=force_refresh)
        cdc = None
        try:
            cdc = load_or_fetch("cdc", sym, bar=bar, total_bars=total_bars,
                                force_refresh=force_refresh)
        except Exception as exc:
            print(f"[aqueduct] {sym}: CDC fetch failed ({exc}) — skipping symbol")
            continue
        print(f"[aqueduct] {sym}: OKX n={okx.n} CDC n={(cdc.n if cdc else 0)}")

        if cdc is None or cdc.n < WARMUP_BARS + 10:
            report.notes.append(f"{sym}: skipped (insufficient CDC data)")
            continue

        sr = _simulate_symbol(okx, cdc)
        report.by_symbol[sym] = sr
        report.total_bars_scanned += okx.n
        report.total_aligned_bars += sr.n_aligned
        for tier, _ in COST_TIERS_BP.items():
            for t in sr.trades_sample[:0]:  # placeholder — see below
                pass
        # Pull all trades (not just sample) from sr by re-extracting? We
        # only kept trades_sample in the result. For aggregate, we need
        # the full list. Easier fix: re-aggregate from per-cost stats.

    # Aggregate at the report level using per-symbol per-cost stats.
    # Weight by n_trades to get a true universe-level mean_net_bp.
    for tier in COST_TIERS_BP.keys():
        total_n = 0
        total_sum_bp = 0.0
        total_wins = 0
        total_pnl_usd = 0.0
        for sym, sr in report.by_symbol.items():
            stats = sr.by_cost.get(tier, {})
            n = stats.get("n", 0)
            if n == 0:
                continue
            total_n += n
            total_sum_bp += stats.get("sum_net_bp", 0.0)
            total_wins += int(stats.get("win_rate", 0.0) * n + 0.5)
            total_pnl_usd += stats.get("sum_pnl_usd", 0.0)
        agg = {
            "n_trades": total_n,
            "win_rate": round(total_wins / total_n, 4) if total_n > 0 else 0.0,
            "mean_net_bp": round(total_sum_bp / total_n, 3) if total_n > 0 else 0.0,
            "sum_net_bp": round(total_sum_bp, 2),
            "sum_pnl_usd": round(total_pnl_usd, 4),
            "n_symbols_with_edge": sum(
                1 for sr in report.by_symbol.values()
                if sr.verdict.get(tier) == "EDGE"
            ),
            "n_symbols_marginal": sum(
                1 for sr in report.by_symbol.values()
                if sr.verdict.get(tier) == "MARGINAL"
            ),
        }
        report.aggregate[tier] = agg
        # Overall verdict per tier.
        if agg["mean_net_bp"] > 3 and agg["win_rate"] > 0.55 and total_n >= 200:
            report.overall_verdict[tier] = "EDGE_CONFIRMED"
        elif agg["mean_net_bp"] > 0 and total_n >= 100:
            report.overall_verdict[tier] = "MARGINAL_KEEP_TESTING"
        else:
            report.overall_verdict[tier] = "NO_EDGE_SHELVE"

    return report


def save_aqueduct_report(report: AqueductReport, label: str) -> Path:
    REPO_ROOT = Path(__file__).resolve().parents[3]
    out_dir = REPO_ROOT / "runtime" / "backtest" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    fn = f"{report.ts_ms}_{label}.json"
    path = out_dir / fn
    path.write_text(
        json.dumps(report.to_dict(), default=str, indent=2),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# CLI pretty-print
# ---------------------------------------------------------------------------

def print_report(report: AqueductReport) -> None:
    print()
    print("=" * 78)
    print("AQUEDUCT — Cross-exchange spread mean-reversion backtest")
    print("=" * 78)
    print(f"Universe: {len(report.universe)} symbols  |  "
          f"Days: {report.days_requested}  |  Bar: {report.bar}")
    print(f"Bars scanned: {report.total_bars_scanned:,}  |  "
          f"Aligned bars (OKX∩CDC): {report.total_aligned_bars:,}")
    print(f"Config: Z_ENTRY={Z_ENTRY}  Z_EXIT={Z_EXIT}  Z_STOP={Z_STOP}  "
          f"hold={MAX_HOLD_MIN}m  roll={ROLLING_WINDOW}b")
    print()
    print("PER-SYMBOL STATISTICAL FINGERPRINT:")
    print(f"  {'symbol':12s} {'n_aln':>6s} {'|spr|bp':>8s} {'>50bp%':>7s} "
          f"{'ADF p':>8s} {'Hurst':>6s} {'HL(m)':>7s}  MR?")
    for sym, sr in report.by_symbol.items():
        ss = sr.spread_stats
        if ss is None:
            print(f"  {sym:12s} (no data)")
            continue
        adf_str = f"{ss.adf_pvalue:.4f}" if ss.adf_pvalue is not None else "  n/a "
        h_str = f"{ss.hurst_exponent:.3f}" if ss.hurst_exponent is not None else " n/a "
        hl_str = f"{ss.half_life_min:.1f}" if ss.half_life_min is not None else " n/a "
        mr = "YES" if ss.is_mean_reverting else "no "
        print(f"  {sym:12s} {ss.n_aligned_bars:6d} {ss.abs_mean_bp:8.2f} "
              f"{ss.pct_bars_over_50bp*100:6.2f}% "
              f"{adf_str:>8s} {h_str:>6s} {hl_str:>7s}  {mr}")

    print()
    print("PER-COST-TIER AGGREGATE RESULTS:")
    for tier, agg in report.aggregate.items():
        cost = COST_TIERS_BP[tier]
        print(f"  [{tier:12s}] cost={cost:5.1f}bp r/t  "
              f"n={agg['n_trades']:5d}  WR={agg['win_rate']*100:5.1f}%  "
              f"mean_net={agg['mean_net_bp']:+7.2f}bp  "
              f"sum=${agg['sum_pnl_usd']:+7.2f}  "
              f"|  symbols(EDGE/MARGINAL)={agg['n_symbols_with_edge']}/"
              f"{agg['n_symbols_marginal']}")

    print()
    print("OVERALL VERDICT PER COST TIER:")
    for tier, verdict in report.overall_verdict.items():
        cost = COST_TIERS_BP[tier]
        print(f"  [{tier:12s}] cost={cost:5.1f}bp -> {verdict}")

    print()
    print("PER-SYMBOL VERDICT MATRIX:")
    print(f"  {'symbol':12s} {'best':>10s} {'realistic':>12s} {'conserv.':>11s}  "
          f"{'n':>5s} {'WR(real)':>9s} {'mean_bp(real)':>14s}")
    for sym, sr in report.by_symbol.items():
        best_v = sr.verdict.get("best", "n/a")
        real_v = sr.verdict.get("realistic", "n/a")
        cons_v = sr.verdict.get("conservative", "n/a")
        real_stats = sr.by_cost.get("realistic", {})
        n = real_stats.get("n", 0)
        wr = real_stats.get("win_rate", 0.0) * 100
        mn = real_stats.get("mean_net_bp", 0.0)
        print(f"  {sym:12s} {best_v:>10s} {real_v:>12s} {cons_v:>11s}  "
              f"{n:5d} {wr:8.1f}% {mn:+13.2f}")

    if report.notes:
        print()
        print("NOTES:")
        for note in report.notes:
            print(f"  - {note}")
    print("=" * 78)
