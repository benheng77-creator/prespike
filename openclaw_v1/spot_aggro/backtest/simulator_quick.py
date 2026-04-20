"""Opportunity Fabric historical simulation — Option A (quick).

Replays the variant admission rules (contrarian / deep_value / momentum)
against historical OKX candles. Uses a simple fill model:
    entry fill = bar_open + spread_cost_bp
    exit fill  = bar_open - spread_cost_bp
    fee        = 0.10% per side (OKX EEA spot taker, matches engine.py)

CDC candles are loaded in parallel and used ONLY as a consistency check:
flag bars where the two exchanges' close price differs by > 50bp (data
integrity signal). Trades are sized + priced on OKX.

Deliberately NOT included (per the A-vs-B scope):
    - fractal regime gate (needs regime stream)
    - liquidity_inference gate (needs book depth feed)
    - exploration wallet (needs rolling 24h PnL of prior simulated trades)
    - meta-gate U1-U5
    - contradiction_freeze / kill_ladder

Next-bar-open fill, walk-forward, single symbol at a time, independent
runs. Report is aggregated across symbols at the end.
"""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .data_puller import CandleSet, load_or_fetch


SLIPPAGE_BP = 5.0                  # one-side spread cost
FEE_BP_PER_SIDE = 10.0             # OKX spot taker
NOTIONAL_USD = 5.0                 # matches live engine scalp size
MAX_CONCURRENT = 5                 # cap open positions per symbol
WARMUP_BARS = 180                  # 3h for rolling stats before first trade


# Variant admission thresholds — match the LIVE code loosened phase-vv
# values where applicable.
# Contrarian (phase-11n-9-ii style): admit when short-term pullback with
# 7d positive drift.
CONTRARIAN_RET_5M_MAX = -0.003      # >=0.3% down in last 5m
CONTRARIAN_RET_7D_MIN = -0.02       # 7d return at least -2% (not crashing)
CONTRARIAN_TP_PCT = 0.015           # +1.5%
CONTRARIAN_SL_PCT = -0.010          # -1.0%
CONTRARIAN_HOLD_MAX_MIN = 45

# Deep value (phase-vv Path B loosened):
DV_MIN_WR = 0.45                    # can't compute here; proxy with ret_7d pattern
DV_MIN_7D_RET = -0.20
DV_MAX_7D_RET = -0.005
DV_TP_PCT = 0.020
DV_SL_PCT = -0.015
DV_HOLD_MAX_MIN = 120

# Momentum (5-of-5 mean-cross proxy):
MOMENTUM_RET_5M_MIN = 0.003
MOMENTUM_RET_15M_MIN = 0.002
MOMENTUM_TP_PCT = 0.020
MOMENTUM_SL_PCT = -0.012
MOMENTUM_HOLD_MAX_MIN = 60


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    variant: str
    symbol: str
    ts_open_ms: int
    ts_close_ms: int
    entry_px: float
    exit_px: float
    notional_usd: float
    realized_pnl_usd: float
    realized_ret_pct: float
    hold_min: float
    exit_reason: str                # "TP" | "SL" | "TIME_STOP"
    ret_5m_at_entry: float
    ret_7d_at_entry: float


@dataclass
class VariantStats:
    variant: str
    n_trades: int
    n_wins: int
    n_losses: int
    win_rate: float
    mean_pnl_usd: float
    sum_pnl_usd: float
    mean_hold_min: float
    exit_reasons: dict[str, int] = field(default_factory=dict)


@dataclass
class SymbolResult:
    symbol: str
    n_bars: int
    n_trades: int
    total_pnl_usd: float
    by_variant: dict[str, VariantStats] = field(default_factory=dict)
    cross_exchange_divergence_count: int = 0


