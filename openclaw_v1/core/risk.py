"""
Risk engine with circuit breakers and Kelly position sizing.

Responsibilities:

    1. Kelly-fraction position sizing given a win probability and RR.
    2. Daily loss limit, max drawdown from peak, max consecutive losses.
    3. External kill-switch via a halt-file path.
    4. Halt latch: once tripped, stays tripped until `resume()` is called.

Call order from main.py each cycle:

    risk.on_tick(balance, today)                  # update peak + daily anchor
    reason = risk.check_halt(balance)             # returns str if halted
    if reason: skip + (force close open trades)
    ...
    size_quote = risk.kelly_size_quote(p, rr, balance)
    ...
    on trade close: risk.on_trade_close(pnl_quote)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


class RiskEngine:
    def __init__(
        self,
        starting_balance: float,
        max_drawdown: float = 0.05,
        max_leverage: float = 3.0,
        max_daily_loss: float = 0.03,
        max_consecutive_losses: int = 5,
        halt_file_path: Optional[str] = None,
    ):
        self.starting_balance = starting_balance
        self.max_drawdown = max_drawdown
        self.max_leverage = max_leverage
        self.max_daily_loss = max_daily_loss
        self.max_consecutive_losses = max_consecutive_losses
        self.halt_file_path = halt_file_path

        self.peak_balance: float = starting_balance
        self.session_date: Optional[str] = None
        self.session_start_balance: float = starting_balance
        self.consecutive_losses: int = 0
        self.halted: bool = False
        self.halt_reason: str = ""

    # ---------- state transitions ----------

    def on_tick(self, balance: float, today: str) -> None:
        if self.session_date != today:
            self.session_date = today
            self.session_start_balance = balance
        if balance > self.peak_balance:
            self.peak_balance = balance

    def on_trade_close(self, pnl_quote: float) -> None:
        if pnl_quote <= 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    # ---------- halt gate ----------

    def check_halt(self, balance: float) -> Optional[str]:
        if self.halted:
            return self.halt_reason

        if self.halt_file_path and os.path.exists(self.halt_file_path):
            self._halt(f"halt file present at {self.halt_file_path}")
            return self.halt_reason

        if self.consecutive_losses >= self.max_consecutive_losses:
            self._halt(
                f"{self.consecutive_losses} consecutive losses "
                f">= cap {self.max_consecutive_losses}"
            )
            return self.halt_reason

        if self.session_start_balance > 0:
            daily_loss = (
                (self.session_start_balance - balance) / self.session_start_balance
            )
            if daily_loss > self.max_daily_loss:
                self._halt(
                    f"daily loss {daily_loss:.2%} > cap {self.max_daily_loss:.2%}"
                )
                return self.halt_reason

        if self.peak_balance > 0:
            dd = (self.peak_balance - balance) / self.peak_balance
            if dd > self.max_drawdown:
                self._halt(
                    f"drawdown {dd:.2%} > cap {self.max_drawdown:.2%}"
                )
                return self.halt_reason

        return None

    def _halt(self, reason: str) -> None:
        self.halt_reason = reason
        self.halted = True
        log.error(f"RiskEngine HALT: {reason}")

    def resume(self) -> None:
        self.halted = False
        self.halt_reason = ""
        log.info("RiskEngine resumed")

    # ---------- sizing ----------

    def kelly_size_quote(
        self,
        win_prob: float,
        reward_risk: float,
        balance: float,
        current_exposure: float = 0.0,
    ) -> float:
        """Return Kelly-sized quote amount, 0 if rejected for any reason."""
        if self.halted:
            return 0.0
        if balance <= 0:
            return 0.0
        if reward_risk <= 0:
            return 0.0
        if not (0.0 < win_prob < 1.0):
            return 0.0
        if current_exposure > 0 and (current_exposure / balance) > self.max_leverage:
            return 0.0

        kelly_fraction = win_prob - ((1.0 - win_prob) / reward_risk)
        if kelly_fraction <= 0:
            return 0.0
        return balance * kelly_fraction
