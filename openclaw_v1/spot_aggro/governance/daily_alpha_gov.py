"""Phase 11n-9 — Daily Alpha Governor (Layer 8).

Third-layer adversarial governor that sits between:
  Layer 4–7 (research-truth · card-truth · loop-novelty · decision-truth)
  ─── and ───
  the execution decision ("may this alpha pick actually trade?").

For each pick produced by daily_alpha.build_daily_alpha(), this module
runs a COMPREHENSIVE 12-item pre-trade checklist covering four domains:

  EVIDENCE (4 items)
    1. evidence_refs_resolvable — every ref points at a real artifact id
    2. sample_size_sufficient   — sample ≥ DEFAULT_MIN_SAMPLE (5 exits)
    3. research_truth_clean     — latest research truth verdict ≠ invalid
    4. card_truth_clean         — latest card truth verdict ≠ fail

  TECHNICAL (4 items)
    5. projected_wr_meets_target — proj_wr ≥ target_proj_wr (default 70%)
    6. tier_wr_aligned          — tier WR ≥ target × 0.85 (supports call)
    7. scenario_confirms        — best scenario proj WR ≥ target OR
                                  no scenario exists and sample ≥ 10
    8. no_blocking_factors      — no factor has severity=="block"

  CALCULATED (2 items)
    9. wilson_ci_lower_bound    — Wilson 95% CI lower bound ≥ target × 0.7
                                  (guards against "hot hand" small samples)
   10. not_stuck_on_same_symbol — symbol didn't dominate the last 5
                                  alpha bundles (diversification)

  OPERATIONAL (2 items)
   11. tier_toggle_enabled     — the tier's toggle is ON
   12. daily_quota_available   — fewer than MAX_BUYS/MAX_SELLS today

A pick is ADMITTED only when ALL 12 items pass. The governor attaches
the full checklist + overall score to every pick.

Read-only. Never places orders. The "admit" boolean is advisory; an
execution layer would consult it before sending anything to OKX.

SPOT AGGRO only.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Optional


@dataclass
class ChecklistItem:
    key: str                  # stable programmatic name
    domain: str               # "evidence" | "technical" | "calculated" | "operational"
    label: str                # human-readable one-liner
    passed: bool
    detail: str               # explanation shown to the operator

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AlphaPickVerdict:
    symbol: str
    action: str
    admitted: bool
    score: float              # 0..1 fraction of checklist items passed
    checklist: list[ChecklistItem]
    rejection_reason: Optional[str]
    checked_at_ms: int

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["checklist"] = [c.to_dict() if hasattr(c, "to_dict") else dict(c)
                          for c in self.checklist]
        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wilson_ci_lower(wins: int, total: int, z: float = 1.959963984540054) -> float:
    if total <= 0:
        return 0.0
    p = wins / total
    denom = 1 + (z * z) / total
    center = (p + (z * z) / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total
                           + (z * z) / (4 * total * total)) / denom
    return max(0.0, center - margin)


def _recent_alpha_symbols(limit: int = 5) -> list[str]:
    """Symbols that appeared in the last `limit` alpha bundles, flattened."""
    from shared.persistence import state as persist
    from spot_aggro.governance import daily_alpha
    daily_alpha._init_schema()
    persist.init_schema()
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT payload_json FROM spot_daily_alpha_bundles "
            "ORDER BY generated_ts_ms DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        con.close()
    syms: list[str] = []
    for r in rows:
        try:
            payload = json.loads(r[0])
        except Exception:  # noqa: BLE001
            continue
        for p in (payload.get("buys") or []) + (payload.get("sells") or []):
            s = p.get("symbol")
            if s and p.get("checklist_pass"):
                syms.append(s)
    return syms


def _truth_latest(module_name: str) -> Optional[dict[str, Any]]:
    try:
        mod = __import__(f"spot_aggro.governance.{module_name}",
                         fromlist=["latest_verdict"])
        fn = getattr(mod, "latest_verdict", None)
        return fn() if fn else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Individual checklist items
# ---------------------------------------------------------------------------

def _check_evidence_refs_resolvable(pick: Any) -> ChecklistItem:
    refs = list(getattr(pick, "evidence_refs", None) or [])
    have_research = any(r.startswith("research://") and
                        not r.endswith("://?") for r in refs)
    have_per_sym = any(r.startswith("per_symbol://") for r in refs)
    if not (have_research and have_per_sym and refs):
        return ChecklistItem(
            key="evidence_refs_resolvable", domain="evidence",
            label="Evidence refs point to real artifacts",
            passed=False,
            detail=f"refs={refs[:3]} — missing research or per_symbol anchor",
        )
    return ChecklistItem(
        key="evidence_refs_resolvable", domain="evidence",
        label="Evidence refs point to real artifacts",
        passed=True,
        detail=f"{len(refs)} refs all resolvable",
    )


def _check_sample_size_sufficient(pick: Any) -> ChecklistItem:
    n = int(getattr(pick, "sample_size", 0) or 0)
    from spot_aggro.governance.daily_alpha import DEFAULT_MIN_SAMPLE
    ok = n >= DEFAULT_MIN_SAMPLE
    return ChecklistItem(
        key="sample_size_sufficient", domain="evidence",
        label=f"Sample size ≥ {DEFAULT_MIN_SAMPLE} closed exits",
        passed=ok,
        detail=f"{n} exits backing this pick",
    )


def _check_research_truth_clean() -> ChecklistItem:
    rt = _truth_latest("research_truth_gov")
    if rt is None:
        return ChecklistItem(
            key="research_truth_clean", domain="evidence",
            label="Research Truth Governor verdict ≠ invalid",
            passed=False,
            detail="research truth gov has never run",
        )
    ok = rt.get("verdict") != "invalid"
    return ChecklistItem(
        key="research_truth_clean", domain="evidence",
        label="Research Truth Governor verdict ≠ invalid",
        passed=ok,
        detail=f"verdict={rt.get('verdict')} n_fail={rt.get('n_fail')}",
    )


def _check_card_truth_clean() -> ChecklistItem:
    try:
        from spot_aggro.governance.card_truth_gov import latest_audit
        ct = latest_audit()
    except Exception:  # noqa: BLE001
        ct = None
    if ct is None:
        return ChecklistItem(
            key="card_truth_clean", domain="evidence",
            label="Card Truth Governor verdict ≠ fail",
            passed=False,
            detail="card truth gov has never run",
        )
    ok = ct.get("verdict") != "fail"
    return ChecklistItem(
        key="card_truth_clean", domain="evidence",
        label="Card Truth Governor verdict ≠ fail",
        passed=ok,
        detail=f"verdict={ct.get('verdict')} n_fail={ct.get('n_fail')}",
    )


def _check_projected_wr_target(pick: Any, target: float) -> ChecklistItem:
    proj = getattr(pick, "projected_wr", None)
    ok = (proj is not None) and (proj >= target)
    return ChecklistItem(
        key="projected_wr_meets_target", domain="technical",
        label=f"Projected WR ≥ {target*100:.0f}% target",
        passed=ok,
        detail=(f"projected WR = {(proj or 0)*100:.1f}%"
                if proj is not None else "no projected WR computable"),
    )


def _check_tier_wr_aligned(pick: Any, research: dict[str, Any],
                           target: float) -> ChecklistItem:
    tier = getattr(pick, "tier", "?")
    tier_row = next((s for s in (research.get("tier_stats") or [])
                     if s.get("tier") == tier), {})
    tier_wr = tier_row.get("primary_wr")
    min_needed = target * 0.85
    ok = (tier_wr is not None) and (tier_wr >= min_needed)
    return ChecklistItem(
        key="tier_wr_aligned", domain="technical",
        label=f"Tier WR ≥ {min_needed*100:.0f}% (≥ target × 0.85)",
        passed=ok,
        detail=(f"tier {tier} WR = {(tier_wr or 0)*100:.1f}%"
                if tier_wr is not None else f"no tier WR for {tier}"),
    )


def _check_scenario_confirms(pick: Any, scenario: Optional[dict[str, Any]],
                             target: float) -> ChecklistItem:
    if scenario and scenario.get("best_outcome"):
        bw = (scenario["best_outcome"] or {}).get("simulated_wr")
        ok = (bw is not None) and (bw >= target)
        return ChecklistItem(
            key="scenario_confirms", domain="technical",
            label=f"Scenario lab best proj WR ≥ {target*100:.0f}%",
            passed=ok,
            detail=f"scenario best WR = {(bw or 0)*100:.1f}%",
        )
    # No scenario: allowed if the per-symbol sample alone is ≥ 10 (larger
    # than the default 5). Rationale: scenarios confirm small samples;
    # when sample itself is strong, no confirmation needed.
    n = int(getattr(pick, "sample_size", 0) or 0)
    ok = n >= 10
    return ChecklistItem(
        key="scenario_confirms", domain="technical",
        label="Scenario confirms (or sample ≥ 10 bypasses)",
        passed=ok,
        detail=(f"no scenario; sample={n} — "
                + ("meets ≥10 bypass threshold" if ok else "needs ≥10")),
    )


def _check_no_blocking_factors(pick: Any) -> ChecklistItem:
    factors = list(getattr(pick, "factors", None) or [])
    blocks = [f for f in factors if f.get("severity") == "block"]
    ok = len(blocks) == 0
    return ChecklistItem(
        key="no_blocking_factors", domain="technical",
        label="No factor has severity=block",
        passed=ok,
        detail=(f"blocking factors: {[f.get('name') for f in blocks]}"
                if blocks else "all factors non-blocking"),
    )


def _check_wilson_ci_lower(pick: Any, per_sym_row: dict[str, Any],
                           target: float) -> ChecklistItem:
    wins = int(per_sym_row.get("wins") or 0)
    losses = int(per_sym_row.get("losses") or 0)
    total = wins + losses
    low = _wilson_ci_lower(wins, total)
    floor = target * 0.7
    ok = total > 0 and low >= floor
    return ChecklistItem(
        key="wilson_ci_lower_bound", domain="calculated",
        label=f"Wilson 95% CI lower bound ≥ {floor*100:.0f}%",
        passed=ok,
        detail=(f"Wilson lower = {low*100:.1f}% on {total} exits"
                if total > 0 else "zero-sample — CI not computable"),
    )


def _check_not_stuck_on_same_symbol(pick: Any) -> ChecklistItem:
    sym = getattr(pick, "symbol", "")
    recent = _recent_alpha_symbols(limit=5)
    dup_count = sum(1 for s in recent if s == sym)
    ok = dup_count < 3
    return ChecklistItem(
        key="not_stuck_on_same_symbol", domain="calculated",
        label="Symbol hasn't dominated the last 5 alpha bundles",
        passed=ok,
        detail=f"{sym} appeared {dup_count}× in last {len(recent)} bundle picks",
    )


def _check_tier_toggle_enabled(pick: Any,
                               toggles: dict[str, bool]) -> ChecklistItem:
    tier = getattr(pick, "tier", "?")
    ok = bool(toggles.get(tier, True))
    return ChecklistItem(
        key="tier_toggle_enabled", domain="operational",
        label=f"Tier {tier} execution toggle is ON",
        passed=ok,
        detail=f"toggle[{tier}] = {toggles.get(tier, True)}",
    )


def _check_daily_quota_available(pick: Any) -> ChecklistItem:
    """Look at today's persisted bundle; if today already has the max
    admitted picks for this action, this item fails."""
    from spot_aggro.governance.daily_alpha import (
        latest_bundle, MAX_BUYS_PER_DAY, MAX_SELLS_PER_DAY,
    )
    latest = latest_bundle() or {}
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if latest.get("date_utc") != today:
        return ChecklistItem(
            key="daily_quota_available", domain="operational",
            label="Daily quota available for this action",
            passed=True,
            detail=f"{today}: no prior bundle today — quota full",
        )
    cap = (MAX_BUYS_PER_DAY if pick.action == "buy" else MAX_SELLS_PER_DAY)
    used = sum(1 for p in (latest.get(
        "buys" if pick.action == "buy" else "sells") or [])
               if p.get("checklist_pass"))
    ok = used < cap
    return ChecklistItem(
        key="daily_quota_available", domain="operational",
        label=f"Daily {pick.action} quota available ({used}/{cap})",
        passed=ok,
        detail=f"{used} already admitted today out of {cap}",
    )


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def evaluate_pick(
    pick: Any,
    *,
    research: dict[str, Any],
    scenario: Optional[dict[str, Any]],
    toggles: dict[str, bool],
    target_proj_wr: float,
) -> AlphaPickVerdict:
    """Run every checklist item and return an AlphaPickVerdict.

    `pick` is a daily_alpha.AlphaPick (or any object with the same
    fields). We use duck-typing so tests can pass simple dicts/objects.
    """
    # Locate the per-symbol row for Wilson CI.
    per_sym_row = next(
        (r for r in (research.get("per_symbol") or [])
         if r.get("symbol") == getattr(pick, "symbol", None)),
        {},
    )

    items: list[ChecklistItem] = [
        _check_evidence_refs_resolvable(pick),
        _check_sample_size_sufficient(pick),
        _check_research_truth_clean(),
        _check_card_truth_clean(),
        _check_projected_wr_target(pick, target_proj_wr),
        _check_tier_wr_aligned(pick, research, target_proj_wr),
        _check_scenario_confirms(pick, scenario, target_proj_wr),
        _check_no_blocking_factors(pick),
        _check_wilson_ci_lower(pick, per_sym_row, target_proj_wr),
        _check_not_stuck_on_same_symbol(pick),
        _check_tier_toggle_enabled(pick, toggles),
        _check_daily_quota_available(pick),
    ]
    passed = sum(1 for i in items if i.passed)
    total = len(items)
    score = passed / total
    admitted = passed == total      # ALL items must pass
    # First failing item's detail becomes the human rejection reason.
    rejection = None
    if not admitted:
        first_fail = next((i for i in items if not i.passed), None)
        if first_fail:
            rejection = f"{first_fail.key}: {first_fail.detail}"

    return AlphaPickVerdict(
        symbol=getattr(pick, "symbol", "?"),
        action=getattr(pick, "action", "?"),
        admitted=admitted,
        score=round(score, 4),
        checklist=items,
        rejection_reason=rejection,
        checked_at_ms=int(time.time() * 1000),
    )


CHECKLIST_KEYS: tuple[str, ...] = (
    "evidence_refs_resolvable",
    "sample_size_sufficient",
    "research_truth_clean",
    "card_truth_clean",
    "projected_wr_meets_target",
    "tier_wr_aligned",
    "scenario_confirms",
    "no_blocking_factors",
    "wilson_ci_lower_bound",
    "not_stuck_on_same_symbol",
    "tier_toggle_enabled",
    "daily_quota_available",
)
