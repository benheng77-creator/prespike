"""
Paper trading portfolio.

Tracks simulated positions and P&L against live market bars. Same stop /
target logic as backtest._resolve_trade but streaming: `check_bar()` is
called once per new candle and closes the open position if the bar's
high/low hit the stop or target.

Supports break-even stop management: once a position reaches
`breakeven_at_r` in favorable excursion, the stop is moved to the entry
price. Subsequent stop hits exit at 0 pnl ("breakeven") instead of -1R.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class PaperPosition:
    symbol: str
    direction: int  # +1 long, -1 short
    size: float     # units of the base asset
    entry_px: float
    stop_px: float
    target_px: float
    entry_ts_ms: int
    initial_stop_px: float = 0.0
    be_moved: bool = False

    def __post_init__(self) -> None:
        if self.initial_stop_px == 0.0:
            self.initial_stop_px = self.stop_px


@dataclass
class PaperPortfolio:
    starting_balance: float
    balance: float = 0.0
    breakeven_at_r: float = 0.0
    position: Optional[PaperPosition] = None
    closed_trades: List[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.balance == 0.0:
            self.balance = self.starting_balance

    def has_open_position(self) -> bool:
        return self.position is not None

    def open(
        self,
        symbol: str,
        direction: int,
        size: float,
        entry_px: float,
        stop_px: float,
        target_px: float,
    ) -> Optional[PaperPosition]:
        if self.position is not None:
            return None
        self.position = PaperPosition(
            symbol=symbol,
            direction=direction,
            size=size,
            entry_px=entry_px,
            stop_px=stop_px,
            target_px=target_px,
            entry_ts_ms=int(time.time() * 1000),
            initial_stop_px=stop_px,
            be_moved=False,
        )
        return self.position

    def check_bar(
        self,
        high: float,
        low: float,
        close_px: float,
        ts_ms: int,
    ) -> Optional[dict]:
        """Close the open position if this bar hit the stop or target.

        Bar resolution order is conservative: stop (against current stop)
        → target → end-of-bar break-even move. A break-even move therefore
        only takes effect on the NEXT bar, matching the backtest engine.
        """
        if self.position is None:
            return None
        p = self.position

        exit_px: Optional[float] = None
        reason = ""
        if p.direction > 0:
            if low <= p.stop_px:
                exit_px = p.stop_px
                reason = "breakeven" if p.be_moved else "stop"
            elif high >= p.target_px:
                exit_px, reason = p.target_px, "target"
        else:
            if high >= p.stop_px:
                exit_px = p.stop_px
                reason = "breakeven" if p.be_moved else "stop"
            elif low <= p.target_px:
                exit_px, reason = p.target_px, "target"

        if exit_px is not None:
            return self._close(exit_px, reason, ts_ms)

        # End-of-bar break-even move (effective from the next bar).
        if self.breakeven_at_r > 0 and not p.be_moved:
            initial_risk = abs(p.entry_px - p.initial_stop_px)
            if initial_risk > 0:
                if p.direction > 0:
                    excursion = high - p.entry_px
                else:
                    excursion = p.entry_px - low
                if excursion / initial_risk >= self.breakeven_at_r:
                    p.stop_px = p.entry_px
                    p.be_moved = True

        return None

    def force_close(self, close_px: float, ts_ms: int) -> Optional[dict]:
        if self.position is None:
            return None
        return self._close(close_px, "forced", ts_ms)

    def _close(self, exit_px: float, reason: str, ts_ms: int) -> dict:
        p = self.position
        assert p is not None
        initial_risk = abs(p.entry_px - p.initial_stop_px)
        pnl_quote = (exit_px - p.entry_px) * p.size * p.direction
        if initial_risk > 0 and p.size > 0:
            pnl_r = pnl_quote / (initial_risk * p.size)
        else:
            pnl_r = 0.0
        self.balance += pnl_quote
        closed = {
            "symbol": p.symbol,
            "direction": p.direction,
            "size": p.size,
            "entry_ts_ms": p.entry_ts_ms,
            "entry_px": p.entry_px,
            "stop_px": p.stop_px,
            "initial_stop_px": p.initial_stop_px,
            "target_px": p.target_px,
            "exit_ts_ms": ts_ms,
            "exit_px": exit_px,
            "exit_reason": reason,
            "pnl_r": pnl_r,
            "pnl_quote": pnl_quote,
            "balance_after": self.balance,
            "be_moved": p.be_moved,
        }
        self.closed_trades.append(closed)
        self.position = None
        return closed

    def stats(self) -> dict:
        total_pnl = self.balance - self.starting_balance
        n = len(self.closed_trades)
        wins = sum(1 for t in self.closed_trades if t["pnl_quote"] > 0)
        scratches = sum(1 for t in self.closed_trades if t["exit_reason"] == "breakeven")
        return {
            "balance": self.balance,
            "total_pnl": total_pnl,
            "return_pct": total_pnl / self.starting_balance if self.starting_balance > 0 else 0.0,
            "n_trades": n,
            "wins": wins,
            "scratches": scratches,
            "win_rate": wins / n if n > 0 else 0.0,
        }
