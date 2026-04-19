"""
Five-phase scoring engine.

Phases 1-5 implement a sentiment/trend/conviction/EV/final pipeline that
consumes upstream feature values and emits a fixed 19-field output set plus
a terminal EXECUTE / WATCH / NO TRADE action.

Coefficients are held constant against the spec. They are the calibration
target — swap them in once a real backtest distribution is available.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping


# ---------- helpers ----------

def clip(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, x))


def pos(x: float) -> float:
    return max(0.0, x)


def _sig(z: float) -> float:
    # numerically stable logistic
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


# ---------- inputs ----------

@dataclass(frozen=True)
class ScoreInputs:
    # sentiment sources
    NewsSent: float
    SocialSent: float
    FlowSent: float
    Freshness: float
    Coverage: float

    # trend / regime
    Samples: float
    Regime: float
    ShrinkN: float
    BaseWR: float
    D5: float
    D15: float
    D60: float
    D240: float

    # upstream conviction
    DecisionPct: float
    ConfidencePct: float

    # risk
    EventRiskScore: float
    DriftScore: float

    # trade geometry
    EntryPx: float
    StopPx: float
    TargetPx: float
    FeeR: float
    SlipR: float

    # gate
    VetoReason: str = ""
    ExecMin: float = 72.0
    WatchMin: float = 58.0

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any]) -> "ScoreInputs":
        fields = {f: m[f] for f in cls.__dataclass_fields__ if f in m}
        return cls(**fields)


# ---------- phases ----------

def score(inp: ScoreInputs) -> dict[str, Any]:
    # Phase 1 — Sentiment
    RawSent = clip(0.50 * inp.NewsSent + 0.30 * inp.SocialSent + 0.20 * inp.FlowSent, -1.0, 1.0)
    SentQuality = clip(math.sqrt(max(0.0, inp.Freshness * inp.Coverage)), 0.0, 1.0)
    SentimentPct = clip(50.0 + 50.0 * RawSent * SentQuality - 18.0 * (1.0 - SentQuality), 0.0, 100.0)

    # Phase 2 — Momentum & Trend
    denom = max(1.0, inp.Samples + inp.ShrinkN)
    RawTrend = ((inp.Samples * inp.Regime) + (inp.ShrinkN * inp.BaseWR)) / denom
    AdaptiveTrendPct = clip(100.0 * _sig(12.0 * (RawTrend - 0.50)), 0.0, 100.0)

    MTFConflict = (abs(inp.D5 - inp.D15) + abs(inp.D15 - inp.D60) + abs(inp.D60 - inp.D240)) / 6.0
    MTFRaw = 0.25 * inp.D5 + 0.35 * inp.D15 + 0.25 * inp.D60 + 0.15 * inp.D240
    MTFConfirmPct = clip((50.0 + 50.0 * MTFRaw) * (1.0 - 0.35 * MTFConflict), 0.0, 100.0)

    # Phase 3 — Conviction & Quality
    CommercialPct = clip(
        0.50 * inp.DecisionPct
        + 0.15 * SentimentPct
        + 0.20 * AdaptiveTrendPct
        + 0.15 * MTFConfirmPct,
        0.0,
        100.0,
    )
    RiskPenalty = 100.0 * (
        0.08 * pos(inp.EventRiskScore - 0.45) ** 1.35
        + 0.10 * pos(inp.DriftScore - 0.60) ** 1.40
    )

    if inp.VetoReason:
        ConfPlusPct = 0.0
    else:
        ConfPlusPct = clip(
            0.62 * inp.ConfidencePct
            + 0.18 * MTFConfirmPct
            + 0.20 * AdaptiveTrendPct
            - RiskPenalty,
            0.0,
            99.0,
        )

    # Phase 4 — Real EV
    z = (
        -2.20
        + 0.035 * ConfPlusPct
        + 0.020 * CommercialPct
        + 0.012 * MTFConfirmPct
        + 0.010 * AdaptiveTrendPct
        + 0.008 * SentimentPct
        - 1.40 * inp.EventRiskScore
        - 1.20 * inp.DriftScore
    )
    PWinPct = clip(100.0 * _sig(z), 5.0, 95.0)

    stop_dist = abs(inp.EntryPx - inp.StopPx)
    tgt_dist = abs(inp.TargetPx - inp.EntryPx)
    RRTrue = clip(tgt_dist / max(1e-6, stop_dist), 0.0, 6.0)

    CostR = max(
        0.0,
        inp.FeeR
        + inp.SlipR
        + 0.20 * pos(inp.EventRiskScore - 0.50)
        + 0.25 * pos(inp.DriftScore - 0.65),
    )

    p = PWinPct / 100.0
    EV_R = p * RRTrue - (1.0 - p) - CostR
    EdgePct = clip(50.0 + 50.0 * math.tanh(1.35 * EV_R), 0.0, 100.0)

    # Phase 5 — Final Score
    BaseScore = (
        0.28 * CommercialPct
        + 0.27 * ConfPlusPct
        + 0.18 * MTFConfirmPct
        + 0.10 * SentimentPct
        + 0.17 * EdgePct
    )
    HardPenalty = (
        30.0 * pos(inp.EventRiskScore - 0.60) ** 1.50
        + 24.0 * pos(inp.DriftScore - 0.70) ** 1.50
        + 20.0 * pos(0.90 - SentQuality) ** 1.25
    )
    ScoreTotal = clip(round(BaseScore - HardPenalty, 2), 0.0, 100.0)

    # Terminal gate
    if inp.VetoReason:
        action = "NO TRADE"
    elif SentQuality < 0.35:
        action = "NO TRADE"
    elif stop_dist <= 0:
        action = "NO TRADE"
    elif tgt_dist <= 0:
        action = "NO TRADE"
    elif PWinPct < 50.0:
        action = "NO TRADE"
    elif RRTrue < 1.20:
        action = "NO TRADE"
    elif EV_R <= 0:
        action = "NO TRADE"
    elif (
        ScoreTotal >= max(72.0, inp.ExecMin)
        and CommercialPct >= 68.0
        and ConfPlusPct >= 66.0
        and EV_R >= 0.25
    ):
        action = "EXECUTE"
    elif ScoreTotal >= max(58.0, inp.WatchMin) and EV_R > 0 and PWinPct >= 47.0:
        action = "WATCH"
    else:
        action = "NO TRADE"

    return {
        "RawSent": RawSent,
        "SentQuality": SentQuality,
        "SentimentPct": SentimentPct,
        "RawTrend": RawTrend,
        "AdaptiveTrendPct": AdaptiveTrendPct,
        "MTFConflict": MTFConflict,
        "MTFConfirmPct": MTFConfirmPct,
        "CommercialPct": CommercialPct,
        "RiskPenalty": RiskPenalty,
        "ConfPlusPct": ConfPlusPct,
        "PWinPct": PWinPct,
        "RRTrue": RRTrue,
        "CostR": CostR,
        "EV_R": EV_R,
        "EdgePct": EdgePct,
        "BaseScore": BaseScore,
        "HardPenalty": HardPenalty,
        "ScoreTotal": ScoreTotal,
        "TerminalAction": action,
    }


def score_from_mapping(m: Mapping[str, Any]) -> dict[str, Any]:
    return score(ScoreInputs.from_mapping(m))
