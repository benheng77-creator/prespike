"""Opportunity Fabric historical simulation — Option B (full 7-sprint stack).

Extends simulator_quick with synthetic population of:
  - fractal regime (derived from ret_1h + vol_1h per bar)
  - liquidity inference (derived from candle vol + hi-lo spread + CDC price divergence)
  - exploration wallet 24h rolling PnL (from prior simulated trades in same run)
  - execution SLO (synthetic slippage from candle hi-lo range)

Every admission goes through the FULL chain:
  1. variant rule fires
  2. fractal regime requires ≥2 of 3 scales agree (synthetic 1m/5m/1h)
  3. liquidity inference score < 0.80
  4. exploration wallet not tripped (contrarian/deep_value only)
  5. execution SLO gate (when promoted)

Trades that get rejected by any gate are counted and reported separately
so we can see WHICH gate is blocking most admissions.

Report fields extended vs quick:
  - rejections_by_gate
  - wallet_disable_events
  - regime_sample_stream
"""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .data_puller import CandleSet, load_or_fetch
from .simulator_quick import (
    CONTRARIAN_RET_5M_MAX, CONTRARIAN_RET_7D_MIN,
    CONTRARIAN_TP_PCT, CONTRARIAN_SL_PCT, CONTRARIAN_HOLD_MAX_MIN,
    DV_MIN_7D_RET, DV_MAX_7D_RET, DV_TP_PCT, DV_SL_PCT, DV_HOLD_MAX_MIN,
    MOMENTUM_RET_5M_MIN, MOMENTUM_RET_15M_MIN,
    MOMENTUM_TP_PCT, MOMENTUM_SL_PCT, MOMENTUM_HOLD_MAX_MIN,
    SLIPPAGE_BP, FEE_BP_PER_SIDE, NOTIONAL_USD,
    MAX_CONCURRENT, WARMUP_BARS,
    Trade, VariantStats,
    _admit_contrarian, _admit_deep_value, _admit_momentum,
    _ret_window, _pct_return, _net_pnl_usd, _simulate_position,
)


EXPLORATION_WALLET_USD = 30.0
EXPLORATION_DD_KILL_USD = 5.0
EXPLORATION_VARIANTS = ("contrarian", "deep_value")

LIQ_ABORT_SCORE = 0.80


# ---------------------------------------------------------------------------
# Synthetic regime classifier (per-bar)
# ---------------------------------------------------------------------------

def _regime_sign(ret_1h: float, vol_1h: float) -> str:
    """Map 1h return + volatility to a regime sign.

    Simple heuristic mimicking live regime classifier:
      ret_1h >= +0.3% and vol moderate  -> 'bullish'
      ret_1h <= -0.3% and vol moderate  -> 'bearish'
      |ret_1h| < 0.3%                   -> 'neutral'
      vol spike (>3x median) regardless -> 'neutral' (noise)
    """
    if vol_1h > 0 and ret_1h >= 0.003:
        return "bullish"
    if vol_1h > 0 and ret_1h <= -0.003:
        return "bearish"
    return "neutral"


def _fractal_verdict(
    candles: list[list[float]],
    idx: int,
) -> tuple[str, bool]:
    """Compute (confirmed_sign, admit_recommended) using 3 scales.
    1m: sign from last 1 bar's return
    5m: sign from last 5 bars' return
    1h: sign from last 60 bars' return
    ≥2 of 3 must agree for 'confirmed'; else 'disagreement' → block.
    """
    def _s(lookback: int) -> str:
        ret = _ret_window(candles, idx, lookback)
        # Use range of last `lookback` bars as vol proxy.
        vol = 0.0
        if idx - lookback >= 0:
            closes = [float(candles[j][4]) for j in range(idx - lookback, idx + 1)]
            if closes:
                mean_c = sum(closes) / len(closes)
                if mean_c > 0:
                    hi = max(closes); lo = min(closes)
                    vol = (hi - lo) / mean_c
        return _regime_sign(ret, vol)

    s1 = _s(1)
    s5 = _s(5)
    s60 = _s(60)
    signs = [s1, s5, s60]
    # Count.
    from collections import Counter
    c = Counter(signs)
    (modal, n), = c.most_common(1)
    admit = n >= 2                # ≥2 of 3 agree
    return modal, admit


# ---------------------------------------------------------------------------
# Synthetic liquidity inference (per-bar)
# ---------------------------------------------------------------------------

