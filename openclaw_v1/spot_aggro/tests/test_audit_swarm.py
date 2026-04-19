"""
Tests for L6 — Audit Swarm Rebuild.

Stubs the LLM dispatcher so no network traffic is required.

Enforces:
  Q1  quorum 4-of-4 (L1..L4); anything less rejects with AS-001 TIMEOUT
  Q2  adjudicator only on unanimous PASS or any UNKNOWN
  Q5  posterior = 0.500 rejects with AS-007
  No fail-open (parse failure marks member not-ok → counts toward missing quorum)
  No default-neutral (missing posterior is OK; uninformative 0.5 is not)
  Reason codes AS-001..AS-010 stable
  No capital/equity/balance key accepted in trade_inputs (AS-010)
  Per-tier A/B/C: swarm behavior identical regardless of tier value
"""

from __future__ import annotations

import asyncio
import ast
import inspect
import json
from pathlib import Path
from typing import Any, Optional

import pytest
import yaml

from spot_aggro.swarm.audit import models as swarm_models
from spot_aggro.swarm.audit import orchestrator as orchestrator_mod
from spot_aggro.swarm.audit.models import (
    AS_ADJUDICATOR_REJECT,
    AS_CALIBRATION_FAIL,
    AS_INVALID_INPUT,
    AS_MEMBER_UNKNOWN,
    AS_PHYSICS_FAIL,
    AS_POSTERIOR_UNINFORM,
    AS_STATE_FAIL,
    AS_TIMEOUT,
    AS_TRUTH_AUDITOR_REJECT,
    Q_PASS,
    Q_REJECT,
)
from spot_aggro.swarm.audit.orchestrator import AuditSwarm


# ---------------------------------------------------------------------------
# config fixture — writes a local audit_swarm.yml so tests don't hit the
# shipped config. Also disables the adjudicator by default so individual
# tests can opt in when they want to exercise L5.
# ---------------------------------------------------------------------------

def _write_cfg(tmp_path: Path, **overrides) -> Path:
    base = {
        "schema_version": "spot.audit_swarm.v1",
        "engine": "spot_aggro",
        "per_call_timeout_s": 0.5,
        "required_quorum": 4,
        "adjudicator_enabled": True,
        "roles": {
            "truth_auditor":     {"provider": "anthropic",   "model": "claude-haiku-4-5"},
            "trade_physics":     {"provider": "openrouter",  "model": "deepseek/deepseek-chat-v3"},
            "state_classifier":  {"provider": "openai",      "model": "gpt-4o-mini"},
            "calibration":       {"provider": "gemini",      "model": "gemini-2.5-flash"},
            "chief_adjudicator": {"provider": "anthropic",   "model": "claude-opus-4-6"},
        },
        "posterior": {"uninformative_value": 0.5},
    }
    for k, v in overrides.items():
        base[k] = v
    p = tmp_path / "audit_swarm.yml"
    p.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Dispatcher stub helpers
# ---------------------------------------------------------------------------

def _make_dispatcher(
    scripted: dict[str, tuple[str, float]] | dict[str, Exception],
    latency_ms: int = 10,
    default_cost: float = 0.001,
):
    """Return an awaitable dispatcher that looks up scripted responses by
    role. Value can be (text, cost) or an Exception to raise."""
    async def _disp(*, role: str, provider: str, model: str,
                    prompt: str, timeout_s: float):
        if role not in scripted:
            raise asyncio.TimeoutError()
        entry = scripted[role]
        if isinstance(entry, Exception):
            raise entry
        text, cost = entry
        return text, cost, latency_ms
    return _disp


def _resp(verdict: str, posterior: Optional[float] = None,
          rationale: str = "ok") -> tuple[str, float]:
    obj = {"verdict": verdict, "rationale": rationale}
    if posterior is not None:
        obj["posterior"] = posterior
    return json.dumps(obj), 0.001


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_config_requires_engine_spot_aggro(tmp_path: Path) -> None:
    from spot_aggro.swarm.audit.orchestrator import SwarmConfig
    with pytest.raises(ValueError):
        SwarmConfig.load(_write_cfg(tmp_path, engine="apex_omega"))


def test_config_rejects_softened_quorum(tmp_path: Path) -> None:
    from spot_aggro.swarm.audit.orchestrator import SwarmConfig
    with pytest.raises(ValueError):
        SwarmConfig.load(_write_cfg(tmp_path, required_quorum=3))


