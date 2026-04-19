"""Phase 11n-9-k — Alert Center (P3).

Unified alert ingestion, classification, aggregation, correlation, and
acknowledgement. Replaces the scattered alert surfaces (watchdog rows,
orchestrator gaps, pre_trade_gov blocks, research-truth invalids) with
ONE system that:

  1. Ingests events from any source via ingest(kind, severity, source,
     message, evidence, aggregation_key=None, correlation_key=None).
  2. Classifies severity into P0/P1/P2/P3:
       P0 — engine halted, execution failure, adapter down
       P1 — WR anomaly, repeated veto, gov verdict invalid
       P2 — latency spike, stale data, warning-level audit
       P3 — info, trace
  3. AGGREGATES repeats: events with the same aggregation_key in the
     last AGG_WINDOW_S fold into the existing alert (bumps occurrences
     + latest_ts_ms) instead of creating a new row.
  4. CORRELATES related alerts: events sharing a correlation_key
     (e.g. "pre_trade_gov_block:INJ-USDT") group under one incident.
  5. Tracks ACK state: unacknowledged | acknowledged (by whom, when).
     Muted alerts stay in history but drop out of /active.
  6. Exposes /active (unacknowledged + unmuted, sorted P0→P3) and
     /history (all, newest first).

Schema: one row per UNIQUE alert (aggregated). Every occurrence
appended to an append-only occurrences table so the timeline survives
ack/mute.

SPOT AGGRO only. Read-only toward trading state.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, asdict, field
from typing import Any, Optional


log = logging.getLogger(__name__)


AGG_WINDOW_S = 600           # 10 minutes — merge repeats within this window
MAX_ACTIVE = 50              # cap /active to avoid dashboard spam
SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Alert:
    alert_id: str
    kind: str                       # "engine_halt" | "wr_anomaly" | ...
    severity: str                   # "P0" | "P1" | "P2" | "P3"
    source: str                     # originator: "orchestrator" | "watchdog" | ...
    message: str                    # one-line operator-readable
    evidence: dict[str, Any]
    aggregation_key: str            # de-dup bucket
    correlation_key: str            # group bucket
    first_ts_ms: int
    latest_ts_ms: int
    occurrences: int
    ack: bool = False
    ack_by: Optional[str] = None
    ack_at_ms: Optional[int] = None
    muted: bool = False
    mute_until_ms: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_alerts (
    alert_id         TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    severity         TEXT NOT NULL,
    source           TEXT NOT NULL,
    message          TEXT NOT NULL,
    aggregation_key  TEXT NOT NULL,
    correlation_key  TEXT NOT NULL,
    first_ts_ms      INTEGER NOT NULL,
    latest_ts_ms     INTEGER NOT NULL,
    occurrences      INTEGER NOT NULL,
    ack              INTEGER NOT NULL DEFAULT 0,
    ack_by           TEXT,
    ack_at_ms        INTEGER,
    muted            INTEGER NOT NULL DEFAULT 0,
    mute_until_ms    INTEGER,
    evidence_json    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_spot_alerts_agg
    ON spot_alerts(aggregation_key);
CREATE INDEX IF NOT EXISTS idx_spot_alerts_latest
    ON spot_alerts(latest_ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_spot_alerts_correlation
    ON spot_alerts(correlation_key);

CREATE TABLE IF NOT EXISTS spot_alert_occurrences (
    occ_id           TEXT PRIMARY KEY,
    alert_id         TEXT NOT NULL,
    ts_ms            INTEGER NOT NULL,
    message          TEXT NOT NULL,
    evidence_json    TEXT NOT NULL,
    FOREIGN KEY(alert_id) REFERENCES spot_alerts(alert_id)
);
CREATE INDEX IF NOT EXISTS idx_spot_alert_occ_ts
    ON spot_alert_occurrences(alert_id, ts_ms DESC);
"""


def _init_schema() -> None:
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Severity mapping rules (deterministic)
# ---------------------------------------------------------------------------

_P0_KINDS = {
    "engine_halt", "engine_crash", "adapter_down", "db_unreachable",
    "execution_failure", "account_access_denied",
}
_P1_KINDS = {
    "wr_anomaly", "repeated_veto", "research_truth_invalid",
    "card_truth_fail", "decision_truth_invalid",
    "pre_trade_gov_block_repeated", "loop_novelty_stuck",
    "tp_sell_rejected",
}
_P2_KINDS = {
    "latency_spike", "stale_data", "audit_warn",
    "pre_trade_gov_block", "sweep_cap_reached",
    "daily_alpha_no_admits", "gap_detected",
}
# Anything else defaults P3.