@dataclass
class BacktestReport:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    variant: str = "all"
    universe: list[str] = field(default_factory=list)
    bar: str = "1m"
    total_bars_scanned: int = 0
    total_trades: int = 0
    total_pnl_usd: float = 0.0
    by_variant: dict[str, VariantStats] = field(default_factory=dict)
    by_symbol: dict[str, SymbolResult] = field(default_factory=dict)
    cross_exchange_divergence_rate: float = 0.0
    trades_sample: list[Trade] = field(default_factory=list)   # first 50

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Keep trades_sample concise.
        d["trades_sample"] = [asdict(t) for t in self.trades_sample[:50]]
        return d


# ---------------------------------------------------------------------------
# Rolling stats
# ---------------------------------------------------------------------------

def _pct_return(prev_close: float, cur_close: float) -> float:
    if prev_close <= 0:
        return 0.0
    return (cur_close - prev_close) / prev_close


def _ret_window(candles: list[list[float]], idx: int, lookback: int) -> float:
    """Return (close[idx] / close[idx-lookback]) - 1, or 0 if out of range."""
    if idx - lookback < 0 or idx >= len(candles):
        return 0.0
    prev = float(candles[idx - lookback][4])
    cur = float(candles[idx][4])
    return _pct_return(prev, cur)


# ---------------------------------------------------------------------------
# Variant admission
# ---------------------------------------------------------------------------

def _admit_contrarian(ret_5m: float, ret_7d: float) -> bool:
    return ret_5m <= CONTRARIAN_RET_5M_MAX and ret_7d >= CONTRARIAN_RET_7D_MIN


def _admit_deep_value(ret_7d: float) -> bool:
    return DV_MIN_7D_RET <= ret_7d <= DV_MAX_7D_RET


def _admit_momentum(ret_5m: float, ret_15m: float) -> bool:
    return ret_5m >= MOMENTUM_RET_5M_MIN and ret_15m >= MOMENTUM_RET_15M_MIN


# ---------------------------------------------------------------------------
# Fill simulation
# ---------------------------------------------------------------------------

def _gross_pnl_ret(entry_px: float, exit_px: float) -> float:
    if entry_px <= 0:
        return 0.0
    return (exit_px - entry_px) / entry_px


def _net_pnl_usd(entry_px: float, exit_px: float, notional_usd: float) -> float:
    """Apply one-side slippage at entry + exit + fees per side."""
    if entry_px <= 0 or exit_px <= 0:
        return 0.0
    # Entry: buy at bar_open + slippage; Exit: sell at bar_open - slippage.
    entry_fill = entry_px * (1 + SLIPPAGE_BP / 10_000)
    exit_fill = exit_px * (1 - SLIPPAGE_BP / 10_000)
    gross = (exit_fill - entry_fill) / entry_fill * notional_usd
    fees = 2 * notional_usd * (FEE_BP_PER_SIDE / 10_000)
    return gross - fees


def _simulate_position(
    candles: list[list[float]],
    entry_idx: int,
    tp_pct: float,
    sl_pct: float,
    hold_max_min: int,
    notional_usd: float,
) -> tuple[int, float, str]:
    """Starting at candles[entry_idx] (fill at this bar's open), walk forward
    bar-by-bar. Exit when:
        high >= entry * (1+tp_pct)   -> TP
        low  <= entry * (1+sl_pct)   -> SL
        bars elapsed >= hold_max_min -> TIME_STOP
    Return (exit_idx, exit_px, reason).
    Ties prefer SL (conservative), matching real engine behavior.
    """
    entry_px = float(candles[entry_idx][1])      # bar open
    tp_px = entry_px * (1 + tp_pct)
    sl_px = entry_px * (1 + sl_pct)
    max_idx = min(len(candles) - 1, entry_idx + hold_max_min)
    for i in range(entry_idx, max_idx + 1):
        hi = float(candles[i][2])
        lo = float(candles[i][3])
        # Conservative: check SL before TP on the same bar (worst case).
        if lo <= sl_px:
            return (i, sl_px, "SL")
        if hi >= tp_px:
            return (i, tp_px, "TP")
    # Time stop — exit at last bar's close.
    return (max_idx, float(candles[max_idx][4]), "TIME_STOP")


# ---------------------------------------------------------------------------
# Per-symbol replay
# ---------------------------------------------------------------------------

