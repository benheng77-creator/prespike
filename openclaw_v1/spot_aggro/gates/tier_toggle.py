"""
Tier Execution Toggle (SPOT AGGRO only).

Execution-only per-tier on/off switch. NOT a state/score/physics gate —
it runs at the very end of the trade-permission chain and short-circuits
only the order-placement step. Scoring, calibration, funnel, heatmaps,
coin-memory, and forensic reports MUST continue to observe every tier
regardless of this toggle (operator directive).

Usage (Phase 8 wiring):
    toggle = TierExecutionToggle()
    if toggle.trade_enabled(tier):
        place_order(...)
    else:
        decision = toggle.decision_for(tier, qualifying=True)
        # funnel / rejection_log / dashboard record decision.reason_code
        # decision.analysis_qualified is True — signal is good, trade paused
        return

The toggle supports at-runtime flip via `set_enabled(tier, enabled, actor)`
for the upcoming control endpoint. Changes are audited in-process; the
caller is expected to additionally persist audit rows if needed.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError as _exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required for the tier execution toggle") from _exc

log = logging.getLogger("spot_aggro.gate.tier_toggle")

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "tiers.yml"
)

# Canonical tier order used by the engine. A+ is a distinct high-SPI
# sub-class of A in scoring.py; keep it as its own toggle so operators
# can flip blitz-only without touching regular A.
KNOWN_TIERS = ("A+", "A", "B", "C")

# Reason codes per operator spec (Phase 3 follow-up). Stable identifiers,
# never reused for other reasons. A+ gets its own code for symmetry with
# the existing four-tier enum.
REASON_CODES: dict[str, str] = {
    "A+": "TIER_APLUS_TRADE_DISABLED",
    "A":  "TIER_A_TRADE_DISABLED",
    "B":  "TIER_B_TRADE_DISABLED",
    "C":  "TIER_C_TRADE_DISABLED",
}


# ---------------------------------------------------------------------------
# Config + decision types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TierToggleConfig:
    schema_version: str
    engine: str
    execution: dict[str, bool]       # {"A+": True, "A": True, ...}

    @staticmethod
    def load(path: Optional[Path] = None) -> "TierToggleConfig":
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            raise FileNotFoundError(f"tier toggle config not found: {cfg_path}")
        with cfg_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        for k in ("schema_version", "engine", "execution"):
            if k not in raw:
                raise ValueError(
                    f"tier toggle config {cfg_path} missing required key: {k}"
                )
        if raw["engine"] != "spot_aggro":
            raise ValueError(
                f"tier toggle config engine must be 'spot_aggro' "
                f"(got {raw['engine']!r})."
            )
        exec_raw = raw["execution"] or {}
        missing = [t for t in KNOWN_TIERS if t not in exec_raw]
        if missing:
            raise ValueError(
                f"tier toggle config missing tiers: {missing}. Every tier in "
                f"{list(KNOWN_TIERS)} must be present (never delete a tier)."
            )
        execution = {t: bool(exec_raw[t]) for t in KNOWN_TIERS}
        return TierToggleConfig(
            schema_version=str(raw["schema_version"]),
            engine=str(raw["engine"]),
            execution=execution,
        )


@dataclass(frozen=True)
class TierDecision:
    """Result of a tier-execution check.

    verdict one of:
        ALLOW    — tier is enabled, order may proceed
        BLOCKED  — tier is disabled; trade must NOT place an order, BUT the
                   signal is still observed by analytics. If `qualifying`
                   was True, `analysis_qualified` is True.
    """

    verdict: str                    # "ALLOW" | "BLOCKED"
    tier: str
    trade_enabled: bool
    analysis_qualified: bool        # True iff caller asserted the signal qualifies
    reason_code: Optional[str]      # None on ALLOW; TIER_*_TRADE_DISABLED on BLOCKED
    reason: str
    checked_at_ts: float

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToggleAuditEntry:
    ts: float
    tier: str
    old_value: bool
    new_value: bool
    actor: str                       # e.g. "operator:ben", "api:token=…:hash"
    note: str


# ---------------------------------------------------------------------------
# Toggle class
# ---------------------------------------------------------------------------

class TierExecutionToggle:
    """Thread-safe per-tier execution toggle.

    The toggle never consults price, score, state, or capital. It is a pure
    on/off switch — a final guard step after all analytical gates have
    already accepted the trade. See module docstring for the separation of
    concerns vs analytics.
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._lock = threading.Lock()
        self._cfg = TierToggleConfig.load(self._config_path)
        # In-memory state starts from config; runtime setter mutates this
        # without rewriting the YAML (unless `persist=True` is passed).
        self._state: dict[str, bool] = dict(self._cfg.execution)
        self._audit: list[ToggleAuditEntry] = []

    # --- read API -----------------------------------------------------------

    @property
    def config(self) -> TierToggleConfig:
        return self._cfg

    def snapshot(self) -> dict[str, bool]:
        """Current execution state (copy). Safe to publish on a dashboard."""
        with self._lock:
            return dict(self._state)

    def trade_enabled(self, tier: str) -> bool:
        """True if the tier's execution toggle is on. Unknown tier → False
        (defensive; unknown inputs must not silently produce orders)."""
        if tier not in KNOWN_TIERS:
            return False
        with self._lock:
            return self._state.get(tier, False)

    def reason_code_for(self, tier: str) -> Optional[str]:
        """Return the stable reason code for a blocked tier, or None."""
        return REASON_CODES.get(tier)

    def decision_for(self, tier: str, *, qualifying: bool) -> TierDecision:
        """Produce a structured decision for logging/funnel.

        `qualifying=True` means all upstream gates (L1 physics, L2 state, L3
        calibration, L6 audit swarm) already PASS — the ONLY thing stopping
        the order is this toggle. Callers should never pass qualifying=True
        for a signal that has not actually passed those gates.
        """
        now = time.time()
        enabled = self.trade_enabled(tier)
        if enabled:
            return TierDecision(
                verdict="ALLOW",
                tier=tier,
                trade_enabled=True,
                analysis_qualified=bool(qualifying),
                reason_code=None,
                reason=f"tier {tier} execution enabled",
                checked_at_ts=now,
            )
        code = REASON_CODES.get(tier, f"TIER_{tier}_TRADE_DISABLED")
        reason = (
            f"tier {tier} execution disabled — signal preserved for analytics"
            if qualifying
            else f"tier {tier} execution disabled"
        )
        return TierDecision(
            verdict="BLOCKED",
            tier=tier,
            trade_enabled=False,
            analysis_qualified=bool(qualifying),
            reason_code=code,
            reason=reason,
            checked_at_ts=now,
        )

    # --- write API ----------------------------------------------------------

    def set_enabled(
        self,
        tier: str,
        enabled: bool,
        *,
        actor: str,
        note: str = "",
        persist: bool = False,
    ) -> ToggleAuditEntry:
        """Flip a tier's execution toggle. Records an audit entry.

        If `persist=True`, the YAML config file is rewritten so the change
        survives process restart. Default is in-memory-only so operators can
        experiment without mutating tracked config.
        """
        if tier not in KNOWN_TIERS:
            raise ValueError(
                f"unknown tier {tier!r}. Allowed: {list(KNOWN_TIERS)}. Never "
                f"delete a tier; add a new one via config+code review first."
            )
        enabled = bool(enabled)
        with self._lock:
            old = self._state.get(tier, False)
            self._state[tier] = enabled
            entry = ToggleAuditEntry(
                ts=time.time(),
                tier=tier,
                old_value=old,
                new_value=enabled,
                actor=str(actor) if actor else "unknown",
                note=str(note),
            )
            self._audit.append(entry)
            if persist:
                self._write_yaml_locked()
        log.info(
            "[tier_toggle] %s %s->%s actor=%s note=%s persist=%s",
            tier, old, enabled, entry.actor, entry.note, persist,
        )
        return entry

    def audit_log(self, *, limit: int = 50) -> list[ToggleAuditEntry]:
        """Most-recent-first copy of the audit trail."""
        with self._lock:
            return list(reversed(self._audit[-limit:]))

    def reload_config(self) -> TierToggleConfig:
        """Reload YAML and overwrite in-memory state. Audits a sync entry
        per tier where the value changed."""
        with self._lock:
            new_cfg = TierToggleConfig.load(self._config_path)
            for t in KNOWN_TIERS:
                old = self._state.get(t, False)
                new = bool(new_cfg.execution[t])
                if old != new:
                    self._audit.append(ToggleAuditEntry(
                        ts=time.time(),
                        tier=t,
                        old_value=old,
                        new_value=new,
                        actor="reload_config",
                        note="reloaded from disk",
                    ))
                self._state[t] = new
            self._cfg = new_cfg
            return new_cfg

    # --- internal -----------------------------------------------------------

    def _write_yaml_locked(self) -> None:
        """Rewrite the YAML with the current in-memory state. Caller must
        hold `_lock`."""
        payload = {
            "schema_version": self._cfg.schema_version,
            "engine": self._cfg.engine,
            "execution": {t: self._state[t] for t in KNOWN_TIERS},
        }
        tmp = self._config_path.with_suffix(".yml.tmp")
        tmp.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        tmp.replace(self._config_path)
