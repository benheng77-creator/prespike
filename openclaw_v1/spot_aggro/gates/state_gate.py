"""
L2 — State Truth Gate + final tier-execution toggle (SPOT AGGRO only).

Order of checks per proposed trade:

    1. State classification (DEAD_CHOP / UNSTABLE → REJECT)
    2. Tier permission table (state not in tier.allowed → REJECT)
    3. Size multiplier (TREND_TRANSITION halves size; informational here)
    4. Tier execution toggle — runs LAST, right at the final order-permission
       step per operator directive. A toggled-off tier that otherwise
       qualifies is logged `analysis_qualified=true, trade_disabled=true`.

Reason codes:
    STA-001  state classification failed (UNKNOWN/UNSTABLE)
    STA-002  state = DEAD_CHOP — no edge
    STA-003  state not in tier's permission set
    STA-004  state computed but feature inputs incomplete
    TIER_APLUS_TRADE_DISABLED
    TIER_A_TRADE_DISABLED
    TIER_B_TRADE_DISABLED
    TIER_C_TRADE_DISABLED

The gate is per-trade. It consults zero account-level figures (capital,
equity, balance). That contract is asserted by a regression test, same
style as L1.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .state_model import (
    ALL_STATES,
    STATE_DEAD_CHOP,
    STATE_TREND_TRANSITION,
    STATE_UNSTABLE,
    StateClassification,
    StateConfig,
    StateFeatures,
    StateModel,
)
from .tier_toggle import TierExecutionToggle, TierDecision, REASON_CODES as TIER_CODES

log = logging.getLogger("spot_aggro.gate.l2")


VERDICT_PASS = "PASS"
VERDICT_REJECT = "REJECT"

REJ_STATE_UNSTABLE   = "STA-001"
REJ_STATE_DEAD       = "STA-002"
REJ_TIER_NOT_ALLOWED = "STA-003"
REJ_STATE_INCOMPLETE = "STA-004"


@dataclass(frozen=True)
class StateGateDecision:
    verdict: str                           # "PASS" | "REJECT"
    reason_code: Optional[str]             # STA-### or TIER_*_TRADE_DISABLED on REJECT
    symbol: str
    tier: str
    state: str
    classification: dict[str, Any]         # serialised StateClassification
    size_multiplier: float
    analysis_qualified: bool               # True iff all analytical gates pass
    trade_disabled_by_toggle: bool         # True iff ONLY toggle blocked the trade
    tier_decision: Optional[dict[str, Any]]
    reason: str
    checked_at_ts: float

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class StateTruthGate:
    """L2 gate with tier-permission enforcement and final execution toggle.

    Usage:
        gate = StateTruthGate()
        feat = StateFeatures(symbol=..., now_ts=..., adx=..., hurst=..., ...)
        decision = gate.check(feat, tier="B", analytically_qualified=True)
        if decision.verdict == "PASS":
            place_order(size_multiplier=decision.size_multiplier)
        else:
            # funnel / rejection_log / dashboard reads decision.reason_code
            # decision.analysis_qualified and decision.trade_disabled_by_toggle
            # tell analytics whether to still count the signal as observed.
            observe_but_do_not_trade(decision)
    """

    def __init__(
        self,
        *,
        state_model: Optional[StateModel] = None,
        tier_toggle: Optional[TierExecutionToggle] = None,
    ) -> None:
        self._model = state_model or StateModel()
        self._toggle = tier_toggle or TierExecutionToggle()

    @property
    def model(self) -> StateModel:
        return self._model

    @property
    def toggle(self) -> TierExecutionToggle:
        return self._toggle

    @property
    def config(self) -> StateConfig:
        return self._model.config

    # --- helpers ----------------------------------------------------------

    def _size_multiplier(self, state: str) -> float:
        mult = self.config.size_multiplier.get(state, 0.0)
        return float(mult)

    def _tier_allowed_states(self, tier: str) -> tuple[str, ...]:
        return tuple(self.config.permissions.get(tier, ()))

    # --- main entry -------------------------------------------------------

    def check(
        self,
        feat: StateFeatures,
        *,
        tier: str,
        analytically_qualified: bool = True,
    ) -> StateGateDecision:
        """Full pass: classify state, check tier permission, check toggle.

        `analytically_qualified` is True when upstream L1 physics + L3
        calibration + L6 audit swarm have already passed. It propagates into
        the decision so analytics can distinguish "signal exists but tier
        trading is off" from "signal failed analysis". The value does not
        affect the verdict — it is a label, not a gate.
        """
        cls = self._model.classify(feat)
        state = cls.state
        reason_prefix = f"{feat.symbol}:{tier}:{state}"

        # 1) Hard UNSTABLE / incomplete state
        if state == STATE_UNSTABLE:
            return self._reject(
                REJ_STATE_UNSTABLE,
                feat=feat, tier=tier, cls=cls,
                message=f"{reason_prefix} UNSTABLE — {cls.reason}",
                analytically_qualified=analytically_qualified,
            )

        # 2) DEAD_CHOP
        if state == STATE_DEAD_CHOP:
            return self._reject(
                REJ_STATE_DEAD,
                feat=feat, tier=tier, cls=cls,
                message=f"{reason_prefix} DEAD_CHOP — no edge",
                analytically_qualified=analytically_qualified,
            )

        # 2b) Incomplete features should have surfaced as UNSTABLE above but
        # guard defensively — a classifier confidence of exactly 0.0 with a
        # non-UNSTABLE label means something is off.
        if cls.confidence <= 0.0 and state not in (STATE_UNSTABLE, STATE_DEAD_CHOP):
            return self._reject(
                REJ_STATE_INCOMPLETE,
                feat=feat, tier=tier, cls=cls,
                message=f"{reason_prefix} state confidence=0 — features incomplete",
                analytically_qualified=analytically_qualified,
            )

        # 3) Tier permission table
        allowed = self._tier_allowed_states(tier)
        if state not in allowed:
            return self._reject(
                REJ_TIER_NOT_ALLOWED,
                feat=feat, tier=tier, cls=cls,
                message=(
                    f"{reason_prefix} state not in tier.allowed={list(allowed)}"
                ),
                analytically_qualified=analytically_qualified,
            )

        size_mult = self._size_multiplier(state)

        # 4) Tier execution toggle — LAST, at the final order-permission step
        tier_dec: TierDecision = self._toggle.decision_for(
            tier,
            qualifying=analytically_qualified,
        )
        if tier_dec.verdict == "BLOCKED":
            reason_code = tier_dec.reason_code or TIER_CODES.get(
                tier, f"TIER_{tier}_TRADE_DISABLED"
            )
            return StateGateDecision(
                verdict=VERDICT_REJECT,
                reason_code=reason_code,
                symbol=feat.symbol,
                tier=tier,
                state=state,
                classification=cls.to_dict(),
                size_multiplier=size_mult,
                analysis_qualified=bool(analytically_qualified),
                trade_disabled_by_toggle=True,
                tier_decision=tier_dec.to_dict(),
                reason=(
                    f"{reason_prefix} analytically_qualified={analytically_qualified} "
                    f"but tier execution toggle is OFF "
                    f"({tier_dec.reason})"
                ),
                checked_at_ts=time.time(),
            )

        # PASS
        return StateGateDecision(
            verdict=VERDICT_PASS,
            reason_code=None,
            symbol=feat.symbol,
            tier=tier,
            state=state,
            classification=cls.to_dict(),
            size_multiplier=size_mult,
            analysis_qualified=bool(analytically_qualified),
            trade_disabled_by_toggle=False,
            tier_decision=tier_dec.to_dict(),
            reason=(
                f"{reason_prefix} ALLOWED size_mult={size_mult:.2f} "
                f"(transitions={cls.transitions_in_window})"
            ),
            checked_at_ts=time.time(),
        )

    # --- internal ---------------------------------------------------------

    def _reject(
        self,
        code: str,
        *,
        feat: StateFeatures,
        tier: str,
        cls: StateClassification,
        message: str,
        analytically_qualified: bool,
    ) -> StateGateDecision:
        size_mult = self._size_multiplier(cls.state)
        d = StateGateDecision(
            verdict=VERDICT_REJECT,
            reason_code=code,
            symbol=feat.symbol,
            tier=tier,
            state=cls.state,
            classification=cls.to_dict(),
            size_multiplier=size_mult,
            analysis_qualified=bool(analytically_qualified),
            trade_disabled_by_toggle=False,
            tier_decision=None,
            reason=f"[{code}] {message}",
            checked_at_ts=time.time(),
        )
        log.info(
            "[L2] REJECT %s tier=%s state=%s %s",
            feat.symbol, tier, cls.state, code,
        )
        return d
