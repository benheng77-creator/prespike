"""Phase 11n-9-nn — VS Code / dev-work completion WhatsApp ping.

Sends a CallMeBot WhatsApp message when a dev task completes and the
assistant is awaiting further instruction. Uses the same CALLMEBOT_*
env already set up. Fail-open — notifications are best-effort.

Usage from code:
    from spot_aggro.ops.notifications.dev_ping import ping_task_complete
    ping_task_complete("phase-nn", "5 modules shipped; engine halted")

Usage from CLI:
    python -m spot_aggro.ops.notifications.dev_ping "title" "body"
"""
from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"


@dataclass
class PingResult:
    ok: bool
    status: str
    response: str = ""


def _env(key: str) -> str:
    return (os.environ.get(key) or "").strip()


def _configured() -> bool:
    return bool(_env("CALLMEBOT_API_KEY") and _env("CALLMEBOT_PHONE"))


def ping_task_complete(
    title: str = "task complete",
    body: str = "awaiting further instruction",
    tag: str = "vscode",
) -> PingResult:
    """Send a short CallMeBot WhatsApp message. Returns PingResult.
    Never raises."""
    if not _configured():
        return PingResult(
            ok=False, status="not_configured",
            response="CALLMEBOT_API_KEY or CALLMEBOT_PHONE not set",
        )
    api_key = _env("CALLMEBOT_API_KEY")
    phone = _env("CALLMEBOT_PHONE")
    # Compose message — short + clear for a phone notification.
    msg = f"[{tag}] {title}\n{body}\n— awaiting further instruction"
    url = (
        CALLMEBOT_URL
        + "?phone=" + urllib.parse.quote(phone)
        + "&text=" + urllib.parse.quote(msg[:400])
        + "&apikey=" + urllib.parse.quote(api_key)
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "spot_aggro/devping"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            code = resp.getcode()
            body_resp = resp.read(2000).decode("utf-8", errors="ignore")
    except Exception as e:
        return PingResult(ok=False, status=f"http_error:{type(e).__name__}",
                          response=str(e)[:180])
    low = (body_resp or "").lower()
    ok = (
        code == 200
        and ("message sent" in low or "queued" in low
             or "message to be sent" in low)
    )
    return PingResult(
        ok=ok,
        status="delivered" if ok else f"rejected_by_callmebot:http{code}",
        response=body_resp[:180],
    )


def _main() -> int:
    title = sys.argv[1] if len(sys.argv) >= 2 else "task complete"
    body = sys.argv[2] if len(sys.argv) >= 3 else "awaiting further instruction"
    r = ping_task_complete(title=title, body=body)
    print(f"ok={r.ok} status={r.status} resp={r.response[:120]}")
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(_main())
