"""
Telegram exporter — best-effort.

Gated entirely by env. If TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is
missing, returns {"sent": False, "reason": "not_configured"} without
error. Uses `urllib` from stdlib to avoid a new dependency.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

from ..summarizer import build_activity_summary


def export_to_telegram(report: dict, *, timeout_s: float = 5.0) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return {"sent": False, "reason": "not_configured"}

    text = (
        f"*OpenClaw EOD — {report.get('date')}*\n"
        f"Realized: {report.get('realized_pnl_quote')}\n"
        f"Closed: {report.get('closed_trades_count')}\n"
        f"{build_activity_summary(report)}"
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "Markdown",
    }).encode("utf-8")
    try:
        with urllib.request.urlopen(url, data=data, timeout=timeout_s) as r:
            ok = 200 <= r.getcode() < 300
            body = r.read(200).decode("utf-8", errors="ignore")
        return {"sent": ok, "http_status": r.getcode(), "body_preview": body}
    except Exception as e:
        return {"sent": False, "reason": f"error: {e}"}
