"""
Spot-local persistence for the governance layer.

Owns two tables:
    spot_aggro_wri_runs
    spot_aggro_governance_runs

Same additive pattern as Phase 5 `calibration_store` — uses the shared
SQLite handle (`shared.persistence.state._connect`) without editing the
shared schema file.

No edits to `forensic_v2/` or any forensic report row. This module only
stores governance *verdicts* keyed by forensic `report_id`; the underlying
reports remain untouched.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from shared.persistence import state as persist

log = logging.getLogger("spot_aggro.governance.store")


_SCHEMA = """
-- Win-Rate Investigator: one row per cadence run (micro / operational / full).
CREATE TABLE IF NOT EXISTS spot_aggro_wri_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    cadence         TEXT    NOT NULL,           -- micro | operational | full
    window_start_ms INTEGER NOT NULL,
    window_end_ms   INTEGER NOT NULL,
    n_trades        INTEGER NOT NULL,
    win_rate        REAL,                        -- [0,1] or NULL if n=0
    report_json     TEXT    NOT NULL,            -- full WRI payload
    confidence      TEXT    NOT NULL             -- PROVEN|LIKELY|WEAK|UNVERIFIABLE
);
CREATE INDEX IF NOT EXISTS idx_wri_ts ON spot_aggro_wri_runs(ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_wri_cadence ON spot_aggro_wri_runs(cadence, ts_ms DESC);

-- Forensic Governor: one row per governance pass (per-report or meta).
CREATE TABLE IF NOT EXISTS spot_aggro_governance_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    kind            TEXT    NOT NULL,            -- per_report | meta_24h
    report_id       TEXT,                        -- null on meta runs
    verdict         TEXT    NOT NULL,            -- APPROVED|APPROVED_WITH_WARNINGS|REJECTED
    trust_score     REAL    NOT NULL,            -- [0,1]
    findings_json   TEXT    NOT NULL             -- full governance payload
);
CREATE INDEX IF NOT EXISTS idx_gov_ts ON spot_aggro_governance_runs(ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_gov_report ON spot_aggro_governance_runs(report_id);
"""


def init_schema() -> None:
    persist.init_schema()
    con = persist._connect()
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# WRI rows
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WRIRow:
    id: Optional[int]
    ts_ms: int
    cadence: str
    window_start_ms: int
    window_end_ms: int
    n_trades: int
    win_rate: Optional[float]
    report: dict[str, Any]
    confidence: str


def write_wri_run(row: WRIRow) -> int:
    init_schema()
    con = persist._connect()
    try:
        cur = con.execute(
            """
            INSERT INTO spot_aggro_wri_runs
                (ts_ms, cadence, window_start_ms, window_end_ms,
                 n_trades, win_rate, report_json, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.ts_ms, row.cadence,
                row.window_start_ms, row.window_end_ms,
                row.n_trades,
                row.win_rate,
                json.dumps(row.report, default=str),
                row.confidence,
            ),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def last_wri_run(cadence: str) -> Optional[WRIRow]:
    init_schema()
    con = persist._connect()
    try:
        r = con.execute(
            """
            SELECT * FROM spot_aggro_wri_runs WHERE cadence=?
            ORDER BY ts_ms DESC LIMIT 1
            """,
            (cadence,),
        ).fetchone()
    finally:
        con.close()
    if r is None:
        return None
    return WRIRow(
        id=int(r["id"]),
        ts_ms=int(r["ts_ms"]),
        cadence=r["cadence"],
        window_start_ms=int(r["window_start_ms"]),
        window_end_ms=int(r["window_end_ms"]),
        n_trades=int(r["n_trades"]),
        win_rate=(float(r["win_rate"]) if r["win_rate"] is not None else None),
        report=json.loads(r["report_json"]) if r["report_json"] else {},
        confidence=r["confidence"],
    )


# ---------------------------------------------------------------------------
# Governance rows
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GovernanceRow:
    id: Optional[int]
    ts_ms: int
    kind: str                      # per_report | meta_24h
    report_id: Optional[str]
    verdict: str
    trust_score: float
    findings: dict[str, Any]


def write_governance_run(row: GovernanceRow) -> int:
    init_schema()
    con = persist._connect()
    try:
        cur = con.execute(
            """
            INSERT INTO spot_aggro_governance_runs
                (ts_ms, kind, report_id, verdict, trust_score, findings_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row.ts_ms, row.kind, row.report_id,
                row.verdict, row.trust_score,
                json.dumps(row.findings, default=str),
            ),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def get_governance_by_report(report_id: str) -> Optional[GovernanceRow]:
    init_schema()
    con = persist._connect()
    try:
        r = con.execute(
            """
            SELECT * FROM spot_aggro_governance_runs
            WHERE report_id=? AND kind='per_report'
            ORDER BY ts_ms DESC LIMIT 1
            """,
            (report_id,),
        ).fetchone()
    finally:
        con.close()
    if r is None:
        return None
    return GovernanceRow(
        id=int(r["id"]),
        ts_ms=int(r["ts_ms"]),
        kind=r["kind"],
        report_id=r["report_id"],
        verdict=r["verdict"],
        trust_score=float(r["trust_score"]),
        findings=json.loads(r["findings_json"]) if r["findings_json"] else {},
    )


def list_governance_runs(
    *, kind: Optional[str] = None,
    since_ts_ms: Optional[int] = None,
    limit: int = 500,
) -> list[GovernanceRow]:
    init_schema()
    where = []
    args: list[Any] = []
    if kind is not None:
        where.append("kind=?")
        args.append(kind)
    if since_ts_ms is not None:
        where.append("ts_ms >= ?")
        args.append(int(since_ts_ms))
    sql = "SELECT * FROM spot_aggro_governance_runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts_ms DESC LIMIT ?"
    args.append(int(limit))

    con = persist._connect()
    try:
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    out: list[GovernanceRow] = []
    for r in rows:
        out.append(GovernanceRow(
            id=int(r["id"]), ts_ms=int(r["ts_ms"]), kind=r["kind"],
            report_id=r["report_id"], verdict=r["verdict"],
            trust_score=float(r["trust_score"]),
            findings=json.loads(r["findings_json"]) if r["findings_json"] else {},
        ))
    return out


def clear_all() -> None:
    """Testing helper."""
    init_schema()
    con = persist._connect()
    try:
        con.execute("DELETE FROM spot_aggro_wri_runs")
        con.execute("DELETE FROM spot_aggro_governance_runs")
        con.commit()
    finally:
        con.close()
