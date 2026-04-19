"""
L0 — Capital Viability Advisory (SPOT AGGRO only, NON-BLOCKING).

Operator directive (post-Phase-2): L0 is advisory, not a blocker. It must
never refuse to start the engine and must never reject a trade purely on a
capital threshold. It publishes an analysis, a gap versus a reference
threshold, and an optional recommendation. The engine continues normally
regardless of the verdict.

Verdicts:
    OK    — working capital is at or above the reference viable threshold.
    WARN  — working capital is below the reference threshold. Engine keeps
            running. Recommendation is attached for operator awareness.

There is no FAIL / REFUSE / REJECT state on this gate. Any future caller
that wants a binary guard must look elsewhere; L0 will not provide one.

This file is:
    - deterministic: same inputs always produce the same decision
    - config-driven: every threshold lives in spot_aggro/config/capital.yml
    - idempotent: safe to call on every boot and every 30 days
    - separated: imports nothing from legacy engine code, must not be
      imported from it
    - non-blocking: never raises on low capital; never exits the process
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError as _exc:  # pragma: no cover - deps are declared in requirements.txt
    raise RuntimeError("PyYAML is required for the L0 capital advisory") from _exc

log = logging.getLogger("spot_aggro.gate.l0")

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "capital.yml"
)

REQUIRED_KEYS = (
    "schema_version",
    "engine",
    "mode",
    "reference_min_viable_capital_usd",
    "avg_notional_usd",
    "daily_trade_count",
    "round_trip_cost_bp",
    "required_edge_multiple",
    "safety_cost_multiple",
    "emergency_reserve_pct",
    "recheck_interval_days",
)


@dataclass(frozen=True)
class CapitalConfig:
    """Validated, immutable snapshot of capital.yml."""

    schema_version: str
    engine: str
    mode: str
    reference_min_viable_capital_usd: float
    avg_notional_usd: float
    daily_trade_count: int
    round_trip_cost_bp: float
    required_edge_multiple: float
    safety_cost_multiple: float
    emergency_reserve_pct: float
    recheck_interval_days: int
    recheck_on_startup: bool
    recommendation_summary: str
    recommendation_suggestion: str
    recommendation_contact: str

    @staticmethod
    def load(path: Optional[Path] = None) -> "CapitalConfig":
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            raise FileNotFoundError(f"L0 capital config not found: {cfg_path}")
        with cfg_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        missing = [k for k in REQUIRED_KEYS if k not in raw]
        if missing:
            raise ValueError(
                f"L0 capital config {cfg_path} missing required keys: {missing}"
            )
        if raw["engine"] != "spot_aggro":
            raise ValueError(
                f"L0 capital config engine must be 'spot_aggro' (got {raw['engine']!r}). "
                "This advisory is spot_aggro-only; do not reuse for legacy engines."
            )

        rec = raw.get("recommendation_when_below_reference") or {}
        return CapitalConfig(
            schema_version=str(raw["schema_version"]),
            engine=str(raw["engine"]),
            mode=str(raw["mode"]),
            reference_min_viable_capital_usd=float(
                raw["reference_min_viable_capital_usd"]
            ),
            avg_notional_usd=float(raw["avg_notional_usd"]),
            daily_trade_count=int(raw["daily_trade_count"]),
            round_trip_cost_bp=float(raw["round_trip_cost_bp"]),
            required_edge_multiple=float(raw["required_edge_multiple"]),
            safety_cost_multiple=float(raw["safety_cost_multiple"]),
            emergency_reserve_pct=float(raw["emergency_reserve_pct"]),
            recheck_interval_days=int(raw["recheck_interval_days"]),
            recheck_on_startup=bool(raw.get("recheck_on_startup", True)),
            recommendation_summary=str(rec.get("summary", "")),
            recommendation_suggestion=str(rec.get("suggestion", "")),
            recommendation_contact=str(rec.get("contact", "")),
        )


# ---------------------------------------------------------------------------
# Decision type
# ---------------------------------------------------------------------------

VERDICT_OK = "OK"
VERDICT_WARN = "WARN"


@dataclass(frozen=True)
class CapitalDecision:
    """Result of an advisory check.

    verdict is one of:
        OK    — capital >= reference viable threshold.
        WARN  — capital <  reference viable threshold. Engine keeps running.

    There is no blocking verdict. gap_usd is positive when below the
    reference threshold and zero otherwise; it is purely informational.
    """

    verdict: str                     # "OK" | "WARN"
    blocking: bool                   # always False — kept for schema stability
    current_capital_usd: float
    reference_min_viable_capital_usd: float
    gap_usd: float                   # positive = below reference, zero otherwise
    derived_from: dict[str, Any]     # arithmetic breakdown for audit
    reason: str
    recommendation: str
    checked_at_ts: float             # epoch seconds
    config_schema_version: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Core arithmetic
# ---------------------------------------------------------------------------

def compute_reference_min_viable_capital(cfg: CapitalConfig) -> dict[str, float]:
    """Return the informational reference-viable-capital level.

    Two reference bounds are considered; the larger is reported:

      A) Operator reference floor
         ref_floor = cfg.reference_min_viable_capital_usd

      B) Daily-friction reference bound
         daily_friction_usd =
             avg_notional_usd * daily_trade_count * (round_trip_cost_bp / 1e4)
         friction_reference = daily_friction_usd / safety_cost_multiple

    Neither bound is enforced — both are reporting aids.
    """

    bps = cfg.round_trip_cost_bp / 10_000.0
    per_trade_cost_usd = cfg.avg_notional_usd * bps
    daily_friction_usd = per_trade_cost_usd * cfg.daily_trade_count

    if cfg.safety_cost_multiple <= 0:
        raise ValueError("safety_cost_multiple must be > 0")
    friction_reference = daily_friction_usd / cfg.safety_cost_multiple

    reference = max(cfg.reference_min_viable_capital_usd, friction_reference)

    return {
        "per_trade_cost_usd": round(per_trade_cost_usd, 6),
        "daily_friction_usd": round(daily_friction_usd, 4),
        "friction_reference_usd": round(friction_reference, 2),
        "operator_reference_usd": round(cfg.reference_min_viable_capital_usd, 2),
        "binding_reference": (
            "operator_reference"
            if cfg.reference_min_viable_capital_usd >= friction_reference
            else "daily_friction"
        ),
        "reference_min_viable_capital_usd": round(reference, 2),
    }


def check_viability(
    current_capital_usd: float,
    cfg: Optional[CapitalConfig] = None,
) -> CapitalDecision:
    """Return an advisory OK/WARN decision for current working capital.

    Never raises on low capital. Only raises on malformed input (negative
    number, None) so the caller gets a clear bug signal.
    """

    if current_capital_usd is None or current_capital_usd < 0:
        raise ValueError(
            f"current_capital_usd must be a non-negative number, got "
            f"{current_capital_usd!r}"
        )

    cfg = cfg or CapitalConfig.load()
    derived = compute_reference_min_viable_capital(cfg)
    reference = derived["reference_min_viable_capital_usd"]
    gap = round(reference - current_capital_usd, 2)

    if current_capital_usd >= reference:
        verdict = VERDICT_OK
        reason = (
            f"working_capital=${current_capital_usd:.2f} >= "
            f"reference=${reference:.2f} "
            f"(binding={derived['binding_reference']})"
        )
        recommendation = ""
        gap_out = 0.0
    else:
        verdict = VERDICT_WARN
        reason = (
            f"working_capital=${current_capital_usd:.2f} is ${gap:.2f} below "
            f"reference=${reference:.2f} (binding={derived['binding_reference']}). "
            f"Advisory only — engine continues."
        )
        recommendation = (
            f"{cfg.recommendation_summary} {cfg.recommendation_suggestion} "
            f"{cfg.recommendation_contact}"
        ).strip()
        gap_out = max(0.0, gap)

    return CapitalDecision(
        verdict=verdict,
        blocking=False,
        current_capital_usd=round(float(current_capital_usd), 2),
        reference_min_viable_capital_usd=reference,
        gap_usd=gap_out,
        derived_from=derived,
        reason=reason,
        recommendation=recommendation,
        checked_at_ts=time.time(),
        config_schema_version=cfg.schema_version,
    )


# ---------------------------------------------------------------------------
# Advisory class (public API used by engine.run_forever in Phase 3+)
# ---------------------------------------------------------------------------

class CapitalViabilityAdvisory:
    """L0 advisory. Exposes startup and recheck entry points. Non-blocking.

    Usage (Phase 3 wiring):
        advisory = CapitalViabilityAdvisory()
        decision = advisory.check_startup(current_capital_usd=adapter_equity)
        # Log/surface decision.verdict; DO NOT conditionally halt on it.

    The class is stateless beyond the config snapshot and the last decision;
    the caller (engine loop) decides when to invoke `should_recheck()` and
    `check_startup()`. Nothing in this class can stop the engine.
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._cfg: CapitalConfig = CapitalConfig.load(self._config_path)
        self._last_decision: Optional[CapitalDecision] = None

    @property
    def config(self) -> CapitalConfig:
        return self._cfg

    @property
    def last_decision(self) -> Optional[CapitalDecision]:
        return self._last_decision

    def reload_config(self) -> CapitalConfig:
        """Reload YAML from disk. Used by the 30-day recheck path so that
        operator-edited thresholds are picked up without a process restart."""
        self._cfg = CapitalConfig.load(self._config_path)
        return self._cfg

    def check_startup(self, current_capital_usd: float) -> CapitalDecision:
        """Evaluate and cache a decision at boot time. Never halts."""
        decision = check_viability(current_capital_usd, self._cfg)
        self._last_decision = decision
        _log_decision(decision, where="startup")
        return decision

    def should_recheck(self, now_ts: Optional[float] = None) -> bool:
        """True if the last decision is older than `recheck_interval_days`."""
        if self._last_decision is None:
            return True
        now = now_ts if now_ts is not None else time.time()
        age_s = now - self._last_decision.checked_at_ts
        return age_s >= self._cfg.recheck_interval_days * 86_400

    def recheck(self, current_capital_usd: float) -> CapitalDecision:
        """Reload config and re-evaluate. Intended for the 30-day cadence."""
        self.reload_config()
        decision = check_viability(current_capital_usd, self._cfg)
        self._last_decision = decision
        _log_decision(decision, where="recheck")
        return decision


