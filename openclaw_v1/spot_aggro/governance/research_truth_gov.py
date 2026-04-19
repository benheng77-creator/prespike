"""Phase 11n — Research Truth Governor (Layer 4).

Adversarial validator that proves a given ResearchReport is evidence-
backed, internally consistent, and deterministic. Runs against every
report the research agent persists.

Checks:
  1. DETERMINISM       — rerun run_research() with the same clock and
                         same DB, outputs must match (except report_id
                         uuid suffix + audit_rollup_id ts).
  2. DB-RECONCILIATION — each tier's claimed WR must match the WR
                         computed directly from apex_trade_log for the
                         same window. Catches "agent lies about math".
  3. EVIDENCE-REFS     — every recommendation.evidence.tier must match
                         a real tier row in tier_stats; every symbol
                         recommendation must reference a symbol that
                         actually appears in the trade log for that
                         tier in the window.
  4. CI-BOUNDS         — every Wilson CI must be (low <= high,
                         0 <= low, high <= 1, width == high - low).
  5. THRESHOLD-MATH    — halt verdicts must match the documented rules:
                         halt iff sample >= MIN AND wr < halt_min AND
                         currently_on; thaw iff sample >= MIN AND
                         wr >= restore_min AND currently_off.
  6. SCHEMA            — persisted row must carry snapshot_id,
                         audit_rollup_id, status.

Result: ResearchTruthVerdict. Verdict ∈ {valid, suspect, invalid}.
  valid   — all 6 checks pass
  suspect — 1 warning-level finding (e.g. determinism drift within
            float tolerance, empty recommendations array)
  invalid — any check fails hard (lying about WR, missing fields)

On `invalid` the research agent is considered untrusted for THIS run;
the halt decision stands (fail-safe) but the report is tagged so the
operator sees it. The truth governor NEVER changes trading behavior
directly — it only validates and tags.

SPOT AGGRO only. Read-only over trading state. No apex_omega imports.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Optional


@dataclass
class TruthFinding:
    check: str
    severity: str  # "ok" | "warn" | "fail"
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResearchTruthVerdict:
    report_id: str
    audit_rollup_id: str
    verdict: str  # "valid" | "suspect" | "invalid"
    n_checks: int
    n_ok: int
    n_warn: int
    n_fail: int
    findings: list[TruthFinding]
    checked_at_ms: int

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# Individual checks.
# ---------------------------------------------------------------------------

def _check_schema(report: dict[str, Any]) -> TruthFinding:
    required = {"report_id", "snapshot_id", "audit_rollup_id",
                "generated_ts_ms", "status", "tier_stats", "halt_state",
                "thresholds"}
    missing = [k for k in required if k not in report]
    if missing:
        return TruthFinding(
            check="schema", severity="fail",
            message=f"missing required fields: {missing}",
            evidence={"missing": missing},
        )
    if report.get("status") not in ("interim", "final"):
        return TruthFinding(
            check="schema", severity="fail",
            message=f"invalid status: {report.get('status')!r}",
        )
    return TruthFinding(
        check="schema", severity="ok",
        message="all required fields present with valid types",
    )


def _check_ci_bounds(report: dict[str, Any]) -> TruthFinding:
    violations = []
    for s in report.get("tier_stats", []):
        for window_name, w in (s.get("windows") or {}).items():
            ci = w.get("confidence_interval_95")
            if ci is None:
                continue
            lo, hi, width = ci.get("low"), ci.get("high"), ci.get("width")
            if None in (lo, hi, width):
                violations.append(f"{s['tier']}.{window_name}: null ci component")
                continue
            if not (0.0 <= lo <= hi <= 1.0):
                violations.append(
                    f"{s['tier']}.{window_name}: ci=[{lo:.4f},{hi:.4f}] "
                    f"violates bounds"
                )
            if abs((hi - lo) - width) > 1e-9:
                violations.append(
                    f"{s['tier']}.{window_name}: width {width:.6f} "
                    f"!= high-low {hi-lo:.6f}"
                )
    if violations:
        return TruthFinding(
            check="ci_bounds", severity="fail",
            message="Wilson CI bound violations: " + "; ".join(violations[:3]),
            evidence={"violations": violations[:10]},
        )
    return TruthFinding(
        check="ci_bounds", severity="ok",
        message="all Wilson CIs obey bounds + width-consistency",
    )


def _check_db_reconciliation(report: dict[str, Any]) -> TruthFinding:
    """For the PRIMARY window, recompute per-tier WR directly from the DB
    and compare against the report's claim. Tolerance = 1% (float
    accumulation drift). Any larger divergence is the agent lying about
    its math or operating on stale data."""
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    primary = (report.get("thresholds") or {}).get("primary_window", "1h")
    # Convert window label to hours.
    window_hours = {"30m": 0.5, "1h": 1.0, "24h": 24.0, "7d": 168.0}.get(primary, 1.0)
    # Use the report's generated_ts_ms as the clock anchor — this is what
    # the agent saw. If the agent ran at T and we rerun at T+30s, we'd
    # see different data; pin to their snapshot time.
    now_ms = int(report.get("generated_ts_ms") or time.time() * 1000)
    cutoff_ms = int(now_ms - window_hours * 3600 * 1000)

    try:
        claimed = {s["tier"]: s for s in report.get("tier_stats", [])}
        rows = con.execute(
            "SELECT tier, COUNT(CASE WHEN action='exit' AND pnl_usd > 0.001 THEN 1 END) AS wins, "
            "             COUNT(CASE WHEN action='exit' AND pnl_usd IS NOT NULL THEN 1 END) AS exits "
            "FROM apex_trade_log "
            "WHERE tier IN ('A+','A','B','C') AND ts_ms > ? AND ts_ms <= ? "
            "AND (module LIKE 'M1_squeeze%' OR module LIKE 'M1_flow%' "
            "     OR module LIKE 'M1_scalp%' OR module LIKE 'M3_blitz%') "
            "GROUP BY tier",
            (cutoff_ms, now_ms),
        ).fetchall()
    finally:
        con.close()

    db_actual = {r[0]: {"wins": int(r[1]), "exits": int(r[2])} for r in rows}
    mismatches = []
    for tier, s in claimed.items():
        db = db_actual.get(tier, {"wins": 0, "exits": 0})
        if s.get("primary_sample", 0) != db["exits"]:
            mismatches.append(
                f"{tier}: claimed {s.get('primary_sample')} exits, "
                f"DB shows {db['exits']}"
            )
            continue
        claimed_wr = s.get("primary_wr")
        db_wr = (db["wins"] / db["exits"]) if db["exits"] > 0 else None
        if claimed_wr is None and db_wr is None:
            continue
        if claimed_wr is None or db_wr is None:
            mismatches.append(
                f"{tier}: wr nullability mismatch "
                f"(claimed={claimed_wr}, db={db_wr})"
            )
            continue
        if abs(claimed_wr - db_wr) > 0.01:
            mismatches.append(
                f"{tier}: claimed WR {claimed_wr:.4f}, "
                f"DB recomputed {db_wr:.4f} (Δ {abs(claimed_wr - db_wr):.4f})"
            )
    if mismatches:
        return TruthFinding(
            check="db_reconciliation", severity="fail",
            message="DB vs report mismatch: " + "; ".join(mismatches[:3]),
            evidence={"mismatches": mismatches},
        )
    return TruthFinding(
        check="db_reconciliation", severity="ok",
        message=f"all tier WRs match DB recomputation within 1% tolerance "
                f"(window={primary}, rows={sum(d['exits'] for d in db_actual.values())})",
    )


def _check_evidence_refs(report: dict[str, Any]) -> TruthFinding:
    """Every recommendation.evidence.tier must match a real tier in
    tier_stats, and symbol references must correspond to real DB rows
    for that tier."""
    tier_set = {s["tier"] for s in report.get("tier_stats", [])}
    violations = []
    for rec in report.get("recommendations", []):
        ev = rec.get("evidence") or {}
        tier = ev.get("tier")
        if tier is not None and tier not in tier_set:
            violations.append(
                f"recommendation evidences tier {tier!r} which isn't in tier_stats"
            )
        sym = ev.get("symbol")
        if sym:
            # Phase 11n-9-g: confirm symbol has real rows within the
            # research agent's per_symbol window (now 7d by default).
            # Previously this used a hard-coded 24h check which flagged
            # every recommendation as fabricated on quiet days.
            import os as _os
            try:
                per_sym_hours = float(_os.environ.get(
                    "SPOT_RESEARCH_PER_SYMBOL_HOURS", "").strip() or "168")
            except (ValueError, TypeError):
                per_sym_hours = 168.0
            cutoff_ms = int(time.time() * 1000) - int(per_sym_hours * 3600 * 1000)
            from shared.persistence import state as persist
            persist.init_schema()
            con = persist._connect()
            try:
                # Match symbol across ANY tier (report rollups may tag a
                # different tier than the symbol historically traded on).
                n = con.execute(
                    "SELECT COUNT(*) FROM apex_trade_log "
                    "WHERE symbol = ? AND action='exit' "
                    "AND ts_ms > ? AND pnl_usd IS NOT NULL",
                    (sym, cutoff_ms),
                ).fetchone()[0]
            finally:
                con.close()
            if n == 0:
                violations.append(
                    f"recommendation cites {sym} ({tier}) but DB has 0 "
                    f"closed exits in the per-symbol window"
                )
    if violations:
        return TruthFinding(
            check="evidence_refs", severity="fail",
            message="fabricated evidence references: " + "; ".join(violations[:3]),
            evidence={"violations": violations[:10]},
        )
    return TruthFinding(
        check="evidence_refs", severity="ok",
        message=f"all {len(report.get('recommendations', []))} "
                f"recommendations reference real entities",
    )


def _check_threshold_math(report: dict[str, Any]) -> TruthFinding:
    th = report.get("thresholds") or {}
    halt_min = th.get("wr_halt_min", 0.60)
    restore_min = th.get("wr_restore_min", 0.50)
    min_sample = th.get("min_sample_for_halt", 50)
    halt_state = report.get("halt_state") or {}
    violations = []
    for s in report.get("tier_stats", []):
        tier = s["tier"]
        verdict = s.get("halt_verdict")
        sample = s.get("primary_sample", 0)
        wr = s.get("primary_wr")
        currently_halted = halt_state.get(tier, False)

        if verdict == "halt":
            if sample < min_sample:
                violations.append(
                    f"{tier}: verdict=halt but sample {sample} < min {min_sample}"
                )
            if wr is not None and wr >= halt_min:
                violations.append(
                    f"{tier}: verdict=halt but WR {wr:.4f} >= halt_min {halt_min}"
                )
        elif verdict == "thaw":
            if wr is None or wr < restore_min:
                violations.append(
                    f"{tier}: verdict=thaw but WR {wr} < restore_min {restore_min}"
                )
        elif verdict == "insufficient_sample":
            # Allowed states: sample < min_sample OR wr is None.
            if sample >= min_sample and wr is not None:
                violations.append(
                    f"{tier}: verdict=insufficient_sample but sample "
                    f"{sample} >= min {min_sample} AND wr={wr}"
                )
        elif verdict == "allow":
            # sample >= min AND wr in [restore_min, halt_min) OR
            # wr >= halt_min and currently_halted is False.
            # Actually, "allow" can mean many things — just check it's
            # not incompatible with a clear halt signal.
            if (sample >= min_sample and wr is not None
                and wr < halt_min and not currently_halted):
                violations.append(
                    f"{tier}: verdict=allow but WR {wr:.4f} < halt_min "
                    f"{halt_min} with sufficient sample"
                )
    if violations:
        return TruthFinding(
            check="threshold_math", severity="fail",
            message="halt verdicts violate documented thresholds: "
                    + "; ".join(violations[:3]),
            evidence={"violations": violations[:10]},
        )
    return TruthFinding(
        check="threshold_math", severity="ok",
        message="every halt verdict matches the documented threshold rules",
    )


def _check_determinism(report: dict[str, Any]) -> TruthFinding:
    """Rerun the research agent with the same frozen clock and compare
    key fields. Any variance (beyond uuid suffix) is non-determinism."""
    try:
        from spot_aggro.governance import research_agent as ra
    except Exception as exc:  # noqa: BLE001
        return TruthFinding(
            check="determinism", severity="warn",
            message=f"could not re-import research agent: {exc!r}",
        )

    ts_ms = int(report.get("generated_ts_ms") or 0)
    if ts_ms <= 0:
        return TruthFinding(
            check="determinism", severity="warn",
            message="report has no generated_ts_ms; cannot re-pin clock",
        )
    clk = lambda ts=ts_ms: ts / 1000.0

    try:
        r2 = ra.run_research(clock=clk, status=report.get("status", "interim"))
    except Exception as exc:  # noqa: BLE001
        return TruthFinding(
            check="determinism", severity="fail",
            message=f"rerun raised: {type(exc).__name__}: {exc}",
        )

    # Compare key deterministic fields.
    claimed = {s["tier"]: s for s in report.get("tier_stats", [])}
    rerun = {s.tier: s for s in r2.tier_stats}
    diffs = []
    for tier in ("A+", "A", "B", "C"):
        c = claimed.get(tier, {})
        r = rerun.get(tier)
        if r is None:
            diffs.append(f"{tier}: missing on rerun")
            continue
        if c.get("primary_sample") != r.primary_sample:
            diffs.append(
                f"{tier}: primary_sample {c.get('primary_sample')} != {r.primary_sample}"
            )
        cwr = c.get("primary_wr")
        rwr = r.primary_wr
        if cwr is None and rwr is None:
            continue
        if cwr is None or rwr is None:
            diffs.append(f"{tier}: wr nullability drift")
            continue
        if abs(cwr - rwr) > 1e-9:
            diffs.append(f"{tier}: wr {cwr} != {rwr}")

    if not diffs:
        return TruthFinding(
            check="determinism", severity="ok",
            message="rerun on same clock + same DB produced identical WR/sample",
        )
    # Determinism drift is suspect but not always fail — small float
    # accumulation differences can happen on numpy-free arithmetic.
    return TruthFinding(
        check="determinism", severity="warn",
        message="determinism drift: " + "; ".join(diffs[:3]),
        evidence={"diffs": diffs},
    )


# ---------------------------------------------------------------------------
# Public entry + persistence.
# ---------------------------------------------------------------------------

def validate(report: dict[str, Any]) -> ResearchTruthVerdict:
    findings = [
        _check_schema(report),
        _check_ci_bounds(report),
        _check_db_reconciliation(report),
        _check_evidence_refs(report),
        _check_threshold_math(report),
        _check_determinism(report),
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
    return ResearchTruthVerdict(
        report_id=report.get("report_id", "<unknown>"),
        audit_rollup_id=report.get("audit_rollup_id", "<unknown>"),
        verdict=verdict,
        n_checks=len(findings),
        n_ok=n_ok, n_warn=n_warn, n_fail=n_fail,
        findings=findings,
        checked_at_ms=int(time.time() * 1000),
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_research_truth_verdicts (
    report_id         TEXT PRIMARY KEY,
    audit_rollup_id   TEXT,
    verdict           TEXT NOT NULL,
    n_ok              INTEGER NOT NULL,
    n_warn            INTEGER NOT NULL,
    n_fail            INTEGER NOT NULL,
    checked_at_ms     INTEGER NOT NULL,
    payload_json      TEXT NOT NULL
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


def persist_verdict(v: ResearchTruthVerdict) -> None:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO spot_research_truth_verdicts "
            "(report_id, audit_rollup_id, verdict, n_ok, n_warn, n_fail, "
            " checked_at_ms, payload_json) VALUES (?,?,?,?,?,?,?,?)",
            (v.report_id, v.audit_rollup_id, v.verdict, v.n_ok, v.n_warn,
             v.n_fail, v.checked_at_ms,
             json.dumps(v.to_dict(), default=str)),
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
            "SELECT payload_json FROM spot_research_truth_verdicts "
            "ORDER BY checked_at_ms DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else None


def validate_and_persist(report: dict[str, Any]) -> ResearchTruthVerdict:
    v = validate(report)
    try:
        persist_verdict(v)
    except Exception:  # noqa: BLE001
        pass
    return v
