"""Phase 11n-9-l — Incident Mode (P4).

When a P0 alert fires, the system enters INCIDENT MODE. This is a
deterministic state machine with three observable effects:

  1. INCIDENT STATE is flipped ACTIVE — /incident/status returns
     {"active": true, "since_ts_ms": ..., "trigger_alert_id": ..., ...}.
  2. A TIMELINE of transitions + diagnostic runs is appended to
     spot_incident_timeline (append-only, never edited).
  3. QUICK DIAGNOSTICS can be invoked on demand (heartbeat, feed,
     exchange, scheduler, db) and each run is stamped into the timeline
     so the post-mortem has full evidence.

Triggers:
  AUTO ENTER: any unacknowledged P0 alert. On each alert_center.ingest()
              call we check — if severity=P0 and current state is
              inactive, we flip active.
  AUTO EXIT:  no unacknowledged P0 alerts remain AND incident has been
              active >= AUTO_EXIT_MIN_DURATION_S (default 60s). The
              minimum-duration guard prevents flapping when a P0 is
              auto-aggregated back-to-back.
  MANUAL:     operator can enter/exit via /incident/enter + /incident/exit
              (admin). Manual transitions are logged with actor="ops".

Read-only toward trading. The incident state never blocks trades — it
just changes UI posture + opens the diagnostics toolbox.

SPOT AGGRO only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, asdict, field
from typing import Any, Optional


log = logging.getLogger(__name__)

AUTO_EXIT_MIN_DURATION_S = 60
DIAGNOSTICS_TIMEOUT_S = 10


@dataclass
class IncidentState:
    active: bool
    since_ts_ms: Optional[int]
    trigger_alert_id: Optional[str]
    trigger_kind: Optional[str]
    actor: Optional[str]               # "auto" | "ops"
    last_transition_ts_ms: Optional[int]
    open_p0_count: int = 0
    open_p1_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiagnosticResult:
    check: str                          # "heartbeat" | "feed" | ...
    ok: bool
    latency_ms: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_incident_state (
    singleton_key   INTEGER PRIMARY KEY CHECK (singleton_key = 1),
    active          INTEGER NOT NULL DEFAULT 0,
    since_ts_ms     INTEGER,
    trigger_alert_id TEXT,
    trigger_kind    TEXT,
    actor           TEXT,
    last_transition_ts_ms INTEGER
);

CREATE TABLE IF NOT EXISTS spot_incident_timeline (
    event_id        TEXT PRIMARY KEY,
    ts_ms           INTEGER NOT NULL,
    kind            TEXT NOT NULL,      -- enter | exit | diagnostic | escalation | ack
    actor           TEXT NOT NULL,
    message         TEXT NOT NULL,
    payload_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_incident_timeline_ts
    ON spot_incident_timeline(ts_ms DESC);
"""


def _init_schema() -> None:
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        con.executescript(_SCHEMA)
        # Ensure singleton row exists.
        con.execute(
            "INSERT OR IGNORE INTO spot_incident_state "
            "(singleton_key, active) VALUES (1, 0)"
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# State access
# ---------------------------------------------------------------------------

def _load_state() -> IncidentState:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT active, since_ts_ms, trigger_alert_id, trigger_kind, "
            " actor, last_transition_ts_ms FROM spot_incident_state "
            "WHERE singleton_key = 1"
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return IncidentState(False, None, None, None, None, None)
    return IncidentState(
        active=bool(row[0]),
        since_ts_ms=row[1],
        trigger_alert_id=row[2],
        trigger_kind=row[3],
        actor=row[4],
        last_transition_ts_ms=row[5],
    )


def _save_state(s: IncidentState) -> None:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        con.execute(
            "UPDATE spot_incident_state SET active = ?, since_ts_ms = ?, "
            " trigger_alert_id = ?, trigger_kind = ?, actor = ?, "
            " last_transition_ts_ms = ? WHERE singleton_key = 1",
            (1 if s.active else 0, s.since_ts_ms, s.trigger_alert_id,
             s.trigger_kind, s.actor, s.last_transition_ts_ms),
        )
        con.commit()
    finally:
        con.close()


def _append_timeline(kind: str, actor: str, message: str,
                     payload: Optional[dict[str, Any]] = None) -> None:
    _init_schema()
    from shared.persistence import state as persist
    now_ms = int(time.time() * 1000)
    event_id = f"ie-{now_ms}-{uuid.uuid4().hex[:6]}"
    con = persist._connect()
    try:
        con.execute(
            "INSERT INTO spot_incident_timeline "
            "(event_id, ts_ms, kind, actor, message, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, now_ms, kind, actor, message,
             json.dumps(payload or {}, default=str)),
        )
        con.commit()
    finally:
        con.close()


