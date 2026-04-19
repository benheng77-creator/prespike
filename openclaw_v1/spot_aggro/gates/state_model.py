"""
L2 — State Model (SPOT AGGRO only).

Pure classification of market state from numeric features. Two independent
classifiers; their agreement is required for any state other than UNSTABLE.

Classifiers:
    C1 (trend):    ADX + Hurst + aligned-candles counter
    C2 (squeeze):  BBW percentile (30d) + volume contraction flag

States:
    TREND_CONFIRMED, TREND_TRANSITION, SQUEEZE_CONFIRMED, POST_SQUEEZE,
    DEAD_CHOP, UNSTABLE

Outputs a classification object that L2's `state_gate.py` consumes.
This module performs NO I/O; features are supplied by the engine.

Spec: TRUTH_FIRST_UPGRADE_PROMPT.md §L2.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Optional

try:
    import yaml
except ImportError as _exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required for the L2 state model") from _exc

log = logging.getLogger("spot_aggro.gate.l2.state_model")

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "state.yml"
)

# --- State labels (canonical strings; never rename without migration) -------
STATE_TREND_CONFIRMED  = "TREND_CONFIRMED"
STATE_TREND_TRANSITION = "TREND_TRANSITION"
STATE_SQUEEZE_CONFIRMED = "SQUEEZE_CONFIRMED"
STATE_POST_SQUEEZE     = "POST_SQUEEZE"
STATE_DEAD_CHOP        = "DEAD_CHOP"
STATE_UNSTABLE         = "UNSTABLE"

ALL_STATES = (
    STATE_TREND_CONFIRMED,
    STATE_TREND_TRANSITION,
    STATE_SQUEEZE_CONFIRMED,
    STATE_POST_SQUEEZE,
    STATE_DEAD_CHOP,
    STATE_UNSTABLE,
)

# --- Classifier labels ------------------------------------------------------
TREND_UP      = "TREND_UP"
TREND_DOWN    = "TREND_DOWN"
TREND_WEAK    = "TREND_WEAK"        # ADX in transition band
TREND_DEAD    = "TREND_DEAD"        # ADX below dead threshold
TREND_UNKNOWN = "TREND_UNKNOWN"

SQZ_CONFIRMED  = "SQUEEZE"
SQZ_EXPANDING  = "POST_SQUEEZE"
SQZ_NEUTRAL    = "NEUTRAL"
SQZ_UNKNOWN    = "SQUEEZE_UNKNOWN"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StateConfig:
    # trend classifier
    adx_trend_confirmed_min: float
    adx_transition_min: float
    adx_transition_max: float
    adx_dead_max: float
    hurst_trend_confirmed_min: float
    hurst_transition_min: float
    hurst_transition_max: float
    aligned_candles_required: int

    # squeeze classifier
    bbw_squeeze_pct_max: float
    bbw_postsqueeze_expansion_min: float
    postsqueeze_max_candles: int
    volume_contraction_required: bool

    # unstable detector
    classifier_disagreement_max: float
    max_transitions_window_s: int
    max_transitions_allowed: int

    permissions: dict[str, tuple[str, ...]]
    size_multiplier: dict[str, float]

    @staticmethod
    def load(path: Optional[Path] = None) -> "StateConfig":
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            raise FileNotFoundError(f"L2 state config not found: {cfg_path}")
        with cfg_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if raw.get("engine") != "spot_aggro":
            raise ValueError(
                f"L2 state config engine must be 'spot_aggro' "
                f"(got {raw.get('engine')!r})."
            )
        trend = raw["classifier_trend"]
        sqz = raw["classifier_squeeze"]
        unst = raw["unstable_detector"]
        perms = {
            tier: tuple(row["allowed"])
            for tier, row in (raw.get("permissions") or {}).items()
        }
        sizes = {k: float(v) for k, v in (raw.get("size_multiplier") or {}).items()}
        for s in ALL_STATES:
            if s not in sizes:
                raise ValueError(
                    f"size_multiplier missing entry for state {s}"
                )
        return StateConfig(
            adx_trend_confirmed_min=float(trend["adx_trend_confirmed_min"]),
            adx_transition_min=float(trend["adx_transition_min"]),
            adx_transition_max=float(trend["adx_transition_max"]),
            adx_dead_max=float(trend["adx_dead_max"]),
            hurst_trend_confirmed_min=float(trend["hurst_trend_confirmed_min"]),
            hurst_transition_min=float(trend["hurst_transition_min"]),
            hurst_transition_max=float(trend["hurst_transition_max"]),
            aligned_candles_required=int(trend["aligned_candles_required"]),
            bbw_squeeze_pct_max=float(sqz["bbw_squeeze_pct_max"]),
            bbw_postsqueeze_expansion_min=float(sqz["bbw_postsqueeze_expansion_min"]),
            postsqueeze_max_candles=int(sqz["postsqueeze_max_candles"]),
            volume_contraction_required=bool(sqz["volume_contraction_required"]),
            classifier_disagreement_max=float(unst["classifier_disagreement_max"]),
            max_transitions_window_s=int(unst["max_transitions_window_s"]),
            max_transitions_allowed=int(unst["max_transitions_allowed"]),
            permissions=perms,
            size_multiplier=sizes,
        )


# ---------------------------------------------------------------------------
# Feature and classification types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StateFeatures:
    """Numeric features passed in by the engine at decision time."""
    symbol: str
    now_ts: float
    adx: Optional[float]                       # current ADX
    hurst: Optional[float]                     # current Hurst
    aligned_candle_count: Optional[int]        # consecutive same-direction candles
    bbw_percentile_30d: Optional[float]        # 0..100
    bbw_expansion_pct: Optional[float]         # fractional expansion vs squeeze
    candles_since_squeeze_break: Optional[int]
    volume_contracting: Optional[bool]
    sigma_realized_percentile: Optional[float] # 0..100 (for DEAD_CHOP)


@dataclass(frozen=True)
class ClassifierOutput:
    label: str
    confidence: float                          # 0..1
    reason: str


@dataclass(frozen=True)
class StateClassification:
    state: str                                 # one of ALL_STATES
    confidence: float                          # min of classifier confidences
    classifier_agreement: float                # 0..1, 1.0 = full agreement
    c1: ClassifierOutput                       # trend classifier
    c2: ClassifierOutput                       # squeeze classifier
    transitions_in_window: int
    reason: str
    ts: float

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        return d


# ---------------------------------------------------------------------------
# Classifier 1 — trend (ADX + Hurst + alignment)
# ---------------------------------------------------------------------------

def classify_trend(feat: StateFeatures, cfg: StateConfig) -> ClassifierOutput:
    """ADX/Hurst-based trend regime. Returns one of:
        TREND_UP / TREND_DOWN  (when ADX and Hurst both clear)
        TREND_WEAK             (transition band on either axis)
        TREND_DEAD             (ADX below dead threshold)
        TREND_UNKNOWN          (any input is None)

    Direction (UP/DOWN) requires `aligned_candle_count` to be sign-carrying
    in the same direction as the trend. We treat positive count as UP and
    negative as DOWN; zero or missing leaves the direction undetermined.
    """
    if feat.adx is None or feat.hurst is None:
        return ClassifierOutput(
            label=TREND_UNKNOWN, confidence=0.0,
            reason="missing adx or hurst",
        )
    adx = float(feat.adx)
    hurst = float(feat.hurst)

    if adx < cfg.adx_dead_max:
        return ClassifierOutput(
            label=TREND_DEAD, confidence=0.9,
            reason=f"adx={adx:.1f}<dead_max={cfg.adx_dead_max}",
        )

    trend_strong = (
        adx >= cfg.adx_trend_confirmed_min
        and hurst >= cfg.hurst_trend_confirmed_min
        and (feat.aligned_candle_count or 0) >= cfg.aligned_candles_required
    )
    trend_transition = (
        cfg.adx_transition_min <= adx < cfg.adx_transition_max
        or cfg.hurst_transition_min <= hurst < cfg.hurst_transition_max
    )

    if trend_strong:
        direction = TREND_UP if (feat.aligned_candle_count or 0) > 0 else TREND_DOWN
        return ClassifierOutput(
            label=direction,
            confidence=min(1.0, (adx / 40.0) * hurst),
            reason=f"adx={adx:.1f} hurst={hurst:.2f} aligned>={cfg.aligned_candles_required}",
        )
    if trend_transition:
        return ClassifierOutput(
            label=TREND_WEAK,
            confidence=0.5,
            reason=f"adx={adx:.1f} hurst={hurst:.2f} in transition band",
        )
    return ClassifierOutput(
        label=TREND_UNKNOWN, confidence=0.3,
        reason=f"adx={adx:.1f} hurst={hurst:.2f} neither strong nor transition",
    )


# ---------------------------------------------------------------------------
# Classifier 2 — squeeze (BBW percentile + volume)
# ---------------------------------------------------------------------------

def classify_squeeze(feat: StateFeatures, cfg: StateConfig) -> ClassifierOutput:
    """BBW+volume squeeze state. Returns one of:
        SQUEEZE_CONFIRMED  BBW below 20th pct (30d) AND volume contracting
        POST_SQUEEZE       BBW expanding >= threshold within N candles of break
        NEUTRAL            neither condition satisfied
        SQUEEZE_UNKNOWN    any input is None
    """
    if feat.bbw_percentile_30d is None:
        return ClassifierOutput(
            label=SQZ_UNKNOWN, confidence=0.0,
            reason="missing bbw_percentile_30d",
        )
    bbw = float(feat.bbw_percentile_30d)

    # POST_SQUEEZE: must be within N candles and expansion >= threshold
    if (
        feat.candles_since_squeeze_break is not None
        and feat.bbw_expansion_pct is not None
        and 0 <= feat.candles_since_squeeze_break <= cfg.postsqueeze_max_candles
        and feat.bbw_expansion_pct >= cfg.bbw_postsqueeze_expansion_min
    ):
        return ClassifierOutput(
            label=SQZ_EXPANDING,
            confidence=min(1.0, feat.bbw_expansion_pct / (cfg.bbw_postsqueeze_expansion_min * 3.0 + 1e-9)),
            reason=(
                f"expansion={feat.bbw_expansion_pct:.3f} in "
                f"{feat.candles_since_squeeze_break}/{cfg.postsqueeze_max_candles} candles"
            ),
        )

    # SQUEEZE_CONFIRMED: BBW below percentile cap + (optionally) volume contracting
    if bbw <= cfg.bbw_squeeze_pct_max:
        if cfg.volume_contraction_required and not bool(feat.volume_contracting):
            return ClassifierOutput(
                label=SQZ_NEUTRAL, confidence=0.4,
                reason=f"bbw_pct={bbw:.1f} low but volume not contracting",
            )
        return ClassifierOutput(
            label=SQZ_CONFIRMED,
            confidence=min(1.0, (cfg.bbw_squeeze_pct_max - bbw) / cfg.bbw_squeeze_pct_max + 0.5),
            reason=f"bbw_pct={bbw:.1f} <= {cfg.bbw_squeeze_pct_max}, volume_contracting={feat.volume_contracting}",
        )
    return ClassifierOutput(
        label=SQZ_NEUTRAL, confidence=0.5,
        reason=f"bbw_pct={bbw:.1f} neutral",
    )


# ---------------------------------------------------------------------------
# State fusion
# ---------------------------------------------------------------------------

def _disagreement(c1: ClassifierOutput, c2: ClassifierOutput) -> float:
    """0..1 disagreement metric.

    The classifiers speak different axes (trend vs squeeze), so label
    mismatch is expected and not a disagreement. A real disagreement is:
      * either classifier is UNKNOWN (data-quality problem), OR
      * the two classifiers assert mutually-exclusive regimes: c1 claims a
        strong trend (UP/DOWN) while c2 claims an active SQUEEZE (the price
        cannot be both strongly trending and inside a volatility squeeze).

    Everything else — DEAD + NEUTRAL, WEAK + SQUEEZE, UP + POST_SQUEEZE —
    is coherent and scores agreement=1.
    """
    if c1.label == TREND_UNKNOWN or c2.label == SQZ_UNKNOWN:
        return 1.0
    if c1.label in (TREND_UP, TREND_DOWN) and c2.label == SQZ_CONFIRMED:
        return 1.0
    return 0.0


def fuse_state(
    feat: StateFeatures,
    c1: ClassifierOutput,
    c2: ClassifierOutput,
    cfg: StateConfig,
    *,
    transitions_in_window: int = 0,
) -> StateClassification:
    """Combine the two classifier outputs into a final state label.

    Rules (in order):
      1. If either classifier is UNKNOWN → UNSTABLE (one-classifier result
         is treated as UNSTABLE per spec §L2).
      2. If disagreement > threshold OR transitions > max → UNSTABLE.
      3. Label rules:
         - c2 == POST_SQUEEZE                                 → POST_SQUEEZE
         - c2 == SQUEEZE_CONFIRMED and c1 in {DEAD, WEAK}     → SQUEEZE_CONFIRMED
         - c1 in {UP, DOWN} and c2 in {NEUTRAL, SQZ_CONFIRMED}→ TREND_CONFIRMED
         - c1 == WEAK                                         → TREND_TRANSITION
         - c1 == DEAD and sigma percentile low                → DEAD_CHOP
         - else                                               → UNSTABLE
    """
    agreement = 1.0 - _disagreement(c1, c2)

    # Rule 1 + 2: forced UNSTABLE
    if c1.label == TREND_UNKNOWN or c2.label == SQZ_UNKNOWN:
        return StateClassification(
            state=STATE_UNSTABLE,
            confidence=0.0,
            classifier_agreement=0.0,
            c1=c1, c2=c2,
            transitions_in_window=transitions_in_window,
            reason="one-classifier UNKNOWN → UNSTABLE",
            ts=feat.now_ts,
        )
    if (1.0 - agreement) > cfg.classifier_disagreement_max:
        return StateClassification(
            state=STATE_UNSTABLE,
            confidence=min(c1.confidence, c2.confidence),
            classifier_agreement=agreement,
            c1=c1, c2=c2,
            transitions_in_window=transitions_in_window,
            reason=(
                f"classifier disagreement {1.0 - agreement:.2f} > "
                f"{cfg.classifier_disagreement_max}"
            ),
            ts=feat.now_ts,
        )
    if transitions_in_window > cfg.max_transitions_allowed:
        return StateClassification(
            state=STATE_UNSTABLE,
            confidence=min(c1.confidence, c2.confidence),
            classifier_agreement=agreement,
            c1=c1, c2=c2,
            transitions_in_window=transitions_in_window,
            reason=(
                f"{transitions_in_window} transitions in window > "
                f"{cfg.max_transitions_allowed}"
            ),
            ts=feat.now_ts,
        )

    # Rule 3: label fusion
    state: str
    reason: str
    if c2.label == SQZ_EXPANDING:
        state = STATE_POST_SQUEEZE
        reason = "c2=POST_SQUEEZE dominates"
    elif c2.label == SQZ_CONFIRMED and c1.label in (TREND_DEAD, TREND_WEAK):
        state = STATE_SQUEEZE_CONFIRMED
        reason = "c2=SQUEEZE and c1 quiescent"
    elif c1.label in (TREND_UP, TREND_DOWN):
        state = STATE_TREND_CONFIRMED
        reason = f"c1={c1.label} with c2={c2.label}"
    elif c1.label == TREND_WEAK:
        state = STATE_TREND_TRANSITION
        reason = "c1=WEAK → TREND_TRANSITION"
    elif c1.label == TREND_DEAD:
        # DEAD_CHOP demands sigma percentile confirmation
        sigma = feat.sigma_realized_percentile
        if sigma is not None and sigma < 30.0:
            state = STATE_DEAD_CHOP
            reason = f"c1=DEAD and sigma_pct={sigma:.1f} < 30"
        else:
            state = STATE_UNSTABLE
            reason = f"c1=DEAD but sigma_pct={sigma} not confirming DEAD_CHOP"
    else:
        state = STATE_UNSTABLE
        reason = "no fusion rule matched"

    return StateClassification(
        state=state,
        confidence=min(c1.confidence, c2.confidence),
        classifier_agreement=agreement,
        c1=c1, c2=c2,
        transitions_in_window=transitions_in_window,
        reason=reason,
        ts=feat.now_ts,
    )


# ---------------------------------------------------------------------------
# Stateful model — tracks per-symbol transitions
# ---------------------------------------------------------------------------

class StateModel:
    """Per-symbol transition tracker + classifier fusion facade.

    Maintains a bounded ring of (ts, state) per symbol so the UNSTABLE
    detector can check "transitions in last N seconds". Features are still
    computed externally and passed in via `classify(feat)`.
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._cfg = StateConfig.load(self._config_path)
        # symbol -> deque[(ts, state)]
        self._history: dict[str, Deque[tuple[float, str]]] = {}

    @property
    def config(self) -> StateConfig:
        return self._cfg

    def reload_config(self) -> StateConfig:
        self._cfg = StateConfig.load(self._config_path)
        return self._cfg

    def _transitions_in_window(self, symbol: str, now_ts: float) -> int:
        hist = self._history.get(symbol)
        if not hist:
            return 0
        window_start = now_ts - self._cfg.max_transitions_window_s
        changes = 0
        prev_state: Optional[str] = None
        for ts, state in hist:
            if ts < window_start:
                prev_state = state
                continue
            if prev_state is not None and state != prev_state:
                changes += 1
            prev_state = state
        return changes

    def classify(self, feat: StateFeatures) -> StateClassification:
        c1 = classify_trend(feat, self._cfg)
        c2 = classify_squeeze(feat, self._cfg)
        transitions = self._transitions_in_window(feat.symbol, feat.now_ts)
        cls = fuse_state(feat, c1, c2, self._cfg, transitions_in_window=transitions)

        # record
        buf = self._history.setdefault(feat.symbol, deque(maxlen=256))
        buf.append((feat.now_ts, cls.state))
        return cls

    def last_state(self, symbol: str) -> Optional[str]:
        buf = self._history.get(symbol)
        if not buf:
            return None
        return buf[-1][1]
