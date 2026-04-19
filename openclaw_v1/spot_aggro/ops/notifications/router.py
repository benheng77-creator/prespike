"""
Notification router — fires on every APEX-Ω action.

Channels (all best-effort, never raise):
    * telegram    via TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
    * whatsapp    via CallMeBot (CALLMEBOT_API_KEY + CALLMEBOT_PHONE)
    * console     always on (stdout)
    * db          every notification logged to apex_notifications table

Event types:
    engine.start, engine.halt, engine.heartbeat,
    pair.enter, pair.exit, pair.reject,
    consensus.fired, consensus.vetoed,
    kill.triggered, kill.cleared,
    pnl.report (every 3h), llm.health_degraded

Notifications are de-duplicated per (event_type, symbol) within a 60s
window so a recovery retry doesn't spam.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from ..persistence import state as persist


log = logging.getLogger("apex.notify")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS apex_notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    event_type      TEXT NOT NULL,
    symbol          TEXT,
    severity        TEXT NOT NULL,       -- info | warn | critical
    title           TEXT NOT NULL,
    body            TEXT,
    channel_tg_ok   INTEGER,
    channel_wa_ok   INTEGER,
    payload_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_apex_notif_ts ON apex_notifications(ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_apex_notif_event ON apex_notifications(event_type, ts_ms DESC);
"""


_dedupe_lock = threading.Lock()
_last_sent: dict[str, int] = {}          # dedupe key -> ts_ms
_DEDUPE_WINDOW_MS = 60_000
_schema_initialized = False


def _init() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    persist.init_schema()
    con = persist._connect()
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()
    _schema_initialized = True


# Eager init at import so read-only endpoints (notifications GET) work before
# any send() has been called.
try:
    _init()
except Exception:
    pass


@dataclass
class NotifyEvent:
    event_type: str
    severity: str                # info | warn | critical
    title: str
    body: str = ""
    symbol: Optional[str] = None
    payload: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send(event: NotifyEvent, *, dedupe_key: Optional[str] = None) -> dict[str, Any]:
    """Send notification across all configured channels. Never raises."""
    _init()
    # Operator-configurable filter + throttle
    try:
        from ..persistence import settings as app_settings
        if not app_settings.notification_allowed(event.event_type):
            return {"sent": False, "reason": "filter_disabled"}
        throttle = app_settings.notification_throttle_s(event.event_type)
    except Exception:
        throttle = 60
    key = dedupe_key or f"{event.event_type}:{event.symbol or '-'}"
    if not _should_send(key, window_ms=max(1000, throttle * 1000)):
        log.debug("notif throttle skip: %s", key)
        # Still persist to DB so the dashboard's notification feed stays complete
        _persist_db(event, False, False)
        return {"sent": False, "reason": "throttle", "throttle_s": throttle}

    tg_ok = _send_telegram(event)
    wa_ok = _send_whatsapp(event)
    _print_console(event)
    _persist_db(event, tg_ok, wa_ok)
    return {"sent": True, "telegram": tg_ok, "whatsapp": wa_ok}


# Convenience helpers — one call per event type
def pair_enter(*, symbol: str, module: str, notional_usd: float, side: str,
               consensus: float, abs_z: float) -> None:
    send(NotifyEvent(
        event_type="pair.enter", severity="info", symbol=symbol,
        title=f"ENTER {symbol} {side}",
        body=(f"module={module}  notional=${notional_usd:,.2f}  "
              f"consensus={consensus:.2f}  z={abs_z:.2f}"),
        payload={"module": module, "side": side, "notional_usd": notional_usd,
                 "consensus": consensus, "abs_z": abs_z},
    ))


def pair_exit(*, symbol: str, module: str, reason: str,
              pnl_usd: Optional[float] = None) -> None:
    send(NotifyEvent(
        event_type="pair.exit", severity="info", symbol=symbol,
        title=f"EXIT {symbol}",
        body=f"module={module}  reason={reason}"
             + (f"  pnl=${pnl_usd:+.2f}" if pnl_usd is not None else ""),
        payload={"module": module, "reason": reason, "pnl_usd": pnl_usd},
    ))


def pair_reject(*, symbol: str, module: str, error: str, leg: str = "") -> None:
    send(NotifyEvent(
        event_type="pair.reject", severity="warn", symbol=symbol,
        title=f"REJECT {symbol}",
        body=f"module={module}  leg={leg}  err={error[:160]}",
        payload={"module": module, "leg": leg, "error": error},
    ))


def consensus_fired(*, symbol: str, consensus: float, conflict: float,
                    vetoed: bool, members_called: int) -> None:
    severity = "warn" if vetoed else "info"
    send(NotifyEvent(
        event_type="consensus.vetoed" if vetoed else "consensus.fired",
        severity=severity, symbol=symbol,
        title=("VETO " if vetoed else "consensus ") + symbol,
        body=(f"consensus={consensus:.2f}  conflict={conflict:.2f}  "
              f"members={members_called}"),
        payload={"consensus": consensus, "conflict": conflict,
                 "vetoed": vetoed, "members": members_called},
    ), dedupe_key=f"consensus:{symbol}:{int(time.time()//60)}")


def kill_triggered(*, reason: str, drawdown_pct: float,
                   equity_usd: float, peak_usd: float) -> None:
    send(NotifyEvent(
        event_type="kill.triggered", severity="critical",
        title=f"🛑 KILL FIRED  dd={drawdown_pct:.2%}",
        body=(f"{reason}\n"
              f"equity=${equity_usd:,.2f}  peak=${peak_usd:,.2f}\n"
              f"Run unlock_after_kill.py to resume."),
        payload={"reason": reason, "drawdown_pct": drawdown_pct,
                 "equity_usd": equity_usd, "peak_usd": peak_usd},
    ))


