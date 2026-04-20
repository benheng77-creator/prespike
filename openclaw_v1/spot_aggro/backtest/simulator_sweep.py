"""Opportunity Fabric parameter sweep.

Runs three experiments in parallel on the same cached OKX+CDC data
as simulator_delta, all going through the full 7-sprint admission chain:

  EXP-1: TP sweep on contrarian — test whether tighter TP alone
         flips expectancy positive. TP ∈ {0.3%, 0.5%, 0.7%, 1.0%}
         holding SL at -1.0% and hold at 45min.

  EXP-2: Tightened contrarian admission
         ret_5m <= -0.8% AND ret_7d >= +1% (was -0.3% / -2%).
         Same TP (1.5%) / SL (-1%) / 45m.

  EXP-3: Breakout variant (swap for contrarian)
         ret_5m >= +0.5% AND ret_1h > 0.
         Same TP / SL / hold as momentum (2.0% / -1.2% / 60m).

Each experiment prints a single summary line per configuration so
the best/worst are immediately visible. Full results persisted.
"""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .data_puller import CandleSet, load_or_fetch
from .simulator_quick import (
    SLIPPAGE_BP, FEE_BP_PER_SIDE, NOTIONAL_USD,
    MAX_CONCURRENT, WARMUP_BARS,
    DV_MIN_7D_RET, DV_MAX_7D_RET, DV_TP_PCT, DV_SL_PCT, DV_HOLD_MAX_MIN,
    Trade,
    _admit_deep_value, _ret_window, _net_pnl_usd, _simulate_position,
)
from .simulator_full import (
    EXPLORATION_DD_KILL_USD, EXPLORATION_VARIANTS, LIQ_ABORT_SCORE,
    _fractal_verdict, _liquidity_score,
)


# ---------------------------------------------------------------------------
# Admission signal variants (pluggable)
# ---------------------------------------------------------------------------

def admit_default_contrarian(ret_5m: float, ret_7d: float,
                             ret_1h: float = 0.0) -> bool:
    return ret_5m <= -0.003 and ret_7d >= -0.02


def admit_tight_contrarian(ret_5m: float, ret_7d: float,
                           ret_1h: float = 0.0) -> bool:
    """EXP-2: stronger pullback, positive weekly drift."""
    return ret_5m <= -0.008 and ret_7d >= 0.01


def admit_breakout(ret_5m: float, ret_7d: float,
                   ret_1h: float = 0.0) -> bool:
    """EXP-3: momentum-style breakout."""
    return ret_5m >= 0.005 and ret_1h > 0


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class SweepConfig:
    label: str
    admission_fn: Callable[[float, float, float], bool]
    tp_pct: float
    sl_pct: float
    hold_min: int
    # Which variant name to log against (drives wallet routing semantics).
    variant_name: str = "contrarian"


def _build_configs() -> list[SweepConfig]:
    out: list[SweepConfig] = []

    # EXP-1: TP sweep on default contrarian.
    for tp in (0.003, 0.005, 0.007, 0.010):
        out.append(SweepConfig(
            label=f"exp1_contrarian_tp{int(tp*1000):03d}",
            admission_fn=admit_default_contrarian,
            tp_pct=tp, sl_pct=-0.010, hold_min=45,
            variant_name="contrarian",
        ))

    # EXP-2: tightened contrarian admission, original TP/SL.
    out.append(SweepConfig(
        label="exp2_tight_contrarian",
        admission_fn=admit_tight_contrarian,
        tp_pct=0.015, sl_pct=-0.010, hold_min=45,
        variant_name="contrarian",
    ))
    # Also try tightened admission WITH TP=0.5% (EXP-1+2 combo).
    out.append(SweepConfig(
        label="exp2_tight_contrarian_tp005",
        admission_fn=admit_tight_contrarian,
        tp_pct=0.005, sl_pct=-0.010, hold_min=45,
        variant_name="contrarian",
    ))

    # EXP-3: breakout variant.
    out.append(SweepConfig(
        label="exp3_breakout",
        admission_fn=admit_breakout,
        tp_pct=0.020, sl_pct=-0.012, hold_min=60,
        variant_name="momentum",        # routed through conservative policy tier
    ))
    # Breakout + tight TP.
    out.append(SweepConfig(
        label="exp3_breakout_tp007",
        admission_fn=admit_breakout,
        tp_pct=0.007, sl_pct=-0.012, hold_min=60,
        variant_name="momentum",
    ))

    return out


