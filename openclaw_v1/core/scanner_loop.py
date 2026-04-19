"""
Scanner loop — runs the SymbolScanner against the configured universe on
a slow cadence and writes `cache/scanner_ranking.json` so the panel can
display live rankings.

This loop uses a SIMPLIFIED evaluator that only relies on the stateless
feature functions (momentum MTF, ATR, drift, regime). It does NOT run
the full decision engine because that needs per-symbol trackers
(FlowTracker, OrderBookTracker, FundingTracker, OpenInterestTracker)
which currently hold single-symbol state.

The intent:
  - The scanner ranks candidates using a coarse-but-real score.
  - The full decision engine still runs in main.py's decision_loop
    against `config.exchange.symbol` for actual EXECUTE decisions.
  - When you see the scanner consistently ranking some other symbol
    above your current trading symbol, that's your signal to swap
    `config.exchange.symbol` manually and restart.

Scanner output schema (written to cache/scanner_ranking.json):

    {
      "generated_at_ms": 1775890000000,
      "generator": "scanner_loop v1 (simplified evaluator)",
      "universe": ["BTC/USDT", "ETH/USDT", ...],
      "rank_by": "score_x_ev",
      "ranking": [
        {
          "symbol": "BTC/USDT",
          "ScoreTotal": 68.4,
          "EV_R": 1.2,
          "PWinPct": 55.0,
          "direction": 1,
          "action": "WATCH",
          "mtf_aligned": true,
          "d5": 0.3, "d15": 0.2, "d60": 0.1, "d240": 0.05,
          "atr_pct": 0.012,
          "drift": 0.08
        },
        ...
      ]
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from core.scanner import ScannerConfig, SymbolScanner
from features.atr import compute_atr
from features.drift import compute_drift_score
from features.momentum import compute_mtf_directions

logger = logging.getLogger(__name__)


async def make_simple_evaluator(config):
    """Build the evaluate_symbol callback the scanner calls per symbol.

    Returns a coroutine `async def evaluate(symbol, bars) -> dict` that
    produces a ScanScore using stateless features only.
    """

    async def evaluate(symbol: str, bars) -> dict:
        if len(bars) < 100:
            return {
                "action": "NO TRADE",
                "ScoreTotal": 0.0,
                "EV_R": 0.0,
                "PWinPct": 0.0,
                "reason": "not enough bars",
            }
        try:
            closes = [b[4] for b in bars]
            try:
                d5, d15, d60, d240 = compute_mtf_directions(closes)
            except Exception:
                d5 = d15 = d60 = d240 = 0.0
            try:
                atr = float(compute_atr(list(bars), period=14) or 0.0)
            except Exception:
                atr = 0.0
            atr_pct = atr / closes[-1] if closes[-1] else 0.0
            try:
                drift = float(compute_drift_score(closes) or 0.0)
            except Exception:
                drift = 0.0

            mtf_aligned = (
                (d5 > 0 and d15 > 0 and d60 > 0 and d240 > 0)
                or (d5 < 0 and d15 < 0 and d60 < 0 and d240 < 0)
            )
            direction = 1 if (d5 + d15 + d60 + d240) > 0 else -1

            # Conviction proxy: magnitude of alignment × 100, penalised
            # heavily if MTF disagrees or drift is high. Tuned so that
            # a cleanly aligned trending symbol lands around 70–80 and
            # noisy/conflicted tapes land below 50.
            magnitude = (abs(d5) + abs(d15) + abs(d60) + abs(d240)) / 4.0
            alignment_factor = 1.0 if mtf_aligned else 0.5
            drift_factor = max(0.0, 1.0 - drift)
            vol_factor = min(1.0, atr_pct / 0.02)  # saturate at 2% ATR
            score = magnitude * 100.0 * alignment_factor * drift_factor * vol_factor

            # Stub PWin/EV_R using simple proxies — this is NOT the real
            # engine and the panel labels it as such.
            pwin_pct = 50.0 + 30.0 * magnitude * alignment_factor
            ev_r = 2.0 * magnitude * alignment_factor * drift_factor - 0.5

            if score >= 72.0 and mtf_aligned and ev_r > 0:
                action = "EXECUTE"
            elif score >= 58.0 and ev_r > 0:
                action = "WATCH"
            else:
                action = "NO TRADE"

            return {
                "action": action,
                "ScoreTotal": round(float(score), 2),
                "EV_R": round(float(ev_r), 3),
                "PWinPct": round(float(pwin_pct), 1),
                "direction": direction,
                "mtf_aligned": bool(mtf_aligned),
                "d5": round(float(d5), 3),
                "d15": round(float(d15), 3),
                "d60": round(float(d60), 3),
                "d240": round(float(d240), 3),
                "atr_pct": round(float(atr_pct), 4),
                "drift": round(float(drift), 3),
            }
        except Exception as exc:
            logger.warning("scanner evaluate %s failed: %s", symbol, exc)
            return {
                "action": "NO TRADE",
                "ScoreTotal": 0.0,
                "EV_R": 0.0,
                "PWinPct": 0.0,
                "reason": f"error: {exc}",
            }

    return evaluate


async def scanner_loop(exchange: Any, config, output_path: str = "cache/scanner_ranking.json") -> None:
    """Run the scanner continuously. Writes rankings to output_path after
    every tick so the panel can display fresh data."""
    if not config.scanner.enabled:
        logger.info("scanner disabled — scanner_loop is a no-op")
        return

    cfg = ScannerConfig(
        enabled=True,
        universe=list(config.scanner.universe),
        max_concurrent=config.scanner.max_concurrent,
        rank_by=config.scanner.rank_by,
        min_bars_before_trade=config.scanner.min_bars_before_trade,
    )

    evaluate = await make_simple_evaluator(config)
    scanner = SymbolScanner(cfg, exchange, evaluate)
    await scanner.warmup(config.exchange.timeframe)
    logger.info(
        "scanner_loop started with %d symbols: %s",
        len(cfg.universe), cfg.universe,
    )

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    while True:
        try:
            ranking = await scanner.tick()
            payload = {
                "generated_at_ms": int(time.time() * 1000),
                "generator": "scanner_loop v1 (simplified evaluator)",
                "universe": cfg.universe,
                "rank_by": cfg.rank_by,
                "ranking": ranking.evaluated,
                "tradable_symbols": [r["symbol"] for r in ranking.tradable],
                "scanner_state": scanner.snapshot(),
            }
            tmp_path = f"{output_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            os.replace(tmp_path, output_path)
        except Exception as exc:
            logger.warning("scanner_loop tick error: %s", exc)
            await asyncio.sleep(5)
