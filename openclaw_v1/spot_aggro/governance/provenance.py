"""Opportunity Fabric — Sprint 2: Provenance fingerprints.

Every trade admission carries a cryptographic fingerprint of WHY it
fired. Reduces post-mortem time from hours to seconds: "trade X was
admitted by variant=contrarian at 14:32 with scorer_version=v12,
regime=UNKNOWN, funding_z=-2.3, prefilter=passed, gate_evidence=..."

Schema lives on spot_live_variant_entries.provenance_json (JSON text).
Default-OFF: nothing writes to this column unless opportunity_fabric
feature flag is set, OR the existing admission code opts in via
attach_to_entry().

Pure audit layer. Never changes admission decisions. Never places orders.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any

# Version bumped whenever fingerprint schema changes — lets future queries
# distinguish pre- and post-schema-change trades.
PROVENANCE_SCHEMA_VERSION = "1.0.0"


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


def _ensure_provenance_column() -> None:
    """Idempotent ALTER TABLE to add the provenance_json column on
    spot_live_variant_entries. Skips silently if column already exists
    or the parent table doesn't yet exist."""
    try:
        con = _connect()
        try:
            cols = [
                r["name"] for r in con.execute(
                    "PRAGMA table_info(spot_live_variant_entries)"
                ).fetchall()
            ]
            if not cols:
                return
            if "provenance_json" not in cols:
                con.execute(
                    "ALTER TABLE spot_live_variant_entries"
                    " ADD COLUMN provenance_json TEXT"
                )
        finally:
            con.close()
    except Exception:
        pass


