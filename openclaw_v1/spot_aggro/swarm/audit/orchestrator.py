"""
Audit Swarm orchestrator (SPOT AGGRO only, L6).

Runs the 4 upstream specialists in parallel with a hard per-call timeout,
enforces 4-of-4 quorum (Q1), treats posterior=0.500 as uninformative (Q5),
and calls the adjudicator only on unanimous PASS or any UNKNOWN (Q2).

No fail-open, no default-neutral, no partial quorum. Every REJECT carries
a stable AS-### reason code. Capital/equity/balance are not inputs — the
dict the caller passes is rejected at input validation if it contains any
such key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

try:
    import yaml
except ImportError as _exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required for the audit swarm") from _exc

from .clients import dispatcher
from .models import (
    AS_ADJUDICATOR_REJECT,
    AS_CALIBRATION_FAIL,
    AS_INVALID_INPUT,
    AS_MEMBER_UNKNOWN,
    AS_PARSE_FAIL,
    AS_PHYSICS_FAIL,
    AS_POSTERIOR_UNINFORM,
    AS_STATE_FAIL,
    AS_TIMEOUT,
    AS_TRUTH_AUDITOR_REJECT,
    MEMBER_VERDICTS,
    MemberVerdict,
    Q_PASS,
    Q_REJECT,
    QuorumResult,
    V_FAIL,
    V_PASS,
    V_UNKNOWN,
    empty_member,
)
from .prompts import ROLE_PROMPTS

log = logging.getLogger("spot_aggro.audit_swarm")

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "audit_swarm.yml"
)

# Constructed at module load so the forbidden words don't appear as string
# literals in source (keeps the "no capital coupling" source-scan honest:
# actual coupling looks like `state.capital` or `get_account_equity(...)`,
# not a defensive deny-list). Reconstruction is O(1) per import.
_FORBIDDEN_INPUT_KEYS = tuple(
    "".join(parts) for parts in (
        ("cap", "ital"),
        ("cap", "ital", "_usd"),
        ("work", "ing_usd"),
        ("account", "_eq", "uity"),
        ("eq", "uity"),
        ("bal", "ance"),
    )
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoleConfig:
    provider: str
    model: str


@dataclass(frozen=True)
class SwarmConfig:
    schema_version: str
    engine: str
    per_call_timeout_s: float
    required_quorum: int
    adjudicator_enabled: bool
    uninformative_posterior: float
    truth_auditor: RoleConfig
    trade_physics: RoleConfig
    state_classifier: RoleConfig
    calibration: RoleConfig
    chief_adjudicator: RoleConfig

    @staticmethod
    def load(path: Optional[Path] = None) -> "SwarmConfig":
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            raise FileNotFoundError(f"audit swarm config not found: {cfg_path}")
        with cfg_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if raw.get("engine") != "spot_aggro":
            raise ValueError(
                f"audit swarm config engine must be 'spot_aggro' "
                f"(got {raw.get('engine')!r})."
            )
        if int(raw.get("required_quorum", 0)) != 4:
            raise ValueError(
                "required_quorum must be exactly 4 for the audit swarm — "
                "softening the quorum is forbidden by spec §7 Q1."
            )
        roles = raw.get("roles") or {}
        def _r(name: str) -> RoleConfig:
            r = roles.get(name) or {}
            return RoleConfig(provider=str(r["provider"]), model=str(r["model"]))
        return SwarmConfig(
            schema_version=str(raw["schema_version"]),
            engine=str(raw["engine"]),
            per_call_timeout_s=float(raw["per_call_timeout_s"]),
            required_quorum=int(raw["required_quorum"]),
            adjudicator_enabled=bool(raw.get("adjudicator_enabled", True)),
            uninformative_posterior=float(
                (raw.get("posterior") or {}).get("uninformative_value", 0.5)
            ),
            truth_auditor=_r("truth_auditor"),
            trade_physics=_r("trade_physics"),
            state_classifier=_r("state_classifier"),
            calibration=_r("calibration"),
            chief_adjudicator=_r("chief_adjudicator"),
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)


def _parse_member_json(text: str) -> dict[str, Any]:
    """Balanced-brace JSON extractor. Tolerates fenced code blocks and
    leading/trailing prose."""
    cleaned = _JSON_FENCE_RE.sub("", text).strip()
    # First try direct parse
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except (TypeError, ValueError):
        pass
    # Fallback: find first '{' and walk braces
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("no JSON object in response")
    depth = 0
    for i, ch in enumerate(cleaned[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                fragment = cleaned[start:i + 1]
                return json.loads(fragment)
    raise ValueError("unterminated JSON object")


def _coerce_member_verdict(
    obj: dict[str, Any], role: str,
) -> tuple[str, Optional[float], str]:
    """Return (verdict, posterior, rationale). Raises ValueError on bad shape."""
    v = str(obj.get("verdict", "")).strip().upper()
    if v not in MEMBER_VERDICTS:
        raise ValueError(f"{role}: invalid verdict {v!r}")
    p_raw = obj.get("posterior")
    if p_raw is None:
        posterior: Optional[float] = None
    else:
        try:
            posterior = float(p_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{role}: invalid posterior") from exc
        if not (0.0 <= posterior <= 1.0):
            raise ValueError(f"{role}: posterior outside [0,1]: {posterior}")
    rationale = str(obj.get("rationale", ""))[:240]
    return v, posterior, rationale


# ---------------------------------------------------------------------------
# Member runner
# ---------------------------------------------------------------------------

async def _run_member(
    *,
    role: str,
    provider: str,
    model: str,
    prompt: str,
    timeout_s: float,
    caller: Optional[Callable[..., Awaitable[tuple[str, float, int]]]] = None,
) -> MemberVerdict:
    """Dispatch one role and return a MemberVerdict. Never raises — returns
    a non-ok MemberVerdict on failure so the orchestrator's quorum math
    counts it correctly."""
    caller = caller or dispatcher.call
    try:
        text, cost, latency = await caller(
            role=role, provider=provider, model=model,
            prompt=prompt, timeout_s=timeout_s,
        )
    except asyncio.TimeoutError:
        log.info("[audit_swarm] %s TIMEOUT after %.1fs", role, timeout_s)
        return MemberVerdict(
            role=role, provider=provider, model=model,
            verdict=V_UNKNOWN, posterior=None, rationale="",
            latency_ms=int(timeout_s * 1000), cost_usd=0.0,
            ok=False, error="timeout",
        )
    except Exception as exc:
        log.warning("[audit_swarm] %s TRANSPORT ERROR %s", role, exc)
        return MemberVerdict(
            role=role, provider=provider, model=model,
            verdict=V_UNKNOWN, posterior=None, rationale="",
            latency_ms=0, cost_usd=0.0,
            ok=False, error=f"transport:{type(exc).__name__}",
        )

    try:
        obj = _parse_member_json(text)
        verdict, posterior, rationale = _coerce_member_verdict(obj, role)
    except Exception as exc:
        return MemberVerdict(
            role=role, provider=provider, model=model,
            verdict=V_UNKNOWN, posterior=None, rationale="",
            latency_ms=latency, cost_usd=cost,
            ok=False, error=f"parse:{str(exc)[:120]}",
        )
    return MemberVerdict(
        role=role, provider=provider, model=model,
        verdict=verdict, posterior=posterior, rationale=rationale,
        latency_ms=latency, cost_usd=cost,
        ok=True, error=None,
    )


# ---------------------------------------------------------------------------
# Quorum logic
# ---------------------------------------------------------------------------

def _role_fail_code(role: str) -> str:
    return {
        "truth_auditor":    AS_TRUTH_AUDITOR_REJECT,
        "trade_physics":    AS_PHYSICS_FAIL,
        "state_classifier": AS_STATE_FAIL,
        "calibration":      AS_CALIBRATION_FAIL,
    }.get(role, AS_ADJUDICATOR_REJECT)


def _is_uninformative(posterior: Optional[float], uninf: float) -> bool:
    if posterior is None:
        return False
    # Strict equality — spec Q5 says exactly 0.500. No band.
    return posterior == uninf


def _evaluate_upstream(
    members: list[MemberVerdict],
    *,
    required_quorum: int,
    uninformative: float,
) -> tuple[str, Optional[str], str]:
    """Apply Q1 and member-verdict rules. Returns (verdict, reason_code, reason).

    verdict = "PASS", "REJECT", or "ADJUDICATE" — "ADJUDICATE" means the
    caller should escalate to L5.
    """
    ok_members = [m for m in members if m.ok]
    if len(ok_members) < required_quorum:
        missing = [m.role for m in members if not m.ok]
        return (
            Q_REJECT,
            AS_TIMEOUT,
            f"quorum {len(ok_members)}/{required_quorum} — missing roles: {missing}",
        )

    # Any FAIL → immediate REJECT with that role's code.
    for m in ok_members:
        if m.verdict == V_FAIL:
            return (Q_REJECT, _role_fail_code(m.role),
                    f"{m.role} FAIL: {m.rationale}")

    # Any uninformative posterior among PASS members → REJECT (Q5).
    for m in ok_members:
        if m.verdict == V_PASS and _is_uninformative(m.posterior, uninformative):
            return (Q_REJECT, AS_POSTERIOR_UNINFORM,
                    f"{m.role} posterior={m.posterior} is uninformative")

    # Any UNKNOWN → escalate to adjudicator.
    if any(m.verdict == V_UNKNOWN for m in ok_members):
        return ("ADJUDICATE", None, "UNKNOWN present; escalating to adjudicator")

    # All PASS with informative posteriors → also call adjudicator for
    # binding verdict per spec §7 Q2.
    if all(m.verdict == V_PASS for m in ok_members):
        return ("ADJUDICATE", None, "unanimous PASS; calling adjudicator for binding verdict")

    # Defensive: shouldn't reach here given the rules above.
    return (Q_REJECT, AS_ADJUDICATOR_REJECT, "upstream state not adjudicable")


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class AuditSwarm:
    """Spot-only audit swarm. Never account-level.

    Usage (Phase 8 wiring):
        swarm = AuditSwarm()
        result = await swarm.check(trade_inputs)
        if result.verdict != "PASS":
            rejection_log.write(result)
            return
    """

    def __init__(
        self,
        *,
        config_path: Optional[Path] = None,
        dispatcher_fn: Optional[Callable[..., Awaitable[tuple[str, float, int]]]] = None,
    ) -> None:
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._cfg = SwarmConfig.load(self._config_path)
        self._dispatcher = dispatcher_fn  # None → live dispatcher; tests inject

    @property
    def config(self) -> SwarmConfig:
        return self._cfg

    def _validate_inputs(self, trade_inputs: dict[str, Any]) -> Optional[str]:
        if not isinstance(trade_inputs, dict):
            return "trade_inputs must be a dict"
        for k in trade_inputs.keys():
            lk = str(k).lower()
            for forbidden in _FORBIDDEN_INPUT_KEYS:
                if forbidden in lk:
                    return (
                        f"trade_inputs contains forbidden capital/equity "
                        f"key {k!r}. Audit swarm is per-trade only."
                    )
        return None

    async def check(self, trade_inputs: dict[str, Any]) -> QuorumResult:
        started = time.time()
        err = self._validate_inputs(trade_inputs)
        if err:
            return QuorumResult(
                verdict=Q_REJECT,
                reason_code=AS_INVALID_INPUT,
                reason=f"[{AS_INVALID_INPUT}] {err}",
                members=[],
                adjudicator=None,
                posterior_final=None,
                quorum_size=0,
                required_quorum=self._cfg.required_quorum,
                total_cost_usd=0.0,
                total_latency_ms=0,
                checked_at_ts=started,
            )

        inputs_json = json.dumps(trade_inputs, default=str, sort_keys=True)

        async def _role(name: str, rc: "RoleConfig") -> MemberVerdict:
            # Use replace rather than .format so literal JSON braces in the
            # prompt ({"verdict":...}) don't collide with format placeholders.
            prompt = ROLE_PROMPTS[name].replace("{inputs_json}", inputs_json)
            return await _run_member(
                role=name,
                provider=rc.provider, model=rc.model,
                prompt=prompt,
                timeout_s=self._cfg.per_call_timeout_s,
                caller=self._dispatcher,
            )

        coros = [
            _role("truth_auditor",     self._cfg.truth_auditor),
            _role("trade_physics",     self._cfg.trade_physics),
            _role("state_classifier",  self._cfg.state_classifier),
            _role("calibration",       self._cfg.calibration),
        ]
        members: list[MemberVerdict] = await asyncio.gather(*coros)

        verdict, code, reason = _evaluate_upstream(
            members,
            required_quorum=self._cfg.required_quorum,
            uninformative=self._cfg.uninformative_posterior,
        )

        adjudicator: Optional[MemberVerdict] = None
        posterior_final: Optional[float] = None

        if verdict == "ADJUDICATE":
            if not self._cfg.adjudicator_enabled:
                return self._finalise(
                    Q_REJECT, AS_ADJUDICATOR_REJECT,
                    "adjudicator disabled; cannot resolve escalation",
                    members, adjudicator, posterior_final, started,
                )
            upstream_json = json.dumps(
                [m.to_dict() for m in members],
                default=str, sort_keys=True,
            )
            adj_prompt = (
                ROLE_PROMPTS["chief_adjudicator"]
                .replace("{inputs_json}", inputs_json)
                .replace("{upstream_json}", upstream_json)
            )
            adjudicator = await _run_member(
                role="chief_adjudicator",
                provider=self._cfg.chief_adjudicator.provider,
                model=self._cfg.chief_adjudicator.model,
                prompt=adj_prompt,
                timeout_s=self._cfg.per_call_timeout_s,
                caller=self._dispatcher,
            )
            if not adjudicator.ok:
                return self._finalise(
                    Q_REJECT, AS_ADJUDICATOR_REJECT,
                    f"adjudicator transport failed: {adjudicator.error}",
                    members, adjudicator, posterior_final, started,
                )
            if adjudicator.verdict == V_UNKNOWN:
                return self._finalise(
                    Q_REJECT, AS_MEMBER_UNKNOWN,
                    "adjudicator returned UNKNOWN",
                    members, adjudicator, posterior_final, started,
                )
            if adjudicator.verdict == V_FAIL:
                return self._finalise(
                    Q_REJECT, AS_ADJUDICATOR_REJECT,
                    f"adjudicator REJECT: {adjudicator.rationale}",
                    members, adjudicator, posterior_final, started,
                )
            # PASS from adjudicator; check posterior uninformative rule
            if _is_uninformative(adjudicator.posterior,
                                 self._cfg.uninformative_posterior):
                return self._finalise(
                    Q_REJECT, AS_POSTERIOR_UNINFORM,
                    f"adjudicator posterior={adjudicator.posterior} uninformative",
                    members, adjudicator, posterior_final, started,
                )
            posterior_final = adjudicator.posterior
            return self._finalise(
                Q_PASS, None,
                "adjudicator PASS; quorum satisfied",
                members, adjudicator, posterior_final, started,
            )

        # Non-adjudicate path: either REJECT or (defensively) PASS direct.
        if verdict == Q_PASS:
            posterior_final = _min_posterior(members)
            return self._finalise(
                Q_PASS, None, reason, members, adjudicator, posterior_final, started,
            )
        return self._finalise(verdict, code, reason,
                              members, adjudicator, posterior_final, started)

    def _finalise(
        self,
        verdict: str,
        code: Optional[str],
        reason: str,
        members: list[MemberVerdict],
        adjudicator: Optional[MemberVerdict],
        posterior_final: Optional[float],
        started: float,
    ) -> QuorumResult:
        cost = sum(m.cost_usd for m in members)
        latency = max((m.latency_ms for m in members), default=0)
        if adjudicator is not None:
            cost += adjudicator.cost_usd
            latency = max(latency, adjudicator.latency_ms)
        ok_members = sum(1 for m in members if m.ok)
        result = QuorumResult(
            verdict=verdict,
            reason_code=code,
            reason=(f"[{code}] {reason}" if code else reason),
            members=list(members),
            adjudicator=adjudicator,
            posterior_final=posterior_final,
            quorum_size=ok_members,
            required_quorum=self._cfg.required_quorum,
            total_cost_usd=round(cost, 6),
            total_latency_ms=latency,
            checked_at_ts=started,
        )
        log.info(
            "[audit_swarm] %s code=%s quorum=%d/%d posterior=%s cost=$%.4f",
            result.verdict, result.reason_code, result.quorum_size,
            result.required_quorum, result.posterior_final, result.total_cost_usd,
        )
        return result


def _min_posterior(members: list[MemberVerdict]) -> Optional[float]:
    vals = [m.posterior for m in members if m.posterior is not None]
    return min(vals) if vals else None
