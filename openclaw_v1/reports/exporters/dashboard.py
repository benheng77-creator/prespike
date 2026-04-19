"""Dashboard exporter: writes cache/reports/latest.json for web/index.html to pick up."""

from __future__ import annotations

import json
from pathlib import Path

from ..summarizer import build_activity_summary


def export_to_dashboard(report: dict, *, cache_path: str = "cache/reports/latest.json") -> str:
    p = Path(cache_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": report.get("date"),
        "generated_at_ms": report.get("generated_at_ms"),
        "realized_pnl_quote": report.get("realized_pnl_quote"),
        "unrealized_pnl_quote": report.get("unrealized_pnl_quote"),
        "closed_trades_count": report.get("closed_trades_count"),
        "actions_summary": report.get("actions_summary"),
        "strategy_contribution": report.get("strategy_contribution"),
        "variance_notes": report.get("variance_notes", []),
        "summary": build_activity_summary(report),
        "scope": report.get("scope"),
    }
    p.write_text(json.dumps(payload, default=str), encoding="utf-8")
    return str(p)
