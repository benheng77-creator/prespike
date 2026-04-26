"""
pre_spike_symptom_v1 — live trading strategy with OKX spot auto-buy.

Two surfaces in one file:
1. Top-level panel contract (SYMBOLS, TIMEFRAME, LOOKBACK_BARS, DEFAULTS,
   reset_state, generate_signal) — for backtest panel.
2. PreSpikeSymptomStrategy(BaseStrategy) — for openclaw_v1 orchestrator.

Both surfaces delegate to a shared SymptomEngine.
"""
from __future__ import annotations

import math
import time
from typing import Optional

import numpy as np

from ._symptom_engine import SymptomEngine

# ============================================================================
# Panel contract (top-level)
# ============================================================================

SYMBOLS = ["BTC-USDT"]
TIMEFRAME = "5m"
LOOKBACK_BARS = 220

DEFAULTS = {
    # Artifact
    "artifact_root": "/var/openclaw/models/",
    "active_model_version": "model_v2025-04-19",

    # Threshold & vetoes
    "threshold_tau_offset": 0.00,
    "sigma_cap": 1.00,
    "adx_cap": 25.0,
    "spread_cap_bps": 10.0,
    "funding_cap_bps_8h": 20.0,
    "max_bar_age_seconds": 90,

    # Sizing
    "spot_per_trade_pct": 1.50,
    "spot_min_trade_quote": 25.0,

    # Exit logic
    "k_stop": 1.20,
    "k_target": 2.50,
    "max_holding_hours": 6,
    "target_chase_attempts": 3,
    "stop_chase_attempts": 1,
}

RECIPE_SPREAD = {
    "threshold_tau_offset":  (-0.10, +0.10, 0.01, "float", "gate"),
    "sigma_cap":             (0.50, 2.00, 0.10, "float", "veto"),
    "adx_cap":               (15.0, 35.0, 1.0, "float", "veto"),
    "spread_cap_bps":        (5.0, 50.0, 1.0, "float", "veto"),
    "funding_cap_bps_8h":    (5.0, 50.0, 1.0, "float", "veto"),
    "k_stop":                (0.50, 2.00, 0.10, "float", "exit"),
    "k_target":              (1.00, 4.00, 0.25, "float", "exit"),
    "max_holding_hours":     (1, 24, 1, "int", "exit"),
    "spot_per_trade_pct":    (0.50, 3.00, 0.10, "float", "sizing"),
}


def get_recipe_spread() -> dict:
    return dict(RECIPE_SPREAD)


def apply_recipe(overrides: dict) -> dict:
    if not isinstance(overrides, dict):
        return DEFAULTS
    for k, v in overrides.items():
        if k not in RECIPE_SPREAD:
            continue
        low, high, _step, vtype, _g = RECIPE_SPREAD[k]
        try:
            val = int(round(float(v))) if vtype == "int" else float(v)
        except (TypeError, ValueError):
            continue
        if val < low: val = int(low) if vtype == "int" else low
        elif val > high: val = int(high) if vtype == "int" else high
        DEFAULTS[k] = val
    return DEFAULTS


# ----------------------------------------------------------------------------
# Module-level engine — lazy init for panel
# ----------------------------------------------------------------------------
_engine: Optional[SymptomEngine] = None


def reset_state() -> None:
    global _engine
    _engine = None


def _ensure_engine() -> SymptomEngine:
    global _engine
    if _engine is None:
        _engine = SymptomEngine(
            artifact_root=DEFAULTS["artifact_root"],
            active_version=DEFAULTS["active_model_version"],
            default_threshold_offset=DEFAULTS["threshold_tau_offset"],
        )
    return _engine


def generate_signal(window):
    """Panel contract entry point."""
    if not window or len(window) < LOOKBACK_BARS:
        return None
    try:
        eng = _ensure_engine()
    except Exception:
        return None

    # Replay window into engine if it's empty (panel use). Heuristic: if not warm,
    # feed bars in order.
    if not eng.is_warm():
        for b in window:
            eng.update(
                int(b.get("ts", 0)),
                float(b["open"]) if "open" in b else float(b["close"]),
                float(b["high"]), float(b["low"]),
                float(b["close"]), float(b.get("volume", 0.0)),
            )

    # Update with last bar (in case panel re-feeds incrementally)
    last = window[-1]
    eng.update(
        int(last.get("ts", 0)),
        float(last.get("open", last["close"])),
        float(last["high"]), float(last["low"]),
        float(last["close"]), float(last.get("volume", 0.0)),
    )
    if not eng.is_warm():
        return None

    p, snapshot = eng.predict()
    if not math.isfinite(p):
        return None
    if p < eng.threshold_tau:
        return None

    entry = float(last["close"])
    atr = eng.last_atr if eng.last_atr > 0 else entry * 0.005
    stop = entry - DEFAULTS["k_stop"] * atr
    target = entry + DEFAULTS["k_target"] * atr

    rr = (target - entry) / max(entry - stop, 1e-9)
    if rr < 1.5:
        return None

    confidence = min(0.55 + (p - eng.threshold_tau) * 1.5, 0.95)

    return {
        "direction": 1,
        "entry": entry,
        "stop": float(stop),
        "tp": float(target),
        "confidence": float(confidence),
        "sizing_multiplier": 1.0,
        "reason": f"pre_spike_symptom | P={p:.3f} τ={eng.threshold_tau:.3f}",
        "detectors_fired": [k for k in snapshot.keys() if math.isfinite(snapshot[k])][:5],
        "score": float(p),
    }


