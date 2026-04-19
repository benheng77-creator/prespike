"""
Accuracy gate — per-symbol rolling win-rate floor + proving period.

This module enforces a trust-based circuit breaker around the existing
decision engine. It does NOT predict accuracy; it MEASURES the bot's own
recent performance and refuses new entries on symbols where the measured
rolling win rate has fallen below a configured floor (default 75%).

Two gates, both additive and conservative:

1. Rolling win-rate floor
   Look at the last `window_size` closed trades for a symbol. If the
   win rate over that window is below `floor_pct`, refuse any new EXECUTE
   on that symbol. The gate re-opens automatically when the rolling WR
   climbs back above the floor, or after `cooldown_bars` elapse, whichever
   comes first.

2. Proving period
   A symbol cannot be traded live until it has accumulated `proving_wins`
   paper-mode wins. The bot stays in paper on that symbol, logs trades as
   usual, and only graduates to live once the win count is met.

Both checks read from the same `trades` SQLite table the bot already writes
to via core.persistence.TradeLogger, so there is no parallel bookkeeping.

The gate does not block the decision engine from computing scores or
labelling WATCH / NO TRADE. It only vetoes new EXECUTE when conditions
are not met.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AccuracyGateConfig:
    enabled: bool = True
    window_size: int = 30
    floor_pct: float = 0.75
    min_trades_before_floor: int = 10
    proving_wins: int = 5
    cooldown_bars: int = 48
    db_path: str = "logs/openclaw.db"
    # Optional: the path to the prover's results file. If a symbol appears
    # in earned_live_symbols there, that symbol is allowed live trading
    # *regardless* of its DB-based proving wins. The DB rolling-WR floor
    # still applies. This is the auto-unlock breakthrough.
    god_mode_results_path: str = "logs/god_mode_results.json"


@dataclass
class SymbolStats:
    symbol: str
    window_trades: int
    window_wins: int
    window_losses: int
    rolling_wr: float
    total_trades_closed: int
    total_wins: int
    proving_satisfied: bool
    floor_satisfied: bool
    cooldown_until_ms: int = 0


@dataclass
class GateDecision:
    allow: bool
    reason: str
    stats: Optional[SymbolStats] = None


@dataclass
class _State:
    cooldowns: dict[str, int] = field(default_factory=dict)


class AccuracyGate:
    """Stateless-ish gate backed by the shared trade SQLite DB.

    Call flow:
      gate = AccuracyGate(cfg)
      decision = gate.evaluate(symbol, mode)
      if not decision.allow:
          logger.info("accuracy gate vetoed %s: %s", symbol, decision.reason)
          return  # skip EXECUTE
    """

    def __init__(self, config: AccuracyGateConfig):
        self._cfg = config
        self._state = _State()

    def _load_prover_unlocks(self) -> set[str]:
        """Return the set of symbols the god-mode prover has marked as
        earning live trading. Re-read on every evaluate() so the unlock
        list is hot-reloadable when you re-run the prover."""
        path = self._cfg.god_mode_results_path
        if not path or not os.path.exists(path):
            return set()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("accuracy_gate: cannot read prover results: %s", exc)
            return set()
        symbols = set()
        for item in data.get("earned_live_symbols", []):
            if isinstance(item, str):
                symbols.add(item)
            elif isinstance(item, dict) and "symbol" in item:
                symbols.add(item["symbol"])
        return symbols

    def evaluate(self, symbol: str, mode: str = "paper") -> GateDecision:
        if not self._cfg.enabled:
            return GateDecision(allow=True, reason="accuracy gate disabled")

        now_ms = _now_ms()

        cooldown_until = self._state.cooldowns.get(symbol, 0)
        if cooldown_until and now_ms < cooldown_until:
            return GateDecision(
                allow=False,
                reason=f"{symbol} in cooldown until ms={cooldown_until}",
            )

        stats = self._load_symbol_stats(symbol)

        # --- Proving period ---
        # Only gate live trading on proving wins; paper mode always allowed.
        # Auto-unlock: a symbol the god-mode prover marked as earned skips
        # the proving-wins requirement (the prover already proved it).
        if mode.lower() == "live" and not stats.proving_satisfied:
            prover_unlocks = self._load_prover_unlocks()
            if symbol not in prover_unlocks:
                return GateDecision(
                    allow=False,
                    reason=(
                        f"{symbol} has {stats.total_wins} lifetime wins, "
                        f"needs {self._cfg.proving_wins} before live trading "
                        f"(or run god_mode_prove.py to auto-unlock)"
                    ),
                    stats=stats,
                )
            logger.info(
                "accuracy_gate: %s auto-unlocked by god-mode prover", symbol
            )

        # --- Rolling WR floor ---
        # Only enforce once we have enough samples, to avoid early sample bias.
        if stats.window_trades >= self._cfg.min_trades_before_floor:
            if stats.rolling_wr < self._cfg.floor_pct:
                # Latch a cooldown: give the next N bars a chance before retest
                self._state.cooldowns[symbol] = (
                    now_ms + self._cfg.cooldown_bars * 5 * 60 * 1000
                )
                return GateDecision(
                    allow=False,
                    reason=(
                        f"{symbol} rolling WR {stats.rolling_wr:.1%} "
                        f"below floor {self._cfg.floor_pct:.0%} "
                        f"(n={stats.window_trades})"
                    ),
                    stats=stats,
                )

        return GateDecision(
            allow=True,
            reason=(
                f"{symbol} rolling WR {stats.rolling_wr:.1%} "
                f"n={stats.window_trades} proving={stats.proving_satisfied}"
            ),
            stats=stats,
        )

    def snapshot(self, symbols: list[str]) -> dict[str, SymbolStats]:
        return {sym: self._load_symbol_stats(sym) for sym in symbols}

    def _load_symbol_stats(self, symbol: str) -> SymbolStats:
        """Read last N closed trades for symbol from the DB."""
        try:
            conn = sqlite3.connect(
                f"file:{self._cfg.db_path}?mode=ro",
                uri=True,
                timeout=1.0,
            )
        except sqlite3.Error as exc:
            logger.warning("accuracy_gate: cannot open DB (%s)", exc)
            return SymbolStats(
                symbol=symbol,
                window_trades=0,
                window_wins=0,
                window_losses=0,
                rolling_wr=0.0,
                total_trades_closed=0,
                total_wins=0,
                proving_satisfied=False,
                floor_satisfied=False,
            )

        try:
            totals = conn.execute(
                "SELECT COUNT(*) n, "
                "COALESCE(SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END), 0) w "
                "FROM trades "
                "WHERE symbol = ? AND status <> 'open' AND pnl_r IS NOT NULL",
                (symbol,),
            ).fetchone()
            total_trades = int(totals[0] or 0)
            total_wins = int(totals[1] or 0)

            window_rows = conn.execute(
                "SELECT pnl_r FROM trades "
                "WHERE symbol = ? AND status <> 'open' AND pnl_r IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (symbol, self._cfg.window_size),
            ).fetchall()
            window_trades = len(window_rows)
            window_wins = sum(1 for (r,) in window_rows if r and r > 0)
            window_losses = window_trades - window_wins
            rolling_wr = window_wins / window_trades if window_trades else 0.0
        finally:
            conn.close()

        proving_satisfied = total_wins >= self._cfg.proving_wins
        floor_satisfied = (
            window_trades < self._cfg.min_trades_before_floor
            or rolling_wr >= self._cfg.floor_pct
        )

        return SymbolStats(
            symbol=symbol,
            window_trades=window_trades,
            window_wins=window_wins,
            window_losses=window_losses,
            rolling_wr=rolling_wr,
            total_trades_closed=total_trades,
            total_wins=total_wins,
            proving_satisfied=proving_satisfied,
            floor_satisfied=floor_satisfied,
        )


def _now_ms() -> int:
    return int(time.time() * 1000)