# ---------------------------------------------------------------------------
# Per-symbol replay for a single config
# ---------------------------------------------------------------------------

@dataclass
class SweepResult:
    label: str
    tp_pct: float
    sl_pct: float
    hold_min: int
    n_trades: int = 0
    n_wins: int = 0
    n_losses: int = 0
    win_rate: float = 0.0
    total_pnl_usd: float = 0.0
    mean_pnl_usd: float = 0.0
    mean_ret_bp: float = 0.0
    mean_hold_min: float = 0.0
    exit_reasons: dict[str, int] = field(default_factory=dict)
    rejections_by_gate: dict[str, int] = field(default_factory=dict)
    wallet_disable_events: int = 0


def _run_one_config(
    cfg: SweepConfig,
    all_candles: dict[str, tuple[CandleSet, CandleSet | None]],
) -> SweepResult:
    res = SweepResult(label=cfg.label, tp_pct=cfg.tp_pct,
                      sl_pct=cfg.sl_pct, hold_min=cfg.hold_min)
    all_trades: list[Trade] = []
    rejections: dict[str, int] = {}

    def _bump(r: str) -> None:
        rejections[r] = rejections.get(r, 0) + 1

    for sym, (okx, cdc) in all_candles.items():
        if okx.n < WARMUP_BARS + 10:
            continue
        cdc_by_ts: dict[int, list[float]] = {}
        if cdc and cdc.rows:
            for r in cdc.rows:
                cdc_by_ts[int(r[0])] = r

        open_positions: list[tuple[int, str, int]] = []
        wallet_disabled_until_ts: int | None = None

        for i in range(WARMUP_BARS, okx.n - 1):
            open_positions = [p for p in open_positions if p[2] >= i]
            if len(open_positions) >= MAX_CONCURRENT:
                _bump("max_concurrent_cap")
                continue

            ts = int(okx.rows[i][0])
            ret_5m = _ret_window(okx.rows, i, 5)
            ret_1h = _ret_window(okx.rows, i, 60)
            ret_7d = _ret_window(okx.rows, i, 10_080)

            # Primary variant admission per config's rule.
            admitted = cfg.admission_fn(ret_5m, ret_7d, ret_1h)
            variant_name = cfg.variant_name
            # Also give deep_value a chance (same default rule).
            if not admitted and _admit_deep_value(ret_7d):
                admitted = True
                variant_name = "deep_value"
                # Deep value keeps its own TP/SL.
                tp_local = DV_TP_PCT; sl_local = DV_SL_PCT; hold_local = DV_HOLD_MAX_MIN
            else:
                tp_local = cfg.tp_pct; sl_local = cfg.sl_pct; hold_local = cfg.hold_min

            if not admitted:
                _bump("no_variant_admits")
                continue

            # GATE 1: fractal regime.
            _, fractal_ok = _fractal_verdict(okx.rows, i)
            if not fractal_ok:
                _bump("fractal_regime_disagreement")
                continue

            # GATE 2: liquidity inference.
            cdc_close = (float(cdc_by_ts[ts][4])
                         if ts in cdc_by_ts else None)
            prior_sym = [t for t in all_trades if t.symbol == sym][-10:]
            own_slip = None
            if prior_sym:
                own_slip = statistics.mean(
                    abs(t.realized_ret_pct) * 10_000 for t in prior_sym
                )
            liq_score = _liquidity_score(okx.rows[i], cdc_close, own_slip)
            if liq_score >= LIQ_ABORT_SCORE:
                _bump("liquidity_inference_reject")
                continue

            # GATE 3: exploration wallet (for exploratory variants).
            if variant_name in EXPLORATION_VARIANTS:
                if wallet_disabled_until_ts and ts < wallet_disabled_until_ts:
                    _bump("exploration_wallet_disabled")
                    continue
                cutoff_ms = ts - 86_400_000
                pnl_24h = sum(
                    t.realized_pnl_usd for t in all_trades
                    if t.variant in EXPLORATION_VARIANTS
                    and t.ts_open_ms >= cutoff_ms
                )
                if pnl_24h <= -EXPLORATION_DD_KILL_USD:
                    wallet_disabled_until_ts = ts + 86_400_000
                    res.wallet_disable_events += 1
                    _bump("exploration_wallet_disabled")
                    continue

            # Fill simulation.
            entry_idx = i + 1
            close_idx, exit_px, reason = _simulate_position(
                okx.rows, entry_idx, tp_local, sl_local, hold_local, NOTIONAL_USD,
            )
            entry_px = float(okx.rows[entry_idx][1])
            pnl = _net_pnl_usd(entry_px, exit_px, NOTIONAL_USD)
            ret_pct = (exit_px - entry_px) / entry_px if entry_px else 0.0

            all_trades.append(Trade(
                variant=variant_name, symbol=sym,
                ts_open_ms=int(okx.rows[entry_idx][0]),
                ts_close_ms=int(okx.rows[close_idx][0]),
                entry_px=entry_px, exit_px=exit_px,
                notional_usd=NOTIONAL_USD,
                realized_pnl_usd=round(pnl, 4),
                realized_ret_pct=round(ret_pct, 6),
                hold_min=close_idx - entry_idx,
                exit_reason=reason,
                ret_5m_at_entry=round(ret_5m, 6),
                ret_7d_at_entry=round(ret_7d, 6),
            ))
            open_positions.append((entry_idx, variant_name, close_idx))

    # Aggregate.
    res.n_trades = len(all_trades)
    pnls = [t.realized_pnl_usd for t in all_trades]
    rets = [t.realized_ret_pct for t in all_trades]
    holds = [t.hold_min for t in all_trades]
    res.n_wins = sum(1 for p in pnls if p > 0)
    res.n_losses = sum(1 for p in pnls if p < 0)
    res.win_rate = round(res.n_wins / max(res.n_trades, 1), 4)
    res.total_pnl_usd = round(sum(pnls), 2)
    res.mean_pnl_usd = round(sum(pnls) / max(res.n_trades, 1), 4)
    res.mean_ret_bp = round(
        (sum(rets) / max(res.n_trades, 1)) * 10_000, 2,
    )
    res.mean_hold_min = round(statistics.mean(holds), 1) if holds else 0.0
    for t in all_trades:
        res.exit_reasons[t.exit_reason] = res.exit_reasons.get(t.exit_reason, 0) + 1
    res.rejections_by_gate = rejections
    return res


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------

