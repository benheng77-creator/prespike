"""Pre-publish report validation."""

from __future__ import annotations

from typing import Any


REQUIRED = {
    "date", "generated_at_ms", "scope",
    "realized_pnl_quote", "closed_trades_count",
    "actions_summary",
}


def validate_report(report: dict) -> tuple[str, list[str]]:
    """Returns (status, notes). status ∈ {ok, needs_review, failed}."""
    notes: list[str] = []
    missing = REQUIRED - set(report.keys())
    if missing:
        return "failed", [f"missing: {sorted(missing)}"]
    if report.get("variance_notes"):
        notes += list(report["variance_notes"])
        return "needs_review", notes
    return "ok", notes