def timeline(limit: int = 100) -> list[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT event_id, ts_ms, kind, actor, message, payload_json "
            "FROM spot_incident_timeline ORDER BY ts_ms DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        con.close()
    return [
        {"event_id": r[0], "ts_ms": r[1], "kind": r[2], "actor": r[3],
         "message": r[4],
         "payload": json.loads(r[5]) if r[5] else {}}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

def _count_open(severity: str) -> int:
    try:
        from spot_aggro.governance.alert_center import active as ac_active
        return sum(1 for a in ac_active(limit=200) if a.get("severity") == severity)
    except Exception:  # noqa: BLE001
        return 0


def status() -> dict[str, Any]:
    s = _load_state()
    s.open_p0_count = _count_open("P0")
    s.open_p1_count = _count_open("P1")
    return s.to_dict()


def enter(*, actor: str = "ops",
          trigger_alert_id: Optional[str] = None,
          trigger_kind: Optional[str] = None,
          message: Optional[str] = None) -> dict[str, Any]:
    s = _load_state()
    if s.active:
        return {"ok": True, "changed": False, "state": status()}
    now_ms = int(time.time() * 1000)
    s.active = True
    s.since_ts_ms = now_ms
    s.trigger_alert_id = trigger_alert_id
    s.trigger_kind = trigger_kind
    s.actor = actor
    s.last_transition_ts_ms = now_ms
    _save_state(s)
    _append_timeline(
        kind="enter", actor=actor,
        message=message or (f"incident mode entered (trigger: "
                            f"{trigger_kind or 'manual'})"),
        payload={"trigger_alert_id": trigger_alert_id,
                 "trigger_kind": trigger_kind},
    )
    return {"ok": True, "changed": True, "state": status()}


def exit_(*, actor: str = "ops",
          force: bool = False,
          message: Optional[str] = None) -> dict[str, Any]:
    s = _load_state()
    if not s.active:
        return {"ok": True, "changed": False, "state": status()}
    now_ms = int(time.time() * 1000)
    # Guard auto-exits against flapping: require AUTO_EXIT_MIN_DURATION_S
    # since entry UNLESS forced (manual).
    if not force and s.since_ts_ms is not None:
        elapsed_s = (now_ms - s.since_ts_ms) / 1000.0
        if elapsed_s < AUTO_EXIT_MIN_DURATION_S:
            return {"ok": True, "changed": False,
                    "state": status(),
                    "note": (f"auto-exit blocked: elapsed {elapsed_s:.0f}s "
                             f"< {AUTO_EXIT_MIN_DURATION_S}s min")}
    # Also guard against exit when P0 is still open.
    if not force and _count_open("P0") > 0:
        return {"ok": True, "changed": False,
                "state": status(),
                "note": "auto-exit blocked: P0 alerts still active"}
    s.active = False
    s.trigger_alert_id = None
    s.trigger_kind = None
    s.since_ts_ms = None
    s.actor = actor
    s.last_transition_ts_ms = now_ms
    _save_state(s)
    _append_timeline(
        kind="exit", actor=actor,
        message=message or "incident mode cleared",
        payload={"force": force},
    )
    return {"ok": True, "changed": True, "state": status()}


def maybe_auto_enter(alert: dict[str, Any]) -> None:
    """Called by alert_center.ingest after a successful insert. If the
    alert is P0 and we're not already in incident mode, flip active."""
    try:
        if alert.get("severity") != "P0":
            return
        s = _load_state()
        if s.active:
            return
        enter(
            actor="auto",
            trigger_alert_id=alert.get("alert_id"),
            trigger_kind=alert.get("kind"),
            message=(f"auto-entered on P0 {alert.get('kind')} "
                     f"({alert.get('message','')[:80]})"),
        )
    except Exception:  # noqa: BLE001
        log.exception("maybe_auto_enter failed")


def maybe_auto_exit() -> None:
    """Called periodically (orchestrator tick) to auto-exit when P0
    queue is clear AND minimum duration elapsed."""
    try:
        s = _load_state()
        if not s.active:
            return
        if _count_open("P0") > 0:
            return
        exit_(actor="auto", force=False, message="auto-exit: no open P0 alerts")
    except Exception:  # noqa: BLE001
        log.exception("maybe_auto_exit failed")


# ---------------------------------------------------------------------------
# Quick diagnostics
# ---------------------------------------------------------------------------

def _diag_heartbeat() -> DiagnosticResult:
    """Engine is importable + state reachable."""
    t0 = time.monotonic()
    try:
        from spot_aggro import _engine_instance
        running = _engine_instance is not None
        pos = 0
        if running:
            pos = len(getattr(_engine_instance.state, "positions", {}) or {})
        dur = int((time.monotonic() - t0) * 1000)
        return DiagnosticResult(
            check="heartbeat", ok=running, latency_ms=dur,
            detail=(f"engine running · {pos} positions" if running
                    else "engine not running"),
        )
    except Exception as exc:  # noqa: BLE001
        dur = int((time.monotonic() - t0) * 1000)
        return DiagnosticResult(
            check="heartbeat", ok=False, latency_ms=dur,
            detail=f"heartbeat raised: {exc!s}"[:120],
        )


def _diag_db() -> DiagnosticResult:
    t0 = time.monotonic()
    try:
        from shared.persistence import state as persist
        persist.init_schema()
        con = persist._connect()
        try:
            row = con.execute("SELECT 1").fetchone()
        finally:
            con.close()
        dur = int((time.monotonic() - t0) * 1000)
        ok = row and row[0] == 1
        return DiagnosticResult(
            check="db", ok=bool(ok), latency_ms=dur,
            detail="SELECT 1 -> OK" if ok else "SELECT 1 did not return 1",
        )
    except Exception as exc:  # noqa: BLE001
        dur = int((time.monotonic() - t0) * 1000)
        return DiagnosticResult(
            check="db", ok=False, latency_ms=dur,
            detail=f"db error: {exc!s}"[:120],
        )


def _diag_exchange() -> DiagnosticResult:
    """Adapter can resolve the spot pair + fetch a BTC ticker. Ticker
    price > 0 is the only real proof the exchange is reachable + our
    credentials work."""
    t0 = time.monotonic()
    try:
        from spot_aggro import _engine_instance
        if _engine_instance is None:
            return DiagnosticResult(
                check="exchange", ok=False,
                latency_ms=int((time.monotonic() - t0) * 1000),
                detail="engine not running",
            )
        adapter = _engine_instance._ensure_adapter()
        async def _probe():
            return await adapter.get_spot_ticker("BTC-USDT")
        ticker = asyncio.run(asyncio.wait_for(_probe(), DIAGNOSTICS_TIMEOUT_S))
        dur = int((time.monotonic() - t0) * 1000)
        px = float((ticker or {}).get("last") or 0)
        ok = px > 0
        return DiagnosticResult(
            check="exchange", ok=ok, latency_ms=dur,
            detail=(f"BTC-USDT last={px:.2f}" if ok else "ticker returned 0"),
        )
    except Exception as exc:  # noqa: BLE001
        dur = int((time.monotonic() - t0) * 1000)
        return DiagnosticResult(
            check="exchange", ok=False, latency_ms=dur,
            detail=f"exchange probe failed: {exc!s}"[:120],
        )


def _diag_scheduler() -> DiagnosticResult:
    """Orchestrator thread is alive."""
    t0 = time.monotonic()
    try:
        from spot_aggro.governance import auto_orchestrator as ao
        running = ao.is_running()
        last = ao.last_tick() or ao.latest_tick() or {}
        age_s = None
        if last and last.get("started_ts_ms"):
            age_s = (int(time.time() * 1000) - int(last["started_ts_ms"])) / 1000.0
        dur = int((time.monotonic() - t0) * 1000)
        fresh = age_s is not None and age_s < 600
        ok = running and fresh
        detail = (f"orchestrator running · last tick {age_s:.0f}s ago"
                  if age_s is not None else
                  ("orchestrator running · no tick recorded yet"
                   if running else "orchestrator not running"))
        return DiagnosticResult(
            check="scheduler", ok=ok, latency_ms=dur, detail=detail,
        )
    except Exception as exc:  # noqa: BLE001
        dur = int((time.monotonic() - t0) * 1000)
        return DiagnosticResult(
            check="scheduler", ok=False, latency_ms=dur,
            detail=f"scheduler probe failed: {exc!s}"[:120],
        )


def _diag_feed() -> DiagnosticResult:
    """Research agent produced a fresh report (<= 2h)."""
    t0 = time.monotonic()
    try:
        from spot_aggro.governance.research_agent import latest_report
        r = latest_report() or {}
        ts = int(r.get("generated_ts_ms") or 0)
        age_s = (int(time.time() * 1000) - ts) / 1000.0 if ts else 1e9
        dur = int((time.monotonic() - t0) * 1000)
        ok = age_s < 7200
        return DiagnosticResult(
            check="feed", ok=ok, latency_ms=dur,
            detail=(f"latest research {age_s:.0f}s ago" if ts else
                    "no research report persisted"),
        )
    except Exception as exc:  # noqa: BLE001
        dur = int((time.monotonic() - t0) * 1000)
        return DiagnosticResult(
            check="feed", ok=False, latency_ms=dur,
            detail=f"feed probe failed: {exc!s}"[:120],
        )


def run_diagnostics() -> dict[str, Any]:
    checks = [_diag_heartbeat(), _diag_db(), _diag_exchange(),
              _diag_scheduler(), _diag_feed()]
    results = [c.to_dict() for c in checks]
    _append_timeline(
        kind="diagnostic", actor="ops",
        message=(f"ran {len(results)} diagnostics · "
                 f"{sum(1 for c in checks if c.ok)}/{len(checks)} OK"),
        payload={"results": results},
    )
    return {
        "ok": all(c.ok for c in checks),
        "results": results,
        "checked_at_ms": int(time.time() * 1000),
    }
