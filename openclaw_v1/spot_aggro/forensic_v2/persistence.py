"""
spot_forensic_runs persistence helpers.

Thin wrapper around shared.persistence.state — only writes/reads the new
spot_forensic_runs table. Synchronous, single-row upserts.
"""

from __future__ import annotations

import json
import time
from typing import Any

from shared.persistence import state as persist


def upsert_run(
    *,
    report_id: str,
    window_start_ms: int,
    window_end_ms: int,
    n_trades: int,
    overall_verdict: str,
    expectancy_usd: float | None,
    win_rate: float | None,
    profit_factor: float | None,
    friction_ratio: float | None,
    quorum_returned: int,
    quorum_required: int,
    degraded: bool,
    cost_usd_total: float,
    latency_ms_total: int,
    evidence_gap_count: int,
    payload: dict[str, Any],
    pdf_path: str | None = None,
) -> None:
    persist.init_schema()
    con = persist._connect()
    try:
        con.execute(
            "INSERT INTO spot_forensic_runs ("
            " report_id, generated_ts_ms, window_start_ts_ms, window_end_ts_ms,"
            " n_trades_in_window, overall_verdict, expectancy_usd, win_rate,"
            " profit_factor, friction_ratio, quorum_returned, quorum_required,"
            " degraded_confidence, cost_usd_total, latency_ms_total,"
            " evidence_gap_count, payload_json, pdf_path"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(report_id) DO UPDATE SET "
            " generated_ts_ms=excluded.generated_ts_ms,"
            " overall_verdict=excluded.overall_verdict,"
            " expectancy_usd=excluded.expectancy_usd,"
            " win_rate=excluded.win_rate,"
            " profit_factor=excluded.profit_factor,"
            " friction_ratio=excluded.friction_ratio,"
            " quorum_returned=excluded.quorum_returned,"
            " quorum_required=excluded.quorum_required,"
            " degraded_confidence=excluded.degraded_confidence,"
            " cost_usd_total=excluded.cost_usd_total,"
            " latency_ms_total=excluded.latency_ms_total,"
            " evidence_gap_count=excluded.evidence_gap_count,"
            " payload_json=excluded.payload_json,"
            " pdf_path=excluded.pdf_path",
            (
                report_id,
                int(time.time() * 1000),
                int(window_start_ms),
                int(window_end_ms),
                int(n_trades),
                str(overall_verdict),
                expectancy_usd,
                win_rate,
                profit_factor,
                friction_ratio,
                int(quorum_returned),
                int(quorum_required),
                1 if degraded else 0,
                float(cost_usd_total or 0.0),
                int(latency_ms_total or 0),
                int(evidence_gap_count),
                json.dumps(payload, default=str),
                pdf_path,
            ),
        )
        con.commit()
    finally:
        con.close()


def get_run(report_id: str) -> dict | None:
    persist.init_schema()
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT * FROM spot_forensic_runs WHERE report_id = ?",
            (report_id,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d.pop("payload_json"))
    except (TypeError, ValueError):
        d["payload"] = None
    return d


def list_runs(limit: int = 50) -> list[dict]:
    persist.init_schema()
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT report_id, generated_ts_ms, window_start_ts_ms, window_end_ts_ms,"
            "       n_trades_in_window, overall_verdict, expectancy_usd, win_rate,"
            "       profit_factor, friction_ratio, quorum_returned, quorum_required,"
            "       degraded_confidence, cost_usd_total, latency_ms_total,"
            "       evidence_gap_count, pdf_path "
            "FROM spot_forensic_runs ORDER BY generated_ts_ms DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]
