"""Phase 11n-9-oo — ensemble meta-learner + volatility sizing + regime gating.

Five upgrades from the contest-ready report, implemented for our scale:

  U1: meta-learner confidence gating — require composite of
      variant_score * regime_confidence >= threshold. OR-logic
      augmented with an AND-gate on confidence.

  U2: volatility-adaptive sizing — size_mult = min(1.0, sigma_target /
      sigma_30m). When volatility is high, size shrinks proportionally
      to keep per-trade risk constant.

  U3: regime classifier (Calm / Trending / Volatile) — combines
      24h return magnitude + sigma_30m to classify. Each variant
      gets a per-regime weight multiplier.

  U4: slippage budget — expected_slippage_bp subtracted from
      expected return. Reject if net expectancy <= 0.

  U5: ensemble-disagreement trigger — if enabled variants
      disagree with |contrarian_score - momentum_score| > 0.35,
      flag for contradiction freeze evaluation.

Pure functions. No side effects. Safe to call hot.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# U3 — regime classifier
# ---------------------------------------------------------------------------

# Volatility & return bands for regime classification.
SIGMA_CALM_MAX = 0.00015       # below this: calm
SIGMA_VOLATILE_MIN = 0.00040   # above this: volatile
TREND_RET_THRESHOLD = 0.03     # 24h return magnitude to flag trending

Regime = str  # Literal["calm", "trending", "volatile", "unknown"]


def classify_regime(coin: dict[str, Any], mio: Any = None) -> Regime:
    """Return regime label based on coin features.

    Logic:
      - if sigma_30d >= SIGMA_VOLATILE_MIN -> 'volatile'
      - elif |return_24h| >= TREND_RET_THRESHOLD -> 'trending'
      - elif sigma_30d <= SIGMA_CALM_MAX -> 'calm'
      - else -> 'unknown' (transitional)
    """
    try:
        sigma = float(coin.get("sigma_30d") or 0)
        ret_24h = float(coin.get("return_24h") or 0)
    except (TypeError, ValueError):
        return "unknown"
    if sigma >= SIGMA_VOLATILE_MIN:
        return "volatile"
    if abs(ret_24h) >= TREND_RET_THRESHOLD:
        return "trending"
    if 0 < sigma <= SIGMA_CALM_MAX:
        return "calm"
    return "unknown"


# Per-regime variant weight multipliers.
# Each row: (regime, variant) -> size_multiplier applied on top of base.
# Missing entries default to 1.0.
REGIME_VARIANT_WEIGHTS: dict[tuple[str, str], float] = {
    # Volatile: shrink every variant 50% per report spec.
    ("volatile", "contrarian"):       0.50,
    ("volatile", "deep_value"):       0.50,
    ("volatile", "mean_reversion"):   0.50,
    ("volatile", "momentum"):         0.50,
    # Trending: favor momentum (1.2x), shrink contrarian (0.6x).
    ("trending", "momentum"):         1.20,
    ("trending", "contrarian"):       0.60,
    # Calm: favor deep_value (1.15x).
    ("calm", "deep_value"):           1.15,
    ("calm", "momentum"):             0.80,
}


def regime_weight(regime: str, variant: str) -> float:
    return REGIME_VARIANT_WEIGHTS.get((regime, variant), 1.0)


# ---------------------------------------------------------------------------
# U2 — volatility-adaptive sizing
# ---------------------------------------------------------------------------

SIGMA_TARGET = 0.00020        # target per-trade sigma exposure (~2% vol)


def volatility_size_multiplier(coin: dict[str, Any]) -> float:
    """Returns a size multiplier in (0, 1]. When sigma_30d is high the
    multiplier shrinks to keep per-trade risk constant.

    Floor 0.25 to avoid zero-size orders on extreme volatility.
    """
    try:
        sigma = float(coin.get("sigma_30d") or 0)
    except (TypeError, ValueError):
        return 1.0
    if sigma <= 0:
        return 1.0
    m = SIGMA_TARGET / sigma
    return max(0.25, min(m, 1.0))


# ---------------------------------------------------------------------------
# U4 — slippage budget
# ---------------------------------------------------------------------------

def estimated_slippage_bp(coin: dict[str, Any], notional_usd: float) -> float:
    """Heuristic slippage estimate based on depth + spread.

    - Spreads contribute half-spread to slippage.
    - Orders consuming >10% of depth add depth-impact term.
    - Floor 1bp, ceiling 100bp.
    """
    try:
        spread_bp = float(coin.get("spread_bp") or 10.0)
        depth_usd = float(coin.get("depth_usd") or 1000.0)
    except (TypeError, ValueError):
        return 20.0
    half_spread = spread_bp / 2.0
    if depth_usd > 0:
        depth_consumption = min(notional_usd / depth_usd, 1.0)
        depth_impact_bp = depth_consumption * 40.0  # up to 40bp on full-depth order
    else:
        depth_impact_bp = 40.0
    est = half_spread + depth_impact_bp
    return max(1.0, min(est, 100.0))


def net_expectancy_bp(
    variant_score: float, avg_win_bp: float,
    slippage_bp: float, fee_bp: float = 20.0,
) -> float:
    """Net expectancy in bp after subtracting slippage + fees.
    variant_score is used as confidence scaling on avg_win."""
    gross = float(variant_score) * float(avg_win_bp)
    return gross - float(slippage_bp) - float(fee_bp)


# ---------------------------------------------------------------------------
# U1 — meta-learner confidence gate
# ---------------------------------------------------------------------------

META_MIN_SCORE = 0.60         # variant_score floor
META_MIN_REGIME_WEIGHT = 0.50 # regime-weight floor (blocks bad regime trades)
META_MIN_NET_BP = 10.0        # net expectancy must exceed 10bp


@dataclass
class MetaGateResult:
    ok: bool
    variant: str
    variant_score: float
    regime: str
    regime_weight: float
    vol_size_mult: float
    slippage_bp: float
    net_bp: float
    reason: str
    size_multiplier: float  # final size = base_size * size_multiplier

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def meta_gate(
    *, variant: str, variant_score: float,
    coin: dict[str, Any], notional_usd: float,
    avg_win_bp: float = 100.0,
) -> MetaGateResult:
    """U1+U2+U3+U4 combined. Returns MetaGateResult with ok + size mult."""
    regime = classify_regime(coin)
    rw = regime_weight(regime, variant)
    vol_m = volatility_size_multiplier(coin)
    slip = estimated_slippage_bp(coin, notional_usd)
    net = net_expectancy_bp(variant_score, avg_win_bp, slip)
    # Final size multiplier combines regime weight and volatility sizing.
    size_m = rw * vol_m

    reasons: list[str] = []
    if variant_score < META_MIN_SCORE:
        reasons.append(
            f"variant_score {variant_score:.2f} < {META_MIN_SCORE}"
        )
    if rw < META_MIN_REGIME_WEIGHT:
        reasons.append(
            f"regime_weight {rw:.2f} < {META_MIN_REGIME_WEIGHT} (regime={regime})"
        )
    if net < META_MIN_NET_BP:
        reasons.append(
            f"net_expectancy {net:.1f}bp < {META_MIN_NET_BP}bp "
            f"(slippage {slip:.1f}bp eating the edge)"
        )
    if reasons:
        return MetaGateResult(
            ok=False, variant=variant, variant_score=variant_score,
            regime=regime, regime_weight=rw, vol_size_mult=vol_m,
            slippage_bp=slip, net_bp=net,
            reason="; ".join(reasons),
            size_multiplier=size_m,
        )
    return MetaGateResult(
        ok=True, variant=variant, variant_score=variant_score,
        regime=regime, regime_weight=rw, vol_size_mult=vol_m,
        slippage_bp=slip, net_bp=net,
        reason=f"meta OK: regime={regime} rw={rw:.2f} vol_m={vol_m:.2f} net={net:.1f}bp",
        size_multiplier=size_m,
    )


# ---------------------------------------------------------------------------
# U5 — ensemble disagreement trigger
# ---------------------------------------------------------------------------

DISAGREEMENT_THRESHOLD = 0.35


def ensemble_disagreement(variant_scores: dict[str, float]) -> float:
    """Returns max pairwise |a - b| across variant scores.
    Empty or single-variant input returns 0.0."""
    vs = [v for v in variant_scores.values() if v is not None]
    if len(vs) < 2:
        return 0.0
    return max(vs) - min(vs)


def should_trigger_freeze(variant_scores: dict[str, float]) -> tuple[bool, float]:
    d = ensemble_disagreement(variant_scores)
    return (d > DISAGREEMENT_THRESHOLD, d)
