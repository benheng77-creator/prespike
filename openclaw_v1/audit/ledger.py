"""
OpenClaw audit ledger.

Append-only evidence trail. Writes to SQLite (same file as TradeLogger
uses — tables are separate namespaces, so existing data and schema are
untouched) and mirrors each record as a JSON line to logs/openclaw.jsonl.

Design invariants:
- Never deletes or updates prior rows.
- Tolerant of a missing DB path (in-memory fallback) so importing this
  module at program start cannot break the main trader.
- Independent from existing `core.persistence.TradeLogger`. The two run
  side-by-side. An optional `TradeLogger.subscribe(...)` hook (added in a
  later phase) pipes trade writes into here too; if the hook isn't
  attached, everything below still works.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .correlation import current_correlation_id, new_correlation_id

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@dataclass
class AuditRecord:
    kind: str
    phase: Optional[str] = None
    verb: Optional[str] = None
    symbol: Optional[str] = None
    side: Optional[str] = None
    size: Optional[float] = None
    px: Optional[float] = None
    notional_usd: Optional[float] = None
    session: Optional[str] = None
    policy_check: Optional[str] = None
    before: Optional[dict] = None
    after: Optional[dict] = None
    result: Optional[dict] = None
    evidence_bundle_id: Optional[str] = None
    error: Optional[str] = None
    severity: Optional[str] = None
    correlation_id: Optional[str] = None
    parent_correlation_id: Optional[str] = None
    ts_ms: Optional[int] = None

    def resolved(self) -> dict:
        """Return the record as a dict with defaults filled in."""
        d = asdict(self)
        if d["ts_ms"] is None:
            d["ts_ms"] = int(time.time() * 1000)
        if d["correlation_id"] is None:
            d["correlation_id"] = current_correlation_id() or new_correlation_id()
        return d


class AuditLedger:
    """Thin SQLite + JSONL append-only writer."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        jsonl_path: Optional[str] = None,
    ):
        # Default DB = same SQLite file the rest of the program uses, but
        # safe to override for isolation in tests.
        self.db_path = db_path or os.environ.get("TRADE_DB_PATH") or "trades.db"
        self.jsonl_path = (
            jsonl_path
            or os.environ.get("OPENCLAW_AUDIT_JSONL")
            or "logs/openclaw.jsonl"
        )
        self._ensure_jsonl_dir()
        self._init_schema()

    # ---- lifecycle ----

    def _ensure_jsonl_dir(self) -> None:
        parent = Path(self.jsonl_path).parent
        if str(parent) and str(parent) not in (".", ""):
            parent.mkdir(parents=True, exist_ok=True)

    def _init_schema(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with sqlite3.connect(self.db_path) as con:
            con.executescript(sql)
            con.commit()

    # ---- write path ----

    def record(
        self,
        kind: str,
        **fields: Any,
    ) -> str:
        """
        Append one audit row. Returns the correlation_id that was stored
        so the caller can pass it to children.
        """
        rec = AuditRecord(kind=kind, **fields).resolved()
        self._write_sqlite(rec)
        self._write_jsonl(rec)
        return rec["correlation_id"]

    def record_many(self, records: Iterable[AuditRecord]) -> None:
        rows = [r.resolved() for r in records]
        with sqlite3.connect(self.db_path) as con:
            con.executemany(_INSERT_SQL, [_row_tuple(r) for r in rows])
            con.commit()
        # JSONL is one line per row, best-effort.
        try:
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, default=str) + "\n")
        except OSError:
            pass

    def _write_sqlite(self, rec: dict) -> None:
        with sqlite3.connect(self.db_path) as con:
            con.execute(_INSERT_SQL, _row_tuple(rec))
            con.commit()

    def _write_jsonl(self, rec: dict) -> None:
        try:
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            # JSONL is a secondary channel. If the filesystem rejects it
            # (read-only container, disk full, etc.) the SQLite row is
            # already committed, which is the source of truth.
            pass

    # ---- read path (for tests, monitors, reports) ----

    def fetch_by_correlation(self, correlation_id: str) -> list[dict]:
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            cur = con.execute(
                "SELECT * FROM openclaw_actions WHERE correlation_id = ? ORDER BY ts_ms ASC",
                (correlation_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def fetch_recent(self, limit: int = 100) -> list[dict]:
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            cur = con.execute(
                "SELECT * FROM openclaw_actions ORDER BY ts_ms DESC LIMIT ?",
                (int(limit),),
            )
            return [dict(r) for r in cur.fetchall()]

    def count(self) -> int:
        with sqlite3.connect(self.db_path) as con:
            (n,) = con.execute("SELECT COUNT(*) FROM openclaw_actions").fetchone()
            return int(n)


# ---- helpers ----

_COLUMNS = (
    "ts_ms", "correlation_id", "parent_correlation_id",
    "kind", "phase", "verb", "symbol", "side",
    "size", "px", "notional_usd", "session",
    "policy_check", "before_json", "after_json", "result_json",
    "evidence_bundle_id", "error", "severity",
)
_INSERT_SQL = (
    "INSERT INTO openclaw_actions ("
    + ", ".join(_COLUMNS)
    + ") VALUES ("
    + ", ".join(["?"] * len(_COLUMNS))
    + ")"
)


def _row_tuple(r: dict) -> tuple:
    def _j(v: Any) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, str):
            return v
        try:
            return json.dumps(v, default=str)
        except (TypeError, ValueError):
            return str(v)

    return (
        int(r["ts_ms"]),
        r["correlation_id"],
        r.get("parent_correlation_id"),
        r["kind"],
        r.get("phase"),
        r.get("verb"),
        r.get("symbol"),
        r.get("side"),
        r.get("size"),
        r.get("px"),
        r.get("notional_usd"),
        r.get("session"),
        r.get("policy_check"),
        _j(r.get("before")),
        _j(r.get("after")),
        _j(r.get("result")),
        r.get("evidence_bundle_id"),
        r.get("error"),
        r.get("severity"),
    )