def simulate_symbol(
    okx: CandleSet,
    cdc: CandleSet | None = None,
) -> SymbolResult:
    """Walk-forward simulation of all three variants on one symbol."""
    res = SymbolResult(symbol=okx.symbol, n_bars=okx.n, n_trades=0,
                       total_pnl_usd=0.0)
    if okx.n < WARMUP_BARS + 10:
        return res

    # Cross-exchange consistency — map CDC bars by ts.
    cdc_by_ts: dict[int, list[float]] = {}
    if cdc and cdc.rows:
        for r in cdc.rows:
            cdc_by_ts[int(r[0])] = r

    trades: list[Trade] = []
    open_positions: list[tuple[int, str, int]] = []  # (entry_idx, variant, close_idx)

    # For per-symbol concurrency cap: positions already held.
    held = 0

    for i in range(WARMUP_BARS, okx.n - 1):
        # Close any positions whose close_idx == i-1.
        open_positions = [p for p in open_positions if p[2] >= i]
        held = len(open_positions)

        if held >= MAX_CONCURRENT:
            continue

        # Rolling stats at bar i (use close of bar i, entry at bar i+1 open).
        ret_5m = _ret_window(okx.rows, i, 5)
        ret_15m = _ret_window(okx.rows, i, 15)
        ret_7d = _ret_window(okx.rows, i, 10_080)  # 7d × 1440 min

        # Cross-exchange divergence check.
        ts = int(okx.rows[i][0])
        if ts in cdc_by_ts:
            okx_close = float(okx.rows[i][4])
            cdc_close = float(cdc_by_ts[ts][4])
            if okx_close > 0:
                diff_bp = abs(okx_close - cdc_close) / okx_close * 10_000
                if diff_bp > 50:
                    res.cross_exchange_divergence_count += 1

        # Evaluate variants; first admit wins (OR-logic, same as live).
        admitted_variant: str | None = None
        if _admit_contrarian(ret_5m, ret_7d):
            admitted_variant = "contrarian"
            tp, sl, hold = CONTRARIAN_TP_PCT, CONTRARIAN_SL_PCT, CONTRARIAN_HOLD_MAX_MIN
        elif _admit_deep_value(ret_7d):
            admitted_variant = "deep_value"
            tp, sl, hold = DV_TP_PCT, DV_SL_PCT, DV_HOLD_MAX_MIN
        elif _admit_momentum(ret_5m, ret_15m):
            admitted_variant = "momentum"
            tp, sl, hold = MOMENTUM_TP_PCT, MOMENTUM_SL_PCT, MOMENTUM_HOLD_MAX_MIN
        else:
            continue

        # Entry at bar i+1 open.
        entry_idx = i + 1
        close_idx, exit_px, reason = _simulate_position(
            okx.rows, entry_idx, tp, sl, hold, NOTIONAL_USD,
        )
        entry_px = float(okx.rows[entry_idx][1])
        pnl = _net_pnl_usd(entry_px, exit_px, NOTIONAL_USD)
        ret_pct = (exit_px - entry_px) / entry_px
        hold_min = close_idx - entry_idx

        trade = Trade(
            variant=admitted_variant,
            symbol=okx.symbol,
            ts_open_ms=int(okx.rows[entry_idx][0]),
            ts_close_ms=int(okx.rows[close_idx][0]),
            entry_px=entry_px,
            exit_px=exit_px,
            notional_usd=NOTIONAL_USD,
            realized_pnl_usd=round(pnl, 4),
            realized_ret_pct=round(ret_pct, 6),
            hold_min=hold_min,
            exit_reason=reason,
            ret_5m_at_entry=round(ret_5m, 6),
            ret_7d_at_entry=round(ret_7d, 6),
        )
        trades.append(trade)
        open_positions.append((entry_idx, admitted_variant, close_idx))
        res.total_pnl_usd += pnl

    res.n_trades = len(trades)
    # Aggregate per variant.
    by_variant: dict[str, list[Trade]] = {}
    for t in trades:
        by_variant.setdefault(t.variant, []).append(t)
    for v, ts_list in by_variant.items():
        pnls = [t.realized_pnl_usd for t in ts_list]
        holds = [t.hold_min for t in ts_list]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        reasons: dict[str, int] = {}
        for t in ts_list:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        res.by_variant[v] = VariantStats(
            variant=v, n_trades=len(ts_list),
            n_wins=wins, n_losses=losses,
            win_rate=round(wins / max(len(ts_list), 1), 4),
            mean_pnl_usd=round(sum(pnls) / len(pnls), 4),
            sum_pnl_usd=round(sum(pnls), 2),
            mean_hold_min=round(statistics.mean(holds), 1) if holds else 0.0,
            exit_reasons=reasons,
        )
    # Attach trades separately (caller decides whether to keep).
    res._trades = trades   # type: ignore[attr-defined]
    return res


