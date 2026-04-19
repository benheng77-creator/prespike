"""Plain-English activity summary composer."""

from __future__ import annotations

from typing import Any


def build_activity_summary(report: dict) -> str:
    s = report.get("actions_summary") or {}
    lines: list[str] = []
    intents = int(s.get("intents") or 0)
    filled = int(s.get("filled") or 0)
    failed = int(s.get("failed") or 0)
    denied = int(s.get("denied") or 0)
    realized = report.get("realized_pnl_quote", 0)
    closed = report.get("closed_trades_count", 0)
    lines.append(
        f"OpenClaw: {intents} intent(s) today — {filled} filled, {failed} failed, {denied} denied."
    )
    lines.append(f"Realized PnL: {realized}, closed trades: {closed}.")
    contribs = report.get("strategy_contribution") or {}
    if contribs:
        pretty = ", ".join(f"{k}({v.get('trades', 0)})" for k, v in contribs.items())
        lines.append(f"Contribution by session: {pretty}.")
    if report.get("variance_notes"):
        lines.append("Variance: " + "; ".join(report["variance_notes"]))
    return " ".join(lines)