def kill_cleared(*, operator: str, reason: str) -> None:
    send(NotifyEvent(
        event_type="kill.cleared", severity="info",
        title="kill cleared",
        body=f"operator={operator}  reason={reason}",
        payload={"operator": operator, "reason": reason},
    ))


def engine_start(*, mode: str, pairs_recovered: int, peak_usd: float) -> None:
    send(NotifyEvent(
        event_type="engine.start", severity="info",
        title=f"APEX-Ω started ({mode})",
        body=f"mode={mode}  pairs_recovered={pairs_recovered}  peak=${peak_usd:,.2f}",
    ))


def engine_halt(*, reason: str) -> None:
    send(NotifyEvent(
        event_type="engine.halt", severity="warn",
        title="APEX-Ω halted",
        body=reason,
    ))


def pnl_report(report: dict[str, Any]) -> None:
    """Rich 3-hour PnL summary. Forced through de-dupe (fresh on each schedule)."""
    send(NotifyEvent(
        event_type="pnl.report", severity="info",
        title="APEX-Ω PnL 3h",
        body=_format_pnl_body(report),
        payload=report,
    ), dedupe_key=f"pnl:{int(time.time()//3600)}")


def llm_health(*, provider: str, ok: bool, detail: str) -> None:
    send(NotifyEvent(
        event_type="llm.health_degraded" if not ok else "llm.health_ok",
        severity="warn" if not ok else "info",
        title=f"LLM {provider} {'DEGRADED' if not ok else 'restored'}",
        body=detail,
    ), dedupe_key=f"llm.health:{provider}")


# ---------------------------------------------------------------------------
# Channel senders — each is best-effort and returns bool ok
# ---------------------------------------------------------------------------

def _send_telegram(e: NotifyEvent) -> bool:
    try:
        from ..persistence import settings as app_settings
        if not app_settings.notification_channel_allowed("telegram", e.event_type):
            return False
    except Exception:
        pass
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not tok or not chat:
        return False
    text = _compact(e)
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "Markdown",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return 200 <= r.status < 300
    except Exception as exc:
        log.debug("telegram send failed: %s", exc)
        return False


def _send_whatsapp(e: NotifyEvent) -> bool:
    try:
        from ..persistence import settings as app_settings
        if not app_settings.notification_channel_allowed("whatsapp", e.event_type):
            return False
    except Exception:
        pass
    key = os.environ.get("CALLMEBOT_API_KEY", "").strip()
    phone = os.environ.get("CALLMEBOT_PHONE", "").strip().lstrip("+")
    if not key or not phone:
        return False
    text = _compact(e, max_chars=480)        # WhatsApp gets shorter msgs
    url = (f"https://api.callmebot.com/whatsapp.php"
           f"?phone={phone}&text={urllib.parse.quote(text)}&apikey={key}")
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return 200 <= r.status < 300
    except Exception as exc:
        log.debug("whatsapp send failed: %s", exc)
        return False


def _print_console(e: NotifyEvent) -> None:
    mark = {"info": "·", "warn": "!", "critical": "X"}.get(e.severity, "·")
    try:
        print(f"[{mark}] {e.title}  {e.body}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Persistence + helpers
# ---------------------------------------------------------------------------

def _persist_db(e: NotifyEvent, tg_ok: bool, wa_ok: bool) -> None:
    try:
        con = persist._connect()
        try:
            con.execute(
                """
                INSERT INTO apex_notifications
                    (ts_ms, event_type, symbol, severity, title, body,
                     channel_tg_ok, channel_wa_ok, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (int(time.time()*1000), e.event_type, e.symbol, e.severity,
                 e.title, e.body,
                 1 if tg_ok else 0, 1 if wa_ok else 0,
                 json.dumps(e.payload or {}, default=str)),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        log.exception("notif persist failed")


def _should_send(key: str, *, window_ms: int = _DEDUPE_WINDOW_MS) -> bool:
    now = int(time.time() * 1000)
    with _dedupe_lock:
        last = _last_sent.get(key, 0)
        if now - last < window_ms:
            return False
        _last_sent[key] = now
    return True


def _compact(e: NotifyEvent, *, max_chars: int = 1200) -> str:
    badge = {"info": "·", "warn": "⚠️", "critical": "🛑"}.get(e.severity, "·")
    header = f"{badge} *{e.title}*"
    body = e.body or ""
    if e.symbol and e.symbol not in header:
        header += f"  _{e.symbol}_"
    text = f"{header}\n{body}" if body else header
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


def _format_pnl_body(r: dict[str, Any]) -> str:
    lines = [
        f"equity=${r.get('equity_usd', 0):,.2f}",
        f"peak=${r.get('peak_usd', 0):,.2f}  "
        f"dd={r.get('drawdown_pct', 0)*100:.2f}%",
        f"open_pairs={r.get('open_pairs', 0)}  "
        f"trades_3h={r.get('trades_3h', 0)}  "
        f"enter/exit={r.get('enters_3h', 0)}/{r.get('exits_3h', 0)}",
        f"pnl_3h=${r.get('pnl_3h_usd', 0):+,.2f}  "
        f"fees_3h=${r.get('fees_3h_usd', 0):,.4f}",
        f"llm_calls_3h={r.get('llm_calls_3h', 0)}  "
        f"cost=${r.get('llm_cost_3h_usd', 0):.4f}",
    ]
    return "\n".join(lines)
