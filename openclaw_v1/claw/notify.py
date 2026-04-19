"""
Claw notify — multi-channel fan-out for Claw infra events.

Supported channels:
    telegram    TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
    whatsapp    CALLMEBOT_API_KEY + CALLMEBOT_PHONE

Design rules (non-interference):
    * Notifications describe infra / audit events, never bot decisions.
    * Failures never raise to the caller — notification is best-effort.
    * Disabled channels are silently skipped; no noise.
    * Rate-limit guard: same exact text within 60s is suppressed per channel.

The env switch CLAW_NOTIFY_CHANNELS can pin channels (e.g. "telegram" or
"telegram,whatsapp"). Default = use every channel that is configured.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterable, Optional


log = logging.getLogger("claw.notify")

SEVERITIES = ("info", "warn", "error", "critical")

_TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
_CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"

# Per-channel dedupe cache {channel: (last_text, ts)}
_recent: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()
_DEDUPE_WINDOW_S = 60.0


@dataclass
class NotifyResult:
    channel: str
    ok: bool
    detail: str = ""


# ---------------------------------------------------------------------------
# Channel inspectors
# ---------------------------------------------------------------------------

def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _telegram_configured() -> bool:
    return bool(_env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"))


def _whatsapp_configured() -> bool:
    return bool(_env("CALLMEBOT_API_KEY") and _env("CALLMEBOT_PHONE"))


def configured_channels() -> list[str]:
    """Return channel names that have all their env vars present."""
    out = []
    if _telegram_configured(): out.append("telegram")
    if _whatsapp_configured(): out.append("whatsapp")
    return out


def _resolve_channels(explicit: Optional[Iterable[str]] = None) -> list[str]:
    if explicit:
        asked = [c.strip().lower() for c in explicit if c and str(c).strip()]
    else:
        pinned = _env("CLAW_NOTIFY_CHANNELS")
        asked = [c.strip().lower() for c in pinned.split(",")] if pinned else configured_channels()
    out = []
    for c in asked:
        if c == "telegram" and _telegram_configured(): out.append(c)
        elif c == "whatsapp" and _whatsapp_configured(): out.append(c)
    return out


# ---------------------------------------------------------------------------
# Transport (urllib — no external deps)
# ---------------------------------------------------------------------------

def _http_post(url: str, payload: dict, timeout: float = 8.0) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(2048).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(2048).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def _http_get(url: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read(2048).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(2048).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Channel senders
# ---------------------------------------------------------------------------

def _format(text: str, severity: str, tag: str) -> str:
    emoji = {"info": "·", "warn": "⚠", "error": "✖", "critical": "🚨"}.get(severity, "·")
    prefix = f"{emoji} [{tag}]" if tag else emoji
    return f"{prefix} {text}"


def send_telegram(
    text: str,
    *,
    severity: str = "info",
    tag: str = "claw",
) -> NotifyResult:
    if not _telegram_configured():
        return NotifyResult("telegram", False, "not_configured")
    body = _format(text, severity, tag)
    if _is_duplicate("telegram", body):
        return NotifyResult("telegram", True, "deduped")
    token = _env("TELEGRAM_BOT_TOKEN")
    chat = _env("TELEGRAM_CHAT_ID")
    code, resp = _http_post(
        _TELEGRAM_URL.format(token=token),
        {"chat_id": chat, "text": body},
    )
    ok = (code == 200 and '"ok":true' in resp)
    if ok:
        _remember("telegram", body)
    return NotifyResult("telegram", ok, f"http {code}" if not ok else "delivered")


def send_whatsapp(
    text: str,
    *,
    severity: str = "info",
    tag: str = "claw",
) -> NotifyResult:
    if not _whatsapp_configured():
        return NotifyResult("whatsapp", False, "not_configured")
    body = _format(text, severity, tag)
    if _is_duplicate("whatsapp", body):
        return NotifyResult("whatsapp", True, "deduped")
    api_key = _env("CALLMEBOT_API_KEY")
    phone = _env("CALLMEBOT_PHONE")
    url = (
        _CALLMEBOT_URL
        + "?phone=" + urllib.parse.quote(phone)
        + "&text=" + urllib.parse.quote(body)
        + "&apikey=" + urllib.parse.quote(api_key)
    )
    code, resp = _http_get(url)
    # CallMeBot returns 200 even on some failures — check body heuristically.
    low = (resp or "").lower()
    if code == 200 and ("sent" in low or "queued" in low or "message" in low):
        ok = "not registered" not in low and "not allowed" not in low and "apikey" not in low.split("apikey: ",1)[-1][:32].lower()
        if ok:
            _remember("whatsapp", body)
        return NotifyResult("whatsapp", ok, "delivered" if ok else "rejected_by_callmebot")
    return NotifyResult("whatsapp", False, f"http {code}")


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

def notify(
    text: str,
    *,
    severity: str = "info",
    tag: str = "claw",
    channels: Optional[Iterable[str]] = None,
) -> list[NotifyResult]:
    """Send to every configured channel. Never raises.

    Returns a list of NotifyResult (one per attempted channel). Channels that
    aren't configured are omitted from the result — they don't count as
    failures.
    """
    if severity not in SEVERITIES:
        severity = "info"
    active = _resolve_channels(channels)
    results: list[NotifyResult] = []
    for ch in active:
        try:
            if ch == "telegram":
                results.append(send_telegram(text, severity=severity, tag=tag))
            elif ch == "whatsapp":
                results.append(send_whatsapp(text, severity=severity, tag=tag))
        except Exception as exc:                        # pragma: no cover - defensive
            log.exception("notify channel %s raised", ch)
            results.append(NotifyResult(ch, False, f"{type(exc).__name__}: {exc}"))
    return results


# ---------------------------------------------------------------------------
# Dedupe helpers
# ---------------------------------------------------------------------------

def _is_duplicate(channel: str, body: str) -> bool:
    with _lock:
        entry = _recent.get(channel)
        if not entry:
            return False
        last_body, last_ts = entry
        return body == last_body and (time.time() - last_ts) < _DEDUPE_WINDOW_S


def _remember(channel: str, body: str) -> None:
    with _lock:
        _recent[channel] = (body, time.time())


def _reset_dedupe_for_tests() -> None:
    with _lock:
        _recent.clear()
