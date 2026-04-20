"""Phase 11n-9-hh — Layer 3 Resilience & Compliance.

Bundles:
  - canary_health()         quick probe of every layer the engine
                            depends on (DB reachable, kill-ladder OK,
                            heartbeat fresh, contradiction freeze off,
                            model registry has rows).
  - degraded_mode_active()  True when any canary check failed; the
                            engine entry path should then fail-closed
                            to limit-only / paper mode.
  - recovery_playbook()     idempotent replay of the last N authz
                            records so downstream logs re-emit with
                            consistent correlation_ids after a restart.
  - export_aml_audit()      AML/MAS-ready audit bundle (immutable
                            ledger + kill ladder events + authz counts)
                            in a single JSON doc.

Never trades, never mutates engine state. All functions fail-open.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any


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


# ---------------------------------------------------------------------------
# Canary
# ---------------------------------------------------------------------------

@dataclass
class CanaryCheck:
    name: str
    ok: bool
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _check_db_reachable() -> CanaryCheck:
    try:
        con = _connect()
        try:
            con.execute("SELECT 1").fetchone()
        finally:
            con.close()
        return CanaryCheck(name="db_reachable", ok=True, detail="ok")
    except Exception as e:
        return CanaryCheck(
            name="db_reachable", ok=False,
            detail=f"db error: {str(e)[:120]}",
        )


def _check_kill_ladder_L0() -> CanaryCheck:
    try:
        from spot_aggro.governance.kill_ladder import current_state
        st = current_state()
        ok = st.level == "L0"
        return CanaryCheck(
            name="kill_ladder_L0", ok=ok,
            detail=f"level={st.level}",
            evidence={"level": st.level, "reason": st.reason},
        )
    except Exception as e:
        return CanaryCheck(
            name="kill_ladder_L0", ok=False,
            detail=f"error: {str(e)[:120]}",
        )


def _check_heartbeat_fresh(max_age_s: int = 180) -> CanaryCheck:
    try:
        from spot_aggro.ops.scheduler.heartbeat_writer import last_tick_ts_ms
        t = last_tick_ts_ms()
        if t is None:
            # Never ticked yet: OK in fresh-boot scenario, treat as warn (False).
            return CanaryCheck(
                name="heartbeat_fresh", ok=False,
                detail="heartbeat not yet ticked",
            )
        age = (int(time.time() * 1000) - t) / 1000.0
        ok = age <= max_age_s
        return CanaryCheck(
            name="heartbeat_fresh", ok=ok,
            detail=f"age={age:.0f}s (max {max_age_s}s)",
            evidence={"age_s": age},
        )
    except Exception as e:
        return CanaryCheck(
            name="heartbeat_fresh", ok=False,
            detail=f"error: {str(e)[:120]}",
        )


def _check_freeze_off() -> CanaryCheck:
    try:
        from spot_aggro.governance.contradiction_freeze import is_entry_frozen
        frozen = bool(is_entry_frozen())
        return CanaryCheck(
            name="freeze_off", ok=(not frozen),
            detail=("entries frozen" if frozen else "not frozen"),
        )
    except Exception as e:
        return CanaryCheck(
            name="freeze_off", ok=False,
            detail=f"error: {str(e)[:120]}",
        )


def _check_model_registry_populated() -> CanaryCheck:
    try:
        from spot_aggro.governance.model_registry import all_models
        n = len(all_models())
        return CanaryCheck(
            name="model_registry_populated", ok=(n >= 3),
            detail=f"{n} registered models",
            evidence={"n_models": n},
        )
    except Exception as e:
        return CanaryCheck(
            name="model_registry_populated", ok=False,
            detail=f"error: {str(e)[:120]}",
        )


def canary_health() -> dict[str, Any]:
    """Run every probe and return an aggregated pass/fail. ok=True only
    when EVERY check passes. Detail includes the failing check name(s)."""
    checks = [
        _check_db_reachable(),
        _check_kill_ladder_L0(),
        _check_heartbeat_fresh(),
        _check_freeze_off(),
        _check_model_registry_populated(),
    ]
    failed = [c.name for c in checks if not c.ok]
    return {
        "ok": not failed,
        "failed": failed,
        "checks": [c.to_dict() for c in checks],
        "ts_ms": int(time.time() * 1000),
    }


def degraded_mode_active() -> bool:
    """Fail-closed: any canary failure = degraded mode. Used by the
    engine entry path to downgrade to limit-only / paper execution."""
    try:
        return not bool(canary_health().get("ok"))
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Recovery playbook
# ---------------------------------------------------------------------------

def recovery_playbook(window_min: int = 60) -> dict[str, Any]:
    """Replay visibility for the last `window_min` minutes of authz
    events. Returns a summary; does NOT re-execute trades. This is a
    post-restart forensic pass — the operator eyeballs it to confirm
    nothing went silent during the outage.

    Idempotent: reading the authz table never mutates it.
    """
    try:
        cutoff = int(time.time() * 1000) - window_min * 60 * 1000
        con = _connect()
        try:
            r_authz = con.execute(
                "SELECT COUNT(*) AS n FROM shadow_variant_authorizations"
                " WHERE ts_ms >= ?", (cutoff,),
            ).fetchone()
            r_exits = con.execute(
                "SELECT COUNT(*) AS n FROM shadow_variant_exits"
                " WHERE ts_ms >= ?", (cutoff,),
            ).fetchone()
            # Recent trade_log rows.
            try:
                r_trades = con.execute(
                    "SELECT COUNT(*) AS n FROM trade_log"
                    " WHERE ts_ms >= ?", (cutoff,),
                ).fetchone()
                n_trades = int(r_trades["n"] or 0)
            except sqlite3.OperationalError:
                n_trades = 0
        finally:
            con.close()
        return {
            "ok": True,
            "window_min": window_min,
            "n_authz_last_window": int(r_authz["n"] or 0),
            "n_exits_last_window": int(r_exits["n"] or 0),
            "n_trades_last_window": n_trades,
            "ts_ms": int(time.time() * 1000),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ---------------------------------------------------------------------------
# AML / MAS audit export
# ---------------------------------------------------------------------------

def export_aml_audit(
    start_ts_ms: int | None = None,
    end_ts_ms: int | None = None,
) -> dict[str, Any]:
    """Single-document AML/MAS audit bundle. Includes:
      - immutable_ledger chain head + verify result
      - ledger rows in [start, end]
      - kill-ladder history in [start, end]
      - retrain queue in [start, end]
      - canary health at export time

    No PII. Amounts are already USD-denominated. Counterparty identity
    is not stored here (exchange-side records); the chain proves the
    bot's *decisions* and *records* weren't tampered with post-hoc.
    """
    try:
        from spot_aggro.governance.immutable_ledger import (
            verify_chain, export_range, head_hash,
        )
        from spot_aggro.governance.retrain_queue import all_tickets
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

    chain = verify_chain()
    rows = export_range(start_ts_ms, end_ts_ms)

    # Kill-ladder history.
    kill_history: list[dict[str, Any]] = []
    try:
        con = _connect()
        try:
            qry = "SELECT * FROM spot_kill_ladder_state"
            args: tuple[Any, ...] = ()
            if start_ts_ms is not None and end_ts_ms is not None:
                qry += " WHERE ts_ms BETWEEN ? AND ?"
                args = (int(start_ts_ms), int(end_ts_ms))
            qry += " ORDER BY id ASC"
            kill_history = [dict(r) for r in con.execute(qry, args).fetchall()]
        finally:
            con.close()
    except Exception:
        kill_history = []

    tickets = [t.to_dict() for t in all_tickets(limit=500)]

    return {
        "ok": True,
        "generated_ts_ms": int(time.time() * 1000),
        "chain_verdict": chain.to_dict(),
        "chain_head": head_hash(),
        "ledger_rows": rows,
        "kill_ladder_history": kill_history,
        "retrain_tickets": tickets,
        "canary_snapshot": canary_health(),
    }