@dataclass
class SweepReport:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    universe: list[str] = field(default_factory=list)
    total_bars_scanned: int = 0
    results: list[SweepResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_sweep(
    universe: list[str], total_bars: int = 3500,
    force_refresh: bool = False, pull_cdc: bool = True,
) -> SweepReport:
    report = SweepReport(universe=list(universe))
    # Load all candles once.
    all_candles: dict[str, tuple[CandleSet, CandleSet | None]] = {}
    for sym in universe:
        okx = load_or_fetch("okx", sym, bar="1m", total_bars=total_bars,
                            force_refresh=force_refresh)
        cdc = None
        if pull_cdc:
            try:
                cdc = load_or_fetch("cdc", sym, bar="1m", total_bars=total_bars,
                                    force_refresh=force_refresh)
            except Exception:
                cdc = None
        all_candles[sym] = (okx, cdc)
        report.total_bars_scanned += okx.n
        print(f"[sweep] loaded {sym}: OKX n={okx.n} CDC n={(cdc.n if cdc else 0)}")

    configs = _build_configs()
    for cfg in configs:
        print(f"[sweep] running {cfg.label}...")
        res = _run_one_config(cfg, all_candles)
        report.results.append(res)
        print(
            f"    n={res.n_trades:4d}  WR={res.win_rate*100:5.1f}%  "
            f"mean_bp={res.mean_ret_bp:+7.2f}  total=${res.total_pnl_usd:+6.2f}  "
            f"TPs={res.exit_reasons.get('TP', 0)}  "
            f"SLs={res.exit_reasons.get('SL', 0)}  "
            f"TS={res.exit_reasons.get('TIME_STOP', 0)}"
        )

    return report


def save_sweep_report(report: SweepReport, label: str) -> Path:
    REPO_ROOT = Path(__file__).resolve().parents[3]
    out_dir = REPO_ROOT / "runtime" / "backtest" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    fn = f"{report.ts_ms}_{label}.json"
    path = out_dir / fn
    path.write_text(json.dumps(report.to_dict(), default=str, indent=2),
                    encoding="utf-8")
    return path
