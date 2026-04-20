"""Opportunity Fabric delta backtest — 7-sprint stack on CDV strategy only,
with per-trade counterfactual causal-delta measurement.

Scope:
  - Universe: current live 12-symbol list.
  - Variants admitted: contrarian + deep_value only (CDV strategy).
    Momentum disabled here to isolate the CDV causal story.
  - Admission chain: full 7-sprint (same as simulator_full).
  - For every admitted trade, also simulate three counterfactual TP/SL
    policies against the SAME entry bar, record causal deltas.

Counterfactual policies (matches governance/counterfactual_replay.py):
    conservative : TP +1.5% / SL -1.0%
    exploratory  : TP +2.0% / SL -1.5%
    aggressive   : TP +3.0% / SL -2.0%

Output per variant:
  n trades, WR, total_pnl_usd,
  cf_conservative: mean_delta_bp, live_wins, cf_wins, ties
  cf_exploratory : (same)
  cf_aggressive  : (same)

Outlier-filter threshold matches the live hygiene patch
(SPOT_CFR_OUTLIER_PCT=10%): any trade with |realized_ret|>10% is
excluded from causal-delta stats (counts in n_outliers).
"""
from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .data_puller import CandleSet, load_or_fetch
from .simulator_quick import (
    SLIPPAGE_BP, FEE_BP_PER_SIDE, NOTIONAL_USD,
    MAX_CONCURRENT, WARMUP_BARS,
    CONTRARIAN_RET_5M_MAX, CONTRARIAN_RET_7D_MIN,
    CONTRARIAN_TP_PCT, CONTRARIAN_SL_PCT, CONTRARIAN_HOLD_MAX_MIN,
    DV_MIN_7D_RET, DV_MAX_7D_RET,
    DV_TP_PCT, DV_SL_PCT, DV_HOLD_MAX_MIN,
    Trade, VariantStats,
    _admit_contrarian, _admit_deep_value,
    _ret_window, _net_pnl_usd, _simulate_position,
)
from .simulator_full import (
    EXPLORATION_WALLET_USD, EXPLORATION_DD_KILL_USD,
    EXPLORATION_VARIANTS, LIQ_ABORT_SCORE,
    _fractal_verdict, _liquidity_score, _wallet_pnl_24h,
)


CFR_POLICY_TARGETS = {
    "conservative": (0.015, -0.010),
    "exploratory":  (0.020, -0.015),
    "aggressive":   (0.030, -0.020),
}
OUTLIER_RET_PCT = float(os.environ.get("SPOT_CFR_OUTLIER_PCT", "0.10"))


@dataclass
class DeltaTrade:
    variant: str
    symbol: str
    ts_open_ms: int
    entry_px: float
    notional_usd: float
    # Live policy outcome.
    live_exit_px: float
    live_pnl_usd: float
    live_ret_pct: float
    live_exit_reason: str
    live_hold_min: int
    # Counterfactual outcomes keyed by policy name.
    cf: dict[str, dict[str, Any]] = field(default_factory=dict)
    outlier: bool = False


@dataclass
class CausalDeltaStats:
    policy: str
    n: int
    mean_delta_bp: float
    live_wins: int
    cf_wins: int
    ties: int


@dataclass
class DeltaVariantReport:
    variant: str
    n_trades: int
    n_outliers: int
    win_rate: float
    total_pnl_usd: float
    mean_pnl_usd: float
    mean_hold_min: float
    exit_reasons: dict[str, int] = field(default_factory=dict)
    causal: dict[str, CausalDeltaStats] = field(default_factory=dict)


@dataclass
class DeltaReport:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    universe: list[str] = field(default_factory=list)
    bar: str = "1m"
    total_bars_scanned: int = 0
    total_trades: int = 0
    total_outliers: int = 0
    total_pnl_usd: float = 0.0
    by_variant: dict[str, DeltaVariantReport] = field(default_factory=dict)
    rejections_by_gate: dict[str, int] = field(default_factory=dict)
    wallet_disable_events: int = 0
    # Grand causal summary across both variants (excluding outliers).
    overall_causal: dict[str, CausalDeltaStats] = field(default_factory=dict)
    trades_sample: list[DeltaTrade] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trades_sample"] = [asdict(t) for t in self.trades_sample[:30]]
        return d


# ---------------------------------------------------------------------------
# Core per-bar replay
# ---------------------------------------------------------------------------

def _replay_counterfactuals(
    candles: list[list[float]],
    entry_idx: int,
    hold_max_min: int,
    notional_usd: float,
) -> dict[str, dict[str, Any]]:
    """For the same entry bar, simulate each CF policy's outcome."""
    out: dict[str, dict[str, Any]] = {}
    for name, (tp, sl) in CFR_POLICY_TARGETS.items():
        close_idx, exit_px, reason = _simulate_position(
            candles, entry_idx, tp, sl, hold_max_min, notional_usd,
        )
        entry_px = float(candles[entry_idx][1])
        cf_pnl = _net_pnl_usd(entry_px, exit_px, notional_usd)
        out[name] = {
            "tp_pct": tp, "sl_pct": sl,
            "exit_px": exit_px, "exit_reason": reason,
            "hold_min": close_idx - entry_idx,
            "pnl_usd": round(cf_pnl, 4),
        }
    return out


