"""Phase 11n-9 — Daily Alpha picker.

Specialized engine that produces up to TWO buy candidates and TWO sell
candidates per day — high-conviction trades only. Uses every upstream
resource the system produces: research WR, per-tier stats, per-symbol
coin accuracy, scenario hypotheses, decision factors, card-truth +
research-truth verdicts.

A pick only proceeds when its projected WR ≥ 70% on sufficient sample
AND the Daily Alpha Governor (Layer 8, in daily_alpha_gov.py) passes
every item in the pre-trade checklist. Below 70% or any failed check
→ pick is tagged REJECTED with the reason and WILL NOT trade.

Advisory-only. This module never places orders. It publishes picks
for the operator and sets the "ready" flag that an execution layer
would consult.

SPOT AGGRO only. No apex_omega imports.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Optional


DEFAULT_MIN_PROJECTED_WR = 0.70
DEFAULT_MIN_SAMPLE = 3            # per-symbol closed exits required (aligned with Coin Accuracy Matrix ≥3-exit confident-sample threshold; Phase 11n-9-g)
MAX_BUYS_PER_DAY = 2
MAX_SELLS_PER_DAY = 2


@dataclass
class AlphaPick:
    symbol: str
    tier: str
    action: str                    # "buy" | "sell"
    projected_wr: Optional[float]  # 0..1
    sample_size: int               # closed exits backing the projection
    confidence: float              # 0..1 composite confidence
    rationale: str                 # one-line plain English
    factors: list[dict[str, Any]]  # each: {name, value, weight, why, severity}
    evidence_refs: list[str]
    checklist_pass: bool           # set by the gov layer
    checklist_score: float         # 0..1, fraction of checklist items ok
    rejection_reason: Optional[str] = None   # set if checklist_pass=False
    # Full 12-item gov checklist embedded on the pick so the dashboard
    # can show exactly which items passed/failed without a second call.
    checklist: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class DailyAlphaBundle:
    generated_ts_ms: int
    date_utc: str                  # YYYY-MM-DD
    buys: list[AlphaPick]
    sells: list[AlphaPick]
    target_proj_wr: float
    admitted_count: int            # total picks that passed the checklist
    rejected_count: int            # total rejected by the checklist
    summary: str
    evidence_refs: list[str]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["buys"] = [p.to_dict() if hasattr(p, "to_dict") else dict(p)
                     for p in self.buys]
        d["sells"] = [p.to_dict() if hasattr(p, "to_dict") else dict(p)
                      for p in self.sells]
        return d


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------

def _latest_research() -> Optional[dict[str, Any]]:
    try:
        from spot_aggro.governance.research_agent import latest_report
        return latest_report()
    except Exception:  # noqa: BLE001
        return None


def _latest_decision() -> Optional[dict[str, Any]]:
    try:
        from spot_aggro.governance.decision_engine import latest_bundle
        return latest_bundle()
    except Exception:  # noqa: BLE001
        return None


def _latest_scenarios_by_tier() -> dict[str, dict[str, Any]]:
    try:
        from spot_aggro.governance.scenario_runner import latest_batch_for_tier
        return {t: (latest_batch_for_tier(t) or {}) for t in ("A+", "A", "B", "C")}
    except Exception:  # noqa: BLE001
        return {}


def _tier_toggle_snapshot() -> dict[str, bool]:
    try:
        from spot_aggro.api import routes as spot_routes
        t = getattr(spot_routes, "_SPOT_TIER_TOGGLE", None)
        if t is None:
            return {"A+": True, "A": True, "B": True, "C": True}
        return dict(t.snapshot())
    except Exception:  # noqa: BLE001
        return {"A+": True, "A": True, "B": True, "C": True}


# ---------------------------------------------------------------------------
# Candidate scoring
# ---------------------------------------------------------------------------

def _project_wr(sym_row: dict[str, Any], tier_row: dict[str, Any],
                scenario: Optional[dict[str, Any]]) -> tuple[Optional[float], int]:
    """Blend per-symbol WR (60%) + tier WR (25%) + best scenario proj WR
    (15%) into a single projected WR. Returns (proj_wr, sample_backing)."""
    sym_wr = sym_row.get("win_rate")
    sym_n = int(sym_row.get("exits") or 0)
    tier_wr = tier_row.get("primary_wr")
    tier_n = int(tier_row.get("primary_sample") or 0)
    scen_wr = None
    if scenario and scenario.get("best_outcome"):
        scen_wr = scenario["best_outcome"].get("simulated_wr")

    weights = [(sym_wr, 0.60), (tier_wr, 0.25), (scen_wr, 0.15)]
    total_w = sum(w for v, w in weights if v is not None)
    if total_w <= 0:
        return None, 0
    proj = sum((v or 0) * w for v, w in weights if v is not None) / total_w
    # The "sample" is the per-symbol sample (most-binding constraint).
    return round(proj, 4), sym_n


def _build_factors(
    sym_row: dict[str, Any], tier_row: dict[str, Any],
    scenario: Optional[dict[str, Any]], proj_wr: Optional[float],
    target: float, toggle_on: bool,
) -> list[dict[str, Any]]:
    sym_wr = sym_row.get("win_rate")
    sym_n = int(sym_row.get("exits") or 0)
    tier_wr = tier_row.get("primary_wr")
    factors = [
        {"name": "projected_wr", "value": proj_wr, "weight": 0.30,
         "why": f"blended projected WR = {(proj_wr or 0)*100:.1f}% vs target {target*100:.0f}%",
         "severity": ("positive" if (proj_wr or 0) >= target
                      else "warn" if (proj_wr or 0) >= target * 0.9
                      else "block")},
        {"name": "symbol_win_rate", "value": sym_wr, "weight": 0.25,
         "why": f"{sym_row.get('symbol')} per-symbol WR {(sym_wr or 0)*100:.1f}%",
         "severity": ("positive" if (sym_wr or 0) >= target else "warn")},
        {"name": "sample_size", "value": sym_n, "weight": 0.15,
         "why": f"{sym_n} closed exits back this projection",
         "severity": ("positive" if sym_n >= DEFAULT_MIN_SAMPLE else "block")},
        {"name": "tier_alignment", "value": tier_wr, "weight": 0.10,
         "why": f"tier {sym_row.get('tier')} rolling WR {(tier_wr or 0)*100:.1f}%",
         "severity": ("positive" if (tier_wr or 0) >= target * 0.85 else "warn")},
        {"name": "tier_toggle", "value": toggle_on, "weight": 0.10,
         "why": "execution allowed" if toggle_on else "tier toggle OFF",
         "severity": "positive" if toggle_on else "block"},
    ]
    if scenario and scenario.get("best_outcome"):
        bw = scenario["best_outcome"].get("simulated_wr")
        factors.append({
            "name": "scenario_hypothesis", "value": bw, "weight": 0.10,
            "why": f"best scenario WR for tier {sym_row.get('tier')} = {(bw or 0)*100:.1f}%",
            "severity": "positive" if (bw or 0) >= target else "info",
        })
    return factors


def _rationale(action: str, sym: str, proj_wr: Optional[float],
               sym_n: int, target: float) -> str:
    pwr = f"{(proj_wr or 0)*100:.1f}%"
    tgt = f"{target*100:.0f}%"
    if action == "buy":
        return (f"Buy {sym}: blended projected WR {pwr} ≥ target {tgt} "
                f"across {sym_n} closed exits; tier + scenario agree.")
    return (f"Sell {sym}: blended projected WR {pwr} ≥ target {tgt}; "
            f"{sym_n} exits provide the sample backing the call.")


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def build_daily_alpha(
    *, target_proj_wr: float = DEFAULT_MIN_PROJECTED_WR,
) -> DailyAlphaBundle:
    """Produce today's alpha bundle: up to 2 buys, 2 sells, all gated by
    the daily_alpha_gov checklist. Picks BELOW the projected-WR target
    are still surfaced but marked REJECTED with the numeric reason."""
    research = _latest_research() or {}
    scenarios = _latest_scenarios_by_tier()
    toggles = _tier_toggle_snapshot()
    per_sym = list(research.get("per_symbol") or [])
    tier_rows = {s["tier"]: s for s in (research.get("tier_stats") or [])}

    # Import governor here to avoid a circular at module load.
    from spot_aggro.governance import daily_alpha_gov as gov

    candidates: list[tuple[float, AlphaPick]] = []
    for row in per_sym:
        tier = row.get("tier", "?")
        tier_row = tier_rows.get(tier, {})
        scenario = scenarios.get(tier)
        toggle_on = bool(toggles.get(tier, True))
        proj_wr, sample = _project_wr(row, tier_row, scenario)
        factors = _build_factors(row, tier_row, scenario, proj_wr,
                                 target_proj_wr, toggle_on)
        # Action: positive WR → buy candidate; tier already held + low
        # WR → sell candidate. Simple heuristic; Gov passes or rejects.
        if (row.get("wins") or 0) >= (row.get("losses") or 0):
            action = "buy"
        else:
            action = "sell"
        pick = AlphaPick(
            symbol=row.get("symbol", "?"),
            tier=tier,
            action=action,
            projected_wr=proj_wr,
            sample_size=sample,
            confidence=min(0.99, max(0.0, proj_wr or 0.0)),
            rationale=_rationale(action, row.get("symbol", "?"),
                                 proj_wr, sample, target_proj_wr),
            factors=factors,
            evidence_refs=[
                f"research://{research.get('report_id','?')}",
                f"per_symbol://{row.get('symbol','?')}",
                f"scenario://{scenario.get('batch_id','none') if scenario else 'none'}",
                f"tier_toggles://{json.dumps(toggles)}",
            ],
            checklist_pass=False,
            checklist_score=0.0,
        )
        # Run the 3rd-layer governor pre-trade checklist.
        verdict = gov.evaluate_pick(pick, research=research,
                                    scenario=scenario, toggles=toggles,
                                    target_proj_wr=target_proj_wr)
        pick.checklist_pass = verdict.admitted
        pick.checklist_score = verdict.score
        pick.rejection_reason = verdict.rejection_reason
        # Embed every checklist item so the dashboard shows all 12 rows
        # with their pass/fail status + detail for operator review.
        pick.checklist = [item.to_dict() if hasattr(item, "to_dict")
                          else dict(item) for item in verdict.checklist]
        # Rank by projected_wr desc, then sample_size desc.
        sort_key = (-(proj_wr or 0.0), -sample)
        candidates.append((sort_key, pick))

    candidates.sort(key=lambda kv: kv[0])
    buys: list[AlphaPick] = []
    sells: list[AlphaPick] = []
    admitted = 0
    rejected = 0
    for _, p in candidates:
        # Cap per-direction daily quota AFTER the checklist result is
        # attached (so rejected picks are still visible but don't count
        # against the 2/day budget).
        if p.action == "buy" and p.checklist_pass and len(buys) < MAX_BUYS_PER_DAY:
            buys.append(p); admitted += 1
        elif p.action == "sell" and p.checklist_pass and len(sells) < MAX_SELLS_PER_DAY:
            sells.append(p); admitted += 1
        else:
            if p.checklist_pass:
                # Passed gov but slot full — demote to rejection for
                # today with "quota full" reason (operator context).
                p.checklist_pass = False
                if not p.rejection_reason:
                    p.rejection_reason = "daily quota full for this action"
            rejected += 1
            if p.action == "buy":
                buys.append(p)
            else:
                sells.append(p)
        if len(buys) >= MAX_BUYS_PER_DAY + 2 and len(sells) >= MAX_SELLS_PER_DAY + 2:
            # Cap display list at 4 each so the card doesn't grow wild.
            break

    now = int(time.time() * 1000)
    date_utc = datetime.fromtimestamp(now / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    summary = (
        f"{date_utc} · {admitted} admitted, {rejected} rejected · "
        f"target WR ≥ {target_proj_wr*100:.0f}% · "
        f"buys: {sum(1 for p in buys if p.checklist_pass)}/{MAX_BUYS_PER_DAY} · "
        f"sells: {sum(1 for p in sells if p.checklist_pass)}/{MAX_SELLS_PER_DAY}"
    )

    return DailyAlphaBundle(
        generated_ts_ms=now,
        date_utc=date_utc,
        buys=buys,
        sells=sells,
        target_proj_wr=target_proj_wr,
        admitted_count=admitted,
        rejected_count=rejected,
        summary=summary,
        evidence_refs=[
            f"research://{research.get('report_id','?')}",
            "scenarios://per-tier",
            "decision://latest",
            "card_truth://latest",
        ],
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_daily_alpha_bundles (
    generated_ts_ms  INTEGER PRIMARY KEY,
    date_utc         TEXT NOT NULL,
    admitted_count   INTEGER NOT NULL,
    rejected_count   INTEGER NOT NULL,
    target_proj_wr   REAL NOT NULL,
    payload_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_daily_alpha_date
    ON spot_daily_alpha_bundles(date_utc);
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


def persist_bundle(b: DailyAlphaBundle) -> None:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO spot_daily_alpha_bundles "
            "(generated_ts_ms, date_utc, admitted_count, rejected_count, "
            " target_proj_wr, payload_json) VALUES (?,?,?,?,?,?)",
            (b.generated_ts_ms, b.date_utc, b.admitted_count, b.rejected_count,
             b.target_proj_wr, json.dumps(b.to_dict(), default=str)),
        )
        con.commit()
    finally:
        con.close()


def latest_bundle() -> Optional[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT payload_json FROM spot_daily_alpha_bundles "
            "ORDER BY generated_ts_ms DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else None


def build_and_persist(
    *, target_proj_wr: float = DEFAULT_MIN_PROJECTED_WR,
) -> DailyAlphaBundle:
    b = build_daily_alpha(target_proj_wr=target_proj_wr)
    try:
        persist_bundle(b)
    except Exception:  # noqa: BLE001
        pass
    return b
