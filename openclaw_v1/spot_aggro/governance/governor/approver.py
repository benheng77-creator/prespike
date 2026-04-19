"""
Forensic Governor — report approval layer.

Consumes a forensic_v2 report *payload* (a dict) and emits a governance
verdict. This module does NOT import `spot_aggro.forensic_v2.*` and does
NOT modify any report field — it reads the caller-supplied payload and
writes a separate governance row keyed by `report_id`.

Verdicts:
    APPROVED                — trust_score >= approved threshold, no hard flags
    APPROVED_WITH_WARNINGS  — soft flags present, trust_score >= warn floor
    REJECTED                — hard flags or trust_score < warn floor

Output payload:
    {
      "report_id": str,
      "verdict": ...,
      "trust_score": 0..1,
      "contradictions": [...],
      "unsupported_claims": [...],
      "telemetry_gaps": [...],
      "quorum_ok": int | None,
      "approval_note": str,
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import yaml
except ImportError as _exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required for the governor") from _exc


DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "governance.yml"
)


VERDICT_APPROVED           = "APPROVED"
VERDICT_APPROVED_WARNINGS  = "APPROVED_WITH_WARNINGS"
VERDICT_REJECTED           = "REJECTED"


@dataclass(frozen=True)
class GovernorConfig:
    enabled: bool
    trust_score_min_approved: float
    trust_score_min_warnings: float
    unverifiable_warn_threshold: int
    unverifiable_reject_threshold: int
    contradiction_warn_threshold: int
    contradiction_reject_threshold: int
    quorum_ok_minimum: int
    meta_enabled: bool
    meta_interval_s: int
    meta_window_s: int
    meta_recurring_fraction: float

    @staticmethod
    def load(path: Optional[Path] = None) -> "GovernorConfig":
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        if not p.exists():
            raise FileNotFoundError(f"governance config not found: {p}")
        with p.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if raw.get("engine") != "spot_aggro":
            raise ValueError("governance config must declare engine=spot_aggro")
        g = raw.get("governor") or {}
        meta = g.get("meta_review") or {}
        return GovernorConfig(
            enabled=bool(g.get("enabled", True)),
            trust_score_min_approved=float(g["trust_score_min_approved"]),
            trust_score_min_warnings=float(g["trust_score_min_warnings"]),
            unverifiable_warn_threshold=int(g["unverifiable_warn_threshold"]),
            unverifiable_reject_threshold=int(g["unverifiable_reject_threshold"]),
            contradiction_warn_threshold=int(g["contradiction_warn_threshold"]),
            contradiction_reject_threshold=int(g["contradiction_reject_threshold"]),
            quorum_ok_minimum=int(g["quorum_ok_minimum"]),
            meta_enabled=bool(meta.get("enabled", True)),
            meta_interval_s=int(meta.get("interval_s", 86400)),
            meta_window_s=int(meta.get("window_s", 86400)),
            meta_recurring_fraction=float(meta.get("recurring_defect_fraction", 0.30)),
        )


# ---------------------------------------------------------------------------
# Core approval logic
# ---------------------------------------------------------------------------

def _extract_section_confidences(report: dict[str, Any]) -> list[str]:
    """Pull every `confidence` value from known forensic_v2 sections."""
    out: list[str] = []
    for section_key in ("sections", "analysis_sections", "findings"):
        sec = report.get(section_key)
        if isinstance(sec, list):
            for item in sec:
                if isinstance(item, dict):
                    c = item.get("confidence")
                    if isinstance(c, str):
                        out.append(c.upper())
        elif isinstance(sec, dict):
            for _k, v in sec.items():
                if isinstance(v, dict):
                    c = v.get("confidence")
                    if isinstance(c, str):
                        out.append(c.upper())
    # Top-level `confidence` (if any)
    top = report.get("confidence")
    if isinstance(top, str):
        out.append(top.upper())
    return out


def _count_contradictions(report: dict[str, Any]) -> list[str]:
    """Forensic_v2 can emit contradictions under a few key names; collect
    all of them. Missing → empty list."""
    items: list[str] = []
    for key in ("contradictions", "contradiction_list"):
        v = report.get(key)
        if isinstance(v, list):
            for x in v:
                items.append(str(x))
    return items


def _count_unsupported(report: dict[str, Any]) -> list[str]:
    items: list[str] = []
    for key in ("unsupported_claims", "unsupported_findings", "overclaims"):
        v = report.get(key)
        if isinstance(v, list):
            for x in v:
                items.append(str(x))
    return items


def _count_gaps(report: dict[str, Any]) -> list[str]:
    items: list[str] = []
    for key in ("evidence_gaps", "telemetry_gaps", "missing_inputs"):
        v = report.get(key)
        if isinstance(v, list):
            for x in v:
                items.append(str(x))
    return items


def _quorum_ok(report: dict[str, Any]) -> Optional[int]:
    """Pull quorum count from report metadata. Returns the number of OK
    members (0..N) or None if the report does not carry it."""
    for key in ("quorum", "swarm_quorum"):
        v = report.get(key)
        if isinstance(v, dict):
            ok = v.get("ok_members")
            if isinstance(ok, int):
                return ok
    meta = report.get("meta") or report.get("metadata") or {}
    if isinstance(meta, dict):
        q = meta.get("quorum")
        if isinstance(q, dict):
            ok = q.get("ok_members")
            if isinstance(ok, int):
                return ok
    return None


def _recommendation_strength(report: dict[str, Any]) -> Optional[str]:
    """Forensic reports often carry a `recommendation` or `verdict` field.
    Returns its string value (lowercased) or None."""
    for key in ("recommendation", "verdict", "conclusion"):
        v = report.get(key)
        if isinstance(v, str):
            return v.lower().strip()
    return None


def _weakest_confidence(confs: list[str]) -> str:
    """Return the weakest confidence label in the list. Missing → UNVERIFIABLE."""
    ranks = {"UNVERIFIABLE": 0, "WEAK": 1, "LIKELY": 2, "PROVEN": 3}
    if not confs:
        return "UNVERIFIABLE"
    return min(confs, key=lambda c: ranks.get(c.upper(), 0))


def approve(
    report: dict[str, Any],
    *,
    cfg: Optional[GovernorConfig] = None,
) -> dict[str, Any]:
    """Evaluate one forensic report payload. Returns a governance dict.

    Pure function — reads the payload, writes nothing. Caller is
    responsible for persisting via `store.write_governance_run`.
    """
    if not isinstance(report, dict):
        raise TypeError("report must be a dict")

    cfg = cfg or GovernorConfig.load()

    report_id = str(report.get("report_id") or "")
    confs = _extract_section_confidences(report)
    n_unverifiable = sum(1 for c in confs if c == "UNVERIFIABLE")
    weakest = _weakest_confidence(confs)

    contradictions = _count_contradictions(report)
    unsupported = _count_unsupported(report)
    gaps = _count_gaps(report)
    quorum_ok = _quorum_ok(report)
    recommendation = _recommendation_strength(report)

    # Hard reject triggers
    hard_flags: list[str] = []
    if n_unverifiable >= cfg.unverifiable_reject_threshold:
        hard_flags.append(
            f"{n_unverifiable} UNVERIFIABLE sections (>= "
            f"{cfg.unverifiable_reject_threshold})"
        )
    if len(contradictions) >= cfg.contradiction_reject_threshold:
        hard_flags.append(
            f"{len(contradictions)} contradictions (>= "
            f"{cfg.contradiction_reject_threshold})"
        )
    if quorum_ok is not None and quorum_ok < cfg.quorum_ok_minimum:
        hard_flags.append(
            f"swarm quorum {quorum_ok}/{cfg.quorum_ok_minimum}"
        )

    # Overclaim: a "BUY"/"STRONG"/"SELL"/"ACT NOW"-style recommendation
    # whose weakest supporting confidence is UNVERIFIABLE or WEAK.
    STRONG_RECS = ("buy", "sell", "strong", "increase", "decrease",
                   "enable", "disable", "raise", "lower", "must",
                   "required", "halt")
    overclaim = False
    if recommendation is not None:
        strong = any(w in recommendation for w in STRONG_RECS)
        if strong and weakest in ("UNVERIFIABLE", "WEAK"):
            overclaim = True
            hard_flags.append(
                f"recommendation '{recommendation[:60]}' overclaims "
                f"(weakest supporting confidence={weakest})"
            )

    # Soft flags (warnings)
    soft_flags: list[str] = []
    if n_unverifiable >= cfg.unverifiable_warn_threshold:
        soft_flags.append(f"{n_unverifiable} UNVERIFIABLE section(s)")
    if len(contradictions) >= cfg.contradiction_warn_threshold:
        soft_flags.append(f"{len(contradictions)} contradiction(s)")
    if unsupported:
        soft_flags.append(f"{len(unsupported)} unsupported-claim flag(s)")
    if gaps:
        soft_flags.append(f"{len(gaps)} telemetry gap(s)")

    # Trust score: start at 1.0, subtract penalties.
    trust = 1.0
    trust -= 0.15 * n_unverifiable
    trust -= 0.20 * len(contradictions)
    trust -= 0.10 * len(unsupported)
    trust -= 0.05 * len(gaps)
    if quorum_ok is not None and quorum_ok < cfg.quorum_ok_minimum:
        trust -= 0.25
    if overclaim:
        trust -= 0.30
    trust = max(0.0, round(trust, 4))

    if hard_flags or trust < cfg.trust_score_min_warnings:
        verdict = VERDICT_REJECTED
    elif soft_flags or trust < cfg.trust_score_min_approved:
        verdict = VERDICT_APPROVED_WARNINGS
    else:
        verdict = VERDICT_APPROVED

    approval_note = _approval_note(
        verdict=verdict, trust=trust,
        hard_flags=hard_flags, soft_flags=soft_flags,
    )

    return {
        "report_id": report_id,
        "verdict": verdict,
        "trust_score": trust,
        "weakest_confidence": weakest,
        "n_unverifiable_sections": n_unverifiable,
        "contradictions": contradictions,
        "unsupported_claims": unsupported,
        "telemetry_gaps": gaps,
        "quorum_ok": quorum_ok,
        "overclaim_detected": overclaim,
        "hard_flags": hard_flags,
        "soft_flags": soft_flags,
        "approval_note": approval_note,
    }


def _approval_note(
    *, verdict: str, trust: float,
    hard_flags: list[str], soft_flags: list[str],
) -> str:
    if verdict == VERDICT_REJECTED:
        parts = ["REJECTED — "]
        if hard_flags:
            parts.append("; ".join(hard_flags))
        else:
            parts.append(f"trust_score={trust:.2f} below warn threshold")
        return "".join(parts)
    if verdict == VERDICT_APPROVED_WARNINGS:
        return f"APPROVED_WITH_WARNINGS (trust={trust:.2f}): " + "; ".join(soft_flags)
    return f"APPROVED (trust={trust:.2f}) — no flags"


# ---------------------------------------------------------------------------
# Meta-review (24h rolling)
# ---------------------------------------------------------------------------

def meta_review(
    governance_rows: Iterable[dict[str, Any]],
    *, cfg: Optional[GovernorConfig] = None,
) -> dict[str, Any]:
    """Scan the last 24h of per-report governance rows for recurring
    defects. `governance_rows` is an iterable of dicts matching the
    `approve()` output schema (typically loaded from
    `store.list_governance_runs(kind='per_report', since_ts_ms=...)`).

    Returns a dict with counts + any defect whose occurrence rate >=
    recurring_defect_fraction.
    """
    cfg = cfg or GovernorConfig.load()
    rows = list(governance_rows)
    total = len(rows)
    if total == 0:
        return {
            "total_reports": 0,
            "recurring_defects": [],
            "approved": 0,
            "approved_with_warnings": 0,
            "rejected": 0,
        }

    approved = sum(1 for r in rows if r.get("verdict") == VERDICT_APPROVED)
    aww = sum(1 for r in rows if r.get("verdict") == VERDICT_APPROVED_WARNINGS)
    rejected = sum(1 for r in rows if r.get("verdict") == VERDICT_REJECTED)

    # Count recurring tokens in hard_flags + soft_flags
    defect_counts: dict[str, int] = {}
    for r in rows:
        for bucket in ("hard_flags", "soft_flags"):
            for flag in (r.get(bucket) or []):
                token = str(flag).split(" ", 2)[0]  # coarse key; "3", "swarm", "recommendation", ...
                # Use the category word, skipping numerics
                if token.isdigit() and " " in str(flag):
                    token = str(flag).split(" ", 2)[1]
                defect_counts[token] = defect_counts.get(token, 0) + 1

    recurring = [
        {"defect": tok, "n": n, "share": round(n / total, 4)}
        for tok, n in defect_counts.items()
        if (n / total) >= cfg.meta_recurring_fraction
    ]
    recurring.sort(key=lambda x: x["n"], reverse=True)

    return {
        "total_reports": total,
        "approved": approved,
        "approved_with_warnings": aww,
        "rejected": rejected,
        "recurring_defects": recurring,
    }