def test_default_shipped_config_loads() -> None:
    from spot_aggro.swarm.audit.orchestrator import SwarmConfig
    cfg = SwarmConfig.load()
    assert cfg.engine == "spot_aggro"
    assert cfg.required_quorum == 4
    assert cfg.uninformative_posterior == 0.5


# ---------------------------------------------------------------------------
# Input validation — no capital/equity leakage
# ---------------------------------------------------------------------------

def _run(swarm: AuditSwarm, inputs: dict[str, Any]):
    return asyncio.run(swarm.check(inputs))


def test_trade_inputs_forbids_capital_key(tmp_path: Path) -> None:
    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=_make_dispatcher({}))
    r = _run(swarm, {"symbol": "INJ-USDT", "capital_usd": 500})
    assert r.verdict == Q_REJECT
    assert r.reason_code == AS_INVALID_INPUT


@pytest.mark.parametrize("key", [
    "capital", "capital_usd", "working_usd", "account_equity",
    "equity", "balance", "current_equity", "account_balance",
])
def test_trade_inputs_forbids_all_capital_variants(tmp_path: Path, key: str) -> None:
    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=_make_dispatcher({}))
    r = _run(swarm, {"symbol": "INJ-USDT", key: 1.0})
    assert r.verdict == Q_REJECT
    assert r.reason_code == AS_INVALID_INPUT


# ---------------------------------------------------------------------------
# Q1 — 4-of-4 quorum
# ---------------------------------------------------------------------------

def test_timeout_on_three_responders_rejects_with_as001(tmp_path: Path) -> None:
    """Only three members respond → quorum fails → AS-001."""
    disp = _make_dispatcher({
        "truth_auditor":    _resp("PASS", 0.8),
        "trade_physics":    _resp("PASS", 0.75),
        "state_classifier": _resp("PASS", 0.7),
        # calibration missing → raises TimeoutError
    })
    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "B"})
    assert r.verdict == Q_REJECT
    assert r.reason_code == AS_TIMEOUT
    assert r.quorum_size == 3


def test_timeout_on_transport_error_counts_as_missing(tmp_path: Path) -> None:
    disp = _make_dispatcher({
        "truth_auditor":    _resp("PASS", 0.8),
        "trade_physics":    _resp("PASS", 0.75),
        "state_classifier": _resp("PASS", 0.7),
        "calibration":      RuntimeError("boom"),
    })
    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "B"})
    assert r.verdict == Q_REJECT
    assert r.reason_code == AS_TIMEOUT


def test_parse_failure_counts_as_missing(tmp_path: Path) -> None:
    """A garbage response from one member must NOT be treated as neutral;
    it drops that member from the quorum."""
    disp = _make_dispatcher({
        "truth_auditor":    _resp("PASS", 0.8),
        "trade_physics":    _resp("PASS", 0.75),
        "state_classifier": _resp("PASS", 0.7),
        "calibration":      ("not json at all", 0.001),
    })
    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "B"})
    assert r.verdict == Q_REJECT
    assert r.reason_code == AS_TIMEOUT
    cal_member = [m for m in r.members if m.role == "calibration"][0]
    assert cal_member.ok is False
    assert cal_member.error and cal_member.error.startswith("parse:")


# ---------------------------------------------------------------------------
# Member FAIL paths
# ---------------------------------------------------------------------------

def test_truth_auditor_fail_rejects_with_as002(tmp_path: Path) -> None:
    disp = _make_dispatcher({
        "truth_auditor":    _resp("FAIL", 0.1, "contradiction"),
        "trade_physics":    _resp("PASS", 0.75),
        "state_classifier": _resp("PASS", 0.7),
        "calibration":      _resp("PASS", 0.7),
    })
    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "A"})
    assert r.verdict == Q_REJECT
    assert r.reason_code == AS_TRUTH_AUDITOR_REJECT


def test_physics_fail_rejects_with_as003(tmp_path: Path) -> None:
    disp = _make_dispatcher({
        "truth_auditor":    _resp("PASS", 0.8),
        "trade_physics":    _resp("FAIL", 0.2, "edge<2*cost"),
        "state_classifier": _resp("PASS", 0.7),
        "calibration":      _resp("PASS", 0.7),
    })
    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "B"})
    assert r.verdict == Q_REJECT
    assert r.reason_code == AS_PHYSICS_FAIL


