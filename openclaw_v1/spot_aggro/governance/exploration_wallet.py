"""Opportunity Fabric — Sprint 1: Exploration Wallet.

Hard-separates R&D losses from production capital.

Production wallet  = (equity) − (exploration allocation)   funds 'conservative' variants
Exploration wallet = fixed USD allocation                   funds 'exploratory' variants

When the exploration wallet's rolling 24h realized PnL breaches
SPOT_EXPLORATION_DD_KILL_USD, every exploratory variant is disabled
until the operator resets. Production variants keep trading.

Default-OFF: if SPOT_EXPLORATION_WALLET_USD is unset/0, this module is
inert — the existing variant_trip_wire + live_variant_gate stack runs
unchanged. Activation requires an env var + operator restart.

NEVER places orders. NEVER reads balances from the exchange. Pure
governance layer that advises live_variant_gate on which variants are
currently permitted.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Config (all env-driven; default-off)
# ---------------------------------------------------------------------------

def _wallet_usd() -> float:
    try:
        return float(os.environ.get("SPOT_EXPLORATION_WALLET_USD", "0"))
    except (TypeError, ValueError):
        return 0.0


def _dd_kill_usd() -> float:
    try:
        return float(os.environ.get("SPOT_EXPLORATION_DD_KILL_USD", "5"))
    except (TypeError, ValueError):
        return 5.0


def _exploratory_variants() -> tuple[str, ...]:
    raw = os.environ.get("SPOT_EXPLORATION_VARIANTS", "").strip()
    if not raw:
        return ()
    return tuple(v.strip() for v in raw.split(",") if v.strip())


def is_enabled() -> bool:
    """Exploration wallet is active iff operator set SPOT_EXPLORATION_WALLET_USD > 0."""
    return _wallet_usd() > 0 and len(_exploratory_variants()) > 0


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
    con = _connect()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS spot_exploration_wallet_state("
            " id INTEGER PRIMARY KEY CHECK (id = 1),"
            " allocation_usd REAL NOT NULL,"
            " realized_pnl_usd REAL NOT NULL DEFAULT 0.0,"
            " disabled_until_ts_ms INTEGER,"
            " last_updated_ts_ms INTEGER NOT NULL,"
            " last_kill_ts_ms INTEGER,"
            " last_kill_reason TEXT"
            ")"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS spot_exploration_wallet_events("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts_ms INTEGER NOT NULL,"
            " kind TEXT NOT NULL,"        # 'kill' | 'reset' | 'snapshot'
            " allocation_usd REAL,"
            " realized_pnl_usd REAL,"
            " pnl_24h_usd REAL,"
            " rationale TEXT"
            ")"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_expl_events_ts"
            " ON spot_exploration_wallet_events(ts_ms DESC)"
        )
    finally:
        con.close()


# ---------------------------------------------------------------------------
# State dataclasses
# ---------------------------------------------------------------------------

@dataclass
class WalletState:
    enabled: bool
    allocation_usd: float
    realized_pnl_usd: float
    pnl_24h_usd: float
    dd_kill_threshold_usd: float
    disabled: bool
    disabled_until_ts_ms: int | None
    last_kill_reason: str | None
    funded_variants: list[str]
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def _variants_pnl(variants: tuple[str, ...], since_ms: int | None = None) -> tuple[float, float]:
    """Return (all_time_realized_pnl, pnl_since_cutoff) for the given variants.

    Source of truth: spot_live_variant_entries (closed status, realized_pnl_usd set).
    """
    if not variants:
        return 0.0, 0.0
    _init_schema()
    placeholders = ",".join("?" for _ in variants)
    try:
        con = _connect()
        try:
            rows = con.execute(
                f"SELECT realized_pnl_usd, closed_ts_ms"
                f" FROM spot_live_variant_entries"
                f" WHERE variant IN ({placeholders})"
                f"  AND status = 'closed'"
                f"  AND realized_pnl_usd IS NOT NULL",
                tuple(variants),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError:
        return 0.0, 0.0
    all_time = sum(float(r["realized_pnl_usd"] or 0) for r in rows)
    if since_ms is None:
        return round(all_time, 4), round(all_time, 4)
    cutoff = since_ms
    recent = sum(
        float(r["realized_pnl_usd"] or 0) for r in rows
        if (r["closed_ts_ms"] or 0) >= cutoff
    )
    return round(all_time, 4), round(recent, 4)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def state() -> WalletState:
    """Snapshot the exploration wallet's current state. Does not mutate."""
    variants = _exploratory_variants()
    allocation = _wallet_usd()
    dd_kill = _dd_kill_usd()
    now_ms = int(time.time() * 1000)
    cutoff_24h = now_ms - 86_400_000
    all_time, pnl_24h = _variants_pnl(variants, since_ms=cutoff_24h)

    disabled = False
    disabled_until: int | None = None
    last_kill_reason: str | None = None

    if is_enabled():
        _init_schema()
        try:
            con = _connect()
            try:
                row = con.execute(
                    "SELECT disabled_until_ts_ms, last_kill_reason,"
                    "       last_kill_ts_ms"
                    " FROM spot_exploration_wallet_state WHERE id = 1"
                ).fetchone()
            finally:
                con.close()
        except Exception:
            row = None
        if row is not None:
            # Disabled if last_kill_ts_ms is populated and disabled_until
            # has NOT been cleared (reset writes NULL).
            has_kill = row["last_kill_ts_ms"] is not None
            cleared = row["disabled_until_ts_ms"] is None
            if has_kill and not cleared:
                disabled = True
                until_val = row["disabled_until_ts_ms"]
                disabled_until = int(until_val) if until_val and int(until_val) > 0 else None
                last_kill_reason = row["last_kill_reason"]

    return WalletState(
        enabled=is_enabled(),
        allocation_usd=allocation,
        realized_pnl_usd=all_time,
        pnl_24h_usd=pnl_24h,
        dd_kill_threshold_usd=dd_kill,
        disabled=disabled,
        disabled_until_ts_ms=disabled_until,
        last_kill_reason=last_kill_reason,
        funded_variants=list(variants),
        ts_ms=now_ms,
    )


