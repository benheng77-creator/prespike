"""Phase 11n-9-ee — Strategy-variant shadow racer.

Three strategies compete in shadow for first-to-200-positive-Wilson
exits. No real capital touched by any variant.

Variants
--------
- control           Current composite scorer (tier gate at 0.70).
- contrarian        Buy coins the control scorer REJECTS at the bottom
                    of its distribution. Hypothesis: the control
                    scorer is anti-correlated with realized PnL, so
                    its worst picks should be the best trades.
- mean_reversion    Classic oversold-bounce pattern, no composite
                    score involved. Admits coins with:
                      fz <= -1.0   (deep negative funding = squeeze)
                      24h return <= -8%
                      depth_usd > $200k (liquidity floor)
                      spread_bp <= 15 (executable)
                    Exit rules are MANAGED by the same engine as
                    control — we only differ in WHICH coins get
                    admitted, not how they close.

Contract
--------
Each variant exposes:
  evaluate(coin, mio) -> VariantDecision

Returns a VariantDecision with:
  score       : float — variant-specific score (diagnostics only)
  passed      : bool — would this variant have entered?
  reason      : str — one-line explanation for logs/dashboard
  evidence    : dict — feature values that drove the decision

Decisions are logged to `shadow_variant_authorizations` by the
three_way_shadow scorer. Every live exit mirrors into every variant
that admitted the same trade, so all three get apples-to-apples PnL.

Never trades. Never consults capital. Fail-open: any exception in a
variant is logged and the variant is marked `did_not_fire` for that
tick, never blocks the live path.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Tunables — centralize thresholds so tests can monkey-patch.
# ---------------------------------------------------------------------------

# Contrarian admits when the control score is in the bottom quartile of
# its range. Control gate is 0.70; contrarian gate admits scores <= 0.30.
CONTRARIAN_MAX_SCORE = 0.30

# Mean-reversion thresholds.
MR_FUNDING_Z_MAX = -1.0          # fz <= -1.0 (deep negative squeeze)
MR_24H_RETURN_MAX = -0.08        # 24h return <= -8%
MR_MIN_DEPTH_USD = 200_000.0     # liquidity floor
MR_MAX_SPREAD_BP = 15.0          # executable spreads only
MR_RSI_MAX = 25.0                # oversold (optional; if RSI present)


@dataclass
class VariantDecision:
    variant: str
    score: float
    passed: bool
    reason: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Variant: control — wraps the existing scorer
# ---------------------------------------------------------------------------

def evaluate_control(coin: dict[str, Any], mio: Any) -> VariantDecision:
    try:
        from spot_aggro.scoring import compute_composite_score
        s = float(compute_composite_score(coin, mio) or 0.0)
    except Exception as e:  # noqa: BLE001
        return VariantDecision(
            variant="control", score=0.0, passed=False,
            reason=f"scoring-error: {str(e)[:80]}",
            evidence={"error": str(e)[:120]},
        )
    passed = s >= 0.70
    return VariantDecision(
        variant="control", score=round(s, 4), passed=passed,
        reason=f"composite={s:.3f} {'>=0.70 pass' if passed else '<0.70 reject'}",
        evidence={"composite": s},
    )


# ---------------------------------------------------------------------------
# Variant: contrarian — buy what control rejects at the bottom
# ---------------------------------------------------------------------------

def evaluate_contrarian(coin: dict[str, Any], mio: Any) -> VariantDecision:
    try:
        from spot_aggro.scoring import compute_composite_score
        s = float(compute_composite_score(coin, mio) or 0.0)
    except Exception as e:  # noqa: BLE001
        return VariantDecision(
            variant="contrarian", score=0.0, passed=False,
            reason=f"scoring-error: {str(e)[:80]}",
            evidence={"error": str(e)[:120]},
        )
    # Contrarian passes when control's composite is bottom-quartile.
    # Liquidity + spread floors still apply so we don't buy dust.
    depth = float(coin.get("depth_usd", 0) or 0)
    spread = float(coin.get("spread_bp", 999) or 999)
    liquid = depth >= MR_MIN_DEPTH_USD and spread <= MR_MAX_SPREAD_BP
    passed = (s <= CONTRARIAN_MAX_SCORE) and liquid
    reason_bits = [f"composite={s:.3f}"]
    if s > CONTRARIAN_MAX_SCORE:
        reason_bits.append(f">={CONTRARIAN_MAX_SCORE:.2f} (not bottom-quartile)")
    if not liquid:
        reason_bits.append(
            f"illiquid depth=${depth:,.0f} spread={spread:.1f}bp"
        )
    if passed:
        reason_bits.append("contrarian ADMIT")
    return VariantDecision(
        variant="contrarian",
        score=round(1.0 - s, 4),       # invert so higher = stronger admit
        passed=passed,
        reason=" ".join(reason_bits),
        evidence={
            "control_composite": s,
            "depth_usd": depth,
            "spread_bp": spread,
            "gate": CONTRARIAN_MAX_SCORE,
        },
    )


# ---------------------------------------------------------------------------
# Variant: mean_reversion — classic oversold-bounce
# ---------------------------------------------------------------------------

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def evaluate_mean_reversion(coin: dict[str, Any], mio: Any) -> VariantDecision:
    fz = _safe_float(coin.get("funding_z"), default=0.0)
    ret_24h = _safe_float(coin.get("return_24h"), default=0.0)
    depth = _safe_float(coin.get("depth_usd"), default=0.0)
    spread = _safe_float(coin.get("spread_bp"), default=999.0)
    rsi = coin.get("rsi_14")                 # optional
    rsi_ok = True
    rsi_val: float | None = None
    if rsi is not None:
        rsi_val = _safe_float(rsi, default=50.0)
        rsi_ok = rsi_val <= MR_RSI_MAX

    # Five filters. All must pass.
    checks = {
        "funding_squeeze": fz <= MR_FUNDING_Z_MAX,
        "deep_drawdown": ret_24h <= MR_24H_RETURN_MAX,
        "liquidity": depth >= MR_MIN_DEPTH_USD,
        "spread": spread <= MR_MAX_SPREAD_BP,
        "rsi_oversold_or_absent": rsi_ok,
    }
    passed = all(checks.values())
    failed = [k for k, v in checks.items() if not v]
    # Synthetic score: how deeply oversold are we?
    # Combine fz and return_24h into [0, 1].
    fz_strength = min(max((-fz) / 3.0, 0.0), 1.0)     # fz=-3 -> 1.0
    ret_strength = min(max((-ret_24h) / 0.20, 0.0), 1.0)  # -20% -> 1.0
    score = round((fz_strength + ret_strength) / 2.0, 4)

    if passed:
        reason = (
            f"mean-rev ADMIT fz={fz:.2f} ret24h={ret_24h * 100:.1f}% "
            f"depth=${depth:,.0f} spread={spread:.1f}bp"
        )
    else:
        reason = f"mean-rev reject: failed={','.join(failed)}"

    return VariantDecision(
        variant="mean_reversion",
        score=score,
        passed=passed,
        reason=reason,
        evidence={
            "funding_z": fz,
            "return_24h": ret_24h,
            "depth_usd": depth,
            "spread_bp": spread,
            "rsi_14": rsi_val,
            "checks": checks,
        },
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

VARIANT_NAMES: tuple[str, ...] = ("control", "contrarian", "mean_reversion")


def evaluate_all(coin: dict[str, Any], mio: Any) -> list[VariantDecision]:
    """Run every registered variant against the same coin + mio snapshot.
    Fail-open: a variant that raises returns a did-not-fire decision."""
    out: list[VariantDecision] = []
    for name, fn in (
        ("control", evaluate_control),
        ("contrarian", evaluate_contrarian),
        ("mean_reversion", evaluate_mean_reversion),
    ):
        try:
            out.append(fn(coin, mio))
        except Exception as e:  # noqa: BLE001
            out.append(VariantDecision(
                variant=name, score=0.0, passed=False,
                reason=f"variant-error: {str(e)[:80]}",
                evidence={"error": str(e)[:120]},
            ))
    return out