# ============================================================================
# BaseStrategy class — for openclaw_v1 orchestrator
# ============================================================================

# Soft-import — only used if openclaw_v1 is installed alongside.
try:
    from core.strategy_base import BaseStrategy, TradeIntent  # type: ignore
except ImportError:
    BaseStrategy = object  # type: ignore
    TradeIntent = dict     # type: ignore


class PreSpikeSymptomStrategy(BaseStrategy):
    strategy_id = "pre_spike_symptom_v1"
    timeframe = "5m"
    instruments = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]

    def __init__(self, services, params: dict):
        self.services = services
        self.params = {**DEFAULTS, **(params or {})}
        self.engine = SymptomEngine(
            artifact_root=self.params["artifact_root"],
            active_version=self.params["active_model_version"],
            default_threshold_offset=self.params["threshold_tau_offset"],
        )
        self._last_signal_ts: dict[str, int] = {}

    # ------------------------------------------------------------------
    def on_bar_close(self, bar) -> list:
        instrument = bar.instrument
        self.engine.update(
            int(bar.ts), float(bar.open), float(bar.high),
            float(bar.low), float(bar.close), float(bar.volume),
        )
        if not self.engine.is_warm():
            return []

        # Soft vetoes
        if self._is_stale(bar):
            return []
        if not self._spread_ok(instrument):
            return []
        if not self._funding_ok(instrument):
            return []

        p, snapshot = self.engine.predict()
        if not math.isfinite(p) or p < self.engine.threshold_tau:
            return []

        # Build provenance
        prov = {
            "model_version": self.params["active_model_version"],
            "P_spike": p,
            "threshold_tau": self.engine.threshold_tau,
            "feature_snapshot": snapshot,
            "atr_at_entry": self.engine.last_atr,
            "bar_close_ts": int(bar.ts),
            "feature_config": {},   # populated in real wiring
        }
        equity = self.services.portfolio.equity_quote()
        size_quote = max(
            self.params["spot_min_trade_quote"],
            equity * (self.params["spot_per_trade_pct"] / 100.0),
        )

        intent = {
            "id": f"{self.strategy_id}-{instrument}-{int(bar.ts)}",
            "strategy_id": self.strategy_id,
            "instrument": instrument,
            "venue": "okx_spot",
            "side": "BUY",
            "size_quote": float(size_quote),
            "entry_type": "limit_maker",
            "entry_price": float(bar.close),
            "k_stop": self.params["k_stop"],
            "k_target": self.params["k_target"],
            "max_holding_hours": self.params["max_holding_hours"],
            "provenance": prov,
            "audit_token": None,
        }
        return [intent]

    def on_tick(self, tick) -> list:
        return []

    def on_fill(self, fill) -> None:
        return None

    def on_position_closed(self, pos) -> None:
        return None

    def health_check(self):
        from dataclasses import dataclass
        @dataclass
        class _H: ok: bool; reason: str = ""
        return _H(ok=self.engine.is_warm(), reason="" if self.engine.is_warm() else "warming")

    def snapshot_state(self) -> dict:
        return {"strategy_id": self.strategy_id,
                "active_model_version": self.params["active_model_version"]}

    def restore_state(self, state: dict) -> None:
        return None

    # ------------------------------------------------------------------
    def _is_stale(self, bar) -> bool:
        age = int(time.time()) - int(bar.ts)
        return age > self.params["max_bar_age_seconds"]

    def _spread_ok(self, instrument: str) -> bool:
        try:
            book = self.services.okx_client.spot_get_book_top_sync(instrument)
            mid = (book["bid"] + book["ask"]) / 2.0
            spread_bps = (book["ask"] - book["bid"]) / mid * 10000
            return spread_bps <= self.params["spread_cap_bps"]
        except Exception:
            return False

    def _funding_ok(self, instrument: str) -> bool:
        try:
            f_bps = self.services.market_data.last_funding_bps(instrument)
            return abs(f_bps) <= self.params["funding_cap_bps_8h"]
        except Exception:
            return True   # missing funding data is non-blocking
