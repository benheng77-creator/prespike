"""
Multi-symbol scanner — picks today's best pair from a configured universe.

The original decision_loop in main.py only evaluates `config.exchange.symbol`.
This module adds a lightweight scanner that can evaluate a universe of
symbols on the same cadence and rank them so the strategy can concentrate
on the best one at a time.

Design choices:
- Pure additive. decision_loop still runs; main.py chooses scanner-mode
  or single-symbol mode based on config.scanner.enabled.
- One symbol traded at a time by default (matches the existing portfolio
  model which tracks a single open position). scanner.max_concurrent > 1
  is supported but requires the portfolio to accept multiple positions,
  which the current PaperPortfolio does not — we log a warning if set.
- Ranking is deterministic given inputs: ScoreTotal × EV_R, tiebreak by
  PWinPct. Both numbers come out of the existing decision_engine; we do
  not invent a new scoring function.
- Per-symbol state (bar deque, last decision, feature trackers) is kept
  in a small holder object so we do not pay the warmup cost every cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SymbolState:
    """Per-symbol state carried across scanner cycles."""

    symbol: str
    timeframe: str
    bars: deque = field(default_factory=lambda: deque(maxlen=500))
    warmed_up: bool = False
    last_score: float = 0.0
    last_ev_r: float = 0.0
    last_pwin: float = 0.0
    last_action: str = "NO TRADE"
    last_eval_ts_ms: int = 0
    consecutive_errors: int = 0


@dataclass
class ScannerConfig:
    """Scanner tunables, loaded from config/openclaw.yaml:scanner."""

    enabled: bool = False
    universe: list[str] = field(default_factory=list)
    max_concurrent: int = 1
    warmup_bars: int = 500
    rank_by: str = "score_x_ev"  # or "score", "ev", "pwin"
    min_bars_before_trade: int = 250


@dataclass
class ScannerRanking:
    """Single cycle's ranking output."""

    generated_at_ms: int
    evaluated: list[dict[str, Any]]
    tradable: list[dict[str, Any]]


