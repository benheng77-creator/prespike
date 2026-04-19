"""
L3 — Calibration Gate (SPOT AGGRO only).

Reads `spot_aggro_calibration_table` and answers "is this (score, tier,
symbol, regime) a VALID bucket we should trade?"

Verdicts:
    PASS        bucket.status == VALID
    PASS_FLAGGED bucket.status == NON_MONOTONIC (positive expectancy but
                 non-monotone vs lower deciles — tradeable, logged)
    REJECT      bucket.status == INVALID
    REJECT      bucket.status == INSUFFICIENT_DATA
    REJECT      no bucket row for this key at all

The gate NEVER consults capital/equity/balance. Like L1/L2, it is purely
per-trade. Tier C is treated like any other tier — its bucket can be
VALID, INVALID, INSUFFICIENT, or NON_MONOTONIC. Nothing here hard-bans or
hard-excludes any tier; that is the tier execution toggle's job (L2 final
step, Phase 4).

Reason codes (stable):
    CAL-001   no bucket row for (tier, symbol, regime, decile)
    CAL-002   bucket INSUFFICIENT_DATA
    CAL-003   bucket INVALID (negative expectancy)
    CAL-004   invalid input (None score, unknown tier, ...)
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from . import calibration_engine as cal_engine
from . import calibration_store as store

log = logging.getLogger("spot_aggro.gate.l3")


VERDICT_PASS         = "PASS"
VERDICT_PASS_FLAGGED = "PASS_FLAGGED"
VERDICT_REJECT       = "REJECT"

REJ_NO_BUCKET     = "CAL-001"
REJ_INSUFFICIENT  = "CAL-002"
REJ_INVALID       = "CAL-003"
REJ_INVALID_INPUT = "CAL-004"


@dataclass(frozen=True)
class CalibrationDecision:
    verdict: str                 # PASS | PASS_FLAGGED | REJECT
    reason_code: Optional[str]   # None on clean PASS; CAL-### or flag
    tier: str
    symbol: str
    regime: str
    composite_score: float
    score_decile: int
    bucket_status: Optional[str]
    bucket: Optional[dict[str, Any]]
    reason: str
    checked_at_ts: float

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class CalibrationGate:
    """Read-only L3 gate. Backed by the persisted calibration table.

    Writers (nightly calibration runs) go through `calibration_engine.build_*`.
    This class never writes.
    """

    def check(
        self,
        *,
        tier: str,
        symbol: str,
        regime: str,
        composite_score: float,
    ) -> CalibrationDecision:
        if not isinstance(tier, str) or not tier:
            return self._invalid_input("tier must be a non-empty string",
                                       tier, symbol, regime, composite_score)
        if not isinstance(symbol, str) or not symbol:
            return self._invalid_input("symbol must be a non-empty string",
                                       tier, symbol, regime, composite_score)
        if composite_score is None:
            return self._invalid_input("composite_score is None",
                                       tier, symbol, regime, 0.0)

        decile = cal_engine.score_to_decile(float(composite_score))
        regime_key = regime or cal_engine.DEFAULT_REGIME_LABEL
        bucket = store.lookup(tier, symbol, regime_key, decile)

        if bucket is None:
            return CalibrationDecision(
                verdict=VERDICT_REJECT,
                reason_code=REJ_NO_BUCKET,
                tier=tier, symbol=symbol, regime=regime_key,
                composite_score=float(composite_score),
                score_decile=decile,
                bucket_status=None,
                bucket=None,
                reason=(
                    f"[{REJ_NO_BUCKET}] no calibration bucket for "
                    f"tier={tier} symbol={symbol} regime={regime_key} "
                    f"decile={decile}"
                ),
                checked_at_ts=time.time(),
            )

        status = bucket.status
        base = dict(
            tier=tier, symbol=symbol, regime=regime_key,
            composite_score=float(composite_score),
            score_decile=decile,
            bucket_status=status,
            bucket=bucket.to_dict(),
        )

        if status == store.STATUS_VALID:
            return CalibrationDecision(
                verdict=VERDICT_PASS,
                reason_code=None,
                reason=(
                    f"bucket VALID mean_pct={bucket.mean_pnl_pct:+.4f} "
                    f"n={bucket.n_trades}"
                ),
                checked_at_ts=time.time(),
                **base,
            )
        if status == store.STATUS_NON_MONOTONIC:
            # Positive expectancy but not monotonic — tradeable with a flag.
            return CalibrationDecision(
                verdict=VERDICT_PASS_FLAGGED,
                reason_code=store.STATUS_NON_MONOTONIC,
                reason=(
                    f"bucket NON_MONOTONIC mean_pct={bucket.mean_pnl_pct:+.4f} "
                    f"n={bucket.n_trades} — tradeable but flagged"
                ),
                checked_at_ts=time.time(),
                **base,
            )
        if status == store.STATUS_INSUFFICIENT:
            return CalibrationDecision(
                verdict=VERDICT_REJECT,
                reason_code=REJ_INSUFFICIENT,
                reason=(
                    f"[{REJ_INSUFFICIENT}] bucket n={bucket.n_trades} < "
                    f"{cal_engine.MIN_TRADES_PER_BUCKET}"
                ),
                checked_at_ts=time.time(),
                **base,
            )
        if status == store.STATUS_INVALID:
            return CalibrationDecision(
                verdict=VERDICT_REJECT,
                reason_code=REJ_INVALID,
                reason=(
                    f"[{REJ_INVALID}] bucket mean_pct={bucket.mean_pnl_pct:+.4f} "
                    f"n={bucket.n_trades}"
                ),
                checked_at_ts=time.time(),
                **base,
            )

        # Unknown status — treat defensively as REJECT without crashing.
        return CalibrationDecision(
            verdict=VERDICT_REJECT,
            reason_code=REJ_INVALID,
            reason=f"[{REJ_INVALID}] unexpected bucket status={status}",
            checked_at_ts=time.time(),
            **base,
        )

    # --- helpers ----------------------------------------------------------

    def _invalid_input(
        self, message: str,
        tier: str, symbol: str, regime: str, composite_score: float,
    ) -> CalibrationDecision:
        return CalibrationDecision(
            verdict=VERDICT_REJECT,
            reason_code=REJ_INVALID_INPUT,
            tier=tier or "", symbol=symbol or "",
            regime=regime or cal_engine.DEFAULT_REGIME_LABEL,
            composite_score=float(composite_score or 0.0),
            score_decile=0,
            bucket_status=None,
            bucket=None,
            reason=f"[{REJ_INVALID_INPUT}] {message}",
            checked_at_ts=time.time(),
        )