def _simulate_symbol_delta(
    okx: CandleSet,
    cdc: CandleSet | None,
    prior_trades: list[DeltaTrade],
) -> tuple[list[DeltaTrade], dict[str, int], int]:
    """Per-symbol walk-forward with full admission chain + CF replay."""
    if okx.n < WARMUP_BARS + 10:
        return [], {}, 0
    cdc_by_ts: dict[int, list[float]] = {}
    if cdc and cdc.rows:
        for r in cdc.rows:
            cdc_by_ts[int(r[0])] = r

    trades: list[DeltaTrade] = []
    rejections: dict[str, int] = {}
    wallet_disable_events = 0
    wallet_disabled_until_ts: int | None = None
    open_positions: list[tuple[int, str, int]] = []

    def _bump(r: str) -> None:
        rejections[r] = rejections.get(r, 0) + 1

    # Seed prior trades (for 24h wallet PnL computation across symbols).
    all_trades_ref = list(prior_trades)

    for i in range(WARMUP_BARS, okx.n - 1):
        open_positions = [p for p in open_positions if p[2] >= i]
        if len(open_positions) >= MAX_CONCURRENT:
            _bump("max_concurrent_cap")
            continue

        ts = int(okx.rows[i][0])
        ret_5m = _ret_window(okx.rows, i, 5)
        ret_7d = _ret_window(okx.rows, i, 10_080)

        cdc_close: float | None = None
        if ts in cdc_by_ts:
            cdc_close = float(cdc_by_ts[ts][4])

        # CDV admission only — NO momentum.
        admitted_variant: str | None = None
        tp = sl = hold = 0.0
        if _admit_contrarian(ret_5m, ret_7d):
            admitted_variant = "contrarian"
            tp, sl, hold = CONTRARIAN_TP_PCT, CONTRARIAN_SL_PCT, CONTRARIAN_HOLD_MAX_MIN
        elif _admit_deep_value(ret_7d):
            admitted_variant = "deep_value"
            tp, sl, hold = DV_TP_PCT, DV_SL_PCT, DV_HOLD_MAX_MIN
        else:
            _bump("no_cdv_admits")
            continue

        # GATE 1: fractal regime.
        _, fractal_ok = _fractal_verdict(okx.rows, i)
        if not fractal_ok:
            _bump("fractal_regime_disagreement")
            continue

        # GATE 2: liquidity inference.
        prior_sym = [t for t in trades if t.symbol == okx.symbol][-10:]
        own_slip = None
        if prior_sym:
            own_slip = statistics.mean(
                abs(t.live_ret_pct) * 10_000 for t in prior_sym
            )
        liq_score = _liquidity_score(okx.rows[i], cdc_close, own_slip)
        if liq_score >= LIQ_ABORT_SCORE:
            _bump("liquidity_inference_reject")
            continue

        # GATE 3: exploration wallet.
        if admitted_variant in EXPLORATION_VARIANTS:
            if wallet_disabled_until_ts and ts < wallet_disabled_until_ts:
                _bump("exploration_wallet_disabled")
                continue
            # Compute 24h rolling PnL from prior CDV trades in this run.
            cutoff_ms = ts - 86_400_000
            pnl_24h = sum(
                t.live_pnl_usd for t in all_trades_ref + trades
                if t.variant in EXPLORATION_VARIANTS
                and t.ts_open_ms >= cutoff_ms
            )
            if pnl_24h <= -EXPLORATION_DD_KILL_USD:
                wallet_disabled_until_ts = ts + 86_400_000
                wallet_disable_events += 1
                _bump("exploration_wallet_disabled")
                continue

        # GATES PASSED — run live policy fill + CF replays on SAME entry_idx.
        entry_idx = i + 1
        close_idx, live_exit_px, live_reason = _simulate_position(
            okx.rows, entry_idx, tp, sl, hold, NOTIONAL_USD,
        )
        entry_px = float(okx.rows[entry_idx][1])
        live_pnl = _net_pnl_usd(entry_px, live_exit_px, NOTIONAL_USD)
        live_ret = (live_exit_px - entry_px) / entry_px if entry_px else 0.0
        live_hold = close_idx - entry_idx

        cf_outcomes = _replay_counterfactuals(okx.rows, entry_idx, hold, NOTIONAL_USD)

        # Outlier flag (matches live hygiene rule).
        outlier = abs(live_ret) > OUTLIER_RET_PCT

        t = DeltaTrade(
            variant=admitted_variant, symbol=okx.symbol,
            ts_open_ms=int(okx.rows[entry_idx][0]),
            entry_px=entry_px, notional_usd=NOTIONAL_USD,
            live_exit_px=live_exit_px, live_pnl_usd=round(live_pnl, 4),
            live_ret_pct=round(live_ret, 6),
            live_exit_reason=live_reason, live_hold_min=live_hold,
            cf=cf_outcomes, outlier=outlier,
        )
        trades.append(t)
        open_positions.append((entry_idx, admitted_variant, close_idx))

    return trades, rejections, wallet_disable_events


