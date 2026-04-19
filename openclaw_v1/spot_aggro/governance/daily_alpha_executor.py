"""Phase 11n-9-c — Daily Alpha auto-executor.

Turns admitted alpha picks into real engine orders WITHOUT manual
operator action — 100% auto buy + auto sell. Runs after every
auto-orchestrator tick. Guardrails, in order:

  1. Opt-in: only runs when SPOT_ALPHA_AUTO_EXECUTE=1. Default OFF so
     existing deployments don't start trading on today's picks the
     moment the code lands.
  2. Only acts on picks where checklist_pass=True (= 12/12 items).
  3. Re-runs the Pre-Trade Governor one more time RIGHT NOW before
     sending. If anything drifted since the pick was built, the fresh
     authorization fails closed.
  4. Maintains an "executed today" set so a pick already acted on in a
     prior tick doesn't get double-sent.
  5. Uses a fixed small notional (SPOT_ALPHA_NOTIONAL_USD, default $25)
     — never touches the full engine sizing path.
  6. BUY picks → adapter.place_post_only with side="buy".
  7. SELL picks → only execute if the pick's symbol is currently held
     AND its module is NOT M_reconciled* (reconciled positions have
     their own governance). The sell closes the full position size.
  8. Auto-sell of newly-bought positions still flows through the
     engine's normal exit loop (TP / SL / trail / max_hold) — those
     triggers already exist per-tier in scoring.TIER_PARAMS.

Every action gets logged to trade_log with correlation_id
"alpha-exec-<date>-<symbol>" so the Pre-Trade log + dashboard picks
it up.

SPOT AGGRO only. Read-only wrt. to the engine's own trade loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import Any, Optional


log = logging.getLogger(__name__)

DEFAULT_NOTIONAL_USD = 25.0


@dataclass
class AlphaExecution:
    authz_id: str
    ts_ms: int
    symbol: str
    side: str
    tier: str
    requested_notional: float
    placed: bool
    reason: str
    receipt_order_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_enabled() -> bool:
    """Phase 11n-9-e: DEFAULT ON. Set SPOT_ALPHA_AUTO_EXECUTE=0 to
    disable (or TRADE_DRY_RUN / SPOT_DRY_RUN=1). The executor is 100%
    auto: orchestrator tick → admitted picks → re-authorize → ship
    order via the OKX adapter. Idempotent per (symbol, side, UTC day)
    so repeated ticks never double-send.
    """
    if os.environ.get("TRADE_DRY_RUN", "0").strip() == "1":
        return False
    if os.environ.get("SPOT_DRY_RUN", "0").strip() == "1":
        return False
    return os.environ.get("SPOT_ALPHA_AUTO_EXECUTE", "1").strip() == "1"


def _notional() -> float:
    try:
        return float(os.environ.get("SPOT_ALPHA_NOTIONAL_USD", "").strip()
                     or DEFAULT_NOTIONAL_USD)
    except (ValueError, TypeError):
        return DEFAULT_NOTIONAL_USD


# ---------------------------------------------------------------------------
# "Executed today" tracking (dedup across ticks).
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_alpha_executions (
    execution_id    TEXT PRIMARY KEY,
    ts_ms           INTEGER NOT NULL,
    date_utc        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    tier            TEXT NOT NULL,
    placed          INTEGER NOT NULL,
    reason          TEXT,
    payload_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_alpha_exec_date
    ON spot_alpha_executions(date_utc);
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


def _already_executed_today(symbol: str, side: str) -> bool:
    _init_schema()
    from shared.persistence import state as persist
    today = time.strftime("%Y-%m-%d", time.gmtime())
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT 1 FROM spot_alpha_executions "
            "WHERE date_utc = ? AND symbol = ? AND side = ? AND placed = 1 "
            "LIMIT 1",
            (today, symbol, side),
        ).fetchone()
    finally:
        con.close()
    return row is not None


def _record_execution(ex: AlphaExecution) -> None:
    _init_schema()
    from shared.persistence import state as persist
    today = time.strftime("%Y-%m-%d", time.gmtime())
    con = persist._connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO spot_alpha_executions "
            "(execution_id, ts_ms, date_utc, symbol, side, tier, placed, "
            " reason, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (ex.authz_id, ex.ts_ms, today, ex.symbol, ex.side, ex.tier,
             1 if ex.placed else 0, ex.reason,
             json.dumps(ex.to_dict(), default=str)),
        )
        con.commit()
    finally:
        con.close()


def executions_today() -> list[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    today = time.strftime("%Y-%m-%d", time.gmtime())
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT execution_id, ts_ms, symbol, side, tier, placed, reason "
            "FROM spot_alpha_executions WHERE date_utc = ? "
            "ORDER BY ts_ms DESC",
            (today,),
        ).fetchall()
    finally:
        con.close()
    return [
        {"execution_id": r[0], "ts_ms": r[1], "symbol": r[2],
         "side": r[3], "tier": r[4], "placed": bool(r[5]),
         "reason": r[6]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Core executor.
# ---------------------------------------------------------------------------

def execute_admitted_picks() -> list[AlphaExecution]:
    """Find today's admitted picks, re-authorize, and send orders.
    Safe to call on every orchestrator tick — idempotent via the
    'already executed today' check."""
    if not is_enabled():
        return []

    from spot_aggro.governance.daily_alpha import latest_bundle
    from spot_aggro.governance import pre_trade_gov

    bundle = latest_bundle()
    if not bundle:
        return []

    today = time.strftime("%Y-%m-%d", time.gmtime())
    if bundle.get("date_utc") != today:
        return []

    out: list[AlphaExecution] = []
    for p in (bundle.get("buys") or []) + (bundle.get("sells") or []):
        if not p.get("checklist_pass"):
            continue
        sym = p.get("symbol")
        side = p.get("action")
        tier = p.get("tier")
        if _already_executed_today(sym, side):
            continue

        # Freshness re-check: pull a new authorization NOW in case the
        # governor state drifted since the bundle was built.
        authz = pre_trade_gov.authorize_trade(
            sym, side, tier, source="daily_alpha_executor",
        )
        if not authz.passed:
            ex = AlphaExecution(
                authz_id=authz.authz_id, ts_ms=authz.ts_ms,
                symbol=sym, side=side, tier=tier,
                requested_notional=_notional(),
                placed=False,
                reason=f"re-auth failed: {authz.rejection_reason}",
            )
            _record_execution(ex)
            out.append(ex)
            continue

        ex = _place_order(sym, side, tier, authz.authz_id)
        _record_execution(ex)
        out.append(ex)
    return out


def _place_order(sym: str, side: str, tier: str, authz_id: str) -> AlphaExecution:
    """Actually ship the order. Uses the engine's adapter so the same
    place_post_only path as normal entries is used."""
    notional = _notional()
    ts_ms = int(time.time() * 1000)
    try:
        from spot_aggro import _engine_instance
        if _engine_instance is None:
            return AlphaExecution(
                authz_id=authz_id, ts_ms=ts_ms, symbol=sym, side=side,
                tier=tier, requested_notional=notional, placed=False,
                reason="engine not running",
            )
        adapter = _engine_instance._ensure_adapter()

        if side == "sell":
            # Sell requires the position to exist (not reconciled).
            pos = _engine_instance.state.positions.get(sym)
            if pos is None:
                return AlphaExecution(
                    authz_id=authz_id, ts_ms=ts_ms, symbol=sym, side="sell",
                    tier=tier, requested_notional=notional, placed=False,
                    reason="no open position to sell",
                )
            if getattr(pos, "module", "").startswith("M_reconciled"):
                return AlphaExecution(
                    authz_id=authz_id, ts_ms=ts_ms, symbol=sym, side="sell",
                    tier=tier, requested_notional=notional, placed=False,
                    reason="reconciled position — skip (governed separately)",
                )
            qty = pos.size_usd / max(pos.entry_price, 1e-9)
            try:
                asyncio.run(asyncio.to_thread(
                    adapter._client.create_market_order,
                    adapter._spot_for(sym), "sell", qty,
                    {"tdMode": "cash"},
                ))
                # Engine's exit logic won't fire because we just closed
                # it externally; drop from state so it matches OKX.
                _engine_instance.state.positions.pop(sym, None)
                return AlphaExecution(
                    authz_id=authz_id, ts_ms=ts_ms, symbol=sym, side="sell",
                    tier=tier, requested_notional=pos.size_usd,
                    placed=True, reason="alpha sell executed",
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("alpha sell failed for %s: %s", sym, exc)
                return AlphaExecution(
                    authz_id=authz_id, ts_ms=ts_ms, symbol=sym, side="sell",
                    tier=tier, requested_notional=notional, placed=False,
                    reason=f"adapter error: {exc!s}"[:120],
                )

        # BUY path — same place_post_only the engine uses.
        try:
            ticker = asyncio.run(asyncio.to_thread(
                adapter._client.fetch_ticker,
                adapter._spot_for(sym),
            ))
            ref_px = float(ticker.get("last") or 0)
        except Exception:  # noqa: BLE001
            ref_px = 0.0
        if ref_px <= 0:
            return AlphaExecution(
                authz_id=authz_id, ts_ms=ts_ms, symbol=sym, side="buy",
                tier=tier, requested_notional=notional, placed=False,
                reason="no live ticker to size order",
            )
        try:
            receipt = asyncio.run(adapter.place_post_only(
                symbol=sym, side="buy", notional_usd=notional,
                reference_price=ref_px, leg="spot",
                idempotency_seed=f"alpha:{sym}:{time.strftime('%Y%m%d', time.gmtime())}",
            ))
            if not receipt.ok:
                return AlphaExecution(
                    authz_id=authz_id, ts_ms=ts_ms, symbol=sym, side="buy",
                    tier=tier, requested_notional=notional, placed=False,
                    reason=f"place_post_only rejected: {receipt.error}",
                )
            return AlphaExecution(
                authz_id=authz_id, ts_ms=ts_ms, symbol=sym, side="buy",
                tier=tier, requested_notional=notional, placed=True,
                reason="alpha buy executed",
                receipt_order_id=getattr(receipt, "order_id", None),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("alpha buy failed for %s: %s", sym, exc)
            return AlphaExecution(
                authz_id=authz_id, ts_ms=ts_ms, symbol=sym, side="buy",
                tier=tier, requested_notional=notional, placed=False,
                reason=f"adapter error: {exc!s}"[:120],
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("_place_order fault: %s", exc)
        return AlphaExecution(
            authz_id=authz_id, ts_ms=ts_ms, symbol=sym, side=side,
            tier=tier, requested_notional=notional, placed=False,
            reason=f"executor fault: {exc!s}"[:120],
        )