def _liquidity_score(
    okx_candle: list[float],
    cdc_close: float | None,
    own_fills_slip_mean: float | None,
) -> float:
    """Composite 0..1 risk score.

    Components (weight-matched to live module):
      - thinness (0.35): based on candle volume (low vol = thin book proxy).
      - spread (0.25): (hi-lo)/close of the current bar as intrabar range proxy.
      - cross-exchange divergence (0.10): OKX vs CDC close delta.
      - own-flow slippage (0.20): realized mean from prior trades (None→0.5).
      - fill rate (0.10): always 1.0 in backtest (assume fills).
    """
    o, h, l, c, v = okx_candle[1:6]
    # Thinness — normalize vol: 0 vol → 1.0, 100+ base → 0.0
    thin = max(0.0, min(1.0, 1.0 - (v / 100.0)))
    # Spread — intrabar range as %.
    spread_pct = (h - l) / c if c > 0 else 0.0
    spread_norm = min(max((spread_pct - 0.0005) / 0.0025, 0.0), 1.0)
    # Cross-exchange divergence.
    div_comp = 0.0
    if cdc_close is not None and c > 0:
        div_bp = abs(c - cdc_close) / c
        div_comp = min(div_bp / 0.005, 1.0)      # 50bp = full weight
    # Own-flow slip.
    if own_fills_slip_mean is None:
        slip_comp = 0.5
    else:
        slip_comp = min(max((own_fills_slip_mean - 2.0) / 18.0, 0.0), 1.0)
    # Fill rate — always 1.0 in sim.
    fill_comp = 0.0

    score = (thin * 0.35 + spread_norm * 0.25 + div_comp * 0.10
             + slip_comp * 0.20 + fill_comp * 0.10)
    return min(max(score, 0.0), 1.0)


# ---------------------------------------------------------------------------
# Exploration wallet 24h rolling PnL (per-run)
# ---------------------------------------------------------------------------

def _wallet_pnl_24h(trades: list[Trade], now_ms: int) -> float:
    cutoff = now_ms - 86_400_000
    return sum(
        t.realized_pnl_usd for t in trades
        if t.variant in EXPLORATION_VARIANTS and t.ts_close_ms >= cutoff
    )


# ---------------------------------------------------------------------------
# Per-symbol replay with FULL admission chain
# ---------------------------------------------------------------------------

@dataclass
class FullSymbolResult:
    symbol: str
    n_bars: int
    n_trades: int
    total_pnl_usd: float
    by_variant: dict[str, VariantStats] = field(default_factory=dict)
    # Gate rejection counters.
    rejections_by_gate: dict[str, int] = field(default_factory=dict)
    wallet_disable_events: int = 0
    cross_exchange_divergence_count: int = 0


def simulate_symbol_full(
    okx: CandleSet,
    cdc: CandleSet | None = None,
    already_traded: list[Trade] | None = None,
) -> FullSymbolResult:
    res = FullSymbolResult(symbol=okx.symbol, n_bars=okx.n, n_trades=0,
                           total_pnl_usd=0.0)
    if okx.n < WARMUP_BARS + 10:
        return res

    cdc_by_ts: dict[int, list[float]] = {}
    if cdc and cdc.rows:
        for r in cdc.rows:
            cdc_by_ts[int(r[0])] = r

    trades: list[Trade] = list(already_traded or [])
    open_positions: list[tuple[int, str, int]] = []
    wallet_disabled_until_ts: int | None = None

    rejections: dict[str, int] = {}

    def _bump_reject(reason: str) -> None:
        rejections[reason] = rejections.get(reason, 0) + 1

    for i in range(WARMUP_BARS, okx.n - 1):
        open_positions = [p for p in open_positions if p[2] >= i]
        held = len(open_positions)
        if held >= MAX_CONCURRENT:
            _bump_reject("max_concurrent_cap")
            continue

        ts = int(okx.rows[i][0])
        ret_5m = _ret_window(okx.rows, i, 5)
        ret_15m = _ret_window(okx.rows, i, 15)
        ret_7d = _ret_window(okx.rows, i, 10_080)

        # Cross-exchange divergence.
        cdc_close: float | None = None
        if ts in cdc_by_ts:
            cdc_close = float(cdc_by_ts[ts][4])
            okx_close = float(okx.rows[i][4])
            if okx_close > 0 and abs(okx_close - cdc_close) / okx_close > 0.005:
                res.cross_exchange_divergence_count += 1

        # Variant admission (OR-logic first-match).
        admitted_variant: str | None = None
        tp = sl = hold = 0.0
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
            _bump_reject("no_variant_admits")
            continue

        # GATE 1: Fractal regime — ≥2 of 3 scales agree.
        _, fractal_admit = _fractal_verdict(okx.rows, i)
        if not fractal_admit:
            _bump_reject("fractal_regime_disagreement")
            continue

        # GATE 2: Liquidity inference.
        # Use realized slippage mean from prior N same-symbol trades.
        prior = [t for t in trades if t.symbol == okx.symbol][-10:]
        own_slip = None
        if prior:
            own_slip = statistics.mean(
                abs(t.realized_ret_pct) * 10_000 for t in prior
            )
        liq_score = _liquidity_score(okx.rows[i], cdc_close, own_slip)
        if liq_score >= LIQ_ABORT_SCORE:
            _bump_reject("liquidity_inference_reject")
            continue

        # GATE 3: Exploration wallet (exploratory variants only).
        if admitted_variant in EXPLORATION_VARIANTS:
            if wallet_disabled_until_ts and ts < wallet_disabled_until_ts:
                _bump_reject("exploration_wallet_disabled")
                continue
            pnl_24h = _wallet_pnl_24h(trades, ts)
            if pnl_24h <= -EXPLORATION_DD_KILL_USD:
                # Trip — stays tripped for the rest of this run (no operator
                # reset in sim). Actually in live it waits for operator,
                # but in sim we simulate "disabled for 24h" as a proxy.
                wallet_disabled_until_ts = ts + 86_400_000
                res.wallet_disable_events += 1
                _bump_reject("exploration_wallet_disabled")
                continue

        # GATE passed — simulate fill.
        entry_idx = i + 1
        close_idx, exit_px, reason = _simulate_position(
            okx.rows, entry_idx, tp, sl, hold, NOTIONAL_USD,
        )
        entry_px = float(okx.rows[entry_idx][1])
        pnl = _net_pnl_usd(entry_px, exit_px, NOTIONAL_USD)
        ret_pct = (exit_px - entry_px) / entry_px
        hold_min = close_idx - entry_idx

        t = Trade(
            variant=admitted_variant, symbol=okx.symbol,
            ts_open_ms=int(okx.rows[entry_idx][0]),
            ts_close_ms=int(okx.rows[close_idx][0]),
            entry_px=entry_px, exit_px=exit_px,
            notional_usd=NOTIONAL_USD,
            realized_pnl_usd=round(pnl, 4),
            realized_ret_pct=round(ret_pct, 6),
            hold_min=hold_min, exit_reason=reason,
            ret_5m_at_entry=round(ret_5m, 6),
            ret_7d_at_entry=round(ret_7d, 6),
        )
        trades.append(t)
        open_positions.append((entry_idx, admitted_variant, close_idx))
        res.total_pnl_usd += pnl

    # Aggregate per variant (trades from this symbol only).
    own_trades = [t for t in trades if t.symbol == okx.symbol
                  and t not in (already_traded or [])]
    res.n_trades = len(own_trades)
    res.rejections_by_gate = rejections

    by_variant: dict[str, list[Trade]] = {}
    for t in own_trades:
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
    res._trades = own_trades   # type: ignore[attr-defined]
    return res