class SymbolScanner:
    """Evaluates a universe of symbols per cycle and ranks them.

    Usage from the decision loop:

        scanner = SymbolScanner(cfg, engine, evaluate_symbol)
        await scanner.warmup()
        while True:
            ranking = await scanner.tick()
            for candidate in ranking.tradable[:cfg.max_concurrent]:
                # route EXECUTE into the live_executor or portfolio
                ...

    `evaluate_symbol(symbol, bars)` is a callback the caller provides. It
    runs the existing decision engine on the given bars and returns a
    dict with at least:  action, ScoreTotal, EV_R, PWinPct, EntryPx,
    StopPx, TargetPx, direction. This keeps the scanner free of feature
    extraction logic (which stays in main.py / decision_engine.py).
    """

    def __init__(
        self,
        config: ScannerConfig,
        exchange: Any,
        evaluate_symbol,
    ):
        self._cfg = config
        self._exchange = exchange
        self._evaluate_symbol = evaluate_symbol
        self._state: dict[str, SymbolState] = {
            sym: SymbolState(symbol=sym, timeframe="") for sym in config.universe
        }

        if config.max_concurrent > 1:
            logger.warning(
                "scanner.max_concurrent=%d requested but PaperPortfolio tracks "
                "a single position; clamping concurrent trades to 1 until the "
                "portfolio supports multiple simultaneous positions.",
                config.max_concurrent,
            )

    @property
    def universe(self) -> list[str]:
        return list(self._cfg.universe)

    async def warmup(self, timeframe: str) -> None:
        """Backfill historical bars for every symbol once at startup."""
        if not self._cfg.enabled:
            return
        logger.info("scanner warmup for %d symbols", len(self._cfg.universe))
        for symbol in self._cfg.universe:
            state = self._state[symbol]
            state.timeframe = timeframe
            try:
                ohlcv = await self._exchange.fetch_ohlcv(
                    symbol, timeframe, limit=self._cfg.warmup_bars
                )
                state.bars = deque(ohlcv, maxlen=500)
                state.warmed_up = len(state.bars) >= self._cfg.min_bars_before_trade
                logger.info(
                    "scanner %s: backfilled %d bars, warmed_up=%s",
                    symbol,
                    len(state.bars),
                    state.warmed_up,
                )
            except Exception as exc:
                logger.error("scanner warmup %s failed: %s", symbol, exc)
                state.consecutive_errors += 1

    async def tick(self) -> ScannerRanking:
        """Pull the latest candle for each symbol and re-evaluate."""
        if not self._cfg.enabled:
            return ScannerRanking(generated_at_ms=_now_ms(), evaluated=[], tradable=[])

        evaluated: list[dict[str, Any]] = []
        for symbol in self._cfg.universe:
            row = await self._evaluate_one(symbol)
            if row is not None:
                evaluated.append(row)

        ranked = sorted(evaluated, key=self._rank_key, reverse=True)
        tradable = [
            r for r in ranked
            if r["action"] == "EXECUTE" and r.get("warmed_up", False)
        ]

        return ScannerRanking(
            generated_at_ms=_now_ms(),
            evaluated=ranked,
            tradable=tradable,
        )

    async def _evaluate_one(self, symbol: str) -> dict[str, Any] | None:
        state = self._state[symbol]
        try:
            candles = await asyncio.wait_for(
                self._exchange.watch_ohlcv(symbol, state.timeframe),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.warning("scanner %s: watch_ohlcv timed out", symbol)
            state.consecutive_errors += 1
            return None
        except Exception as exc:
            logger.warning("scanner %s: watch_ohlcv error: %s", symbol, exc)
            state.consecutive_errors += 1
            return None

        state.consecutive_errors = 0
        for candle in candles or []:
            _append_or_replace_bar(state.bars, candle)

        if len(state.bars) < self._cfg.min_bars_before_trade:
            return {
                "symbol": symbol,
                "action": "NO TRADE",
                "warmed_up": False,
                "ScoreTotal": 0.0,
                "EV_R": 0.0,
                "PWinPct": 0.0,
                "reason": "not warmed up",
            }
        state.warmed_up = True

        out = await self._evaluate_symbol(symbol, state.bars)
        if not out:
            return None

        state.last_action = out.get("action", "NO TRADE")
        state.last_score = float(out.get("ScoreTotal", 0.0))
        state.last_ev_r = float(out.get("EV_R", 0.0))
        state.last_pwin = float(out.get("PWinPct", 0.0))
        state.last_eval_ts_ms = _now_ms()

        return {
            "symbol": symbol,
            "warmed_up": True,
            **out,
        }

    def _rank_key(self, row: dict[str, Any]) -> tuple[float, float, float]:
        score = float(row.get("ScoreTotal", 0.0))
        ev_r = float(row.get("EV_R", 0.0))
        pwin = float(row.get("PWinPct", 0.0))
        mode = self._cfg.rank_by
        if mode == "score":
            primary = score
        elif mode == "ev":
            primary = ev_r
        elif mode == "pwin":
            primary = pwin
        else:  # score_x_ev (default) — require both to be positive to rank high
            primary = score * max(0.0, ev_r)
        return (primary, ev_r, pwin)

    def snapshot(self) -> dict[str, Any]:
        """Diagnostic snapshot for the panel / logs."""
        return {
            "enabled": self._cfg.enabled,
            "universe": self._cfg.universe,
            "max_concurrent": self._cfg.max_concurrent,
            "symbols": [
                {
                    "symbol": s.symbol,
                    "warmed_up": s.warmed_up,
                    "last_action": s.last_action,
                    "last_score": s.last_score,
                    "last_ev_r": s.last_ev_r,
                    "last_pwin": s.last_pwin,
                    "last_eval_ts_ms": s.last_eval_ts_ms,
                    "consecutive_errors": s.consecutive_errors,
                }
                for s in self._state.values()
            ],
        }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _append_or_replace_bar(bars: deque, candle: Any) -> None:
    """CCXT-Pro re-sends the current forming bar every tick; replace the
    last bar if the timestamp matches, append otherwise."""
    if not bars:
        bars.append(candle)
        return
    try:
        last_ts = bars[-1][0]
        new_ts = candle[0]
    except (IndexError, TypeError, KeyError):
        bars.append(candle)
        return
    if last_ts == new_ts:
        bars[-1] = candle
    else:
        bars.append(candle)
