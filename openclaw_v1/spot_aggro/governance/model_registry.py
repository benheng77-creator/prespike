"""Phase 11n-9-gg — Layer 2 Model Governance: model registry.

Central registry for every scoring/variant model the engine calls. Each
registered model carries:

  - model_id         "control", "contrarian", "mean_reversion" (variant
                     name) OR a parameter-set name for scoring variants.
  - version          semantic string bumped on any scoring-logic change.
  - code_hash        SHA-256 of the scoring source file at registration
                     time. Used to detect silent drift (someone edits
                     scoring.py without bumping version).
  - data_snapshot    hash of the input feature schema (column names) so
                     we know if the feature vector shape changed.
  - registered_ts_ms timestamp the version first appeared in registry.
  - notes            free-form.

Tables
------
spot_model_registry    one row per (model_id, version). Insert-only.
spot_model_observations one row per shadow authz tag — used for audit
                       "what version of this model did we call here?".

Public API
----------
register_model(model_id, version, code_hash, data_snapshot, notes="")
current_version(model_id) -> str      currently tagged version for a model
all_models() -> list[ModelRecord]      full registry
hash_source_file(path) -> str          helper for code_hash
hash_feature_schema(names) -> str      helper for data_snapshot
stamp_authz(model_id, live_authz_id, version=None) -> str
                                        record which version was used
                                        for a specific authz event;
                                        returns the version string.

All functions fail-closed for missing registry rows — any caller
asking for current_version("x") on an unregistered model gets the
literal string "unregistered" so shadow rows are never left blank.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

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
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_model_registry("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " model_id TEXT NOT NULL,"
                " version TEXT NOT NULL,"
                " code_hash TEXT NOT NULL,"
                " data_snapshot TEXT NOT NULL,"
                " registered_ts_ms INTEGER NOT NULL,"
                " notes TEXT,"
                " UNIQUE(model_id, version)"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_mr_model_ts "
                "ON spot_model_registry(model_id, registered_ts_ms DESC)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_model_observations("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " model_id TEXT NOT NULL,"
                " version TEXT NOT NULL,"
                " live_authz_id TEXT,"
                " payload_json TEXT"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_mo_authz "
                "ON spot_model_observations(live_authz_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_mo_model_ts "
                "ON spot_model_observations(model_id, ts_ms DESC)"
            )
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def hash_source_file(path: str | Path) -> str:
    """SHA-256 hex of a file's bytes. Fail-open: missing file -> 'missing'."""
    try:
        b = Path(path).read_bytes()
        return hashlib.sha256(b).hexdigest()
    except Exception:
        return "missing"


def hash_feature_schema(names: list[str] | tuple[str, ...]) -> str:
    """Hash a stable-ordered list of feature names. Used for data_snapshot."""
    canonical = json.dumps(sorted(list(names)), separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass
class ModelRecord:
    model_id: str
    version: str
    code_hash: str
    data_snapshot: str
    registered_ts_ms: int
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def register_model(
    model_id: str, version: str,
    code_hash: str, data_snapshot: str,
    notes: str = "",
) -> ModelRecord:
    """Insert-only: (model_id, version) must be unique. Re-registering the
    same (id, version) is a no-op and returns the existing row."""
    _init_schema()
    now = int(time.time() * 1000)
    with _DB_LOCK:
        con = _connect()
        try:
            con.execute(
                "INSERT OR IGNORE INTO spot_model_registry("
                " model_id, version, code_hash, data_snapshot,"
                " registered_ts_ms, notes"
                ") VALUES(?,?,?,?,?,?)",
                (model_id, version, code_hash, data_snapshot, now, notes),
            )
            r = con.execute(
                "SELECT * FROM spot_model_registry"
                " WHERE model_id = ? AND version = ?",
                (model_id, version),
            ).fetchone()
        finally:
            con.close()
    return ModelRecord(
        model_id=r["model_id"], version=r["version"],
        code_hash=r["code_hash"], data_snapshot=r["data_snapshot"],
        registered_ts_ms=int(r["registered_ts_ms"]),
        notes=r["notes"] or "",
    )


def current_version(model_id: str) -> str:
    """Latest registered version for a model. 'unregistered' if never seen."""
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            r = con.execute(
                "SELECT version FROM spot_model_registry"
                " WHERE model_id = ?"
                " ORDER BY registered_ts_ms DESC LIMIT 1",
                (model_id,),
            ).fetchone()
        finally:
            con.close()
    return r["version"] if r else "unregistered"


def all_models() -> list[ModelRecord]:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT * FROM spot_model_registry"
                " ORDER BY registered_ts_ms DESC"
            ).fetchall()
        finally:
            con.close()
    return [
        ModelRecord(
            model_id=r["model_id"], version=r["version"],
            code_hash=r["code_hash"], data_snapshot=r["data_snapshot"],
            registered_ts_ms=int(r["registered_ts_ms"]),
            notes=r["notes"] or "",
        ) for r in rows
    ]


