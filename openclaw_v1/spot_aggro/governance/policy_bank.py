"""Opportunity Fabric — Sprint 7: Policy Bank Tiering.

Metadata layer over existing variants. Each variant is tagged with
a policy_tier:

    conservative : proven, production capital, widest caps
    exploratory  : R&D, exploration-wallet-funded, tight caps
    baseline     : mandatory paper-only (never live), for control
                   sample in A/B experiments

Default assignment (hard-coded here; can be overridden via env):
    contrarian     -> exploratory       (anti-momentum thesis, unproven)
    deep_value     -> exploratory       (oversold bounce, unproven)
    mean_reversion -> baseline           (paper control)
    control        -> baseline           (paper control)
    momentum       -> conservative      (shipped as primary live variant)

Promotion workflow:
    exploratory  ->  conservative        (by Sprint 3 SLO + Wilson gate)
    conservative ->  (stays conservative) (no tier above)
    baseline     ->  exploratory          (operator opt-in only)

This module is PURE METADATA — it does not change caps, does not
change admission, does not change scoring. It EXPOSES the tier so
downstream consumers (exploration_wallet, execution_slo, panel UI)
can route capital and SLOs by tier instead of by variant name.

Default-OFF gate: nothing reads this module until a consumer opts in.
The tier assignment itself is always available (info-only).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Any


DEFAULT_TIERS = {
    "contrarian":     "exploratory",
    "deep_value":     "exploratory",
    "mean_reversion": "baseline",
    "control":        "baseline",
    "momentum":       "conservative",
}

VALID_TIERS = {"conservative", "exploratory", "baseline"}


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
    try:
        con = _connect()
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_policy_tier_overrides("
                " variant TEXT PRIMARY KEY,"
                " tier TEXT NOT NULL,"
                " assigned_ts_ms INTEGER NOT NULL,"
                " assigned_by TEXT,"
                " rationale TEXT"
                ")"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_policy_tier_events("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " variant TEXT NOT NULL,"
                " old_tier TEXT,"
                " new_tier TEXT NOT NULL,"
                " actor TEXT,"
                " rationale TEXT"
                ")"
            )
        finally:
            con.close()
    except Exception:
        pass


@dataclass
class VariantTier:
    variant: str
    tier: str
    source: str           # 'default' | 'override' | 'env'
    assigned_ts_ms: int | None = None
    assigned_by: str | None = None
    rationale: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _env_override(variant: str) -> str | None:
    """Allow operator to pin a tier via SPOT_POLICY_TIER_<VARIANT> env."""
    key = f"SPOT_POLICY_TIER_{variant.upper()}"
    val = os.environ.get(key, "").strip().lower()
    if val in VALID_TIERS:
        return val
    return None


def tier_for(variant: str) -> VariantTier:
    """Resolve tier with precedence: env override > db override > default."""
    _init_schema()
    env = _env_override(variant)
    if env:
        return VariantTier(
            variant=variant, tier=env, source="env",
            rationale=f"SPOT_POLICY_TIER_{variant.upper()}={env}",
        )
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT tier, assigned_ts_ms, assigned_by, rationale"
                " FROM spot_policy_tier_overrides WHERE variant = ?",
                (variant,),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        row = None
    if row and row["tier"] in VALID_TIERS:
        return VariantTier(
            variant=variant, tier=row["tier"], source="override",
            assigned_ts_ms=int(row["assigned_ts_ms"]) if row["assigned_ts_ms"] else None,
            assigned_by=row["assigned_by"],
            rationale=row["rationale"],
        )
    default = DEFAULT_TIERS.get(variant, "exploratory")
    return VariantTier(
        variant=variant, tier=default, source="default",
        rationale=f"default for variant {variant}",
    )


def all_assignments() -> list[VariantTier]:
    """Tier for every known variant (DEFAULT_TIERS set)."""
    return [tier_for(v) for v in sorted(DEFAULT_TIERS)]


def assign(variant: str, tier: str, *, actor: str,
           rationale: str) -> dict[str, Any]:
    """Persist an override. Promotion / demotion logged to events table."""
    if tier not in VALID_TIERS:
        return {"ok": False, "error": f"invalid tier {tier!r}"}
    _init_schema()
    now_ms = int(time.time() * 1000)
    current = tier_for(variant)
    old_tier = current.tier
    if old_tier == tier:
        return {"ok": True, "unchanged": True, "tier": tier}
    try:
        con = _connect()
        try:
            con.execute(
                "INSERT OR REPLACE INTO spot_policy_tier_overrides("
                " variant, tier, assigned_ts_ms, assigned_by, rationale)"
                " VALUES(?,?,?,?,?)",
                (variant, tier, now_ms, actor[:40], rationale[:240]),
            )
            con.execute(
                "INSERT INTO spot_policy_tier_events("
                " ts_ms, variant, old_tier, new_tier, actor, rationale)"
                " VALUES(?,?,?,?,?,?)",
                (now_ms, variant, old_tier, tier, actor[:40], rationale[:240]),
            )
        finally:
            con.close()
    except Exception as e:
        return {"ok": False, "error": f"persist failed: {str(e)[:200]}"}
    return {
        "ok": True,
        "variant": variant,
        "old_tier": old_tier,
        "new_tier": tier,
        "actor": actor,
        "rationale": rationale,
    }


def variants_in_tier(tier: str) -> list[str]:
    """All variants currently assigned to the given tier."""
    return [a.variant for a in all_assignments() if a.tier == tier]


def events_recent(limit: int = 30) -> list[dict[str, Any]]:
    _init_schema()
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT ts_ms, variant, old_tier, new_tier, actor, rationale"
                " FROM spot_policy_tier_events"
                " ORDER BY ts_ms DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    return [dict(r) for r in rows]


def summary() -> dict[str, Any]:
    """Panel-ready snapshot: tier buckets + counts."""
    buckets: dict[str, list[str]] = {t: [] for t in VALID_TIERS}
    for a in all_assignments():
        buckets[a.tier].append(a.variant)
    return {
        "ts_ms": int(time.time() * 1000),
        "buckets": buckets,
        "counts": {t: len(v) for t, v in buckets.items()},
        "assignments": [a.to_dict() for a in all_assignments()],
    }