# Back-compat alias — older code/imports expecting a "Gate" name get the same
# advisory class. Name preserved so Phase 1's inventory references still resolve.
CapitalViabilityGate = CapitalViabilityAdvisory


def _log_decision(decision: CapitalDecision, *, where: str) -> None:
    if decision.verdict == VERDICT_OK:
        log.info(
            "[L0:%s] OK capital=$%.2f reference=$%.2f binding=%s",
            where,
            decision.current_capital_usd,
            decision.reference_min_viable_capital_usd,
            decision.derived_from.get("binding_reference"),
        )
    else:
        # WARN — informational. Use warning level so operators notice, but no
        # halt, no raise, no SystemExit. This log line is the whole effect.
        log.warning(
            "[L0:%s] WARN capital=$%.2f reference=$%.2f gap=$%.2f (advisory; engine continues) reason=%s",
            where,
            decision.current_capital_usd,
            decision.reference_min_viable_capital_usd,
            decision.gap_usd,
            decision.reason,
        )


# ---------------------------------------------------------------------------
# CLI for operator manual check
# ---------------------------------------------------------------------------

def _main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="spot_aggro.gates.capital_gate",
        description="Run the L0 capital viability advisory manually (non-blocking).",
    )
    parser.add_argument(
        "--capital",
        type=float,
        required=True,
        help="Current working capital in USD",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Override path to capital.yml (default: spot_aggro/config/capital.yml)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit decision as JSON (for piping into ops tooling)",
    )
    args = parser.parse_args()

    advisory = CapitalViabilityAdvisory(config_path=args.config)
    decision = advisory.check_startup(current_capital_usd=args.capital)

    if args.json:
        print(json.dumps(decision.to_dict(), indent=2, default=str))
    else:
        print(f"verdict:           {decision.verdict}   (blocking={decision.blocking})")
        print(f"current_capital:   ${decision.current_capital_usd:.2f}")
        print(f"reference:         ${decision.reference_min_viable_capital_usd:.2f}")
        print(f"gap:               ${decision.gap_usd:.2f}")
        print(f"binding_reference: {decision.derived_from.get('binding_reference')}")
        print(f"reason:            {decision.reason}")
        if decision.recommendation:
            print(f"recommendation:    {decision.recommendation}")

    # CLI always exits 0 — the advisory never "fails". A non-zero exit would
    # contradict the non-blocking contract when this is called from scripts.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
