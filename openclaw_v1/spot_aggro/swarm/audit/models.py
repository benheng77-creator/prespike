"""
Audit swarm data types (SPOT AGGRO only).

MemberVerdict and QuorumResult are the only shapes the L6 gate and its
callers need to know about. The dispatcher and per-role modules return
MemberVerdict instances; the orchestrator assembles them into a
QuorumResult.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# Member verdict labels — stable strings, never rename.
V_PASS    = "PASS"
V_FAIL    = "FAIL"
V_UNKNOWN = "UNKNOWN"
MEMBER_VERDICTS = (V_PASS, V_FAIL, V_UNKNOWN)


# Final quorum verdicts
Q_PASS   = "PASS"
Q_REJECT = "REJECT"


# Per-spec §7 reason codes (stable). Callers key dashboards/logs on these.
AS_TIMEOUT               = "AS-001"
AS_TRUTH_AUDITOR_REJECT  = "AS-002"
AS_PHYSICS_FAIL          = "AS-003"
AS_STATE_FAIL            = "AS-004"
AS_CALIBRATION_FAIL      = "AS-005"
AS_ADJUDICATOR_REJECT    = "AS-006"
AS_POSTERIOR_UNINFORM    = "AS-007"
AS_MEMBER_UNKNOWN        = "AS-008"
AS_PARSE_FAIL            = "AS-009"
AS_INVALID_INPUT         = "AS-010"


@dataclass(frozen=True)
class MemberVerdict:
    """One per-role response from the audit swarm."""
    role: str                      # truth_auditor / trade_physics / ...
    provider: str
    model: str
    verdict: str                   # PASS | FAIL | UNKNOWN (MEMBER_VERDICTS)
    posterior: Optional[float]     # 0..1; None if role doesn't emit one
    rationale: str
    latency_ms: int
    cost_usd: float
    ok: bool                       # False if timeout/parse/transport error
    error: Optional[str]           # populated when ok=False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class QuorumResult:
    """Final swarm decision for one proposed trade."""
    verdict: str                   # PASS | REJECT
    reason_code: Optional[str]     # None on PASS; AS-### on REJECT
    reason: str
    members: list[MemberVerdict]
    adjudicator: Optional[MemberVerdict]
    posterior_final: Optional[float]
    quorum_size: int               # number of successful member responses in L1..L4
    required_quorum: int
    total_cost_usd: float
    total_latency_ms: int
    checked_at_ts: float

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["members"] = [m if isinstance(m, dict) else m.to_dict()
                        for m in d["members"]]
        if d.get("adjudicator") and not isinstance(d["adjudicator"], dict):
            d["adjudicator"] = d["adjudicator"].to_dict()
        return d


def empty_member(role: str, provider: str, model: str,
                 *, verdict: str = V_UNKNOWN, error: str = "") -> MemberVerdict:
    return MemberVerdict(
        role=role, provider=provider, model=model,
        verdict=verdict, posterior=None, rationale="",
        latency_ms=0, cost_usd=0.0, ok=False, error=error,
    )
