"""Phase 11n-9-d — Reconciled Sweeper.

Audits every reconciled / low-conf position the engine is holding, and
produces a per-position PLAN: KEEP, SELL, or LINK. Reconciled positions
today sit untouched (no TP/SL, no live management) — this module
brings them under spot_aggro governance.

Decision table (advisory; real action gated by SPOT_RECON_SWEEP_EXECUTE):

  SELL when:
    - live_ret ≤ -15% (stop big losers)
    - live_ret ≥ +15% (lock in big winners)
    - dust (value < SPOT_RECON_DUST_USD, default $1) AND age > 24h
    - very stale (age ≥ 72h) AND |live_ret| < 2% (position isn't moving)

  LINK when:
    - live_ret ≥ +3% AND age < 24h
      → upgrade to an engine-tracked position with synthetic TP/SL
        (entry = current price - 1%, giving a reasonable stop; tp
        follows current tier config). From that point the engine's
        normal exit loop manages it.

  KEEP otherwise — no action; revisit next tick.

Every decision goes through pre_trade_gov via source="recon_sweep" so
the universal Layer 8 log captures each sell before it ships. The
safety-exit bypass does NOT apply (this is a deliberate action, not a
TP/SL fire); the governor must approve.

SPOT AGGRO only. Default DRY RUN — set SPOT_RECON_SWEEP_EXECUTE=1 to
actually place sells. Never touches non-reconciled positions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, asdict, field
from typing import Any, Optional


log = logging.getLogger(__name__)

# Thresholds — tunable via env.
BIG_LOSS_RET = -0.15
BIG_WIN_RET  = 0.15
LINK_WIN_RET = 0.03
DUST_USD = 1.0
STALE_HOURS = 72.0
LINK_AGE_MAX_H = 24.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def is_execute_enabled() -> bool:
    """Phase 11n-9-e: DEFAULT ON. Set SPOT_RECON_SWEEP_EXECUTE=0 to
    disable (or TRADE_DRY_RUN=1 or SPOT_DRY_RUN=1 for safety-first
    environments). The sweeper is otherwise fully auto: every
    orchestrator tick builds + executes the plan; sells ship via the
    OKX adapter; links upgrade the in-memory position's module.

    A sell is still guarded by Layer 8 pre_trade_gov which re-runs the
    full 12-item checklist at the moment of execution. If the governor
    blocks a sell, it stays in plan output as "BLOCKED" with the
    rejection reason and no order ships.
    """
    if os.environ.get("TRADE_DRY_RUN", "0").strip() == "1":
        return False
    if os.environ.get("SPOT_DRY_RUN", "0").strip() == "1":
        return False
    return os.environ.get("SPOT_RECON_SWEEP_EXECUTE", "1").strip() == "1"


@dataclass
class SweepAction:
    symbol: str
    action: str              # "sell" | "link" | "keep"
    reason: str
    value_usd: float
    entry_price: float
    live_price: Optional[float]
    live_ret: Optional[float]
    age_h: float
    module: str
    executed: bool = False   # True when SPOT_RECON_SWEEP_EXECUTE=1 and ship succeeded
    authz_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SweepPlan:
    plan_id: str
    generated_ts_ms: int
    dry_run: bool
    actions: list[SweepAction]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["actions"] = [a.to_dict() if hasattr(a, "to_dict") else dict(a)
                        for a in self.actions]
        return d


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def _decide(
    value_usd: float, live_ret: Optional[float], age_h: float,
) -> tuple[str, str]:
    big_loss = _env_float("SPOT_RECON_BIG_LOSS_RET", BIG_LOSS_RET)
    big_win  = _env_float("SPOT_RECON_BIG_WIN_RET",  BIG_WIN_RET)
    link_win = _env_float("SPOT_RECON_LINK_WIN_RET", LINK_WIN_RET)
    dust_usd = _env_float("SPOT_RECON_DUST_USD",     DUST_USD)
    stale_h  = _env_float("SPOT_RECON_STALE_HOURS",  STALE_HOURS)
    link_max = _env_float("SPOT_RECON_LINK_AGE_MAX_H", LINK_AGE_MAX_H)

    if live_ret is not None and live_ret <= big_loss:
        return ("sell",
                f"live loss {live_ret*100:.1f}% ≤ {big_loss*100:.1f}% — stop loss")
    if live_ret is not None and live_ret >= big_win:
        return ("sell",
                f"live gain {live_ret*100:.1f}% ≥ {big_win*100:.1f}% — take profit")
    if value_usd < dust_usd:
        # OKX rejects spot sells under ~$1 min-notional. Don't propose
        # a sell that's going to fail every tick — tag it as
        # non-sellable dust so the operator sees the real blocker.
        return ("dust_unsellable",
                f"non-sellable dust ${value_usd:.2f} < ${dust_usd:.2f} · "
                "OKX min-notional — use Small Balance Convert on OKX UI")
    if age_h >= stale_h and live_ret is not None and abs(live_ret) < 0.02:
        return ("sell",
                f"stale {age_h:.1f}h ≥ {stale_h:.0f}h · |ret| < 2% — dead position")
    if (live_ret is not None and live_ret >= link_win and age_h <= link_max):
        return ("link",
                f"live gain {live_ret*100:.1f}% ≥ {link_win*100:.1f}% · "
                f"age {age_h:.1f}h ≤ {link_max:.0f}h — link into spot_aggro governance")
    return ("keep",
            f"no trigger met · ret={None if live_ret is None else f'{live_ret*100:.1f}%'} "
            f"· age {age_h:.1f}h · value ${value_usd:.2f}")


# ---------------------------------------------------------------------------
# Plan build + execute
# ---------------------------------------------------------------------------

def build_plan() -> SweepPlan:
    """Inspect every engine reconciled position; produce per-symbol plan.
    Read-only in terms of engine state."""
    ts_ms = int(time.time() * 1000)
    plan_id = f"sweep-{ts_ms}-{uuid.uuid4().hex[:6]}"
    actions: list[SweepAction] = []

    try:
        from spot_aggro import _engine_instance
    except Exception:  # noqa: BLE001
        _engine_instance = None

    if _engine_instance is None:
        return SweepPlan(
            plan_id=plan_id, generated_ts_ms=ts_ms,
            dry_run=not is_execute_enabled(),
            actions=[],
            summary="engine not running — nothing to sweep",
        )

    positions = dict(getattr(_engine_instance.state, "positions", {}))
    # Fetch live prices for the whole set once (3s-cached helper).
    reconciled_syms = [
        s for s, p in positions.items()
        if getattr(p, "module", "").startswith("M_reconciled")
    ]
    try:
        live_prices = _engine_instance._snapshot_live_prices(reconciled_syms)
    except Exception:  # noqa: BLE001
        live_prices = {}

    for sym, p in positions.items():
        module = getattr(p, "module", "") or ""
        if not module.startswith("M_reconciled"):
            continue
        px = live_prices.get(sym)
        entry = float(getattr(p, "entry_price", 0) or 0)
        live_ret = ((px - entry) / entry
                    if (px is not None and entry > 0) else None)
        # Phase 11n-9-e: compute age from entry_time (age_h is NOT a
        # Position attribute — it's derived in engine.status() only).
        entry_time = float(getattr(p, "entry_time", 0) or 0)
        age_h = max(0.0, (time.time() - entry_time) / 3600.0) if entry_time > 0 else 0.0
        value_usd = float(getattr(p, "size_usd", 0) or 0)
        action, reason = _decide(value_usd, live_ret, age_h)
        actions.append(SweepAction(
            symbol=sym, action=action, reason=reason,
            value_usd=value_usd, entry_price=entry,
            live_price=px, live_ret=live_ret,
            age_h=age_h, module=module,
        ))

    n_sell = sum(1 for a in actions if a.action == "sell")
    n_link = sum(1 for a in actions if a.action == "link")
    n_keep = sum(1 for a in actions if a.action == "keep")
    n_dust = sum(1 for a in actions if a.action == "dust_unsellable")
    total_sell_usd = sum(a.value_usd for a in actions if a.action == "sell")
    total_dust_usd = sum(a.value_usd for a in actions
                         if a.action == "dust_unsellable")
    dry = not is_execute_enabled()
    summary = (
        f"{len(actions)} reconciled positions · "
        f"{n_sell} sell (${total_sell_usd:.2f}) · {n_link} link · "
        f"{n_keep} keep · "
        f"{n_dust} dust-locked (${total_dust_usd:.2f}) · "
        + ("DRY RUN (set SPOT_RECON_SWEEP_EXECUTE=1 to ship)"
           if dry else "AUTO EXECUTE")
    )
    return SweepPlan(
        plan_id=plan_id, generated_ts_ms=ts_ms,
        dry_run=dry, actions=actions, summary=summary,
    )


def execute_plan(plan: SweepPlan) -> SweepPlan:
    """Ship the sells + apply the links. Guarded by is_execute_enabled().
    Every sell runs through pre_trade_gov first for audit; the
    governor's safety-exit bypass does NOT apply (this is a deliberate
    sweep). If the pre-trade gate blocks a sell, we mark it executed=False
    with the rejection reason."""
    if not is_execute_enabled():
        return plan   # plan already marked dry_run=True; nothing to do

    from spot_aggro.governance import pre_trade_gov
    try:
        from spot_aggro import _engine_instance
    except Exception:  # noqa: BLE001
        _engine_instance = None
    if _engine_instance is None:
        return plan

    for a in plan.actions:
        if a.action == "keep":
            continue
        if a.action == "sell":
            tier = "RECON"
            authz = pre_trade_gov.authorize_trade(
                a.symbol, "sell", tier,
                source="recon_sweep",
            )
            a.authz_id = authz.authz_id
            if not authz.passed:
                a.reason += f" · BLOCKED by gov: {authz.rejection_reason}"
                continue
            try:
                pos = _engine_instance.state.positions.get(a.symbol)
                if pos is None:
                    a.reason += " · position gone"
                    continue
                qty = pos.size_usd / max(pos.entry_price, 1e-9)
                adapter = _engine_instance._ensure_adapter()
                asyncio.run(asyncio.to_thread(
                    adapter._client.create_market_order,
                    adapter._spot_for(a.symbol), "sell", qty,
                    {"tdMode": "cash"},
                ))
                _engine_instance.state.positions.pop(a.symbol, None)
                a.executed = True
            except Exception as exc:  # noqa: BLE001
                log.exception("recon sell failed for %s: %s", a.symbol, exc)
                msg = str(exc)
                # OKX code 51020 = "below minimum order amount". This
                # happens on dust <$1 in many coins; only way out is
                # manual convert via OKX Small Balance Convert. Tag it
                # so the sweeper's dashboard label changes and the
                # position no longer shows up as a sell candidate.
                if "51020" in msg or "minimum order amount" in msg.lower():
                    a.action = "dust_unsellable"
                    a.reason = (
                        f"non-sellable dust ${a.value_usd:.2f} — OKX "
                        "rejects (code 51020). Use OKX Small Balance "
                        "Convert to aggregate into USDT manually."
                    )
                else:
                    a.reason += f" · SELL ERROR: {msg[:180]}"
        elif a.action == "link":
            # Upgrade in-place: change the engine position's module to
            # "M_linked" + set synthetic tp/sl tied to live_ret so the
            # engine's exit loop starts managing it. Never touches OKX.
            try:
                pos = _engine_instance.state.positions.get(a.symbol)
                if pos is None:
                    a.reason += " · position gone"
                    continue
                # Synthetic TP/SL based on current live_ret as baseline.
                if a.live_ret is not None:
                    pos.tp = round(max(0.03, a.live_ret + 0.02), 4)
                    pos.sl = round(min(-0.02, a.live_ret - 0.05), 4)
                else:
                    pos.tp = 0.04
                    pos.sl = -0.03
                pos.module = "M_linked"
                pos.max_hold_h = 48.0
                a.executed = True
            except Exception as exc:  # noqa: BLE001
                log.exception("recon link failed for %s: %s", a.symbol, exc)
                a.reason += f" · LINK ERROR: {exc!s}"[:200]

    try:
        _persist(plan)
    except Exception:  # noqa: BLE001
        pass
    return plan


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_recon_sweep_plans (
    plan_id         TEXT PRIMARY KEY,
    generated_ts_ms INTEGER NOT NULL,
    dry_run         INTEGER NOT NULL,
    n_sell          INTEGER NOT NULL,
    n_link          INTEGER NOT NULL,
    n_keep          INTEGER NOT NULL,
    total_sell_usd  REAL NOT NULL,
    payload_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_recon_sweep_ts
    ON spot_recon_sweep_plans(generated_ts_ms DESC);
"""