@dataclass
class Provenance:
    """Canonical fingerprint written at admission time."""
    ts_ms: int
    schema_version: str
    variant: str
    symbol: str
    tier: str | None
    # Scorer layer
    scorer_version: str | None
    score_value: float | None
    score_components: dict[str, float] = field(default_factory=dict)
    # Market intelligence
    regime: str | None = None
    regime_confidence: float | None = None
    universe_snapshot_hash: str | None = None
    # Prefilter / gate evidence
    prefilter_verdicts: dict[str, Any] = field(default_factory=dict)
    variant_gate_evidence: dict[str, Any] = field(default_factory=dict)
    # Execution context
    notional_usd: float | None = None
    spread_bp: float | None = None
    depth_usd: float | None = None
    # Governance context
    sign_flip_commit: str | None = None
    server_build: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """sha256 of the canonical JSON form. Stable across serializations."""
        body = json.dumps(
            asdict(self), sort_keys=True, default=str, separators=(",", ":")
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        d = asdict(self)
        d["_fingerprint"] = self.fingerprint()
        return json.dumps(d, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "Provenance":
        d = json.loads(raw)
        d.pop("_fingerprint", None)
        return cls(**d)


# ---------------------------------------------------------------------------
# Collection helpers — best-effort reads of the current system state
# ---------------------------------------------------------------------------

def _current_server_build() -> str | None:
    try:
        from spot_aggro.api.routes import SERVER_BUILD
        return SERVER_BUILD
    except Exception:
        return None


def _current_mio() -> dict[str, Any]:
    try:
        from spot_aggro.research.runner import get_mio
        mio = get_mio()
        return {
            "regime": getattr(mio, "regime", None),
            "regime_confidence": getattr(mio, "regime_confidence", None),
            "top_6": list(getattr(mio, "top_6", []) or []),
        }
    except Exception:
        return {}


def _universe_snapshot_hash() -> str | None:
    """Hash the current top-N universe so we can replay the admission
    environment later. Cheap and stable — h(sorted_symbols + ts_bucket)."""
    try:
        mio = _current_mio()
        symbols = sorted(mio.get("top_6") or [])
        if not symbols:
            return None
        minute_bucket = int(time.time() // 60)
        h = hashlib.sha256(
            (",".join(symbols) + f"|{minute_bucket}").encode("utf-8")
        ).hexdigest()
        return h[:16]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build(
    *,
    variant: str,
    symbol: str,
    tier: str | None = None,
    scorer_version: str | None = None,
    score_value: float | None = None,
    score_components: dict[str, float] | None = None,
    prefilter_verdicts: dict[str, Any] | None = None,
    variant_gate_evidence: dict[str, Any] | None = None,
    notional_usd: float | None = None,
    spread_bp: float | None = None,
    depth_usd: float | None = None,
    extra: dict[str, Any] | None = None,
) -> Provenance:
    """Assemble a Provenance record from the admission context.

    Callers supply variant-specific fields; module fills in global context
    (server build, sign-flip commit, regime, universe hash) automatically.
    """
    mio = _current_mio()
    return Provenance(
        ts_ms=int(time.time() * 1000),
        schema_version=PROVENANCE_SCHEMA_VERSION,
        variant=variant,
        symbol=symbol,
        tier=tier,
        scorer_version=scorer_version,
        score_value=score_value,
        score_components=score_components or {},
        regime=mio.get("regime"),
        regime_confidence=mio.get("regime_confidence"),
        universe_snapshot_hash=_universe_snapshot_hash(),
        prefilter_verdicts=prefilter_verdicts or {},
        variant_gate_evidence=variant_gate_evidence or {},
        notional_usd=notional_usd,
        spread_bp=spread_bp,
        depth_usd=depth_usd,
        sign_flip_commit=os.environ.get("SIGN_FLIP_COMMIT") or None,
        server_build=_current_server_build(),
        extra=extra or {},
    )


def attach_to_entry(entry_id: int, prov: Provenance) -> bool:
    """Write fingerprint to spot_live_variant_entries.provenance_json.

    Returns True on success, False on any error. Idempotent — later
    calls for the same entry_id overwrite earlier values (should not
    happen in normal flow; admission writes once).
    """
    _ensure_provenance_column()
    try:
        con = _connect()
        try:
            con.execute(
                "UPDATE spot_live_variant_entries"
                " SET provenance_json = ?"
                " WHERE id = ?",
                (prov.to_json(), int(entry_id)),
            )
            return True
        finally:
            con.close()
    except Exception:
        return False


def fetch(entry_id: int) -> Provenance | None:
    _ensure_provenance_column()
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT provenance_json FROM spot_live_variant_entries"
                " WHERE id = ?",
                (int(entry_id),),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row or not row["provenance_json"]:
        return None
    try:
        return Provenance.from_json(row["provenance_json"])
    except Exception:
        return None


def fetch_raw(entry_id: int) -> dict[str, Any] | None:
    """Return the raw JSON dict including _fingerprint, for display."""
    _ensure_provenance_column()
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT id, variant, symbol, opened_ts_ms, closed_ts_ms,"
                "       realized_pnl_usd, provenance_json"
                " FROM spot_live_variant_entries WHERE id = ?",
                (int(entry_id),),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row:
        return None
    prov_raw = None
    if row["provenance_json"]:
        try:
            prov_raw = json.loads(row["provenance_json"])
        except Exception:
            prov_raw = None
    return {
        "entry_id": int(row["id"]),
        "variant": row["variant"],
        "symbol": row["symbol"],
        "opened_ts_ms": row["opened_ts_ms"],
        "closed_ts_ms": row["closed_ts_ms"],
        "realized_pnl_usd": row["realized_pnl_usd"],
        "provenance": prov_raw,
    }


def verify(entry_id: int) -> dict[str, Any]:
    """Re-hash the stored provenance and confirm the fingerprint matches.

    Protects against silent DB corruption or tampering.
    """
    prov = fetch(entry_id)
    if prov is None:
        return {"ok": False, "reason": "no provenance recorded"}
    stored_raw = fetch_raw(entry_id) or {}
    stored_prov = (stored_raw.get("provenance") or {})
    stored_fp = stored_prov.get("_fingerprint")
    recomputed = prov.fingerprint()
    return {
        "ok": stored_fp == recomputed,
        "stored_fingerprint": stored_fp,
        "recomputed_fingerprint": recomputed,
    }