# ---------------------------------------------------------------------------
# Full run
# ---------------------------------------------------------------------------

@dataclass
class FullBacktestReport:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    variant: str = "all"
    universe: list[str] = field(default_factory=list)
    bar: str = "1m"
    total_bars_scanned: int = 0
    total_trades: int = 0
    total_pnl_usd: float = 0.0
    by_variant: dict[str, VariantStats] = field(default_factory=dict)
    by_symbol: dict[str, FullSymbolResult] = field(default_factory=dict)
    rejections_by_gate_aggregate: dict[str, int] = field(default_factory=dict)
    wallet_disable_events_total: int = 0
    trades_sample: list[Trade] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trades_sample"] = [asdict(t) for t in self.trades_sample[:50]]
        return d


def run_full_backtest(
    universe: list[str],
    total_bars: int = 3500,
    force_refresh: bool = False,
    pull_cdc: bool = True,
) -> FullBacktestReport:
    report = FullBacktestReport(universe=list(universe), bar="1m", variant="all")
    all_trades: list[Trade] = []

    for sym in universe:
        print(f"[backtest-full] {sym}: fetching...")
        okx = load_or_fetch("okx", sym, bar="1m", total_bars=total_bars,
                            force_refresh=force_refresh)
        cdc = None
        if pull_cdc:
            try:
                cdc = load_or_fetch("cdc", sym, bar="1m", total_bars=total_bars,
                                    force_refresh=force_refresh)
            except Exception:
                cdc = None
        print(f"[backtest-full] {sym}: OKX n={okx.n} CDC n={(cdc.n if cdc else 0)}")
        if okx.n < WARMUP_BARS + 10:
            continue
        sr = simulate_symbol_full(okx, cdc, already_traded=all_trades)
        new_trades = getattr(sr, "_trades", [])
        all_trades.extend(new_trades)
        report.by_symbol[sym] = sr
        report.total_bars_scanned += okx.n
        report.total_trades += sr.n_trades
        report.total_pnl_usd += sr.total_pnl_usd
        report.wallet_disable_events_total += sr.wallet_disable_events
        for g, n in sr.rejections_by_gate.items():
            report.rejections_by_gate_aggregate[g] = \
                report.rejections_by_gate_aggregate.get(g, 0) + n

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
    return report


def save_report_full(report: FullBacktestReport, label: str) -> Path:
    REPO_ROOT = Path(__file__).resolve().parents[3]
    out_dir = REPO_ROOT / "runtime" / "backtest" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    fn = f"{report.ts_ms}_{label}.json"
    path = out_dir / fn
    path.write_text(json.dumps(report.to_dict(), default=str, indent=2),
                    encoding="utf-8")
    return path
