"""
Claw DB helpers — schema initialisation, connection path.

Claw shares the same SQLite file as the trade logger so the audit trail stays
in one place. Callers may override the path with CLAW_DB_PATH (takes priority)
or TRADE_DB_PATH (shared with core.persistence).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional


_SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def claw_db_path(override: Optional[str] = None) -> str:
    """Resolve the SQLite file Claw should read/write."""
    if override:
        return override
    env = os.environ.get("CLAW_DB_PATH") or os.environ.get("TRADE_DB_PATH")
    if env:
        return env
    return "trades.db"


def init_claw_schema(db_path: Optional[str] = None) -> str:
    """Create Claw's Plane-A tables + triggers if they don't already exist.

    Idempotent. Safe to call on every boot. Returns the resolved db path.
    """
    path = claw_db_path(db_path)
    schema_sql = _SCHEMA_FILE.read_text(encoding="utf-8")
    con = sqlite3.connect(path)
    try:
        con.executescript(schema_sql)
        con.commit()
    finally:
        con.close()
    return path


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open a SQLite connection with row_factory set to Row."""
    con = sqlite3.connect(claw_db_path(db_path))
    con.row_factory = sqlite3.Row
    return con