def stamp_authz(
    model_id: str, live_authz_id: str,
    version: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """Record which model version was invoked for a specific live authz.
    Returns the version string actually recorded."""
    try:
        _init_schema()
        v = version or current_version(model_id)
        now = int(time.time() * 1000)
        with _DB_LOCK:
            con = _connect()
            try:
                con.execute(
                    "INSERT INTO spot_model_observations("
                    " ts_ms, model_id, version, live_authz_id,"
                    " payload_json"
                    ") VALUES(?,?,?,?,?)",
                    (now, model_id, v, live_authz_id,
                     json.dumps(payload or {})),
                )
            finally:
                con.close()
        return v
    except Exception:
        return version or "unregistered"


# ---------------------------------------------------------------------------
# Boot-time self-registration
# ---------------------------------------------------------------------------

# Bump this when scoring.py logic changes materially. Used as the
# initial version for the control model on first server boot.
CONTROL_VERSION = "v2.1-phase11n-9-gg"
CONTRARIAN_VERSION = "v1.0-phase11n-9-ee"
MEAN_REV_VERSION = "v1.0-phase11n-9-ee"
DEEP_VALUE_VERSION = "v1.0-phase11n-9-ii"
MOMENTUM_VERSION = "v1.0-phase11n-9-nn"


def bootstrap_self_register() -> list[ModelRecord]:
    """Called once per process at startup. Registers the three variant
    models with current code hashes so every shadow row has a version
    to stamp. Idempotent."""
    try:
        scoring_path = (
            Path(__file__).resolve().parent.parent / "scoring.py"
        )
        variants_path = (
            Path(__file__).resolve().parent / "strategy_variants.py"
        )
        code_hash_scoring = hash_source_file(scoring_path)
        code_hash_variants = hash_source_file(variants_path)
        # Feature schema used by compute_composite_score.
        feature_schema = hash_feature_schema([
            "spi", "funding_z", "depth_usd", "spread_bp",
            "sigma_30d", "return_24h", "rsi_14", "symbol",
        ])
        out: list[ModelRecord] = []
        out.append(register_model(
            "control", CONTROL_VERSION,
            code_hash_scoring, feature_schema,
            notes="default composite scorer",
        ))
        out.append(register_model(
            "contrarian", CONTRARIAN_VERSION,
            code_hash_variants, feature_schema,
            notes="bottom-quartile composite + liquid",
        ))
        out.append(register_model(
            "mean_reversion", MEAN_REV_VERSION,
            code_hash_variants, feature_schema,
            notes="oversold-bounce 5-filter",
        ))
        out.append(register_model(
            "deep_value", DEEP_VALUE_VERSION,
            code_hash_variants, feature_schema,
            notes="WR>=45% + undervalued 7d-drawdown + liquid",
        ))
        out.append(register_model(
            "momentum", MOMENTUM_VERSION,
            code_hash_variants, feature_schema,
            notes="counter-hypothesis to contrarian: +3% 24h + funding>0 + vol>1.5x + liquid",
        ))
        return out
    except Exception:
        return []
