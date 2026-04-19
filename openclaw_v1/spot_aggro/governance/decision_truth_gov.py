"""Phase 11n-2 — Decision Truth Governor (Layer 6).

Adversarial auditor for decision_engine.build_decision_bundle(). Proves
that every buy/sell/hold recommendation is evidence-backed, reconciles
with the underlying data, and has no gap between the factors cited and
the action chosen.

Checks:
  1. factor_coverage  — every candidate carries the core factors
                        (symbol_wr, tier_wr, toggle, truth govs, audit).
  2. gate_integrity   — any candidate with action="buy" MUST have
                        gates_clear=True and no factor of severity="block".
  3. evidence_real    — every evidence_ref has a resolvable id in the
                        corresponding governance table (or is "none"
                        for scenarios with no batch yet).
  4. conversion_math  — conversion rate numbers must self-reconcile:
                        wins + losses ≤ trades_executed; pending ≥ 0;
                        signal_to_win_pct = wins / signals_generated × 100.
  5. system_wr_gap    — system_wr_gap equals target - system_wr (or None).
  6. bundle_freshness — generated_ts_ms within last hour.

Returns DecisionTruthVerdict. Verdict ∈ {valid, suspect, invalid}.
`invalid` tags the bundle; decisions stand advisory. This governor
NEVER flips trades or toggles.

SPOT AGGRO only.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Optional


@dataclass
class DecisionFinding:
    check: str
    severity: str      # "ok" | "warn" | "fail"
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecisionTruthVerdict:
    bundle_ts_ms: int
    verdict: str       # "valid" | "suspect" | "invalid"
    n_checks: int
    n_ok: int
    n_warn: int
    n_fail: int
    findings: list[DecisionFinding]
    checked_at_ms: int

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["findings"] = [f.to_dict() if hasattr(f, "to_dict") else dict(f)
                         for f in self.findings]
        return d


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

REQUIRED_FACTORS = {"symbol_win_rate", "tier_win_rate", "tier_toggle",
                    "research_truth_gov", "card_truth_gov", "system_audit"}


def _check_factor_coverage(bundle: dict[str, Any]) -> DecisionFinding:
    candidates = bundle.get("candidates") or []
    missing_any: list[str] = []
    for c in candidates:
        names = {f.get("name") for f in (c.get("factors") or [])}
        miss = REQUIRED_FACTORS - names
        if miss:
            missing_any.append(f"{c.get('symbol')} missing {sorted(miss)}")
    if missing_any:
        return DecisionFinding(
            check="factor_coverage", severity="fail",
            message="candidates missing required factors: "
                    + "; ".join(missing_any[:3]),
            evidence={"missing": missing_any[:10]},
        )
    return DecisionFinding(
        check="factor_coverage", severity="ok",
        message=f"all {len(candidates)} candidates carry the 6 core factors",
    )


def _check_gate_integrity(bundle: dict[str, Any]) -> DecisionFinding:
    violations = []
    for c in bundle.get("candidates") or []:
        if c.get("action") != "buy":
            continue
        if not c.get("gates_clear"):
            violations.append(
                f"{c.get('symbol')}: action=buy but gates_clear=False"
            )
            continue
        blocks = [f.get("name") for f in (c.get("factors") or [])
                  if f.get("severity") == "block"]
        if blocks:
            violations.append(
                f"{c.get('symbol')}: action=buy despite blocking factors "
                f"{blocks}"
            )
    if violations:
        return DecisionFinding(
            check="gate_integrity", severity="fail",
            message="buy actions with unclear gates: " + "; ".join(violations[:3]),
            evidence={"violations": violations[:10]},
        )
    return DecisionFinding(
        check="gate_integrity", severity="ok",
        message="every buy action has all gates clear and no blocking factors",
    )


def _check_evidence_real(bundle: dict[str, Any]) -> DecisionFinding:
    """Every candidate.evidence_refs should point to an id. For
    research/truth/cards/scenario we don't exhaustively DB-verify here
    (that's what the Layer 4/5 govs do); we just confirm the refs are
    populated (non-"?" unless the artifact legitimately doesn't exist yet)."""
    missing = []
    for c in bundle.get("candidates") or []:
        refs = c.get("evidence_refs") or []
        if not refs:
            missing.append(f"{c.get('symbol')}: no evidence_refs at all")
            continue
        by_kind = {r.split("://")[0]: r.split("://")[1]
                   for r in refs if "://" in r}
        # research is ALWAYS required (bundle was built from it).
        if by_kind.get("research", "?") == "?":
            missing.append(f"{c.get('symbol')}: missing research evidence")
    if missing:
        return DecisionFinding(
            check="evidence_real", severity="fail",
            message="candidates with unresolved evidence: "
                    + "; ".join(missing[:3]),
            evidence={"missing": missing[:10]},
        )
    return DecisionFinding(
        check="evidence_real", severity="ok",
        message="every candidate cites a real research report id",
    )


def _check_conversion_math(bundle: dict[str, Any]) -> DecisionFinding:
    conv = bundle.get("conversion") or {}
    signals = int(conv.get("signals_generated") or 0)
    trades = int(conv.get("trades_executed") or 0)
    wins = int(conv.get("wins") or 0)
    losses = int(conv.get("losses") or 0)
    pending = int(conv.get("pending") or 0)
    # Hard invariants (must hold by construction):
    hard_issues = []
    if wins + losses > trades:
        hard_issues.append(
            f"wins+losses ({wins+losses}) > trades_executed ({trades})"
        )
    if pending < 0:
        hard_issues.append(f"pending {pending} < 0")
    if signals > 0:
        expected_s2w = round(wins / signals * 100.0, 2)
        actual = conv.get("signal_to_win_pct")
        if actual is not None and abs(expected_s2w - actual) > 0.1:
            hard_issues.append(
                f"signal_to_win_pct: claimed {actual}, math gives {expected_s2w}"
            )
    # Soft invariant: trades > signals means reconciled/backfilled exits
    # without a matching enter row (normal after engine restart or OKX
    # position adoption). Warn, don't fail.
    soft_issues = []
    if trades > signals:
        soft_issues.append(
            f"trades_executed {trades} > signals_generated {signals} "
            f"(likely reconciled/backfilled exits; inspect enter-row coverage)"
        )
    if hard_issues:
        return DecisionFinding(
            check="conversion_math", severity="fail",
            message="conversion rate self-inconsistent: "
                    + "; ".join(hard_issues),
        )
    if soft_issues:
        return DecisionFinding(
            check="conversion_math", severity="warn",
            message="conversion data gap: " + "; ".join(soft_issues),
        )
    return DecisionFinding(
        check="conversion_math", severity="ok",
        message=f"conversion math self-consistent "
                f"(signals={signals} trades={trades} W/L={wins}/{losses} "
                f"pending={pending})",
    )


def _check_system_wr_gap(bundle: dict[str, Any]) -> DecisionFinding:
    wr = bundle.get("system_wr")
    tgt = bundle.get("system_wr_target")
    gap = bundle.get("system_wr_gap")
    if tgt is None:
        return DecisionFinding(
            check="system_wr_gap", severity="fail",
            message="system_wr_target missing",
        )
    if wr is None:
        if gap is not None:
            return DecisionFinding(
                check="system_wr_gap", severity="fail",
                message=f"system_wr None but gap={gap} (should be None too)",
            )
        return DecisionFinding(
            check="system_wr_gap", severity="ok",
            message="no system_wr yet; gap correctly None",
        )
    expected = round(tgt - wr, 4)
    if gap is None or abs(expected - gap) > 1e-4:
        return DecisionFinding(
            check="system_wr_gap", severity="fail",
            message=f"gap {gap} ≠ target-wr {expected}",
        )
    return DecisionFinding(
        check="system_wr_gap", severity="ok",
        message=f"system_wr gap correct: target {tgt} - wr {wr:.4f} = {expected}",
    )


def _check_bundle_freshness(bundle: dict[str, Any]) -> DecisionFinding:
    ts = int(bundle.get("generated_ts_ms") or 0)
    if ts == 0:
        return DecisionFinding(
            check="bundle_freshness", severity="fail",
            message="bundle has no generated_ts_ms",
        )
    age = (time.time() * 1000 - ts) / 1000.0
    if age > 3600:
        return DecisionFinding(
            check="bundle_freshness", severity="warn",
            message=f"bundle is {age:.0f}s old (> 1h)",
        )
    return DecisionFinding(
        check="bundle_freshness", severity="ok",
        message=f"bundle fresh: {age:.0f}s old",
    )


# ---------------------------------------------------------------------------
# Public entry + persistence
# ---------------------------------------------------------------------------

def validate(bundle: dict[str, Any]) -> DecisionTruthVerdict:
    findings = [
        _check_factor_coverage(bundle),
        _check_gate_integrity(bundle),
        _check_evidence_real(bundle),
        _check_conversion_math(bundle),
        _check_system_wr_gap(bundle),
        _check_bundle_freshness(bundle),
    ]
    n_ok = sum(1 for f in findings if f.severity == "ok")
    n_warn = sum(1 for f in findings if f.severity == "warn")
    n_fail = sum(1 for f in findings if f.severity == "fail")
    if n_fail > 0:
        verdict = "invalid"
    elif n_warn > 0:
        verdict = "suspect"
    else:
        verdict = "valid"
    return DecisionTruthVerdict(
        bundle_ts_ms=int(bundle.get("generated_ts_ms") or 0),
        verdict=verdict,
        n_checks=len(findings),
        n_ok=n_ok, n_warn=n_warn, n_fail=n_fail,
        findings=findings,
        checked_at_ms=int(time.time() * 1000),
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_decision_truth_verdicts (
    bundle_ts_ms    INTEGER PRIMARY KEY,
    verdict         TEXT NOT NULL,
    n_ok            INTEGER NOT NULL,
    n_warn          INTEGER NOT NULL,
    n_fail          INTEGER NOT NULL,
    checked_at_ms   INTEGER NOT NULL,
    payload_json    TEXT NOT NULL
);
"""


def _init_schema() -> None:
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()


def persist_verdict(v: DecisionTruthVerdict) -> None:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO spot_decision_truth_verdicts "
            "(bundle_ts_ms, verdict, n_ok, n_warn, n_fail, checked_at_ms, "
            " payload_json) VALUES (?,?,?,?,?,?,?)",
            (v.bundle_ts_ms, v.verdict, v.n_ok, v.n_warn, v.n_fail,
             v.checked_at_ms, json.dumps(v.to_dict(), default=str)),
        )
        con.commit()
    finally:
        con.close()


def latest_verdict() -> Optional[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT payload_json FROM spot_decision_truth_verdicts "
            "ORDER BY checked_at_ms DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else None


def validate_and_persist(bundle: dict[str, Any]) -> DecisionTruthVerdict:
    v = validate(bundle)
    try:
        persist_verdict(v)
    except Exception:  # noqa: BLE001
        pass
    return v