# ---------------------------------------------------------------------------
# Full run
# ---------------------------------------------------------------------------

def run_quick_backtest(
    universe: list[str],
    total_bars: int = 10_000,
    force_refresh: bool = False,
    pull_cdc: bool = True,
) -> BacktestReport:
    report = BacktestReport(universe=list(universe), bar="1m",
                            variant="all")

    all_trades: list[Trade] = []
    cde_total = 0
    cde_matches = 0

    for sym in universe:
        print(f"[backtest] {sym}: fetching OKX...")
        okx = load_or_fetch("okx", sym, bar="1m",
                            total_bars=total_bars,
                            force_refresh=force_refresh)
        cdc = None
        if pull_cdc:
            try:
                print(f"[backtest] {sym}: fetching CDC...")
                cdc = load_or_fetch("cdc", sym, bar="1m",
                                    total_bars=total_bars,
                                    force_refresh=force_refresh)
            except Exception as e:
                print(f"[backtest] {sym}: CDC fetch failed: {e}")
                cdc = None
        print(f"[backtest] {sym}: OKX n={okx.n} CDC n={(cdc.n if cdc else 0)}")
        if okx.n < WARMUP_BARS + 10:
            print(f"[backtest] {sym}: SKIP — insufficient bars")
            continue

        sr = simulate_symbol(okx, cdc)
        trades = getattr(sr, "_trades", [])
        all_trades.extend(trades)
        report.by_symbol[sym] = sr
        report.total_bars_scanned += okx.n
        report.total_trades += sr.n_trades
        report.total_pnl_usd += sr.total_pnl_usd
        if cdc:
            cde_total += okx.n
            cde_matches += (okx.n - sr.cross_exchange_divergence_count)

    # Aggregate by variant across symbols.
    by_var: dict[str, list[Trade]] = {}
    for t in all_trades:
        by_var.setdefault(t.variant, []).append(t)
    for v, ts_list in by_var.items():
        pnls = [t.realized_pnl_usd for t in ts_list]
        holds = [t.hold_min for t in ts_list]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        reasons: dict[str, int] = {}
        for t in ts_list:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        report.by_variant[v] = VariantStats(
            variant=v, n_trades=len(ts_list),
            n_wins=wins, n_losses=losses,
            win_rate=round(wins / max(len(ts_list), 1), 4),
            mean_pnl_usd=round(sum(pnls) / len(pnls), 4),
            sum_pnl_usd=round(sum(pnls), 2),
            mean_hold_min=round(statistics.mean(holds), 1) if holds else 0.0,
            exit_reasons=reasons,
        )
    report.trades_sample = all_trades[:50]
    report.total_pnl_usd = round(report.total_pnl_usd, 2)
    if cde_total:
        report.cross_exchange_divergence_rate = round(
            1.0 - cde_matches / cde_total, 6
        )
    return report


def save_report(report: BacktestReport, label: str) -> Path:
    REPO_ROOT = Path(__file__).resolve().parents[3]
    out_dir = REPO_ROOT / "runtime" / "backtest" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    fn = f"{report.ts_ms}_{label}.json"
    path = out_dir / fn
    path.write_text(json.dumps(report.to_dict(), default=str, indent=2),
                    encoding="utf-8")
    return path
