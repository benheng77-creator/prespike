from __future__ import annotations

from ..summarizer import build_activity_summary


def export_to_cli(report: dict) -> str:
    text = [
        f"=== EOD {report.get('date')} (scope={report.get('scope')}) ===",
        f"Realized PnL: {report.get('realized_pnl_quote')}",
        f"Unrealized PnL: {report.get('unrealized_pnl_quote')}",
        f"Closed trades: {report.get('closed_trades_count')}",
        f"Intents: {(report.get('actions_summary') or {}).get('intents', 0)}",
        build_activity_summary(report),
    ]
    out = "\n".join(text)
    print(out)
    return out
