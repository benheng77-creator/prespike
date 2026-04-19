"""
Live order executor — DRY-RUN by default, multiple safety rails required
before a real order actually reaches the exchange.

The existing `core.exchange.ExecutionEngine.execute_trade` writes an order
to the exchange via ccxt. This module wraps it with the safety layer that
main.py was missing:

  1.  Three independent flags must all be true for a real order to fire:
        config.mode == "live"
        OPENCLAW_LIVE_TRADING=1          (process env)
        config.live_trading.enabled == true
      Any one of these false → dry-run: log the order, record to the
      paper portfolio, but DO NOT hit the exchange.

  2.  Per-order notional cap. Orders above `max_notional_quote` are
      refused. Starts at $50 — tune upward after first live orders settle.

  3.  Per-day trade cap. Once `max_trades_per_day` real orders have been
      submitted in the current UTC day, further EXECUTEs are refused.

  4.  Order reconciliation. After submission, we poll the exchange for
      fill status and compare filled size vs requested. Large mismatches
      flip the halt file so the risk engine takes over.

  5.  Opt-in kill switch. The existing `cache/.halt` file blocks BOTH
      paper and live orders — accuracy_gate and risk_engine already honor
      it, the live executor does too as belt-and-braces.

Nothing in this module modifies main.py's existing paper-mode behaviour.
It is additive: main.py imports `LiveExecutor` and calls it instead of
`portfolio.open` when all live flags align.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class LiveTradingConfig:
    enabled: bool = False
    max_notional_quote: float = 50.0
    max_trades_per_day: int = 5
    reconcile_timeout_s: float = 10.0
    reconcile_tolerance_pct: float = 0.01  # 1% fill mismatch allowed
    halt_file_path: str = "cache/.halt"
    env_flag: str = "OPENCLAW_LIVE_TRADING"


@dataclass
class LiveOrderResult:
    submitted: bool
    dry_run: bool
    reason: str
    order_id: Optional[str] = None
    filled_size: Optional[float] = None
    avg_price: Optional[float] = None
    raw_order: Optional[dict[str, Any]] = None


@dataclass
class _DailyCounters:
    date_utc: str = ""
    real_orders_submitted: int = 0


@dataclass
class _State:
    counters: _DailyCounters = field(default_factory=_DailyCounters)
    disabled_by_reconciliation: bool = False
    disabled_reason: str = ""


class LiveExecutor:
    """Wraps the CCXT ExecutionEngine in the safety layer described above.

    Usage from the decision loop:
        executor = LiveExecutor(config.live_trading, engine)
        result = await executor.submit(
            symbol=sym, side="buy", amount=units, price=limit_px,
            mode=config.mode,
        )
        if result.dry_run:
            logger.info("would have placed live order: %s", result.reason)
        elif result.submitted:
            logger.info("live order filled: %s", result.raw_order)
    """

    def __init__(self, config: LiveTradingConfig, engine: Any):
        self._cfg = config
        self._engine = engine
        self._state = _State()

    def status(self) -> dict[str, Any]:
        """Diagnostic snapshot for the panel / logs."""
        active = self._live_allowed()[0]
        return {
            "live_trading_active": active,
            "mode_gate": True,  # filled by caller if needed
            "env_flag_name": self._cfg.env_flag,
            "env_flag_set": os.getenv(self._cfg.env_flag, "").strip() == "1",
            "config_enabled": self._cfg.enabled,
            "max_notional_quote": self._cfg.max_notional_quote,
            "max_trades_per_day": self._cfg.max_trades_per_day,
            "real_orders_today": self._state.counters.real_orders_submitted,
            "day_utc": self._state.counters.date_utc,
            "disabled_by_reconciliation": self._state.disabled_by_reconciliation,
            "disabled_reason": self._state.disabled_reason,
            "halt_file": self._cfg.halt_file_path,
            "halt_file_present": Path(self._cfg.halt_file_path).exists(),
        }

    def _live_allowed(self) -> tuple[bool, str]:
        """Return (allowed, reason). Requires all three gates to align AND
        no reconciliation-disable AND no halt file."""
        if self._state.disabled_by_reconciliation:
            return False, f"disabled by reconciliation: {self._state.disabled_reason}"
        if Path(self._cfg.halt_file_path).exists():
            return False, f"halt file present at {self._cfg.halt_file_path}"
        if not self._cfg.enabled:
            return False, "config.live_trading.enabled is false"
        if os.getenv(self._cfg.env_flag, "").strip() != "1":
            return False, f"env {self._cfg.env_flag}=1 required"
        return True, "live gates open"

    def _check_mode_gate(self, mode: str) -> tuple[bool, str]:
        if mode.lower() != "live":
            return False, f"config.mode={mode!r} (not 'live')"
        return True, "mode=live"

    def _rotate_daily_counters(self) -> None:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        if self._state.counters.date_utc != today:
            self._state.counters = _DailyCounters(date_utc=today)

    async def submit(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: Optional[float],
        mode: str,
        *,
        ref_price: Optional[float] = None,
    ) -> LiveOrderResult:
        """Submit an order, or dry-run it, based on all the safety gates.

        `amount` is the size in base units (what ccxt expects).
        `ref_price` is the current mark price used for notional calc when
        `price` is None (market orders). If not provided we try to read
        it off the exchange.
        """
        self._rotate_daily_counters()

        mode_ok, mode_reason = self._check_mode_gate(mode)
        live_ok, live_reason = self._live_allowed()
        live_active = mode_ok and live_ok

        # Notional sanity check applies in both dry-run and live paths.
        notional = await self._estimate_notional(symbol, amount, price, ref_price)
        if self._cfg.max_notional_quote > 0 and notional > self._cfg.max_notional_quote:
            return LiveOrderResult(
                submitted=False,
                dry_run=True,
                reason=(
                    f"notional ${notional:.2f} exceeds cap "
                    f"${self._cfg.max_notional_quote:.2f}"
                ),
            )

        if (
            self._state.counters.real_orders_submitted
            >= self._cfg.max_trades_per_day
        ):
            return LiveOrderResult(
                submitted=False,
                dry_run=True,
                reason=(
                    f"daily trade cap hit "
                    f"({self._cfg.max_trades_per_day} per day)"
                ),
            )

        if not live_active:
            logger.info(
                "DRY-RUN %s %s qty=%s px=%s notional=%.2f  gates: %s | %s",
                side, symbol, amount, price, notional, mode_reason, live_reason,
            )
            return LiveOrderResult(
                submitted=False,
                dry_run=True,
                reason=(
                    f"live gates closed — {mode_reason}; {live_reason}"
                ),
            )

        # --- Real order path ---
        try:
            order_type = "limit" if price else "market"
            logger.warning(
                "LIVE ORDER OUT: %s %s qty=%s px=%s notional=$%.2f",
                side, symbol, amount, price, notional,
            )
            raw = await self._engine.exchange.create_order(
                symbol, order_type, side, amount, price
            )
        except Exception as exc:
            logger.error("live order submission failed: %s", exc)
            return LiveOrderResult(
                submitted=False,
                dry_run=False,
                reason=f"exchange error: {exc}",
            )

        self._state.counters.real_orders_submitted += 1
        order_id = None
        if isinstance(raw, dict):
            order_id = raw.get("id") or raw.get("orderId")

        filled_size, avg_price = await self._reconcile(symbol, order_id, amount)
        if filled_size is not None:
            mismatch = abs(filled_size - amount) / max(amount, 1e-12)
            if mismatch > self._cfg.reconcile_tolerance_pct:
                self._trip_halt(
                    f"fill mismatch {mismatch:.1%} "
                    f"(requested {amount}, filled {filled_size})"
                )

        return LiveOrderResult(
            submitted=True,
            dry_run=False,
            reason="submitted",
            order_id=order_id,
            filled_size=filled_size,
            avg_price=avg_price,
            raw_order=raw if isinstance(raw, dict) else {"raw": raw},
        )

    async def _estimate_notional(
        self,
        symbol: str,
        amount: float,
        price: Optional[float],
        ref_price: Optional[float],
    ) -> float:
        if price and price > 0:
            return amount * price
        if ref_price and ref_price > 0:
            return amount * ref_price
        try:
            ticker = await self._engine.exchange.fetch_ticker(symbol)
            mark = (
                ticker.get("last")
                or ticker.get("close")
                or ticker.get("ask")
                or ticker.get("bid")
                or 0.0
            )
            return amount * float(mark)
        except Exception as exc:
            logger.debug("notional estimate ticker fetch failed: %s", exc)
            return 0.0

    async def _reconcile(
        self,
        symbol: str,
        order_id: Optional[str],
        requested: float,
    ) -> tuple[Optional[float], Optional[float]]:
        if not order_id:
            return None, None
        deadline = time.time() + self._cfg.reconcile_timeout_s
        while time.time() < deadline:
            try:
                order = await self._engine.exchange.fetch_order(order_id, symbol)
            except Exception as exc:
                logger.debug("reconcile fetch_order error: %s", exc)
                await asyncio.sleep(0.5)
                continue
            filled = float(order.get("filled") or 0.0)
            avg = order.get("average") or order.get("price")
            status = str(order.get("status") or "").lower()
            if status in ("closed", "canceled", "expired") or filled >= requested:
                return filled, float(avg) if avg else None
            await asyncio.sleep(0.5)
        logger.warning(
            "reconcile timeout on order %s (requested %s)", order_id, requested
        )
        return None, None

    def _trip_halt(self, reason: str) -> None:
        self._state.disabled_by_reconciliation = True
        self._state.disabled_reason = reason
        logger.error("LIVE EXECUTOR HALTED: %s", reason)
        try:
            halt_path = Path(self._cfg.halt_file_path)
            halt_path.parent.mkdir(parents=True, exist_ok=True)
            halt_path.write_text(f"auto-halt: {reason}\n", encoding="utf-8")
        except OSError as exc:
            logger.error("could not write halt file: %s", exc)