# ---------------------------------------------------------------------------
# Causal stat aggregation
# ---------------------------------------------------------------------------

def _aggregate_causal(
    trades: list[DeltaTrade], policy: str,
) -> CausalDeltaStats:
    deltas = []
    wins = cf_wins = ties = 0
    for t in trades:
        if t.outlier:
            continue
        cf_pnl = (t.cf.get(policy) or {}).get("pnl_usd")
        if cf_pnl is None:
            continue
        delta_bp = ((t.live_pnl_usd - cf_pnl) / t.notional_usd) * 10_000
        deltas.append(delta_bp)
        if delta_bp > 0: wins += 1
        elif delta_bp < 0: cf_wins += 1
        else: ties += 1
    mean = sum(deltas) / len(deltas) if deltas else 0.0
    return CausalDeltaStats(
        policy=policy, n=len(deltas),
        mean_delta_bp=round(mean, 2),
        live_wins=wins, cf_wins=cf_wins, ties=ties,
    )


def _build_variant_report(
    variant: str, trades: list[DeltaTrade],
) -> DeltaVariantReport:
    v_trades = [t for t in trades if t.variant == variant]
    if not v_trades:
        return DeltaVariantReport(
            variant=variant, n_trades=0, n_outliers=0,
            win_rate=0.0, total_pnl_usd=0.0, mean_pnl_usd=0.0,
            mean_hold_min=0.0,
        )
    n_out = sum(1 for t in v_trades if t.outlier)
    clean = [t for t in v_trades if not t.outlier]
    pnls = [t.live_pnl_usd for t in clean]
    holds = [t.live_hold_min for t in clean]
    wins = sum(1 for p in pnls if p > 0)
    reasons: dict[str, int] = {}
    for t in clean:
        reasons[t.live_exit_reason] = reasons.get(t.live_exit_reason, 0) + 1
    causal: dict[str, CausalDeltaStats] = {}
    for policy in CFR_POLICY_TARGETS.keys():
        causal[policy] = _aggregate_causal(v_trades, policy)
    return DeltaVariantReport(
        variant=variant, n_trades=len(v_trades), n_outliers=n_out,
        win_rate=round(wins / max(len(pnls), 1), 4),
        total_pnl_usd=round(sum(pnls), 2),
        mean_pnl_usd=round(sum(pnls) / max(len(pnls), 1), 4),
        mean_hold_min=round(statistics.mean(holds), 1) if holds else 0.0,
        exit_reasons=reasons,
        causal=causal,
    )


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------

def run_delta_backtest(
    universe: list[str],
    total_bars: int = 3500,
    force_refresh: bool = False,
    pull_cdc: bool = True,
) -> DeltaReport:
    report = DeltaReport(universe=list(universe), bar="1m")
    all_trades: list[DeltaTrade] = []

    for sym in universe:
        okx = load_or_fetch("okx", sym, bar="1m", total_bars=total_bars,
                            force_refresh=force_refresh)
        cdc = None
        if pull_cdc:
            try:
                cdc = load_or_fetch("cdc", sym, bar="1m",
                                    total_bars=total_bars,
                                    force_refresh=force_refresh)
            except Exception:
                cdc = None
        print(f"[delta] {sym}: OKX n={okx.n} CDC n={(cdc.n if cdc else 0)}")
        if okx.n < WARMUP_BARS + 10:
            continue
        new_trades, rejs, wallet_events = _simulate_symbol_delta(okx, cdc, all_trades)
        all_trades.extend(new_trades)
        report.total_bars_scanned += okx.n
        report.wallet_disable_events += wallet_events
        for g, n in rejs.items():
            report.rejections_by_gate[g] = report.rejections_by_gate.get(g, 0) + n

    report.total_trades = len(all_trades)
    report.total_outliers = sum(1 for t in all_trades if t.outlier)
    # Exclude outliers from total_pnl (matches live hygiene rule).
    report.total_pnl_usd = round(
        sum(t.live_pnl_usd for t in all_trades if not t.outlier), 2,
    )
    for variant in ("contrarian", "deep_value"):
        report.by_variant[variant] = _build_variant_report(variant, all_trades)
    # Overall causal (both CDV variants combined).
    for policy in CFR_POLICY_TARGETS.keys():
        report.overall_causal[policy] = _aggregate_causal(all_trades, policy)
    report.trades_sample = all_trades[:30]
    return report


def save_delta_report(report: DeltaReport, label: str) -> Path:
    REPO_ROOT = Path(__file__).resolve().parents[3]
    out_dir = REPO_ROOT / "runtime" / "backtest" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    fn = f"{report.ts_ms}_{label}.json"
    path = out_dir / fn
    path.write_text(json.dumps(report.to_dict(), default=str, indent=2),
                    encoding="utf-8")
    return path