def test_state_fail_rejects_with_as004(tmp_path: Path) -> None:
    disp = _make_dispatcher({
        "truth_auditor":    _resp("PASS", 0.8),
        "trade_physics":    _resp("PASS", 0.75),
        "state_classifier": _resp("FAIL", 0.1, "DEAD_CHOP"),
        "calibration":      _resp("PASS", 0.7),
    })
    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "C"})
    assert r.verdict == Q_REJECT
    assert r.reason_code == AS_STATE_FAIL


def test_calibration_fail_rejects_with_as005(tmp_path: Path) -> None:
    disp = _make_dispatcher({
        "truth_auditor":    _resp("PASS", 0.8),
        "trade_physics":    _resp("PASS", 0.75),
        "state_classifier": _resp("PASS", 0.7),
        "calibration":      _resp("FAIL", 0.1, "bucket INSUFFICIENT"),
    })
    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "A+"})
    assert r.verdict == Q_REJECT
    assert r.reason_code == AS_CALIBRATION_FAIL


# ---------------------------------------------------------------------------
# Q5 — posterior 0.500 is uninformative
# ---------------------------------------------------------------------------

def test_posterior_exactly_point_five_rejects_with_as007(tmp_path: Path) -> None:
    disp = _make_dispatcher({
        "truth_auditor":    _resp("PASS", 0.5),   # ← uninformative
        "trade_physics":    _resp("PASS", 0.75),
        "state_classifier": _resp("PASS", 0.7),
        "calibration":      _resp("PASS", 0.7),
    })
    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "B"})
    assert r.verdict == Q_REJECT
    assert r.reason_code == AS_POSTERIOR_UNINFORM


def test_posterior_slightly_above_point_five_is_fine(tmp_path: Path) -> None:
    """0.501 must not trigger the uninformative-posterior reject — strict
    equality only."""
    disp = _make_dispatcher({
        "truth_auditor":    _resp("PASS", 0.501),
        "trade_physics":    _resp("PASS", 0.75),
        "state_classifier": _resp("PASS", 0.7),
        "calibration":      _resp("PASS", 0.7),
        "chief_adjudicator": _resp("PASS", 0.8, "all clear"),
    })
    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "B"})
    assert r.verdict == Q_PASS
    assert r.reason_code is None


# ---------------------------------------------------------------------------
# Q2 — adjudicator only on unanimous PASS or any UNKNOWN
# ---------------------------------------------------------------------------

def test_unanimous_pass_invokes_adjudicator(tmp_path: Path) -> None:
    called = {"n": 0}

    async def disp(*, role, provider, model, prompt, timeout_s):
        called.setdefault(role, 0)
        called[role] = called.get(role, 0) + 1
        called["n"] += 1
        if role == "chief_adjudicator":
            return json.dumps({"verdict": "PASS", "posterior": 0.85, "rationale": "ok"}), 0.01, 20
        return json.dumps({"verdict": "PASS", "posterior": 0.75}), 0.001, 10

    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "B"})
    assert r.verdict == Q_PASS
    assert r.adjudicator is not None
    assert called.get("chief_adjudicator") == 1
    assert r.posterior_final == 0.85


def test_unknown_member_escalates_to_adjudicator(tmp_path: Path) -> None:
    async def disp(*, role, provider, model, prompt, timeout_s):
        if role == "calibration":
            return json.dumps({"verdict": "UNKNOWN", "rationale": "no bucket"}), 0.001, 10
        if role == "chief_adjudicator":
            return json.dumps({"verdict": "PASS", "posterior": 0.7, "rationale": "minor gap"}), 0.01, 20
        return json.dumps({"verdict": "PASS", "posterior": 0.75}), 0.001, 10

    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "A"})
    assert r.verdict == Q_PASS
    assert r.adjudicator is not None
    assert r.adjudicator.verdict == "PASS"


def test_adjudicator_unknown_rejects_with_as008(tmp_path: Path) -> None:
    async def disp(*, role, provider, model, prompt, timeout_s):
        if role == "state_classifier":
            return json.dumps({"verdict": "UNKNOWN"}), 0.001, 10
        if role == "chief_adjudicator":
            return json.dumps({"verdict": "UNKNOWN"}), 0.01, 20
        return json.dumps({"verdict": "PASS", "posterior": 0.7}), 0.001, 10

    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "B"})
    assert r.verdict == Q_REJECT
    assert r.reason_code == AS_MEMBER_UNKNOWN