def classify_severity(kind: str, explicit: Optional[str] = None) -> str:
    if explicit in SEVERITY_RANK:
        return explicit
    if kind in _P0_KINDS:
        return "P0"
    if kind in _P1_KINDS:
        return "P1"
    if kind in _P2_KINDS:
        return "P2"
    return "P3"


def _default_aggregation_key(kind: str, source: str,
                             evidence: dict[str, Any]) -> str:
    sym = evidence.get("symbol") or ""
    tier = evidence.get("tier") or ""
    # Same (kind, source, symbol, tier) collapses. Excludes message so
    # repeated-with-slight-wording events still aggregate.
    raw = f"{kind}|{source}|{sym}|{tier}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _default_correlation_key(kind: str, evidence: dict[str, Any]) -> str:
    # By default correlate by (kind, symbol) so e.g. every pre_trade_gov
    # block for INJ-USDT groups into one incident.
    sym = evidence.get("symbol") or ""
    return f"{kind}:{sym}" if sym else kind


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ingest(
    *,
    kind: str,
    message: str,
    source: str,
    severity: Optional[str] = None,
    evidence: Optional[dict[str, Any]] = None,
    aggregation_key: Optional[str] = None,
    correlation_key: Optional[str] = None,
) -> Alert:
    """Main entry. Dedupes by aggregation_key within AGG_WINDOW_S,
    otherwise inserts a new alert. Every call also appends an occurrence
    row so the timeline is preserved even if the alert gets muted."""
    _init_schema()
    ev = evidence or {}
    sev = classify_severity(kind, severity)
    agg = aggregation_key or _default_aggregation_key(kind, source, ev)
    corr = correlation_key or _default_correlation_key(kind, ev)
    now_ms = int(time.time() * 1000)
    occ_id = f"occ-{now_ms}-{uuid.uuid4().hex[:8]}"

    from shared.persistence import state as persist
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT alert_id, first_ts_ms, latest_ts_ms, occurrences, "
            " ack, muted FROM spot_alerts WHERE aggregation_key = ?",
            (agg,),
        ).fetchone()
        if row and (now_ms - int(row[2])) <= AGG_WINDOW_S * 1000:
            # Merge into existing alert.
            alert_id = row[0]
            new_occ = int(row[3]) + 1
            con.execute(
                "UPDATE spot_alerts SET latest_ts_ms = ?, occurrences = ?, "
                " message = ?, evidence_json = ? "
                "WHERE alert_id = ?",
                (now_ms, new_occ, message,
                 json.dumps(ev, default=str), alert_id),
            )
            first_ts = int(row[1]); ack_flag = bool(row[4]); muted_flag = bool(row[5])
        else:
            # New alert (either never-seen or window elapsed).
            alert_id = f"al-{now_ms}-{uuid.uuid4().hex[:8]}"
            # If row exists but window elapsed, replace it (INSERT OR
            # REPLACE keyed on unique aggregation_key).
            con.execute(
                "INSERT OR REPLACE INTO spot_alerts "
                "(alert_id, kind, severity, source, message, "
                " aggregation_key, correlation_key, first_ts_ms, "
                " latest_ts_ms, occurrences, ack, muted, evidence_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,0,0,?)",
                (alert_id, kind, sev, source, message,
                 agg, corr, now_ms, now_ms, 1,
                 json.dumps(ev, default=str)),
            )
            first_ts = now_ms; new_occ = 1; ack_flag = False; muted_flag = False
        con.execute(
            "INSERT INTO spot_alert_occurrences "
            "(occ_id, alert_id, ts_ms, message, evidence_json) "
            "VALUES (?,?,?,?,?)",
            (occ_id, alert_id, now_ms, message,
             json.dumps(ev, default=str)),
        )
        con.commit()
    finally:
        con.close()

    alert = Alert(
        alert_id=alert_id, kind=kind, severity=sev, source=source,
        message=message, evidence=ev, aggregation_key=agg,
        correlation_key=corr, first_ts_ms=first_ts, latest_ts_ms=now_ms,
        occurrences=new_occ, ack=ack_flag, muted=muted_flag,
    )
    # Phase 11n-9-l: P0 auto-triggers incident mode. Safe to import
    # lazily — no circular dep, and failure here must never break
    # alert ingestion.
    if sev == "P0":
        try:
            from spot_aggro.governance import incident_mode
            incident_mode.maybe_auto_enter(alert.to_dict())
        except Exception:  # noqa: BLE001
            log.exception("incident_mode.maybe_auto_enter failed")
    return alert


