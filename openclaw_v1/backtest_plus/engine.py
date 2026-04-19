"""
Backtest engine — orchestrates: scenarios → composer → overlays →
strategy → fill simulator → accounting → metrics.

Deterministic given (compose seed, overlay seed). Wall-clock is never
read inside the loop. Capital and timeline are validated against the
required boundaries:
    capital ∈ [$1, $10_000_000]
    timeline_days ∈ [1, 1095]   # 3 years
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .composer import ComposeSpec, ComposedSeries, ScenarioPick, compose
from .overlays import OverlayConfig, apply_overlays, evaluate_fill
from .scenarios import SCENARIOS, SEC_PER_BAR_DEFAULT, bars_for_duration
from .strategy import StrategyAdapter, SimpleStrategy, BarState, Decision
from .metrics import summary_metrics, drawdown_series, scenario_attribution

log = logging.getLogger("backtest_plus.engine")

CAPITAL_MIN = 1.0
CAPITAL_MAX = 10_000_000.0
DAYS_MIN = 1.0
DAYS_MAX = 1095.0
DEFAULT_FEE_BPS = 4.0          # 0.04% per side (taker baseline)
DEFAULT_SLIPPAGE_BPS = 2.0


@dataclass
class CostModel:
    fee_bps_per_side: float = DEFAULT_FEE_BPS
    base_slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    funding_bps_8h: float = 0.0   # carried in addition to scenario funding
    leverage: float = 1.0


@dataclass
class BacktestConfig:
    capital: float
    days: float
    picks: list[ScenarioPick]
    mode: str = "single"
    sec_per_bar: int = SEC_PER_BAR_DEFAULT
    seed: int = 42
    cost: CostModel = field(default_factory=CostModel)
    overlays: OverlayConfig = field(default_factory=OverlayConfig)
    strategy: Optional[StrategyAdapter] = None
    start_price: float = 50_000.0
    use_gemini: bool = False
    label: str = ""

    def validate(self) -> None:
        if not (CAPITAL_MIN <= self.capital <= CAPITAL_MAX):
            raise ValueError(f"capital must be in [{CAPITAL_MIN}, {CAPITAL_MAX}]")
        if not (DAYS_MIN <= self.days <= DAYS_MAX):
            raise ValueError(f"days must be in [{DAYS_MIN}, {DAYS_MAX}]")
        if not self.picks:
            raise ValueError("at least one scenario pick required")
        for p in self.picks:
            if p.code not in SCENARIOS:
                raise ValueError(f"unknown scenario: {p.code}")
        if self.cost.leverage < 0.1 or self.cost.leverage > 50.0:
            raise ValueError("leverage must be in [0.1, 50]")


@dataclass
class Trade:
    bar_idx_open: int
    bar_idx_close: int
    side: str
    qty: float                     # base units
    entry: float
    exit: float
    pnl: float
    fee_paid: float
    slippage_paid: float
    funding_paid: float
    reason: str                    # "TP" | "SL" | "EOD" | "REVERSE" | "EOB"


@dataclass
class DecisionEvent:
    bar_idx: int
    ts: int
    side: str                 # "LONG" | "SHORT" | "FLAT"
    size_frac: float
    stop_pct: float
    take_pct: float
    confidence: float
    action: str               # "queue_open" | "queue_reverse" | "hold" | "idle"
    pos_side_before: str


@dataclass
class FillEvent:
    bar_idx: int
    ts: int
    kind: str                 # "OPEN" | "CLOSE"
    side: str                 # position side after OPEN; position side being closed for CLOSE
    price_ref: float
    fill_price: float
    notional: float
    qty: float
    fee: float
    slippage_bps: float
    partial_qty_frac: float
    rejected: bool
    reason: str               # "fill" | "no_fill" | "partial" | "TP" | "SL" | "REVERSE" | "EOB"


@dataclass
class RunResult:
    config: dict
    timeline: list[dict]
    equity_curve: list[float]
    drawdown_curve: list[float]
    trades: list[dict]
    fills: list[dict]
    decisions: list[dict]
    attribution: list[dict]
    summary: dict
    assumptions: dict
    seed: int
    ai_commentary: dict | None = None
    started_ts: int = 0
    finished_ts: int = 0


def _build_compose_spec(cfg: BacktestConfig) -> ComposeSpec:
    n_bars = bars_for_duration(cfg.days, cfg.sec_per_bar)
    return ComposeSpec(
        mode=cfg.mode,
        picks=cfg.picks,
        total_bars=n_bars,
        seed=cfg.seed,
        sec_per_bar=cfg.sec_per_bar,
        start_price=cfg.start_price,
    )


def run_backtest(cfg: BacktestConfig) -> RunResult:
    cfg.validate()
    started = int(time.time())
    series = compose(_build_compose_spec(cfg))
    bars = apply_overlays(series.bars, cfg.overlays, seed=cfg.seed)
    strategy = cfg.strategy or SimpleStrategy()
    rng = random.Random(f"{cfg.seed}:fills")

    # Map scenario funding (bps/8h) per bar by timeline segment
    bars_per_8h = max(1, int(8 * 3600 / cfg.sec_per_bar))
    scenario_funding_bps_per_bar = [0.0] * len(bars)
    for seg in series.timeline:
        sp = SCENARIOS[seg["code"]]
        per_bar = sp.funding_bps_8h / bars_per_8h
        for i in range(seg["start_idx"], seg["end_idx"] + 1):
            scenario_funding_bps_per_bar[i] = per_bar
    overlay_funding_per_bar = (cfg.overlays.funding_drag_bps_8h + cfg.cost.funding_bps_8h) / bars_per_8h

    capital = cfg.capital
    equity_curve: list[float] = [capital]
    trades: list[Trade] = []
    fills: list[FillEvent] = []
    decisions: list[DecisionEvent] = []

    # Position state
    pos_side = "FLAT"
    pos_qty = 0.0
    pos_entry = 0.0
    pos_stop = 0.0
    pos_take = 0.0
    pos_open_idx = -1
    pos_fee = 0.0
    pos_slip = 0.0
    pos_funding = 0.0

    pending_signal: Optional[tuple[int, Decision]] = None

    def _open_position(idx: int, decision: Decision, price_ref: float, ts: int) -> None:
        nonlocal pos_side, pos_qty, pos_entry, pos_stop, pos_take, pos_open_idx
        nonlocal pos_fee, pos_slip, pos_funding, capital
        outcome = evaluate_fill(cfg.overlays, rng)
        if not outcome.filled:
            fills.append(FillEvent(
                bar_idx=idx, ts=ts, kind="OPEN", side=decision.side,
                price_ref=price_ref, fill_price=0.0, notional=0.0, qty=0.0,
                fee=0.0, slippage_bps=cfg.cost.base_slippage_bps,
                partial_qty_frac=0.0, rejected=True, reason="no_fill",
            ))
            return
        slip_bps = cfg.cost.base_slippage_bps + outcome.extra_slippage_bps
        slip_frac = slip_bps / 1e4
        if decision.side == "LONG":
            entry = price_ref * (1.0 + slip_frac)
        else:
            entry = price_ref * (1.0 - slip_frac)
        notional = capital * decision.size_frac * cfg.cost.leverage
        notional *= outcome.fill_qty_frac
        if notional <= 0:
            fills.append(FillEvent(
                bar_idx=idx, ts=ts, kind="OPEN", side=decision.side,
                price_ref=price_ref, fill_price=entry, notional=0.0, qty=0.0,
                fee=0.0, slippage_bps=slip_bps, partial_qty_frac=outcome.fill_qty_frac,
                rejected=True, reason="zero_notional",
            ))
            return
        qty = notional / max(entry, 1e-9)
        fee = notional * cfg.cost.fee_bps_per_side / 1e4
        slip_cost = notional * slip_frac
        pos_side = decision.side
        pos_qty = qty
        pos_entry = entry
        pos_open_idx = idx
        pos_fee = fee
        pos_slip = slip_cost
        pos_funding = 0.0
        if decision.side == "LONG":
            pos_stop = entry * (1.0 - decision.stop_pct * outcome.stop_slip_mult)
            pos_take = entry * (1.0 + decision.take_pct)
        else:
            pos_stop = entry * (1.0 + decision.stop_pct * outcome.stop_slip_mult)
            pos_take = entry * (1.0 - decision.take_pct)
        capital -= fee  # fee debited at entry; slippage embedded in entry price
        fills.append(FillEvent(
            bar_idx=idx, ts=ts, kind="OPEN", side=decision.side,
            price_ref=price_ref, fill_price=entry, notional=notional, qty=qty,
            fee=fee, slippage_bps=slip_bps, partial_qty_frac=outcome.fill_qty_frac,
            rejected=False,
            reason="partial" if outcome.fill_qty_frac < 1.0 else "fill",
        ))

    def _close_position(idx: int, exit_price: float, reason: str, ts: int = 0) -> None:
        nonlocal pos_side, pos_qty, pos_entry, pos_open_idx
        nonlocal pos_fee, pos_slip, pos_funding, capital
        if pos_side == "FLAT" or pos_qty <= 0:
            return
        # exit-side fee
        exit_notional = pos_qty * exit_price
        fee_exit = exit_notional * cfg.cost.fee_bps_per_side / 1e4
        if pos_side == "LONG":
            gross = (exit_price - pos_entry) * pos_qty
        else:
            gross = (pos_entry - exit_price) * pos_qty
        pnl = gross - fee_exit - pos_funding
        capital += pnl
        fills.append(FillEvent(
            bar_idx=idx, ts=ts, kind="CLOSE", side=pos_side,
            price_ref=exit_price, fill_price=exit_price,
            notional=exit_notional, qty=pos_qty,
            fee=fee_exit, slippage_bps=0.0,
            partial_qty_frac=1.0, rejected=False, reason=reason,
        ))
        trades.append(Trade(
            bar_idx_open=pos_open_idx,
            bar_idx_close=idx,
            side=pos_side,
            qty=pos_qty,
            entry=pos_entry,
            exit=exit_price,
            pnl=pnl,
            fee_paid=pos_fee + fee_exit,
            slippage_paid=pos_slip,
            funding_paid=pos_funding,
            reason=reason,
        ))
        pos_side = "FLAT"
        pos_qty = 0.0
        pos_entry = 0.0
        pos_fee = 0.0
        pos_slip = 0.0
        pos_funding = 0.0

    # Main loop
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    for i, bar in enumerate(bars):
        closes.append(bar.close)
        highs.append(bar.high)
        lows.append(bar.low)

        # 1. Resolve open position against this bar's range
        if pos_side != "FLAT":
            # Funding charge
            funding_bps_total = scenario_funding_bps_per_bar[i] + overlay_funding_per_bar
            funding_charge = pos_qty * pos_entry * (funding_bps_total / 1e4)
            # Long pays positive funding; short pays negative funding
            sign = 1.0 if pos_side == "LONG" else -1.0
            pos_funding += sign * funding_charge

            hit_stop = (pos_side == "LONG" and bar.low <= pos_stop) or \
                       (pos_side == "SHORT" and bar.high >= pos_stop)
            hit_take = (pos_side == "LONG" and bar.high >= pos_take) or \
                       (pos_side == "SHORT" and bar.low <= pos_take)
            if hit_stop and hit_take:
                # Conservative: assume stop fills first
                _close_position(i, pos_stop, "SL", ts=bar.ts)
            elif hit_stop:
                _close_position(i, pos_stop, "SL", ts=bar.ts)
            elif hit_take:
                _close_position(i, pos_take, "TP", ts=bar.ts)

        # 2. Fire any latency-deferred signal (executes at this bar's open)
        if pending_signal is not None:
            sig_bar, sig = pending_signal
            if i >= sig_bar + max(0, cfg.overlays.latency_bars):
                if pos_side != "FLAT" and sig.side != pos_side and sig.side != "FLAT":
                    _close_position(i, bar.open, "REVERSE", ts=bar.ts)
                if pos_side == "FLAT" and sig.side != "FLAT":
                    _open_position(i, sig, bar.open, ts=bar.ts)
                pending_signal = None

        # 3. Generate new signal from strategy on bar close
        state = BarState(closes=closes, highs=highs, lows=lows, bar_idx=i, ts=bar.ts)
        decision = strategy.decide(state)
        action = "idle"
        if decision.side != "FLAT" and pos_side == "FLAT":
            pending_signal = (i, decision)       # apply on next bar's open
            action = "queue_open"
        elif decision.side != "FLAT" and pos_side != "FLAT" and decision.side != pos_side:
            pending_signal = (i, decision)
            action = "queue_reverse"
        elif pos_side != "FLAT":
            action = "hold"
        if decision.side != "FLAT" or action == "hold":
            decisions.append(DecisionEvent(
                bar_idx=i, ts=bar.ts,
                side=decision.side, size_frac=decision.size_frac,
                stop_pct=decision.stop_pct, take_pct=decision.take_pct,
                confidence=decision.confidence, action=action,
                pos_side_before=pos_side,
            ))

        # 4. Mark-to-market equity
        if pos_side == "LONG":
            unrealized = (bar.close - pos_entry) * pos_qty - pos_funding
        elif pos_side == "SHORT":
            unrealized = (pos_entry - bar.close) * pos_qty - pos_funding
        else:
            unrealized = 0.0
        equity_curve.append(capital + unrealized)

    # Force-close any open position at end-of-backtest
    if pos_side != "FLAT" and bars:
        _close_position(len(bars) - 1, bars[-1].close, "EOB", ts=bars[-1].ts)
        equity_curve[-1] = capital

    finished = int(time.time())
    trades_j = [asdict(t) for t in trades]
    summary = summary_metrics(
        equity_curve=equity_curve,
        trades=trades_j,
        days=cfg.days,
        starting_capital=cfg.capital,
        sec_per_bar=cfg.sec_per_bar,
    )
    dd_curve = drawdown_series(equity_curve)
    attribution = scenario_attribution(
        equity_curve=equity_curve,
        trades=trades_j,
        timeline=series.timeline,
    )
    assumptions = {
        "capital_start": cfg.capital,
        "days": cfg.days,
        "sec_per_bar": cfg.sec_per_bar,
        "n_bars": len(bars),
        "fee_bps_per_side": cfg.cost.fee_bps_per_side,
        "base_slippage_bps": cfg.cost.base_slippage_bps,
        "leverage": cfg.cost.leverage,
        "overlays": asdict(cfg.overlays),
        "scenario_funding_resolved": True,
        "fills": "next-bar-open with latency offset (bars)",
        "stop_fill": "conservative (stop priority over take when both hit in same bar)",
    }
    return RunResult(
        config={
            "label": cfg.label,
            "capital": cfg.capital,
            "days": cfg.days,
            "mode": cfg.mode,
            "picks": [{"code": p.code, "weight": p.weight} for p in cfg.picks],
            "seed": cfg.seed,
            "use_gemini": cfg.use_gemini,
            "cost": asdict(cfg.cost),
            "overlays": asdict(cfg.overlays),
        },
        timeline=series.timeline,
        equity_curve=equity_curve,
        drawdown_curve=dd_curve,
        trades=trades_j,
        fills=[asdict(f) for f in fills],
        decisions=[asdict(d) for d in decisions],
        attribution=attribution,
        summary=summary,
        assumptions=assumptions,
        seed=cfg.seed,
        started_ts=started,
        finished_ts=finished,
    )
