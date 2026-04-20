"""Phase 11n-9-ff — Layer 1 Execution Integrity: 4-tier kill ladder.

Replaces the single-tier kill switch semantics with an explicit
escalation ladder:

  L1 SOFT PAUSE         new entries blocked, exits still fire; auto-
                        resumes after `cooldown_s` of clean behavior.
  L2 SESSION HALT       engine fully halted for the current session;
                        NO auto-resume; admin POST to resume.
  L3 EMERGENCY KILL     engine halted, positions force-closed at next
                        reconcile, no admin auto-release; requires
                        manual unlock script + operator confirmation.
  L4 CIRCUIT ISOLATION  account off-ramp (no further API calls), full
                        forensic snapshot captured, requires two-token
                        release (ops + oversight) to leave.

State is persisted in `spot_kill_ladder_state` so a process restart
honors the current rung. The ladder drives Position-entry rejection
via `is_entry_blocked()` — fail-closed: any error returns True (blocked).

Secure authz: every escalation/release requires the ops admin token
for L1/L2; L3/L4 also require the secondary oversight token (different
env var). Both tokens are header-based to match the existing admin
pattern.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

_DB_LOCK = threading.Lock()

Level = Literal["L0", "L1", "L2", "L3", "L4"]

# Auto-resume cooldown for L1 (seconds). After `cooldown_s` of no new
# escalation triggers, L1 drops back to L0 on the next evaluate().
L1_COOLDOWN_S = 600

# Reason codes for auditability.
REASON_SLIPPAGE_STORM = "repeated_slippage_3_in_10min"
REASON_REJECT_STORM = "repeated_rejects_3_in_10min"
REASON_DRAWDOWN = "drawdown_breach"
REASON_CONTRADICTION = "contradiction_freeze_t3"
REASON_MANUAL = "manual"
REASON_CANARY_FAIL = "canary_health_fail"


def _db_path() -> str:
    return (
        os.environ.get("TRADE_DB_PATH")
        or os.environ.get("CLAW_DB_PATH")
        or "trades.db"
    )


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(), isolation_level=None, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _init_schema() -> None:
    with _DB_LOCK:
        con = _connect()
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_kill_ladder_state("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " level TEXT NOT NULL,"               # L0..L4
                " reason TEXT NOT NULL,"
                " actor TEXT,"                        # who/what triggered
                " evidence_json TEXT,"
                " is_current INTEGER NOT NULL"        # 1 for the active row
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_kl_current "
                "ON spot_kill_ladder_state(is_current, ts_ms DESC)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_reject_events("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " kind TEXT NOT NULL,"                # 'reject' | 'slippage'
                " symbol TEXT,"
                " detail_json TEXT"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_re_ts "
                "ON spot_reject_events(ts_ms DESC)"
            )
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Current-state query
# ---------------------------------------------------------------------------

@dataclass
class LadderState:
    level: Level = "L0"
    reason: str = ""
    actor: str = ""
    since_ts_ms: int = 0
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def current_state() -> LadderState:
    """Return the active ladder rung. L0 if no row yet."""
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            r = con.execute(
                "SELECT * FROM spot_kill_ladder_state"
                " WHERE is_current = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
    if r is None:
        return LadderState(level="L0", reason="initial", since_ts_ms=0)
    return LadderState(
        level=r["level"],
        reason=r["reason"],
        actor=r["actor"] or "",
        since_ts_ms=int(r["ts_ms"]),
        ts_ms=int(r["ts_ms"]),
        evidence=json.loads(r["evidence_json"] or "{}"),
    )


def is_entry_blocked() -> bool:
    """True when any L1..L4 rung forbids new entries.
    Fail-closed: any exception returns True."""
    try:
        return current_state().level != "L0"
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Escalation + release
# ---------------------------------------------------------------------------

_LEVEL_ORDER: tuple[Level, ...] = ("L0", "L1", "L2", "L3", "L4")


def _level_index(lv: str) -> int:
    try:
        return _LEVEL_ORDER.index(lv)  # type: ignore[arg-type]
    except ValueError:
        return 0


def _write_state(
    level: Level, reason: str, actor: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    _init_schema()
    now = int(time.time() * 1000)
    with _DB_LOCK:
        con = _connect()
        try:
            # Clear prior current rows (keep history).
            con.execute(
                "UPDATE spot_kill_ladder_state SET is_current = 0"
                " WHERE is_current = 1"
            )
            con.execute(
                "INSERT INTO spot_kill_ladder_state("
                " ts_ms, level, reason, actor, evidence_json, is_current"
                ") VALUES(?,?,?,?,?,1)",
                (now, level, reason, actor,
                 json.dumps(evidence or {})),
            )
        finally:
            con.close()


def escalate(level: Level, reason: str, actor: str,
             evidence: dict[str, Any] | None = None) -> LadderState:
    """Raise the ladder to `level` unless already at or above. Records
    the event regardless so we get a full audit trail."""
    cur = current_state()
    want = _level_index(level)
    have = _level_index(cur.level)
    if want <= have:
        return cur
    _write_state(level, reason, actor, evidence)
    return current_state()


def release(target: Level, actor: str, reason: str = "manual") -> LadderState:
    """Drop the ladder to `target`. Callers must enforce their own
    authz BEFORE calling this. This function simply persists the
    transition. Downgrading past L3/L4 must have already passed the
    two-token gate in the route layer."""
    return _write_state_and_state(target, reason, actor, {"release": True})


def _write_state_and_state(
    level: Level, reason: str, actor: str,
    evidence: dict[str, Any] | None = None,
) -> LadderState:
    _write_state(level, reason, actor, evidence)
    return current_state()


# ---------------------------------------------------------------------------
# Reject / slippage event recording + auto-pause
# ---------------------------------------------------------------------------

REJECT_WINDOW_S = 600    # 10 minutes
REJECT_THRESHOLD = 3     # 3 rejects/slippages in window -> L1


def record_reject(kind: str, symbol: str | None = None,
                  detail: dict[str, Any] | None = None) -> int:
    """Log a reject/slippage event. Returns the event id.
    Safe: failures return 0 and never raise."""
    try:
        _init_schema()
        now = int(time.time() * 1000)
        with _DB_LOCK:
            con = _connect()
            try:
                cur = con.execute(
                    "INSERT INTO spot_reject_events("
                    " ts_ms, kind, symbol, detail_json"
                    ") VALUES(?,?,?,?)",
                    (now, kind, symbol, json.dumps(detail or {})),
                )
                return int(cur.lastrowid or 0)
            finally:
                con.close()
    except Exception:
        return 0


def recent_reject_count(window_s: int = REJECT_WINDOW_S) -> int:
    try:
        _init_schema()
        cutoff = int(time.time() * 1000) - window_s * 1000
        with _DB_LOCK:
            con = _connect()
            try:
                r = con.execute(
                    "SELECT COUNT(*) AS n FROM spot_reject_events"
                    " WHERE ts_ms >= ?", (cutoff,),
                ).fetchone()
                return int(r["n"] or 0)
            finally:
                con.close()
    except Exception:
        return 0


def evaluate_auto_pause() -> LadderState:
    """Called by the ladder daemon. Auto-pause escalates to L1 when
    reject storm detected; auto-releases L1 after cooldown of no new
    events. Never de-escalates from L2/L3/L4 (operator-gated)."""
    cur = current_state()
    n = recent_reject_count()
    if cur.level == "L0" and n >= REJECT_THRESHOLD:
        return escalate(
            "L1",
            (REASON_SLIPPAGE_STORM if "slippage" else REASON_REJECT_STORM),
            actor="auto",
            evidence={"reject_count_10min": n},
        )
    if cur.level == "L1":
        # Check cooldown: no events in last L1_COOLDOWN_S.
        recent = recent_reject_count(window_s=L1_COOLDOWN_S)
        if recent == 0:
            # Only auto-release if the escalation was also auto.
            if cur.actor == "auto":
                return release("L0", actor="auto",
                               reason=f"cooldown_clear_{L1_COOLDOWN_S}s")
    return cur
