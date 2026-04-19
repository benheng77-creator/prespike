"""
Backtest Plus — operator-grade scenario backtesting.

Additive feature. Does NOT replace openclaw_v1/backtest/ (the existing
walk-forward/calibration harness) or binary15m/backtest/ (the audited
Kelly backtest). Those remain authoritative for research-grade work.

This layer adds:
  * 10 built-in market regime scenarios with parametric synthesis
  * scenario stacking / weighting / chaining / randomization
  * cost/latency/liquidity overlays
  * a pluggable strategy adapter (default: momentum+mean-reversion shim)
  * optional Gemini intelligence layer (schema-validated, toggleable,
    never alters accounting)
  * deterministic seed → reproducible runs
  * preset save/load + run history

See docs in this package and web/ops/index.html "Backtest" card.
"""

from .scenarios import SCENARIOS, ScenarioSpec, synth_ohlcv
from .composer import ComposedSeries, compose
from .overlays import OverlayConfig, apply_overlays
from .strategy import SimpleStrategy, StrategyAdapter
from .engine import BacktestConfig, run_backtest, RunResult
from .metrics import summary_metrics
from .presets import save_preset, load_preset, list_presets, save_run, list_runs, load_run

__all__ = [
    "SCENARIOS", "ScenarioSpec", "synth_ohlcv",
    "ComposedSeries", "compose",
    "OverlayConfig", "apply_overlays",
    "SimpleStrategy", "StrategyAdapter",
    "BacktestConfig", "run_backtest", "RunResult",
    "summary_metrics",
    "save_preset", "load_preset", "list_presets",
    "save_run", "list_runs", "load_run",
]