def active(limit: int = MAX_ACTIVE) -> list[dict[str, Any]]:
    """Unacknowledged + unmuted, sorted P0 → P3, newest first within
    each severity."""
    _init_schema()
    from shared.persistence import state as persist
    now_ms = int(time.time() * 1000)
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT alert_id, kind, severity, source, message, "
            " aggregation_key, correlation_key, first_ts_ms, "
            " latest_ts_ms, occurrences, ack, ack_by, ack_at_ms, "
            " muted, mute_until_ms, evidence_json "
            "FROM spot_alerts "
            "WHERE ack = 0 AND (muted = 0 OR mute_until_ms IS NULL "
            "  OR mute_until_ms < ?) "
            "ORDER BY "
            "  CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 "
            "    WHEN 'P2' THEN 2 ELSE 3 END ASC, "
            "  latest_ts_ms DESC "
            "LIMIT ?",
            (now_ms, int(limit)),
        ).fetchall()
    finally:
        con.close()
    return [_row_to_dict(r) for r in rows]


def history(limit: int = 200) -> list[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT alert_id, kind, severity, source, message, "
            " aggregation_key, correlation_key, first_ts_ms, "
            " latest_ts_ms, occurrences, ack, ack_by, ack_at_ms, "
            " muted, mute_until_ms, evidence_json "
            "FROM spot_alerts ORDER BY latest_ts_ms DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        con.close()
    return [_row_to_dict(r) for r in rows]


def ack(alert_id: str, *, actor: str) -> bool:
    _init_schema()
    from shared.persistence import state as persist
    now_ms = int(time.time() * 1000)
    con = persist._connect()
    try:
        cur = con.execute(
            "UPDATE spot_alerts SET ack = 1, ack_by = ?, ack_at_ms = ? "
            "WHERE alert_id = ?",
            (actor, now_ms, alert_id),
        )
        con.commit()
        changed = cur.rowcount > 0
    finally:
        con.close()
    # Phase 11n-9-l: after ack, check if incident mode can auto-exit.
    if changed:
        try:
            from spot_aggro.governance import incident_mode
            incident_mode.maybe_auto_exit()
        except Exception:  # noqa: BLE001
            log.exception("incident_mode.maybe_auto_exit failed")
    return changed


def mute(alert_id: str, *, until_ms: Optional[int] = None,
         actor: str = "ops") -> bool:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        cur = con.execute(
            "UPDATE spot_alerts SET muted = 1, mute_until_ms = ?, "
            " ack_by = ? WHERE alert_id = ?",
            (until_ms, actor, alert_id),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def occurrences_for(alert_id: str, limit: int = 50) -> list[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT occ_id, ts_ms, message, evidence_json "
            "FROM spot_alert_occurrences WHERE alert_id = ? "
            "ORDER BY ts_ms DESC LIMIT ?",
            (alert_id, int(limit)),
        ).fetchall()
    finally:
        con.close()
    return [
        {"occ_id": r[0], "ts_ms": r[1], "message": r[2],
         "evidence": json.loads(r[3]) if r[3] else {}}
        for r in rows
    ]


def correlation_groups(limit: int = 20) -> list[dict[str, Any]]:
    """Return correlation_key → [alerts] groups (2+ alerts only)."""
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT correlation_key, COUNT(*), MAX(severity), "
            "       MAX(latest_ts_ms) "
            "FROM spot_alerts WHERE ack = 0 "
            "GROUP BY correlation_key HAVING COUNT(*) >= 2 "
            "ORDER BY MAX(latest_ts_ms) DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        con.close()
    return [
        {"correlation_key": r[0], "count": int(r[1]),
         "worst_severity": r[2], "latest_ts_ms": r[3]}
        for r in rows
    ]


def _row_to_dict(r: tuple) -> dict[str, Any]:
    d = {
        "alert_id": r[0], "kind": r[1], "severity": r[2],
        "source": r[3], "message": r[4],
        "aggregation_key": r[5], "correlation_key": r[6],
        "first_ts_ms": r[7], "latest_ts_ms": r[8],
        "occurrences": int(r[9]),
        "ack": bool(r[10]), "ack_by": r[11], "ack_at_ms": r[12],
        "muted": bool(r[13]), "mute_until_ms": r[14],
        "evidence": json.loads(r[15]) if r[15] else {},
    }
    # Phase 11n-9-m: attach operator-readable label via label_translator.
    # Non-destructive — raw `kind` stays intact for logs / debugging.
    try:
        from spot_aggro.governance.label_translator import translate
        d["label"] = translate(d["kind"])
    except Exception:  # noqa: BLE001
        d["label"] = d["kind"]
    return d
