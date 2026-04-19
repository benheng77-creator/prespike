"""
OpenClaw decision engine — multi-phase scoring pipeline.

Given upstream sentiment, trend, conviction, drift, and trade geometry,
compute a terminal action (EXECUTE / WATCH / NO TRADE) via:

    Phase 1  Sentiment fusion + quality weighting
    Phase 2  Bayesian-shrunk trend + multi-timeframe confirmation
    Phase 3  Commercial / confidence scoring with non-linear risk penalty
    Phase 4  EV from actual entry/stop/target geometry (not a fake floor/cap)
    Phase 5  Final weighted score + hard penalties
    Gate     Veto / quality / geometry / EV / score thresholds

The PWinPct coefficients in Phase 4 are expert priors by default. Calibrate
them from real backtest data via `backtest.calibration.calibrate_pwin()`,
save with `save_pwin_coefficients()`, and load them at process start with
`load_pwin_coefficients()`.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Literal


def _clip(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, x))


def _pos(x: float) -> float:
    return max(0.0, x)


TerminalAction = Literal["EXECUTE", "WATCH", "NO TRADE"]


# Hand-tuned prior coefficients for the PWinPct logistic. Replace via
# load_pwin_coefficients() or set_pwin_coefficients() after calibration.
DEFAULT_PWIN_COEFFS = {
    "intercept": -2.20,
    "ConfPlusPct": 0.035,
    "CommercialPct": 0.020,
    "MTFConfirmPct": 0.012,
    "AdaptiveTrendPct": 0.010,
    "SentimentPct": 0.008,
    "EventRiskScore": -1.40,
    "DriftScore": -1.20,
}

_active_pwin_coeffs = dict(DEFAULT_PWIN_COEFFS)


def get_pwin_coefficients() -> dict:
    return dict(_active_pwin_coeffs)


def set_pwin_coefficients(coeffs: dict) -> None:
    global _active_pwin_coeffs
    missing = set(DEFAULT_PWIN_COEFFS) - set(coeffs)
    if missing:
        raise ValueError(f"PWinPct coefficients missing keys: {sorted(missing)}")
    _active_pwin_coeffs = {k: float(coeffs[k]) for k in DEFAULT_PWIN_COEFFS}


def load_pwin_coefficients(path: str) -> bool:
    """Load fitted coefficients from JSON. Returns True if loaded, False if missing."""
    if not os.path.exists(path):
        return False
    with open(path) as f:
        coeffs = json.load(f)
    set_pwin_coefficients(coeffs)
    return True


def save_pwin_coefficients(coeffs: dict, path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        json.dump(coeffs, f, indent=2)


@dataclass
class DecisionInputs:
    # Sentiment sources, each in [-1, 1]
    NewsSent: float
    SocialSent: float
    FlowSent: float
    # Data quality, each in [0, 1]
    Freshness: float
    Coverage: float
    # Bayesian trend inputs
    Samples: float
    Regime: float      # observed regime win rate, [0, 1]
    ShrinkN: float     # prior strength
    BaseWR: float      # prior win rate, [0, 1]
    # Directional scores per timeframe, each in [-1, 1]
    D5: float
    D15: float
    D60: float
    D240: float
    # Upstream scores, each in [0, 100]
    DecisionPct: float
    ConfidencePct: float
    # Risk in [0, 1]
    EventRiskScore: float
    DriftScore: float
    # Trade geometry (prices)
    EntryPx: float
    StopPx: float
    TargetPx: float
    # Costs expressed as fraction of 1R
    FeeR: float
    SlipR: float
    # Gate config
    VetoReason: str = ""
    ExecMin: float = 72.0
    WatchMin: float = 58.0


@dataclass
class DecisionOutputs:
    RawSent: float
    SentQuality: float
    SentimentPct: float
    RawTrend: float
    AdaptiveTrendPct: float
    MTFConflict: float
    MTFConfirmPct: float
    CommercialPct: float
    RiskPenalty: float
    ConfPlusPct: float
    PWinPct: float
    RRTrue: float
    CostR: float
    EV_R: float
    EdgePct: float
    BaseScore: float
    HardPenalty: float
    ScoreTotal: float
    TerminalAction: TerminalAction


def evaluate(inp: DecisionInputs) -> DecisionOutputs:
    # Phase 1 — Sentiment
    RawSent = _clip(
        0.50 * inp.NewsSent + 0.30 * inp.SocialSent + 0.20 * inp.FlowSent,
        -1.0, 1.0,
    )
    SentQuality = _clip(math.sqrt(inp.Freshness * inp.Coverage), 0.0, 1.0)
    SentimentPct = _clip(
        50.0 + 50.0 * RawSent * SentQuality - 18.0 * (1.0 - SentQuality),
        0.0, 100.0,
    )

    # Phase 2 — Momentum & trend
    RawTrend = (
        (inp.Samples * inp.Regime) + (inp.ShrinkN * inp.BaseWR)
    ) / max(1.0, inp.Samples + inp.ShrinkN)
    AdaptiveTrendPct = _clip(
        100.0 / (1.0 + math.exp(-12.0 * (RawTrend - 0.50))),
        0.0, 100.0,
    )
    MTFConflict = (
        abs(inp.D5 - inp.D15)
        + abs(inp.D15 - inp.D60)
        + abs(inp.D60 - inp.D240)
    ) / 6.0
    MTFRaw = 0.25 * inp.D5 + 0.35 * inp.D15 + 0.25 * inp.D60 + 0.15 * inp.D240
    MTFConfirmPct = _clip(
        (50.0 + 50.0 * MTFRaw) * (1.0 - 0.35 * MTFConflict),
        0.0, 100.0,
    )

    # Phase 3 — Conviction & quality
    CommercialPct = _clip(
        0.50 * inp.DecisionPct
        + 0.15 * SentimentPct
        + 0.20 * AdaptiveTrendPct
        + 0.15 * MTFConfirmPct,
        0.0, 100.0,
    )
    RiskPenalty = 100.0 * (
        0.08 * _pos(inp.EventRiskScore - 0.45) ** 1.35
        + 0.10 * _pos(inp.DriftScore - 0.60) ** 1.40
    )
    if inp.VetoReason:
        ConfPlusPct = 0.0
    else:
        ConfPlusPct = _clip(
            0.62 * inp.ConfidencePct
            + 0.18 * MTFConfirmPct
            + 0.20 * AdaptiveTrendPct
            - RiskPenalty,
            0.0, 99.0,
        )

    # Phase 4 — Real EV (PWinPct uses currently-active coefficients)
    c = _active_pwin_coeffs
    PWinPct = _clip(
        100.0 / (1.0 + math.exp(-(
            c["intercept"]
            + c["ConfPlusPct"] * ConfPlusPct
            + c["CommercialPct"] * CommercialPct
            + c["MTFConfirmPct"] * MTFConfirmPct
            + c["AdaptiveTrendPct"] * AdaptiveTrendPct
            + c["SentimentPct"] * SentimentPct
            + c["EventRiskScore"] * inp.EventRiskScore
            + c["DriftScore"] * inp.DriftScore
        ))),
        5.0, 95.0,
    )
    RRTrue = _clip(
        abs(inp.TargetPx - inp.EntryPx)
        / max(1e-6, abs(inp.EntryPx - inp.StopPx)),
        0.0, 6.0,
    )
    CostR = max(
        0.0,
        inp.FeeR
        + inp.SlipR
        + 0.20 * _pos(inp.EventRiskScore - 0.50)
        + 0.25 * _pos(inp.DriftScore - 0.65),
    )
    p = PWinPct / 100.0
    EV_R = p * RRTrue - (1.0 - p) - CostR
    EdgePct = _clip(50.0 + 50.0 * math.tanh(1.35 * EV_R), 0.0, 100.0)

    # Phase 5 — Final score
    BaseScore = (
        0.28 * CommercialPct
        + 0.27 * ConfPlusPct
        + 0.18 * MTFConfirmPct
        + 0.10 * SentimentPct
        + 0.17 * EdgePct
    )
    HardPenalty = (
        30.0 * _pos(inp.EventRiskScore - 0.60) ** 1.50
        + 24.0 * _pos(inp.DriftScore - 0.70) ** 1.50
        + 20.0 * _pos(0.90 - SentQuality) ** 1.25
    )
    ScoreTotal = _clip(round(BaseScore - HardPenalty, 2), 0.0, 100.0)

    # Gate
    action: TerminalAction
    if inp.VetoReason:
        action = "NO TRADE"
    elif SentQuality < 0.35:
        action = "NO TRADE"
    elif abs(inp.EntryPx - inp.StopPx) <= 0:
        action = "NO TRADE"
    elif abs(inp.TargetPx - inp.EntryPx) <= 0:
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
    elif (
        ScoreTotal >= max(58.0, inp.WatchMin)
        and EV_R > 0
        and PWinPct >= 47.0
    ):
        action = "WATCH"
    else:
        action = "NO TRADE"

    return DecisionOutputs(
        RawSent=RawSent,
        SentQuality=SentQuality,
        SentimentPct=SentimentPct,
        RawTrend=RawTrend,
        AdaptiveTrendPct=AdaptiveTrendPct,
        MTFConflict=MTFConflict,
        MTFConfirmPct=MTFConfirmPct,
        CommercialPct=CommercialPct,
        RiskPenalty=RiskPenalty,
        ConfPlusPct=ConfPlusPct,
        PWinPct=PWinPct,
        RRTrue=RRTrue,
        CostR=CostR,
        EV_R=EV_R,
        EdgePct=EdgePct,
        BaseScore=BaseScore,
        HardPenalty=HardPenalty,
        ScoreTotal=ScoreTotal,
        TerminalAction=action,
    )