def test_adjudicator_fail_rejects_with_as006(tmp_path: Path) -> None:
    async def disp(*, role, provider, model, prompt, timeout_s):
        if role == "chief_adjudicator":
            return json.dumps({"verdict": "FAIL", "posterior": 0.2, "rationale": "block"}), 0.01, 20
        return json.dumps({"verdict": "PASS", "posterior": 0.75}), 0.001, 10

    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "B"})
    assert r.verdict == Q_REJECT
    assert r.reason_code == AS_ADJUDICATOR_REJECT


def test_adjudicator_posterior_uninformative_rejects(tmp_path: Path) -> None:
    async def disp(*, role, provider, model, prompt, timeout_s):
        if role == "chief_adjudicator":
            return json.dumps({"verdict": "PASS", "posterior": 0.5}), 0.01, 20
        return json.dumps({"verdict": "PASS", "posterior": 0.75}), 0.001, 10

    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "B"})
    assert r.verdict == Q_REJECT
    assert r.reason_code == AS_POSTERIOR_UNINFORM


def test_adjudicator_disabled_but_needed_rejects(tmp_path: Path) -> None:
    """If all four pass but the operator disabled L5, swarm must REJECT —
    no weakening of the 4-of-4+adjudicator contract."""
    async def disp(*, role, provider, model, prompt, timeout_s):
        return json.dumps({"verdict": "PASS", "posterior": 0.75}), 0.001, 10

    swarm = AuditSwarm(
        config_path=_write_cfg(tmp_path, adjudicator_enabled=False),
        dispatcher_fn=disp,
    )
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": "B"})
    assert r.verdict == Q_REJECT
    assert r.reason_code == AS_ADJUDICATOR_REJECT


# ---------------------------------------------------------------------------
# Tier agnosticism — A/B/C behave identically
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ["A+", "A", "B", "C"])
def test_swarm_decision_is_tier_agnostic(tmp_path: Path, tier: str) -> None:
    """Identical upstream responses → identical verdict regardless of tier.
    The swarm does not hard-exclude any tier; tier-based execution gating
    is L2's job (tier toggle)."""
    async def disp(*, role, provider, model, prompt, timeout_s):
        if role == "chief_adjudicator":
            return json.dumps({"verdict": "PASS", "posterior": 0.85}), 0.01, 20
        return json.dumps({"verdict": "PASS", "posterior": 0.75}), 0.001, 10

    swarm = AuditSwarm(config_path=_write_cfg(tmp_path), dispatcher_fn=disp)
    r = _run(swarm, {"symbol": "INJ-USDT", "tier": tier})
    assert r.verdict == Q_PASS


# ---------------------------------------------------------------------------
# Regression — no capital-based logic anywhere in L6
# ---------------------------------------------------------------------------

_FORBIDDEN_CODE_TOKENS = (
    "capital_usd", "working_usd", "account_equity", "get_account_equity",
    "deploy_ceil", "CapitalViabilityGate", "CapitalViabilityAdvisory",
    ".capital", ".equity", ".balance", "state.capital",
)


def _strip_docstrings_and_comments(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    doc_ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                d = node.body[0]
                doc_ranges.append((d.lineno, d.end_lineno or d.lineno))
    lines = []
    for idx, line in enumerate(src.splitlines(), start=1):
        if any(lo <= idx <= hi for lo, hi in doc_ranges):
            continue
        lines.append(line.split("#", 1)[0])
    return "\n".join(lines)


def test_audit_swarm_source_never_references_capital_or_equity() -> None:
    from spot_aggro.swarm.audit import prompts as prompts_mod
    from spot_aggro.swarm.audit.clients import dispatcher as disp_mod
    for mod in (orchestrator_mod, swarm_models, prompts_mod, disp_mod):
        path = Path(inspect.getsourcefile(mod))
        code = _strip_docstrings_and_comments(path)
        for token in _FORBIDDEN_CODE_TOKENS:
            assert token not in code, (
                f"audit swarm module {mod.__name__} references forbidden "
                f"capital/equity token {token!r}."
            )


def test_audit_swarm_check_has_no_capital_parameter() -> None:
    sig = inspect.signature(AuditSwarm.check)
    for forbidden in ("capital", "equity", "balance", "account"):
        assert not any(forbidden in p for p in sig.parameters), (
            f"AuditSwarm.check has parameter matching {forbidden!r}"
        )
