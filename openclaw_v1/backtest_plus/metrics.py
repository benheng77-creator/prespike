"""
Summary metrics — deterministic, no external deps.

Computes the standard institutional set: net/gross PnL, CAGR, Sharpe,
Sortino, Calmar, max DD, win rate, expectancy, profit factor, exposure,
turnover, fee/slippage/funding totals, MAE/MFE proxies.
"""

from __future__ import annotations

import math
from typing import Any


def drawdown_series(equity_curve: list[float]) -> list[float]:
    """Per-bar drawdown as a fraction of running peak (>= 0)."""
    out: list[float] = []
    if not equity_curve:
        return out
    peak = equity_curve[0]
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = 0.0 if peak <= 0 else (peak - v) / peak
        out.append(dd)
    return out


def scenario_attribution(
    *,
    equity_curve: list[float],
    trades: list[dict],
    timeline: list[dict],
) -> list[dict]:
    """Attribute PnL / trade counts / drawdown to each scenario segment.

    A trade is attributed to the segment containing its close bar.
    """
    buckets: dict[str, dict[str, Any]] = {}
    for seg in timeline:
        buckets[seg["code"] + f"@{seg['start_idx']}"] = {
            "code": seg["code"],
            "name": seg.get("name", seg["code"]),
            "start_idx": seg["start_idx"],
            "end_idx": seg["end_idx"],
            "weight": seg.get("weight", 1.0),
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "fees": 0.0,
            "slippage": 0.0,
            "funding": 0.0,
            "segment_return": 0.0,
            "segment_max_drawdown": 0.0,
        }
    # Trade attribution by close bar
    for t in trades:
        idx = int(t.get("bar_idx_close", 0))
        for key, b in buckets.items():
            if b["start_idx"] <= idx <= b["end_idx"]:
                b["trades"] += 1
                pnl = float(t.get("pnl", 0.0))
                b["pnl"] += pnl
                b["fees"] += float(t.get("fee_paid", 0.0))
                b["slippage"] += float(t.get("slippage_paid", 0.0))
                b["funding"] += float(t.get("funding_paid", 0.0))
                if pnl > 0:
                    b["wins"] += 1
                elif pnl < 0:
                    b["losses"] += 1
                break
    # Segment equity behaviour (indices are bar-based; equity curve is bar+1 long)
    for b in buckets.values():
        lo = min(b["start_idx"], len(equity_curve) - 1)
        hi = min(b["end_idx"] + 1, len(equity_curve) - 1)
        if hi <= lo:
            continue
        start_eq = equity_curve[lo]
        end_eq = equity_curve[hi]
        b["segment_return"] = (end_eq / start_eq - 1.0) if start_eq > 0 else 0.0
        peak = start_eq
        max_dd = 0.0
        for v in equity_curve[lo:hi + 1]:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak
                if dd > max_dd:
                    max_dd = dd
        b["segment_max_drawdown"] = max_dd
    return list(buckets.values())


def summary_metrics(
    *,
    equity_curve: list[float],
    trades: list[dict],
    days: float,
    starting_capital: float,
    sec_per_bar: int,
) -> dict[str, Any]:
    if not equity_curve:
        return {"error": "empty_equity_curve"}
    end_eq = equity_curve[-1]
    net_pnl = end_eq - starting_capital
    gross_pnl = sum(t["pnl"] for t in trades) if trades else 0.0
    fees = sum(t.get("fee_paid", 0.0) for t in trades)
    slippage = sum(t.get("slippage_paid", 0.0) for t in trades)
    funding = sum(t.get("funding_paid", 0.0) for t in trades)

    n_trades = len(trades)
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] < 0]
    win_rate = (len(wins) / n_trades) if n_trades else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    expectancy = (sum(t["pnl"] for t in trades) / n_trades) if n_trades else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (math.inf if wins else 0.0)
    best = max((t["pnl"] for t in trades), default=0.0)
    worst = min((t["pnl"] for t in trades), default=0.0)
    avg_trade = (sum(t["pnl"] for t in trades) / n_trades) if n_trades else 0.0

    # Returns from equity curve
    rets = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        if prev > 0:
            rets.append((equity_curve[i] - prev) / prev)
    mean_r = (sum(rets) / len(rets)) if rets else 0.0
    var = (sum((r - mean_r) ** 2 for r in rets) / len(rets)) if rets else 0.0
    sd = math.sqrt(var) if var > 0 else 0.0
    bars_per_year = 365 * 86400 / sec_per_bar
    sharpe = (mean_r / sd * math.sqrt(bars_per_year)) if sd > 0 else 0.0
    downside = [min(0.0, r) for r in rets]
    dd_var = (sum(d * d for d in downside) / len(downside)) if downside else 0.0
    dd_sd = math.sqrt(dd_var) if dd_var > 0 else 0.0
    sortino = (mean_r / dd_sd * math.sqrt(bars_per_year)) if dd_sd > 0 else 0.0

    # Max drawdown
    peak = equity_curve[0]
    max_dd = 0.0
    underwater = 0
    max_uw = 0
    for v in equity_curve:
        if v > peak:
            peak = v
            underwater = 0
        else:
            underwater += 1
            max_uw = max(max_uw, underwater)
        if peak > 0:
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd

    cagr = ((end_eq / starting_capital) ** (365.0 / max(days, 1.0)) - 1.0) if starting_capital > 0 else 0.0
    calmar = (cagr / max_dd) if max_dd > 0 else (math.inf if cagr > 0 else 0.0)

    # Exposure / turnover
    if trades:
        exposure_bars = sum(max(1, t["bar_idx_close"] - t["bar_idx_open"]) for t in trades)
    else:
        exposure_bars = 0
    total_bars = max(1, len(equity_curve) - 1)
    exposure = min(1.0, exposure_bars / total_bars)
    turnover = sum(t.get("qty", 0.0) * t.get("entry", 0.0) for t in trades) / max(starting_capital, 1.0)

    return {
        "net_pnl": net_pnl,
        "gross_pnl": gross_pnl,
        "ending_equity": end_eq,
        "cagr": cagr,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "max_drawdown": max_dd,
        "max_underwater_bars": max_uw,
        "win_rate": win_rate,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "trade_count": n_trades,
        "avg_trade": avg_trade,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "best_trade": best,
        "worst_trade": worst,
        "fee_paid": fees,
        "slippage_paid": slippage,
        "funding_paid": funding,
        "exposure_pct": exposure * 100.0,
        "idle_time_pct": (1.0 - exposure) * 100.0,
        "turnover": turnover,
    }
