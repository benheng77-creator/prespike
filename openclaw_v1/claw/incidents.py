"""
Claw incident log — infrastructure events only.

Strictly NOT for bot decision disputes. Use this to record things like
"WS dropped", "DB write failed", "clock skew > 30s", "exchange 5xx".
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping, Optional

from .db import connect, init_claw_schema


VALID_KINDS = ("db", "ws", "rest", "exchange", "clock", "disk", "other")
VALID_SEVERITY = ("info", "warn", "error", "critical")


def open_incident(
    *,
    kind: str,
    severity: str,
    component: Optional[str] = None,
    message: str,
    metadata: Optional[Mapping[str, Any]] = None,
    db_path: Optional[str] = None,
    auto_action: Optional[str] = None,
) -> int:
    """Insert an unresolved incident row. Returns the row id."""
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid incident kind: {kind}")
    if severity not in VALID_SEVERITY:
        raise ValueError(f"invalid severity: {severity}")
    init_claw_schema(db_path)
    con = connect(db_path)
    try:
        cur = con.execute(
            """
            INSERT INTO claw_incidents
                (ts_ms, kind, severity, component, message, auto_action, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1000), kind, severity, component, message,
                auto_action,
                json.dumps(dict(metadata or {}), default=str),
            ),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def resolve_incident(incident_id: int, *, db_path: Optional[str] = None) -> None:
    con = connect(db_path)
    try:
        con.execute(
            "UPDATE claw_incidents SET resolved_ts_ms = ? WHERE id = ?",
            (int(time.time() * 1000), incident_id),
        )
        con.commit()
    finally:
        con.close()


def list_incidents(
    *,
    unresolved_only: bool = False,
    limit: int = 50,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    con = connect(db_path)
    try:
        if unresolved_only:
            rows = con.execute(
                "SELECT * FROM claw_incidents WHERE resolved_ts_ms IS NULL "
                "ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM claw_incidents ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]