def evaluate() -> dict[str, Any]:
    """Daemon entry point. Checks 24h realized PnL against DD threshold and
    flips the wallet into 'disabled' state if breached. Idempotent."""
    if not is_enabled():
        return {"ok": True, "enabled": False, "action": "noop"}

    _init_schema()
    st = state()
    now_ms = st.ts_ms
    action = "healthy"

    if st.pnl_24h_usd <= -st.dd_kill_threshold_usd and not st.disabled:
        # Kill.
        reason = (
            f"exploration 24h PnL ${st.pnl_24h_usd:+.2f} "
            f"<= -${st.dd_kill_threshold_usd:.2f} → disabling "
            f"{','.join(st.funded_variants)} until operator reset"
        )
        try:
            con = _connect()
            try:
                con.execute(
                    "INSERT OR REPLACE INTO spot_exploration_wallet_state("
                    " id, allocation_usd, realized_pnl_usd,"
                    " disabled_until_ts_ms, last_updated_ts_ms,"
                    " last_kill_ts_ms, last_kill_reason)"
                    " VALUES(1, ?, ?, ?, ?, ?, ?)",
                    (st.allocation_usd, st.realized_pnl_usd, 0,
                     now_ms, now_ms, reason[:240]),
                )
                con.execute(
                    "INSERT INTO spot_exploration_wallet_events("
                    " ts_ms, kind, allocation_usd, realized_pnl_usd,"
                    " pnl_24h_usd, rationale)"
                    " VALUES(?,?,?,?,?,?)",
                    (now_ms, "kill", st.allocation_usd, st.realized_pnl_usd,
                     st.pnl_24h_usd, reason[:240]),
                )
            finally:
                con.close()
        except Exception:
            pass
        action = "killed"
    elif st.pnl_24h_usd > -st.dd_kill_threshold_usd and st.disabled:
        # Do NOT auto-reset. Operator must POST /exploration_wallet/reset.
        action = "disabled_awaiting_operator"

    # Snapshot event every eval for history / panel.
    try:
        con = _connect()
        try:
            con.execute(
                "INSERT INTO spot_exploration_wallet_events("
                " ts_ms, kind, allocation_usd, realized_pnl_usd,"
                " pnl_24h_usd, rationale) VALUES(?,?,?,?,?,?)",
                (now_ms, "snapshot", st.allocation_usd, st.realized_pnl_usd,
                 st.pnl_24h_usd, action),
            )
        finally:
            con.close()
    except Exception:
        pass

    return {
        "ok": True,
        "enabled": True,
        "action": action,
        "state": st.to_dict(),
    }


def reset(operator: str) -> dict[str, Any]:
    """Operator-triggered reset. Clears disabled state, logs event."""
    if not is_enabled():
        return {"ok": False, "error": "exploration wallet not enabled"}
    _init_schema()
    now_ms = int(time.time() * 1000)
    try:
        con = _connect()
        try:
            con.execute(
                "UPDATE spot_exploration_wallet_state"
                " SET disabled_until_ts_ms = NULL,"
                "     last_kill_reason = NULL,"
                "     last_updated_ts_ms = ?"
                " WHERE id = 1",
                (now_ms,),
            )
            con.execute(
                "INSERT INTO spot_exploration_wallet_events("
                " ts_ms, kind, allocation_usd, rationale)"
                " VALUES(?, 'reset', ?, ?)",
                (now_ms, _wallet_usd(),
                 f"operator={operator[:40]} reset after kill")
            )
        finally:
            con.close()
    except Exception as e:
        return {"ok": False, "error": f"reset failed: {str(e)[:200]}"}
    return {"ok": True, "reset_ts_ms": now_ms, "operator": operator}


def filter_variants(raw_enabled: tuple[str, ...]) -> tuple[str, ...]:
    """Entry point for live_variant_gate: strip exploratory variants from
    the enabled set iff the exploration wallet is tripped. Conservative
    variants always pass through."""
    if not is_enabled():
        return raw_enabled
    st = state()
    if not st.disabled:
        return raw_enabled
    exploratory = set(st.funded_variants)
    return tuple(v for v in raw_enabled if v not in exploratory)


def recent_events(limit: int = 30) -> list[dict[str, Any]]:
    _init_schema()
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT ts_ms, kind, allocation_usd, realized_pnl_usd,"
                "       pnl_24h_usd, rationale"
                " FROM spot_exploration_wallet_events"
                " ORDER BY ts_ms DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    return [dict(r) for r in rows]
