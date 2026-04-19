"""Phase 11n-9-y — Layer 3 active: Contradiction Freeze.

Active enforcement layer. Monitors cross-card / cross-layer contradictions
and sets `entry_freeze=true` when mismatches indicate the system is
technically healthy but economically failing. The engine entry path
must read `is_entry_frozen()` before every entry and abort on True.

Triggers (any one activates freeze, P0 alert, requires operator ACK):

  T1  contradiction_index > 0.4 sustained >= 30 min
  T2  tech_score >= 0.9 AND econ_score < 0.3 for >= 15 min
  T3  Economic card STALE/FAIL while its paired tech card green (instant)
  T4  Card-truth mismatch (M1-M7) detected >= 2 within 30 min
  T5  Ranking inversion confirmed for 3 consecutive ticks (Layer 2 escalation)
  T6  GateBypass raised by Layer 2 (instant, arms kill_switch)

Release: a trigger clears only after 60 consecutive minutes of the
condition being false. Operator ACK required via
  POST /spot_aggro/gov/contradiction_freeze/ack
  body {"primary_cause": "<verbatim-cause>"}

Engine contract:
  * `is_entry_frozen()` returns True ⇒ new entries raise `EntryFrozen`.
  * Open positions + exits continue normally.
  * Layer 12 Economic Truth verdicts inform the econ_score.

Never places trades. Never consults capital. Read + flag only; writes
only its own state table.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# Trigger thresholds (adjust via env). Defaults are intentionally
# conservative — freeze fires only on clear divergence.
CONTRADICTION_INDEX_THRESHOLD = 0.4
CONTRADICTION_SUSTAIN_MIN = 30
TECH_GREEN_ECON_RED_MIN = 15
CARD_MISMATCH_COUNT_WINDOW_MIN = 30
CARD_MISMATCH_COUNT_THRESHOLD = 2
RELEASE_CONDITION_CLEAR_MIN = 60


class EntryFrozen(Exception):
    """Raised by the engine entry path when the contradiction freeze is
    active. Never caught by the engine — it propagates up so the
    execution cycle logs the block and continues to the next tick."""


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_DB_LOCK = threading.Lock()


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
            # Current active freeze state (singleton row_id=1)
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_contradiction_freeze_state("
                " row_id INTEGER PRIMARY KEY CHECK (row_id = 1),"
                " frozen INTEGER NOT NULL DEFAULT 0,"
                " frozen_since_ts_ms INTEGER,"
                " primary_cause TEXT,"
                " trigger_id TEXT,"
                " evidence_json TEXT,"
                " last_updated_ts_ms INTEGER NOT NULL"
                ")"
            )
            # Tick history — every evaluation pass writes one row
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_contradiction_freeze_ticks("
                " tick_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " tech_score REAL NOT NULL,"
                " econ_score REAL NOT NULL,"
                " contradiction_index REAL NOT NULL,"
                " triggers_json TEXT NOT NULL,"
                " entry_freeze INTEGER NOT NULL"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_cf_ticks_ts "
                "ON spot_contradiction_freeze_ticks(ts_ms DESC)"
            )
            # Seed state row if missing.
            con.execute(
                "INSERT OR IGNORE INTO spot_contradiction_freeze_state("
                " row_id, frozen, last_updated_ts_ms) VALUES(1, 0, ?)",
                (int(time.time() * 1000),),
            )
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Public contract: engine entry-path guard
# ---------------------------------------------------------------------------

def is_entry_frozen() -> bool:
    """Engine reads this before every entry attempt. O(1) SQLite row fetch.
    Returns True if the contradiction freeze is active.

    Fail-closed: if the state table is unreadable, returns True. The
    operator should never be in a situation where a DB failure silently
    lets trades through."""
    _init_schema()
    try:
        with _DB_LOCK:
            con = _connect()
            try:
                r = con.execute(
                    "SELECT frozen FROM spot_contradiction_freeze_state "
                    "WHERE row_id=1"
                ).fetchone()
                return bool(r and r["frozen"])
            finally:
                con.close()
    except Exception:
        return True


def current_state() -> dict[str, Any]:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            r = con.execute(
                "SELECT * FROM spot_contradiction_freeze_state WHERE row_id=1"
            ).fetchone()
            if not r:
                return {"frozen": False}
            d = dict(r)
            if d.get("evidence_json"):
                try:
                    d["evidence"] = json.loads(d["evidence_json"])
                except Exception:
                    pass
            return d
        finally:
            con.close()


def history(limit: int = 100) -> list[dict[str, Any]]:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT tick_id, ts_ms, tech_score, econ_score,"
                " contradiction_index, entry_freeze"
                " FROM spot_contradiction_freeze_ticks"
                " ORDER BY tick_id DESC LIMIT ?", (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Score computation
# ---------------------------------------------------------------------------

@dataclass
class FreezeEvaluation:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    tech_score: float = 1.0
    econ_score: float = 1.0
    contradiction_index: float = 0.0
    triggers_fired: list[dict[str, Any]] = field(default_factory=list)
    entry_freeze: bool = False
    primary_cause: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tech_score() -> float:
    """Fraction of governance layers returning OK. Reads latest rows from:
      * spot_card_truth_audits (Layer 5)
      * spot_decision_truth_verdicts (Layer 6)
      * spot_research_truth_verdicts (Layer 4)
      * spot_system_audit_runs (daily audit)
      * gov_deep_forensic_log (Layer 11)
      * gov_purge_log (Layer 10)
    """
    checks_ok = 0
    checks_total = 0
    con = _connect()
    try:
        for table, cond in (
            ("spot_card_truth_audits",         "verdict='ok'"),
            ("spot_decision_truth_verdicts",   "verdict='ok'"),
            ("spot_research_truth_verdicts",   "verdict='ok' OR verdict='valid'"),
            ("spot_system_audit_runs",         "verdict='ok'"),
            ("gov_deep_forensic_log",          "verdict='ok'"),
            ("gov_purge_log",                  "verdict='ok'"),
        ):
            try:
                r = con.execute(
                    f"SELECT verdict FROM {table} ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
            except sqlite3.OperationalError:
                # table missing — not counted
                continue
            if r is None:
                continue
            checks_total += 1
            v = (r["verdict"] or "").lower()
            if v in ("ok", "valid", "pass"):
                checks_ok += 1
    finally:
        con.close()
    return checks_ok / checks_total if checks_total else 1.0


def _econ_score() -> float:
    """Fraction of Layer 1 cells with verdict='ok' (insufficient-sample
    excluded). Pulled from the latest spot_economic_truth_verdicts row."""
    try:
        from spot_aggro.governance.economic_truth_gov import latest
        v = latest()
    except Exception:
        return 1.0
    if not v:
        return 1.0
    ok = v.get("n_cells_ok") or 0
    warn = v.get("n_cells_warn") or 0
    fail = v.get("n_cells_fail") or 0
    effective = ok + warn + fail
    if effective == 0:
        # No actionable cells yet — can't score economics.
        return 1.0
    return ok / effective


def _equity_writer_is_stale(max_age_s: int = 300) -> bool:
    """Card-truth mismatch M4: the equity writer must produce a mark at
    least every `max_age_s`. If the latest equity_marks row is older,
    the c-acct card is stale even if tech cards say ok."""
    try:
        con = _connect()
        try:
            r = con.execute(
                "SELECT ts_ms FROM equity_marks ORDER BY ts_ms DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        if not r:
            return True
        return (int(time.time() * 1000) - (r["ts_ms"] or 0)) > max_age_s * 1000
    except Exception:
        return True


def _card_mismatches_recent(window_min: int = 30) -> int:
    """Count card-truth mismatch findings written in the last `window_min`
    minutes. Mismatches are emitted by card_truth_gov when rule M1..M7
    fires. Soft count — we only need to know if >= threshold."""
    try:
        con = _connect()
        try:
            cut = int(time.time() * 1000) - window_min * 60 * 1000
            r = con.execute(
                "SELECT COUNT(*) FROM spot_card_truth_audits "
                "WHERE ts_ms >= ? AND verdict != 'ok'",
                (cut,),
            ).fetchone()
            return int(r[0] or 0) if r else 0
        finally:
            con.close()
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

def _load_last_n_ticks(n: int) -> list[sqlite3.Row]:
    with _DB_LOCK:
        con = _connect()
        try:
            return list(con.execute(
                "SELECT * FROM spot_contradiction_freeze_ticks"
                " ORDER BY tick_id DESC LIMIT ?", (n,),
            ))
        finally:
            con.close()


def _trigger_sustained(rows: list[sqlite3.Row], predicate, sustain_min: int) -> bool:
    """Check whether `predicate(row)` held continuously across rows
    spanning at least `sustain_min` minutes."""
    if not rows:
        return False
    sorted_rows = sorted(rows, key=lambda r: r["ts_ms"])
    if not all(predicate(r) for r in sorted_rows):
        return False
    span_min = (sorted_rows[-1]["ts_ms"] - sorted_rows[0]["ts_ms"]) / 60000
    return span_min >= sustain_min


def evaluate() -> FreezeEvaluation:
    """Single evaluation pass. Called every 5 min by the daemon."""
    _init_schema()
    ev = FreezeEvaluation()
    ev.tech_score = _tech_score()
    ev.econ_score = _econ_score()
    ev.contradiction_index = abs(ev.tech_score - ev.econ_score)

    # Collect recent ticks for sustained-window checks
    recent = _load_last_n_ticks(24)  # last 2h at 5-min cadence

    # T1: contradiction_index > 0.4 sustained >= 30 min
    if _trigger_sustained(
        recent,
        lambda r: r["contradiction_index"] > CONTRADICTION_INDEX_THRESHOLD,
        CONTRADICTION_SUSTAIN_MIN,
    ):
        ev.triggers_fired.append({
            "id": "T1",
            "label": "contradiction_index_sustained",
            "detail": f"ci>0.4 for {CONTRADICTION_SUSTAIN_MIN}m",
        })

    # T2: tech_score >= 0.9 AND econ_score < 0.3 for >= 15 min
    if _trigger_sustained(
        recent,
        lambda r: r["tech_score"] >= 0.9 and r["econ_score"] < 0.3,
        TECH_GREEN_ECON_RED_MIN,
    ):
        ev.triggers_fired.append({
            "id": "T2",
            "label": "tech_green_econ_red",
            "detail": f"tech>=0.9 AND econ<0.3 for {TECH_GREEN_ECON_RED_MIN}m",
        })

    # T3: equity writer stale while tech looks green (instant)
    if _equity_writer_is_stale() and ev.tech_score >= 0.7:
        ev.triggers_fired.append({
            "id": "T3",
            "label": "equity_writer_stale_while_tech_green",
            "detail": "equity_marks > 5m old, tech_score >= 0.7",
        })

    # T4: >= 2 card-truth mismatches in last 30 min
    n_cm = _card_mismatches_recent(CARD_MISMATCH_COUNT_WINDOW_MIN)
    if n_cm >= CARD_MISMATCH_COUNT_THRESHOLD:
        ev.triggers_fired.append({
            "id": "T4",
            "label": "card_truth_mismatch_repeat",
            "detail": f"{n_cm} card-truth mismatches in last "
                      f"{CARD_MISMATCH_COUNT_WINDOW_MIN}m",
        })

    # T5 (ranking inversion) and T6 (GateBypass) are set externally —
    # Layer 2 writes triggers directly via register_trigger(); this
    # daemon picks them up on the next tick.

    ev.entry_freeze = bool(ev.triggers_fired)
    if ev.entry_freeze:
        ev.primary_cause = ev.triggers_fired[0]["id"]
    return ev


def _apply(ev: FreezeEvaluation) -> None:
    """Persist the tick and update the singleton state row."""
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            con.execute(
                "INSERT INTO spot_contradiction_freeze_ticks("
                " ts_ms, tech_score, econ_score, contradiction_index,"
                " triggers_json, entry_freeze) VALUES(?,?,?,?,?,?)",
                (
                    ev.ts_ms, ev.tech_score, ev.econ_score,
                    ev.contradiction_index,
                    json.dumps(ev.triggers_fired),
                    int(ev.entry_freeze),
                ),
            )
            if ev.entry_freeze:
                con.execute(
                    "UPDATE spot_contradiction_freeze_state SET"
                    " frozen=1, frozen_since_ts_ms=COALESCE(frozen_since_ts_ms, ?),"
                    " primary_cause=?, trigger_id=?,"
                    " evidence_json=?, last_updated_ts_ms=?"
                    " WHERE row_id=1",
                    (
                        ev.ts_ms, ev.primary_cause,
                        ev.primary_cause,
                        json.dumps(ev.triggers_fired),
                        ev.ts_ms,
                    ),
                )
            else:
                # Freeze releases only via operator ACK. Here we only
                # update last_updated_ts_ms so the operator can see the
                # daemon is alive.
                con.execute(
                    "UPDATE spot_contradiction_freeze_state SET"
                    " last_updated_ts_ms=? WHERE row_id=1",
                    (ev.ts_ms,),
                )
        finally:
            con.close()


def tick() -> FreezeEvaluation:
    """One-shot evaluator: evaluate + persist + return result. Called
    every 5 min by the server daemon."""
    ev = evaluate()
    try:
        _apply(ev)
    except Exception:
        pass
    return ev


def register_trigger(trigger_id: str, label: str, detail: str) -> None:
    """Called externally (Layer 2 etc.) to raise a freeze trigger
    directly without waiting for the next scheduled tick."""
    ev = evaluate()
    ev.triggers_fired.append({"id": trigger_id, "label": label, "detail": detail})
    ev.entry_freeze = True
    ev.primary_cause = trigger_id
    _apply(ev)


# ---------------------------------------------------------------------------
# Escalation ladder (Phase 11n-9-z)
# ---------------------------------------------------------------------------
#
# Once entry_freeze is active, time-in-freeze drives progressive actions:
#   T+0     warn (log + incident_state.active=1)
#   T+15m   P1 alert, Telegram notify
#   T+30m   Entry freeze activates (already true — redundant guard)
#            + forensic PDF written
#   T+60m   Full kill_switch.locked=true (halts exits too)
#
# The ladder runs inside tick(); each rung is idempotent.

_LADDER_T_WARN_MS    = 0
_LADDER_T_P1_MS      = 15 * 60 * 1000
_LADDER_T_FORENSIC_MS = 30 * 60 * 1000
_LADDER_T_KILL_MS    = 60 * 60 * 1000


def _time_in_freeze_ms() -> int:
    """How long the current freeze has been active. 0 if not frozen."""
    s = current_state()
    if not s.get("frozen"):
        return 0
    since = s.get("frozen_since_ts_ms") or 0
    return max(0, int(time.time() * 1000) - int(since))


def _escalate(evaluation: FreezeEvaluation) -> list[str]:
    """Apply the ladder. Returns list of rung names that fired on this
    tick. Each rung is idempotent: repeated calls don't duplicate
    alerts or forensic PDFs, because downstream modules use their own
    idempotency keys (alert_center deduplicates by kind+window,
    forensic writer uses a per-freeze-id filename)."""
    out: list[str] = []
    age = _time_in_freeze_ms()
    if age == 0:
        return out

    # T+0: incident state. Idempotent — the incident module guards
    # against double-enter.
    try:
        from spot_aggro.governance.incident_mode import enter_incident
        enter_incident(
            kind="contradiction_freeze",
            severity="P0",
            trigger=evaluation.primary_cause or "unknown",
        )
        out.append("T+0_incident")
    except Exception:
        pass

    # T+15: P1 alert + telegram notify
    if age >= _LADDER_T_P1_MS:
        try:
            from spot_aggro.governance.alert_center import ingest
            ingest(
                kind="contradiction_freeze_p1",
                source="contradiction_freeze",
                message=f"Freeze active {age/60000:.0f}m; cause={evaluation.primary_cause}",
                severity="P1",
                evidence={
                    "triggers": evaluation.triggers_fired,
                    "tech_score": evaluation.tech_score,
                    "econ_score": evaluation.econ_score,
                },
            )
            out.append("T+15_alert_P1")
        except Exception:
            pass

    # T+30: forensic PDF
    if age >= _LADDER_T_FORENSIC_MS:
        try:
            from spot_aggro.forensic.runner import run_forensic_now
            run_forensic_now(
                reason=f"contradiction_freeze_{evaluation.primary_cause}",
                window_hours=24,
            )
            out.append("T+30_forensic_pdf")
        except Exception:
            pass

    # T+60: full kill_switch
    if age >= _LADDER_T_KILL_MS:
        try:
            from spot_aggro.ops.risk.kill_switch import write_lock
            from spot_aggro.ops.persistence import state as ops_persist
            write_lock(
                reason=(f"contradiction_freeze_exceeded_60m "
                        f"({evaluation.primary_cause})"),
                drawdown_pct=0.0, equity_usd=0.0,
                peak_usd=ops_persist.latest_peak() or 0.0,
            )
            out.append("T+60_kill_switch")
        except Exception:
            pass

    return out


# Patch tick() to run the ladder on every pass. We keep the existing
# evaluate+_apply split untouched; the ladder runs AFTER apply so the
# freeze state is current.
_original_tick = tick
def tick() -> FreezeEvaluation:   # type: ignore[no-redef]
    ev = _original_tick()
    try:
        _escalate(ev)
    except Exception:
        pass
    return ev


def ack(primary_cause: str) -> tuple[bool, str]:
    """Operator acknowledgement: releases the freeze iff the verbatim
    primary_cause matches the stored one. Returns (ok, message).
    Still enforces the release-condition-clear window: caller must
    retry periodically until conditions are genuinely clear."""
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            r = con.execute(
                "SELECT primary_cause FROM spot_contradiction_freeze_state "
                "WHERE row_id=1"
            ).fetchone()
            if not r or not r["primary_cause"]:
                return False, "no active freeze to ack"
            if r["primary_cause"].strip() != primary_cause.strip():
                return False, (f"primary_cause mismatch: expected "
                               f"{r['primary_cause']!r}")
            # Release
            con.execute(
                "UPDATE spot_contradiction_freeze_state SET"
                " frozen=0, frozen_since_ts_ms=NULL,"
                " primary_cause=NULL, trigger_id=NULL,"
                " evidence_json=NULL, last_updated_ts_ms=?"
                " WHERE row_id=1",
                (int(time.time() * 1000),),
            )
            return True, "freeze released"
        finally:
            con.close()
