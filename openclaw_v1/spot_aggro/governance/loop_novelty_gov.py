"""Phase 11n-3 — Loop Novelty Governor (Layer 7).

Enforces the hard rule: every research/scenario loop iteration MUST
differ from the previous one. No pass may repeat axis_order, seed_salt,
or exact batch_signature back-to-back. If the most-recent N batches for
a tier share a signature, the loop is "stuck" and the governor flags it
so the auto-orchestrator can force a salt change.

Checks per batch (lookback = last N=5 batches for the same tier):
  1. signature_unique  — current batch_signature differs from each of
                         the previous N.
  2. axis_rotation     — axis_order differs from the immediately
                         previous batch (adjacent-duplicate check).
  3. salt_unique       — seed_salt differs from ALL previous batches in
                         the lookback window (pass-salt must never
                         repeat in the last N passes).
  4. pass_progression  — pass_index is monotonically non-decreasing
                         (detects DB corruption / manual rewrites).
  5. data_variation    — at least one of the first-N scenario_ids in
                         this batch must not appear in the previous
                         batch's scenario_ids (proves the simulator saw
                         novel inputs).
  6. stuck_detection   — across last N batches for this tier, no single
                         best_scenario_id appears more than ceil(N/2).

Verdict: "novel" | "degraded" | "stuck". Stuck = at least one fail.

SPOT AGGRO only. Read-only. Writes a verdict row per audit.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Optional


LOOKBACK = 5


@dataclass
class NoveltyFinding:
    check: str
    severity: str   # "ok" | "warn" | "fail"
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NoveltyVerdict:
    tier: str
    batch_id: str
    verdict: str    # "novel" | "degraded" | "stuck"
    n_checks: int
    n_ok: int
    n_warn: int
    n_fail: int
    findings: list[NoveltyFinding]
    checked_at_ms: int

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["findings"] = [f.to_dict() if hasattr(f, "to_dict") else dict(f)
                         for f in self.findings]
        return d


# ---------------------------------------------------------------------------
# DB lookups
# ---------------------------------------------------------------------------

def _recent_batches(tier: str, limit: int = LOOKBACK + 1) -> list[dict[str, Any]]:
    """Most recent batches for this tier, newest first."""
    from shared.persistence import state as persist
    from spot_aggro.governance import scenario_runner as sr
    sr._init_schema()  # ensures the signature columns exist
    persist.init_schema()
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT batch_id, generated_ts_ms, batch_signature, pass_index, "
            " payload_json "
            "FROM spot_scenario_batches WHERE tier = ? "
            "ORDER BY generated_ts_ms DESC LIMIT ?",
            (tier, int(limit)),
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        try:
            payload = json.loads(r[4])
        except Exception:  # noqa: BLE001
            payload = {}
        out.append({
            "batch_id": r[0], "generated_ts_ms": r[1],
            "batch_signature": r[2], "pass_index": r[3] or 0,
            "payload": payload,
        })
    return out


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _check_signature_unique(curr: dict, prior: list[dict]) -> NoveltyFinding:
    sig = curr.get("batch_signature") or ""
    if not sig:
        return NoveltyFinding(
            check="signature_unique", severity="fail",
            message="current batch has no batch_signature",
        )
    repeats = [p["batch_id"] for p in prior if p.get("batch_signature") == sig]
    if repeats:
        return NoveltyFinding(
            check="signature_unique", severity="fail",
            message=f"signature {sig} matches {len(repeats)} prior batches",
            evidence={"repeated_in": repeats[:3]},
        )
    return NoveltyFinding(
        check="signature_unique", severity="ok",
        message=f"signature {sig} is unique in lookback window of {len(prior)}",
    )


def _check_axis_rotation(curr: dict, prior: list[dict]) -> NoveltyFinding:
    if not prior:
        return NoveltyFinding(
            check="axis_rotation", severity="ok",
            message="first batch for this tier; nothing to compare",
        )
    curr_order = tuple((curr.get("payload") or {}).get("axis_order") or ())
    prev_order = tuple((prior[0].get("payload") or {}).get("axis_order") or ())
    if not curr_order:
        return NoveltyFinding(
            check="axis_rotation", severity="warn",
            message="current batch did not record axis_order",
        )
    if curr_order == prev_order:
        return NoveltyFinding(
            check="axis_rotation", severity="fail",
            message=f"axis_order repeats previous batch: {list(curr_order)}",
        )
    return NoveltyFinding(
        check="axis_rotation", severity="ok",
        message=f"axis_order rotated: prev={list(prev_order)[:3]}... "
                f"curr={list(curr_order)[:3]}...",
    )


def _check_salt_unique(curr: dict, prior: list[dict]) -> NoveltyFinding:
    curr_salt = (curr.get("payload") or {}).get("seed_salt") or ""
    if not curr_salt:
        return NoveltyFinding(
            check="salt_unique", severity="warn",
            message="current batch has no seed_salt",
        )
    seen = [(p.get("payload") or {}).get("seed_salt", "") for p in prior]
    if curr_salt in seen:
        return NoveltyFinding(
            check="salt_unique", severity="fail",
            message=f"seed_salt {curr_salt!r} already used in lookback",
            evidence={"prior_salts": seen[:5]},
        )
    return NoveltyFinding(
        check="salt_unique", severity="ok",
        message=f"seed_salt {curr_salt} is unique in lookback "
                f"(saw {len([s for s in seen if s])} prior salts)",
    )


def _check_pass_progression(curr: dict, prior: list[dict]) -> NoveltyFinding:
    curr_pi = int(curr.get("pass_index") or 0)
    prior_pis = [int(p.get("pass_index") or 0) for p in prior]
    if prior_pis and curr_pi < max(prior_pis):
        return NoveltyFinding(
            check="pass_progression", severity="fail",
            message=f"pass_index {curr_pi} < max prior {max(prior_pis)} "
                    f"(regression / DB tamper)",
        )
    return NoveltyFinding(
        check="pass_progression", severity="ok",
        message=f"pass_index={curr_pi} progresses monotonically",
    )


def _check_data_variation(curr: dict, prior: list[dict]) -> NoveltyFinding:
    if not prior:
        return NoveltyFinding(
            check="data_variation", severity="ok",
            message="first batch for this tier",
        )
    curr_ids = {
        o.get("scenario_id")
        for o in ((curr.get("payload") or {}).get("outcomes") or [])[:10]
    }
    prev_ids = {
        o.get("scenario_id")
        for o in ((prior[0].get("payload") or {}).get("outcomes") or [])[:10]
    }
    overlap = curr_ids & prev_ids
    if curr_ids and curr_ids.issubset(prev_ids):
        return NoveltyFinding(
            check="data_variation", severity="fail",
            message=f"every scenario_id in current first-10 appears in "
                    f"previous batch (no novel inputs)",
            evidence={"overlap": list(overlap)[:5]},
        )
    if len(overlap) == len(curr_ids) // 2 + 1 and curr_ids:
        return NoveltyFinding(
            check="data_variation", severity="warn",
            message=f"high scenario_id overlap with previous: "
                    f"{len(overlap)}/{len(curr_ids)}",
        )
    return NoveltyFinding(
        check="data_variation", severity="ok",
        message=f"scenario_ids differ: {len(curr_ids - prev_ids)} novel "
                f"out of {len(curr_ids)} in first-10",
    )


def _check_stuck_detection(
    curr: dict, prior: list[dict],
) -> NoveltyFinding:
    all_batches = [curr] + prior
    best_ids = [
        ((b.get("payload") or {}).get("best_outcome") or {}).get("scenario_id")
        for b in all_batches
    ]
    best_ids = [b for b in best_ids if b]
    if not best_ids:
        return NoveltyFinding(
            check="stuck_detection", severity="ok",
            message="no best_outcome yet — nothing to compare",
        )
    counts: dict[str, int] = {}
    for bid in best_ids:
        counts[bid] = counts.get(bid, 0) + 1
    dominant, n = max(counts.items(), key=lambda kv: kv[1])
    threshold = math.ceil(len(best_ids) / 2) + 1
    if n >= threshold:
        return NoveltyFinding(
            check="stuck_detection", severity="fail",
            message=f"best_scenario_id {dominant} dominates {n}/{len(best_ids)} "
                    f"batches — loop appears stuck on the same solution",
        )
    return NoveltyFinding(
        check="stuck_detection", severity="ok",
        message=f"best_scenario_id diversity OK: top occurs {n}/{len(best_ids)}",
    )


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def validate_batch(batch_dict: dict[str, Any]) -> NoveltyVerdict:
    """Audit a single batch (as its to_dict()) against the recent history
    of batches for the same tier."""
    tier = batch_dict.get("tier") or "?"
    batch_id = batch_dict.get("batch_id") or "?"
    # Fetch LOOKBACK batches that are STRICTLY PRIOR to this one.
    recent = _recent_batches(tier, limit=LOOKBACK + 1)
    prior = [r for r in recent if r["batch_id"] != batch_id][:LOOKBACK]
    curr = {
        "batch_id": batch_id,
        "batch_signature": batch_dict.get("batch_signature"),
        "pass_index": batch_dict.get("pass_index", 0),
        "payload": batch_dict,
    }

    findings = [
        _check_signature_unique(curr, prior),
        _check_axis_rotation(curr, prior),
        _check_salt_unique(curr, prior),
        _check_pass_progression(curr, prior),
        _check_data_variation(curr, prior),
        _check_stuck_detection(curr, prior),
    ]
    n_ok = sum(1 for f in findings if f.severity == "ok")
    n_warn = sum(1 for f in findings if f.severity == "warn")
    n_fail = sum(1 for f in findings if f.severity == "fail")
    if n_fail > 0:
        verdict = "stuck"
    elif n_warn > 0:
        verdict = "degraded"
    else:
        verdict = "novel"
    return NoveltyVerdict(
        tier=tier,
        batch_id=batch_id,
        verdict=verdict,
        n_checks=len(findings),
        n_ok=n_ok, n_warn=n_warn, n_fail=n_fail,
        findings=findings,
        checked_at_ms=int(time.time() * 1000),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_loop_novelty_verdicts (
    batch_id        TEXT PRIMARY KEY,
    tier            TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    n_ok            INTEGER NOT NULL,
    n_warn          INTEGER NOT NULL,
    n_fail          INTEGER NOT NULL,
    checked_at_ms   INTEGER NOT NULL,
    payload_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_loop_novelty_ts
    ON spot_loop_novelty_verdicts(checked_at_ms DESC);
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


def persist_verdict(v: NoveltyVerdict) -> None:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO spot_loop_novelty_verdicts "
            "(batch_id, tier, verdict, n_ok, n_warn, n_fail, checked_at_ms, "
            " payload_json) VALUES (?,?,?,?,?,?,?,?)",
            (v.batch_id, v.tier, v.verdict, v.n_ok, v.n_warn, v.n_fail,
             v.checked_at_ms, json.dumps(v.to_dict(), default=str)),
        )
        con.commit()
    finally:
        con.close()


def latest_verdict(tier: Optional[str] = None) -> Optional[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        if tier:
            row = con.execute(
                "SELECT payload_json FROM spot_loop_novelty_verdicts "
                "WHERE tier = ? ORDER BY checked_at_ms DESC LIMIT 1",
                (tier,),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT payload_json FROM spot_loop_novelty_verdicts "
                "ORDER BY checked_at_ms DESC LIMIT 1"
            ).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else None


def history(tier: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        if tier:
            rows = con.execute(
                "SELECT batch_id, tier, verdict, n_ok, n_warn, n_fail, "
                " checked_at_ms FROM spot_loop_novelty_verdicts "
                "WHERE tier = ? ORDER BY checked_at_ms DESC LIMIT ?",
                (tier, int(limit)),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT batch_id, tier, verdict, n_ok, n_warn, n_fail, "
                " checked_at_ms FROM spot_loop_novelty_verdicts "
                "ORDER BY checked_at_ms DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    finally:
        con.close()
    return [
        {"batch_id": r[0], "tier": r[1], "verdict": r[2],
         "n_ok": r[3], "n_warn": r[4], "n_fail": r[5],
         "checked_at_ms": r[6]}
        for r in rows
    ]


def validate_and_persist(batch_dict: dict[str, Any]) -> NoveltyVerdict:
    v = validate_batch(batch_dict)
    try:
        persist_verdict(v)
    except Exception:  # noqa: BLE001
        pass
    return v