def _init_schema() -> None:
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()


def _persist(plan: SweepPlan) -> None:
    _init_schema()
    from shared.persistence import state as persist
    n_sell = sum(1 for a in plan.actions if a.action == "sell")
    n_link = sum(1 for a in plan.actions if a.action == "link")
    n_keep = sum(1 for a in plan.actions if a.action == "keep")
    total_sell_usd = sum(a.value_usd for a in plan.actions if a.action == "sell")
    con = persist._connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO spot_recon_sweep_plans "
            "(plan_id, generated_ts_ms, dry_run, n_sell, n_link, n_keep, "
            " total_sell_usd, payload_json) VALUES (?,?,?,?,?,?,?,?)",
            (plan.plan_id, plan.generated_ts_ms,
             1 if plan.dry_run else 0, n_sell, n_link, n_keep,
             total_sell_usd,
             json.dumps(plan.to_dict(), default=str)),
        )
        con.commit()
    finally:
        con.close()


def latest_plan() -> Optional[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT payload_json FROM spot_recon_sweep_plans "
            "ORDER BY generated_ts_ms DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else None


def build_and_persist() -> SweepPlan:
    p = build_plan()
    try:
        _persist(p)
    except Exception:  # noqa: BLE001
        pass
    return p


def build_and_execute() -> SweepPlan:
    """Build, execute, persist — what the /recon/sweep POST handler uses."""
    p = build_plan()
    p = execute_plan(p)
    try:
        _persist(p)
    except Exception:  # noqa: BLE001
        pass
    return p
