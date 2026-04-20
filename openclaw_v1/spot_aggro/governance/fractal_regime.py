"""Opportunity Fabric — Sprint 4: Fractal Regime Confirmation.

Single-scale regime classifiers are brittle — a minute-scale squeeze can
flip to DEAD then back within an hourly candle. Fractal confirmation
requires ≥2 of 3 scales (1m, 5m, 1h) to agree on the regime sign
before a trade can use that regime as a reason to admit.

Regime sign mapping:
    bullish: SQUEEZE_BUILDING, BREAKOUT_BULL, TREND_UP
    bearish: BREAKDOWN, TREND_DOWN, SELL_PRESSURE
    neutral: DEAD, UNKNOWN, CHOP
    (fallbacks handle exotic labels — any regime not in the two
     signed sets is treated as neutral)

Default-OFF: advisory score only. Not a hard gate until operator
flips SPOT_FRACTAL_REGIME_GATE=1 AND downstream consumers opt in.

Design note: we read EXISTING regime classifier — no new MIO infra.
Single-scale is whatever research_agent produces; 5m/1h come from
rolling aggregation of the 1m regime stream stored in
spot_regime_samples (phase-11n persists samples every 60s).
"""
from __future__ import annotations

import os
import sqlite3
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

BULLISH = {
    "SQUEEZE_BUILDING", "BREAKOUT_BULL", "BREAKOUT", "TREND_UP",
    "STRONG_UP", "ACCUMULATION", "BULL",
}
BEARISH = {
    "BREAKDOWN", "TREND_DOWN", "STRONG_DOWN", "SELL_PRESSURE",
    "DISTRIBUTION", "BEAR",
}
NEUTRAL = {"DEAD", "UNKNOWN", "CHOP", "RANGE", "NONE", ""}


def _db_path() -> str:
    return (
        os.environ.get("TRADE_DB_PATH")
        or os.environ.get("CLAW_DB_PATH")
        or "trades.db"
    )


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(), isolation_level=None, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def gate_enabled() -> bool:
    return os.environ.get("SPOT_FRACTAL_REGIME_GATE", "0").strip() == "1"


def _sign_of(regime_label: str | None) -> str:
    """Map a regime label to +1 / -1 / 0 (as 'bullish'/'bearish'/'neutral')."""
    if not regime_label:
        return "neutral"
    up = regime_label.upper()
    if up in BULLISH:
        return "bullish"
    if up in BEARISH:
        return "bearish"
    if up in NEUTRAL:
        return "neutral"
    # Unknown label: neutral (fail-safe).
    return "neutral"


@dataclass
class ScaleReading:
    scale: str              # "1m" | "5m" | "1h"
    regime: str | None
    sign: str               # "bullish" | "bearish" | "neutral"
    confidence: float | None
    n_samples: int          # how many 1m samples rolled into this reading


@dataclass
class FractalVerdict:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    readings: list[ScaleReading] = field(default_factory=list)
    agreement: str = "none"            # "bullish_confirmed" | "bearish_confirmed" | "neutral" | "disagreement"
    confirmed_sign: str = "neutral"
    confirmed_scales: int = 0          # 0..3
    gate_enabled: bool = False
    admit_recommended: bool = True     # advisory: confirmation OR neutral -> admit
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["readings"] = [asdict(r) for r in self.readings]
        return d


# ---------------------------------------------------------------------------
# Scale reading helpers
# ---------------------------------------------------------------------------

def _latest_1m_sample() -> ScaleReading:
    """The freshest regime sample persisted by the engine heartbeat."""
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT regime, regime_confidence, ts_ms"
                " FROM spot_regime_samples"
                " ORDER BY ts_ms DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
    except Exception:
        row = None
    if row is None:
        return ScaleReading(scale="1m", regime=None, sign="neutral",
                            confidence=None, n_samples=0)
    reg = row["regime"]
    conf = float(row["regime_confidence"]) if row["regime_confidence"] is not None else None
    return ScaleReading(scale="1m", regime=reg, sign=_sign_of(reg),
                        confidence=conf, n_samples=1)


def _aggregate_window(window_minutes: int, scale_label: str) -> ScaleReading:
    """Roll up 1m samples over the last window_minutes into a single
    reading. The modal sign wins; confidence is sample-weighted."""
    cutoff_ms = int(time.time() * 1000) - window_minutes * 60_000
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT regime, regime_confidence, ts_ms"
                " FROM spot_regime_samples"
                " WHERE ts_ms >= ?"
                " ORDER BY ts_ms DESC",
                (cutoff_ms,),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        rows = []
    if not rows:
        return ScaleReading(scale=scale_label, regime=None, sign="neutral",
                            confidence=None, n_samples=0)

    signs = Counter(_sign_of(r["regime"]) for r in rows)
    (modal_sign, _), = signs.most_common(1)

    # Confidence: weighted mean of samples matching the modal sign.
    matching = [r for r in rows if _sign_of(r["regime"]) == modal_sign]
    if matching:
        confs = [float(r["regime_confidence"]) for r in matching
                 if r["regime_confidence"] is not None]
        conf = sum(confs) / len(confs) if confs else None
        # Representative label: most common regime string in the matching set.
        labels = Counter(r["regime"] for r in matching if r["regime"])
        regime_label = labels.most_common(1)[0][0] if labels else None
    else:
        conf = None
        regime_label = None

    return ScaleReading(
        scale=scale_label, regime=regime_label, sign=modal_sign,
        confidence=conf, n_samples=len(rows),
    )


def reading_1m() -> ScaleReading:
    return _latest_1m_sample()


def reading_5m() -> ScaleReading:
    return _aggregate_window(5, "5m")


def reading_1h() -> ScaleReading:
    return _aggregate_window(60, "1h")


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def evaluate() -> FractalVerdict:
    readings = [reading_1m(), reading_5m(), reading_1h()]
    signs = [r.sign for r in readings]

    counts = Counter(signs)
    bullish_n = counts.get("bullish", 0)
    bearish_n = counts.get("bearish", 0)
    neutral_n = counts.get("neutral", 0)

    if bullish_n >= 2:
        agreement = "bullish_confirmed"
        confirmed_sign = "bullish"
        confirmed_scales = bullish_n
        admit = True
        reason = (
            f"≥2 of 3 scales bullish (1m={signs[0]}, 5m={signs[1]}, 1h={signs[2]})"
        )
    elif bearish_n >= 2:
        agreement = "bearish_confirmed"
        confirmed_sign = "bearish"
        confirmed_scales = bearish_n
        admit = True
        reason = (
            f"≥2 of 3 scales bearish (1m={signs[0]}, 5m={signs[1]}, 1h={signs[2]})"
        )
    elif neutral_n >= 2:
        agreement = "neutral"
        confirmed_sign = "neutral"
        confirmed_scales = neutral_n
        admit = True
        reason = f"neutral across scales (1m={signs[0]}, 5m={signs[1]}, 1h={signs[2]})"
    else:
        agreement = "disagreement"
        confirmed_sign = "neutral"
        confirmed_scales = 0
        # Disagreement recommends hold IFF gate is on, else advisory.
        admit = not gate_enabled()
        reason = (
            f"scales disagree (1m={signs[0]}, 5m={signs[1]}, 1h={signs[2]}) — "
            f"{'blocking admit' if gate_enabled() else 'advisory only'}"
        )

    return FractalVerdict(
        readings=readings,
        agreement=agreement,
        confirmed_sign=confirmed_sign,
        confirmed_scales=confirmed_scales,
        gate_enabled=gate_enabled(),
        admit_recommended=admit,
        reason=reason,
    )
